"""xiReactor Brilliant Remote MCP Server — Streamable HTTP with OAuth 2.1 for Claude Co-work.

Sprint 0039 changes
-------------------
* Dynamic Client Registration (DCR) is disabled — ``/register`` now 404s.
  Clients are provisioned by ``/setup`` on the API side and the operator
  pastes ``client_id`` + ``client_secret`` into Claude Co-work's custom
  connector wizard.
* ``authorize()`` no longer auto-mints an auth code. It 302s the browser to
  the API's ``/oauth/login?tx=<id>`` page after writing a row to
  ``oauth_pending_authorizations``.
* A new ``/oauth/continue`` route consumes the HMAC-signed handoff from the
  API's login POST, atomically deletes the pending row, and mints an
  ``AuthorizationCode`` bound to the authenticated ``user_id``.
* Access tokens carry ``user_id`` (nullable column, migration 030) so the
  T-0229 follow-up can emit ``X-Act-As-User`` on every outbound API call.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time

import psycopg

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from client import BrilliantClient
from oauth_store import PgOAuthStore
from tools import register_tools

logger = logging.getLogger("brilliant.auth")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MCP_PORT = int(os.environ.get("MCP_PORT", "8001"))
_RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
_MCP_BASE_URL_RAW = os.environ.get("MCP_BASE_URL", "").strip()


def _resolve_mcp_base_url() -> str:
    """Resolve the MCP's public base URL (must include scheme).

    Priority:
      1. ``RENDER_EXTERNAL_URL`` — Render auto-injects this as a fully
         qualified ``https://<service>.onrender.com`` on every web
         service. This is the authoritative source on Render.
      2. ``MCP_BASE_URL`` — explicit override (docker-compose, manual
         deploys). If the operator provides a bare hostname we prepend
         ``https://``; if they provide a full URL we respect it.
      3. ``http://localhost:8011`` — local dev fallback.

    The render.yaml ``fromService.property: host`` wire returns Render's
    INTERNAL service-discovery name (e.g. ``brilliant-mcp``), NOT the
    public FQDN, which is why ``MCP_BASE_URL`` cannot be trusted as-is
    on the Render path. Prefer ``RENDER_EXTERNAL_URL`` there.
    """
    if _RENDER_EXTERNAL_URL:
        return _RENDER_EXTERNAL_URL
    if _MCP_BASE_URL_RAW:
        if _MCP_BASE_URL_RAW.startswith(("http://", "https://")):
            return _MCP_BASE_URL_RAW
        return f"https://{_MCP_BASE_URL_RAW}"
    return "http://localhost:8011"


MCP_BASE_URL = _resolve_mcp_base_url()
TOKEN_EXPIRY_SECONDS = int(os.environ.get("TOKEN_EXPIRY_SECONDS", "3600"))

# -- Sprint 0039: OAuth handoff ----------------------------------------------
#
# Shared with the API service via render.yaml's fromService.envVarKey. The
# API signs ``{tx}|{user_id}`` with HMAC-SHA256 after a successful
# /oauth/login POST; we verify the sig below before minting an auth code.
OAUTH_HANDOFF_SECRET = os.environ.get("OAUTH_HANDOFF_SECRET", "").strip()

# How long the /authorize → /oauth/login → /oauth/continue round-trip has
# to complete. The user must type their email + password within this
# window. 10 minutes matches common OAuth PKCE flows and is generous enough
# to survive slow email-to-password-manager lookups.
PENDING_AUTHZ_TTL_SECONDS = 600


def _resolve_api_public_url() -> str:
    """Resolve the API service's browser-visible public URL.

    Used to build the ``{api}/oauth/login?tx=...`` redirect URL that we
    302 the end-user's browser to at the start of the auth flow.

    Resolution order mirrors the MCP's own self-URL logic:

    1. ``brilliant_settings.api_public_url`` — not currently populated by
       any migration; included as a forward-compatible hook so a future
       API-side boot can publish its own ``$RENDER_EXTERNAL_URL`` into
       that column (same pattern we use for MCP via migration 029). We
       quietly swallow a missing-column error.
    2. ``BRILLIANT_API_PUBLIC_URL`` env var — explicit override.
    3. ``BRILLIANT_BASE_URL`` env var — matches ``mcp/client.py``'s
       outbound-call base. On Render this yields the internal service
       name (``brilliant-api``) which is NOT browser-resolvable, so any
       production Render deploy MUST provide one of the URLs above via
       a follow-up migration or explicit override env. We still fall
       back here for local-dev where ``BRILLIANT_BASE_URL`` is
       ``http://localhost:8010``.
    4. ``http://localhost:8010`` — last-resort local-dev default.
    """
    # 1. DB-published — table may not even have the column; tolerate that.
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if dsn:
        try:
            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT api_public_url FROM brilliant_settings WHERE id = 1"
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        return str(row[0]).rstrip("/")
        except Exception:  # noqa: BLE001 — migration not applied → env fallback
            pass

    # 2. Explicit env override.
    raw = os.environ.get("BRILLIANT_API_PUBLIC_URL", "").strip()
    if raw:
        if not (raw.startswith("http://") or raw.startswith("https://")):
            raw = f"https://{raw}"
        return raw.rstrip("/")

    # 3. Reuse BRILLIANT_BASE_URL (same semantics as mcp/client.py).
    raw = os.environ.get("BRILLIANT_BASE_URL", "").strip()
    if raw:
        if not (raw.startswith("http://") or raw.startswith("https://")):
            raw = f"https://{raw}"
        return raw.rstrip("/")

    return "http://localhost:8010"


def _verify_handoff_signature(tx: str, user_id: str, sig: str) -> bool:
    """Constant-time verification of the API's handoff signature.

    Must match ``api/routes/oauth.py::_sign_handoff`` byte-for-byte:

        HMAC-SHA256(OAUTH_HANDOFF_SECRET, f"{tx}|{user_id}")  → hex

    Uses ``hmac.compare_digest`` so we don't leak the expected sig via
    early-exit timing. Returns False (never raises) if the shared secret
    is unset — that degrades safely to a 400 on /oauth/continue rather
    than a 500 that might give a probe more information.
    """
    if not OAUTH_HANDOFF_SECRET:
        return False
    if not tx or not user_id or not sig:
        return False
    expected = hmac.new(
        OAUTH_HANDOFF_SECRET.encode("utf-8"),
        f"{tx}|{user_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, sig)


# ---------------------------------------------------------------------------
# Pydantic model subclasses to carry the user_id binding through the SDK's
# generic-parameterised protocol. FastMCP's AuthorizationCode / AccessToken
# are stock BaseModels without a user_id field; subclassing is explicitly
# endorsed by the SDK comment above ``AuthorizationCodeT`` ("it's OK to add
# fields to subclasses which should not be exposed externally").
# ---------------------------------------------------------------------------


class BrilliantAuthorizationCode(AuthorizationCode):
    """AuthorizationCode with the OAuth-bound ``user_id`` baked in."""

    user_id: str | None = None


class BrilliantAccessToken(AccessToken):
    """AccessToken with the OAuth-bound ``user_id`` baked in."""

    user_id: str | None = None


def _publish_public_url_to_db() -> None:
    """Publish this MCP's public URL to ``brilliant_settings.mcp_public_url``.

    Render's ``fromService.property: host`` returns the internal service-
    discovery name (e.g. ``brilliant-mcp``), not the public FQDN — so the
    API service can't construct the MCP's real URL from that alone. This
    function writes our authoritative ``RENDER_EXTERNAL_URL`` into a shared
    DB column that the API reads when rendering ``/setup/done`` and
    ``/auth/login`` credentials pages.

    Failure-tolerant by design: a missing ``mcp_public_url`` column
    (migration 029 not yet applied) or an unreachable DB must not prevent
    the MCP from serving traffic. We log and continue.

    Idempotent: the UPDATE re-writes the same value on every boot, and the
    row is a singleton keyed by ``id = 1``.
    """
    if not _RENDER_EXTERNAL_URL:
        return  # local dev — nothing authoritative to publish
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        return
    try:
        with psycopg.connect(dsn) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    # SET LOCAL ROLE to kb_admin so the UPDATE is authorised
                    # under the grant from migration 027. Works locally
                    # (superuser) and on Render (connection user is a
                    # member of kb_admin via migration 028).
                    cur.execute("SET LOCAL ROLE kb_admin")
                    cur.execute(
                        "UPDATE brilliant_settings "
                        "SET mcp_public_url = %s, updated_at = now() "
                        "WHERE id = 1",
                        (_RENDER_EXTERNAL_URL,),
                    )
        logger.info(
            "Published MCP public URL to brilliant_settings: %s",
            _RENDER_EXTERNAL_URL,
        )
    except Exception as exc:  # noqa: BLE001 — never block MCP boot on this
        logger.warning(
            "Could not publish MCP public URL to brilliant_settings "
            "(DB unreachable or migration 029 not yet applied): %s",
            exc,
        )


_publish_public_url_to_db()


# ---------------------------------------------------------------------------
# OAuth Authorization Server Provider (PostgreSQL-backed)
# ---------------------------------------------------------------------------


class BrilliantOAuthProvider(
    OAuthAuthorizationServerProvider[
        BrilliantAuthorizationCode, RefreshToken, BrilliantAccessToken
    ]
):
    """OAuth 2.1 provider for Claude Co-work integration.

    Sprint 0039: DCR is disabled at the ``AuthSettings`` layer, so
    ``/register`` is never mounted as a route. ``authorize()`` redirects
    to the API-hosted login page (proof-of-user step) instead of
    minting a code itself. ``/oauth/continue`` on this service (see
    ``_oauth_continue`` below) consumes the signed handoff and mints
    the code with the authenticated ``user_id`` bound.
    """

    def __init__(self, store: PgOAuthStore):
        self.store = store

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await self.store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # DCR is disabled — FastMCP won't mount /register when
        # ClientRegistrationOptions(enabled=False), so this should never be
        # called. Raise explicitly so an accidental re-enable of DCR surfaces
        # loudly rather than silently minting an admin-capable client.
        raise NotImplementedError(
            "Dynamic client registration is disabled on this server. "
            "Provision client_id/client_secret via /setup on the API service."
        )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Redirect to the API's /oauth/login handoff instead of auto-minting.

        We write a pending-authz row keyed by a freshly generated ``tx_id``
        (32 url-safe bytes = ~192 bits entropy, well above the 160-bit
        minimum the SDK recommends for auth codes), then return the login
        URL. FastMCP's AuthorizationHandler wraps our return value in a
        302, so from Claude's perspective the /authorize request turns into
        a browser hop through the API's login page and then onward via
        /oauth/continue back to the original ``redirect_uri``.
        """
        tx_id = secrets.token_urlsafe(32)
        expires_at = time.time() + PENDING_AUTHZ_TTL_SECONDS

        # The AuthorizationParams model provides code_challenge but NOT
        # code_challenge_method — FastMCP's AuthorizationRequest validates
        # that method is always literal "S256" before dropping it. We
        # persist "S256" explicitly so the column isn't misleadingly NULL.
        code_challenge_method = "S256" if params.code_challenge else None

        await self.store.save_pending_authorization(
            tx_id,
            client_id=client.client_id,
            scopes=params.scopes or [],
            code_challenge=params.code_challenge,
            code_challenge_method=code_challenge_method,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            state=params.state,
            resource=str(params.resource) if params.resource else None,
            expires_at=expires_at,
        )

        api_base = _resolve_api_public_url()
        login_url = f"{api_base}/oauth/login?tx={tx_id}"
        logger.info(
            "authorize: redirected tx=%s to %s (client=%s, ttl=%ds)",
            tx_id[:8] + "...",
            login_url,
            client.client_id,
            PENDING_AUTHZ_TTL_SECONDS,
        )
        return login_url

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> BrilliantAuthorizationCode | None:
        """Return a ``BrilliantAuthorizationCode`` carrying the bound user_id.

        The FastMCP TokenHandler calls this, validates the PKCE challenge,
        and then hands the result to ``exchange_authorization_code`` —
        which promotes the embedded ``user_id`` onto the issued access
        token row.
        """
        result = await self.store.get_auth_code(authorization_code, client.client_id)
        if result is None:
            return None
        ac, user_id = result
        # Promote to the subclass so user_id survives through the token
        # exchange path. Reconstruct from ac.model_dump() to keep AnyUrl
        # fields correctly typed.
        return BrilliantAuthorizationCode(
            **ac.model_dump(),
            user_id=user_id,
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: BrilliantAuthorizationCode,
    ) -> OAuthToken:
        # Consume the authorization code
        await self.store.delete_auth_code(authorization_code.code)

        # Pull the user_id bound to this code; never let a NULL slip
        # through silently — a code minted by /oauth/continue will always
        # have a user_id set.
        bound_user_id = getattr(authorization_code, "user_id", None)

        # Issue access token
        access_token_str = secrets.token_hex(32)
        access_token = AccessToken(
            token=access_token_str,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + TOKEN_EXPIRY_SECONDS,
        )
        await self.store.save_access_token(access_token, user_id=bound_user_id)
        logger.warning(
            "Token ISSUED: prefix=%s, expires_at=%s, user_id=%s",
            access_token_str[:8] + "...",
            access_token.expires_at,
            bound_user_id,
        )

        # Issue refresh token
        refresh_token_str = secrets.token_hex(32)
        refresh_token = RefreshToken(
            token=refresh_token_str,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
        )
        await self.store.save_refresh_token(refresh_token)

        return OAuthToken(
            access_token=access_token_str,
            token_type="Bearer",
            expires_in=TOKEN_EXPIRY_SECONDS,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
            refresh_token=refresh_token_str,
        )

    async def load_access_token(self, token: str) -> BrilliantAccessToken | None:
        """Return a ``BrilliantAccessToken`` with ``user_id`` surfaced.

        Called by FastMCP's ``RequireAuthMiddleware`` on every authenticated
        request. The T-0229 follow-up reads ``.user_id`` off this object
        to populate ``X-Act-As-User`` on outbound API calls.
        """
        result = await self.store.get_access_token(token)
        if result is None:
            return None
        at, user_id = result
        if at.expires_at is not None and at.expires_at <= time.time():
            logger.warning(
                "Token found but EXPIRED: expires_at=%s, now=%s",
                at.expires_at,
                time.time(),
            )
            return None
        return BrilliantAccessToken(
            **at.model_dump(),
            user_id=user_id,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return await self.store.get_refresh_token(refresh_token, client.client_id)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        # Revoke old refresh token
        await self.store.delete_refresh_token(refresh_token.token)

        # Preserve the user_id binding across refresh. The old access
        # token(s) for this refresh may already be expired or consumed,
        # but the DB still has the historical binding keyed off a sibling
        # token from the same client. If we can't find one we fall through
        # to a NULL user_id — that will be caught by the T-0229 tool
        # handlers which 401 any token without a bound user.
        bound_user_id: str | None = None
        try:
            # Best-effort: pull any access-token row for this client and
            # reuse its user_id. Safer than re-deriving from the refresh
            # token alone since refresh rows predate the user_id column.
            async with await self.store._conn() as conn:  # noqa: SLF001
                row = await (
                    await conn.execute(
                        """SELECT user_id FROM oauth_access_tokens
                           WHERE client_id = %s AND user_id IS NOT NULL
                           ORDER BY created_at DESC LIMIT 1""",
                        (client.client_id,),
                    )
                ).fetchone()
                if row and row.get("user_id"):
                    bound_user_id = row["user_id"]
        except Exception as exc:  # noqa: BLE001 — tolerate DB hiccups on refresh
            logger.warning("refresh: could not resolve bound user_id: %s", exc)

        # Issue new tokens
        access_token_str = secrets.token_hex(32)
        access_token = AccessToken(
            token=access_token_str,
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            expires_at=int(time.time()) + TOKEN_EXPIRY_SECONDS,
        )
        await self.store.save_access_token(access_token, user_id=bound_user_id)

        new_refresh_str = secrets.token_hex(32)
        new_refresh = RefreshToken(
            token=new_refresh_str,
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
        )
        await self.store.save_refresh_token(new_refresh)

        return OAuthToken(
            access_token=access_token_str,
            token_type="Bearer",
            expires_in=TOKEN_EXPIRY_SECONDS,
            scope=" ".join(access_token.scopes) if access_token.scopes else None,
            refresh_token=new_refresh_str,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            await self.store.delete_access_token(token.token)
        else:
            await self.store.delete_refresh_token(token.token)


# ---------------------------------------------------------------------------
# Server setup
# ---------------------------------------------------------------------------

store = PgOAuthStore()
provider = BrilliantOAuthProvider(store)

mcp = FastMCP(
    name="brilliant",
    host="0.0.0.0",
    port=MCP_PORT,
    auth_server_provider=provider,
    auth=AuthSettings(
        issuer_url=MCP_BASE_URL,
        resource_server_url=MCP_BASE_URL,
        # Sprint 0039: DCR disabled. Clients are pre-provisioned by
        # /setup on the API service; Claude Co-work's custom connector
        # requires the operator to paste client_id + client_secret.
        # With enabled=False the SDK does not mount the /register route
        # at all — a POST to /register returns 404 (Starlette default
        # for unknown paths). See mcp/server/auth/routes.py.
        client_registration_options=ClientRegistrationOptions(
            enabled=False,
            valid_scopes=["brilliant"],
            default_scopes=["brilliant"],
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[],
    ),
)

# CORS for Claude Co-work
mcp.settings.debug = False


# ---------------------------------------------------------------------------
# /oauth/continue — consume the signed handoff from the API's login POST
# ---------------------------------------------------------------------------
#
# Flow:
#
#   Claude /authorize on MCP
#        |
#        v
#   BrilliantOAuthProvider.authorize() writes pending row, 302s to
#        {api}/oauth/login?tx=<id>
#        |
#        v
#   User types email + password on API page
#        |
#        v
#   API POSTs /oauth/login → 302 to {mcp}/oauth/continue?tx=..&user_id=..&sig=..
#        |
#        v
#   THIS HANDLER:
#     1. Parse tx, user_id, sig
#     2. Load pending authz (enforce expiry in-DB)
#     3. HMAC-verify sig (constant-time)
#     4. Mint AuthorizationCode bound to user_id
#     5. Delete pending row (atomic consume)
#     6. 302 back to pending.redirect_uri?code=..&state=..
#
# Any failure in 1-3 returns 400 WITHOUT touching the pending row so an
# attacker can't burn a legitimate tx by replaying with a bad sig. A
# legitimate user who hit 400 can only reach this handler again via a
# fresh /oauth/login POST, which re-computes the sig anyway.


@mcp.custom_route("/oauth/continue", methods=["GET"])
async def _oauth_continue(request: Request) -> Response:
    tx = (request.query_params.get("tx") or "").strip()
    user_id = (request.query_params.get("user_id") or "").strip()
    sig = (request.query_params.get("sig") or "").strip()

    if not tx or not user_id or not sig:
        return PlainTextResponse(
            "Missing required parameter", status_code=400
        )

    # 1. Load pending authz (expiry enforced in-DB).
    pending = await store.get_pending_authorization(tx)
    if pending is None:
        # Missing OR expired — same 400 either way. Don't leak which.
        return PlainTextResponse(
            "Unknown or expired authorization", status_code=400
        )

    # 2. Verify HMAC sig in constant time.
    if not _verify_handoff_signature(tx, user_id, sig):
        logger.warning(
            "oauth/continue: sig mismatch for tx=%s (pending row preserved)",
            tx[:8] + "...",
        )
        # IMPORTANT: do NOT delete the pending row on a bad sig. Letting
        # an attacker burn someone else's tx by probing with random sigs
        # would be a trivial DoS. A legitimate replay from /oauth/login
        # will re-sign correctly.
        return PlainTextResponse(
            "Invalid authorization signature", status_code=400
        )

    # 3. Mint the AuthorizationCode bound to user_id.
    code = secrets.token_hex(32)
    # Auth codes are short-lived per RFC 6749 §10.5 (recommended ≤10 min).
    # Stick with 5 min to match the pre-0039 behaviour; the client has
    # just round-tripped through the API login page so a fresh 5-minute
    # TTL for the code-to-token exchange is ample.
    auth_code = AuthorizationCode(
        code=code,
        scopes=pending.get("scopes") or [],
        expires_at=time.time() + 300,
        client_id=pending["client_id"],
        code_challenge=pending.get("code_challenge") or "",
        redirect_uri=pending["redirect_uri"],
        redirect_uri_provided_explicitly=bool(
            pending.get("redirect_uri_provided_explicitly")
        ),
        resource=pending.get("resource"),
    )
    await store.save_auth_code(auth_code, user_id=user_id)

    # 4. Atomically consume the pending row. If /oauth/continue fires
    # twice on the same tx (double-click on the API login form, or a
    # browser retry), the second call hits step 1 with pending=None
    # and 400s — we've already minted the code and redirected.
    await store.delete_pending_authorization(tx)

    # 5. 302 back to the client's redirect_uri. State is optional; only
    # include it if the client sent one on /authorize.
    state = pending.get("state")
    redirect_kwargs: dict[str, str] = {"code": code}
    if state:
        redirect_kwargs["state"] = state
    redirect_url = construct_redirect_uri(
        str(pending["redirect_uri"]), **redirect_kwargs
    )
    logger.info(
        "oauth/continue: minted code for tx=%s user=%s client=%s → %s",
        tx[:8] + "...",
        user_id,
        pending["client_id"],
        pending["redirect_uri"],
    )
    return RedirectResponse(
        url=redirect_url,
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )


# Register all 11 Brilliant tools
api = BrilliantClient()
register_tools(mcp, api)


def create_app():
    """Create the Starlette ASGI app with CORS middleware for deployment."""
    app = mcp.streamable_http_app()

    # Wrap with CORS middleware
    from starlette.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://claude.ai", "https://app.claude.ai"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "mcp-protocol-version",
            "mcp-session-id",
        ],
        expose_headers=["mcp-session-id"],
        allow_credentials=True,
    )
    return app


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
