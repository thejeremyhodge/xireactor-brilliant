"""xiReactor Brilliant Remote MCP Server — Streamable HTTP with OAuth 2.1 for Claude Co-work."""

from __future__ import annotations

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
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.middleware.cors import CORSMiddleware

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
BRILLIANT_API_KEY = os.environ.get("BRILLIANT_API_KEY", "")
TOKEN_EXPIRY_SECONDS = int(os.environ.get("TOKEN_EXPIRY_SECONDS", "3600"))


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


class BrilliantOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    """OAuth 2.1 provider for Claude Co-work integration.

    Supports Dynamic Client Registration (DCR) so Co-work can self-register,
    then the standard authorization_code + PKCE flow.
    Tokens persist in PostgreSQL across container restarts.
    """

    def __init__(self, store: PgOAuthStore):
        self.store = store

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await self.store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        client_id = f"brilliant_{secrets.token_hex(16)}"
        client_secret = secrets.token_hex(32)
        client_info.client_id = client_id
        client_info.client_secret = client_secret
        client_info.client_id_issued_at = int(time.time())
        await self.store.save_client(client_info)

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        code = secrets.token_hex(32)
        auth_code = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + 300,  # 5 min
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        await self.store.save_auth_code(auth_code)

        from mcp.server.auth.provider import construct_redirect_uri
        return construct_redirect_uri(
            str(params.redirect_uri),
            code=code,
            state=params.state or "",
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return await self.store.get_auth_code(authorization_code, client.client_id)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Consume the authorization code
        await self.store.delete_auth_code(authorization_code.code)

        # Issue access token
        access_token_str = secrets.token_hex(32)
        access_token = AccessToken(
            token=access_token_str,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + TOKEN_EXPIRY_SECONDS,
        )
        await self.store.save_access_token(access_token)
        logger.warning(
            "Token ISSUED: prefix=%s, expires_at=%s",
            access_token_str[:8] + "...",
            access_token.expires_at,
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

    async def load_access_token(self, token: str) -> AccessToken | None:
        access_token = await self.store.get_access_token(token)
        if access_token and (access_token.expires_at is None or access_token.expires_at > time.time()):
            return access_token
        if access_token and access_token.expires_at and access_token.expires_at <= time.time():
            logger.warning("Token found but EXPIRED: expires_at=%s, now=%s", access_token.expires_at, time.time())
        return None

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return await self.store.get_refresh_token(refresh_token, client.client_id)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        # Revoke old refresh token
        await self.store.delete_refresh_token(refresh_token.token)

        # Issue new tokens
        access_token_str = secrets.token_hex(32)
        access_token = AccessToken(
            token=access_token_str,
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            expires_at=int(time.time()) + TOKEN_EXPIRY_SECONDS,
        )
        await self.store.save_access_token(access_token)

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
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["brilliant"],
            default_scopes=["brilliant"],
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[],
    ),
)

# CORS for Claude Co-work
mcp.settings.debug = False
mcp._custom_starlette_routes = getattr(mcp, "_custom_starlette_routes", [])

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
