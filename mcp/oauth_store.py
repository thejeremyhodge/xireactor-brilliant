"""PostgreSQL-backed OAuth store for persistent token survival across container restarts.

Sprint 0039 notes
-----------------
``oauth_access_tokens`` and ``oauth_auth_codes`` gained a nullable
``user_id`` column (migration 030). The store threads ``user_id`` through
every save/get for those two tables so the OAuth-bound user identity
survives the code-to-token exchange and round-trips into
``load_access_token`` (consumed by per-request MCP tool dispatch).

We also manage a third table — ``oauth_pending_authorizations`` — that
holds an in-flight ``/authorize`` tx across the redirect hop to the API's
``/oauth/login`` page and back. See the module's ``--- Pending
authorizations ---`` section.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import psycopg
from psycopg.rows import dict_row

from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull

logger = logging.getLogger("brilliant.oauth_store")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:dev@localhost:5432/brilliant",
)


def _client_to_json(client: OAuthClientInformationFull) -> str:
    """Serialize client info to JSON for storage."""
    return client.model_dump_json()


def _client_from_json(data: str | dict) -> OAuthClientInformationFull:
    """Deserialize client info from JSON."""
    if isinstance(data, str):
        return OAuthClientInformationFull.model_validate_json(data)
    return OAuthClientInformationFull.model_validate(data)


class PgOAuthStore:
    """PostgreSQL-backed OAuth store replacing the in-memory OAuthStore."""

    def __init__(self, conninfo: str | None = None):
        self._conninfo = conninfo or DATABASE_URL

    async def _conn(self) -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(
            self._conninfo, row_factory=dict_row, autocommit=True
        )

    # -- Clients ---------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with await self._conn() as conn:
            row = await (
                await conn.execute(
                    "SELECT client_info FROM oauth_clients WHERE client_id = %s",
                    (client_id,),
                )
            ).fetchone()
        if row is None:
            return None
        return _client_from_json(row["client_info"])

    async def save_client(self, client: OAuthClientInformationFull) -> None:
        async with await self._conn() as conn:
            await conn.execute(
                """INSERT INTO oauth_clients (client_id, client_secret, client_id_issued_at, client_info)
                   VALUES (%s, %s, %s, %s::jsonb)
                   ON CONFLICT (client_id) DO UPDATE
                     SET client_info = EXCLUDED.client_info""",
                (
                    client.client_id,
                    client.client_secret or "",
                    client.client_id_issued_at or 0,
                    _client_to_json(client),
                ),
            )

    # -- Authorization Codes ---------------------------------------------------

    async def save_auth_code(
        self,
        code: AuthorizationCode,
        *,
        user_id: str | None = None,
    ) -> None:
        """Persist an authorization code, optionally bound to a ``user_id``.

        The FastMCP SDK's ``AuthorizationCode`` pydantic model has no
        ``user_id`` field, so we take it as a kwarg rather than monkey-
        patching the model. Callers (specifically
        ``BrilliantOAuthProvider._oauth_continue``) pass the user_id they
        just verified via the HMAC handoff. The paired ``get_auth_code``
        returns a ``(auth_code, user_id)`` tuple so the caller can rehydrate
        the binding onto its own subclassed model.
        """
        async with await self._conn() as conn:
            await conn.execute(
                """INSERT INTO oauth_auth_codes
                     (code, client_id, scopes, expires_at, code_challenge, redirect_uri,
                      redirect_uri_provided_explicitly, resource, user_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    code.code,
                    code.client_id,
                    code.scopes or [],
                    code.expires_at,
                    code.code_challenge,
                    str(code.redirect_uri) if code.redirect_uri else None,
                    code.redirect_uri_provided_explicitly,
                    str(code.resource) if code.resource else None,
                    user_id,
                ),
            )

    async def get_auth_code(
        self, code: str, client_id: str
    ) -> tuple[AuthorizationCode, str | None] | None:
        """Load an authorization code plus the bound ``user_id``.

        Returns ``(auth_code, user_id_or_None)`` or ``None`` if the code is
        missing / expired / client-mismatched. We return a tuple rather than
        adding ``user_id`` onto the FastMCP pydantic model because BaseModel
        rejects undeclared attribute assignment under its default config —
        carrying the user_id alongside the model keeps this layer free of
        subclass gymnastics and lets the caller decide how to persist the
        binding (see ``BrilliantOAuthProvider.load_authorization_code`` which
        stashes it on a subclassed pydantic model that *does* declare the
        field).
        """
        async with await self._conn() as conn:
            row = await (
                await conn.execute(
                    """SELECT * FROM oauth_auth_codes
                       WHERE code = %s AND client_id = %s AND expires_at > %s""",
                    (code, client_id, time.time()),
                )
            ).fetchone()
        if row is None:
            return None
        ac = AuthorizationCode(
            code=row["code"],
            scopes=row["scopes"] or [],
            expires_at=row["expires_at"],
            client_id=row["client_id"],
            # code_challenge is declared str (non-optional) on the SDK
            # model, so coerce a legacy NULL to empty string rather than
            # blow up pydantic validation. PKCE-using clients (Claude
            # Co-work is one) always set this; the fallback is defensive.
            code_challenge=row["code_challenge"] or "",
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=row["redirect_uri_provided_explicitly"],
            resource=row["resource"],
        )
        return ac, row.get("user_id")

    async def delete_auth_code(self, code: str) -> None:
        async with await self._conn() as conn:
            await conn.execute("DELETE FROM oauth_auth_codes WHERE code = %s", (code,))

    # -- Access Tokens ---------------------------------------------------------

    async def save_access_token(
        self,
        token: AccessToken,
        *,
        user_id: str | None = None,
    ) -> None:
        """Persist an access token, optionally bound to a ``user_id``.

        ``user_id`` is the OAuth-bound user identity that flowed from the
        authorization code (migration 030). The T-0229 follow-up wires this
        into the ``X-Act-As-User`` header emitted by tool calls.
        """
        async with await self._conn() as conn:
            await conn.execute(
                """INSERT INTO oauth_access_tokens (token, client_id, scopes, expires_at, user_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    token.token,
                    token.client_id,
                    token.scopes or [],
                    token.expires_at,
                    user_id,
                ),
            )

    async def get_access_token(
        self, token: str
    ) -> tuple[AccessToken, str | None] | None:
        """Load an access token and return ``(token, user_id_or_None)``.

        Same rationale as ``get_auth_code`` — we return the user_id alongside
        the model instead of mutating the pydantic instance. Caller is
        ``BrilliantOAuthProvider.load_access_token`` which promotes the
        result to a ``BrilliantAccessToken`` subclass that declares
        ``user_id`` as a first-class field.
        """
        async with await self._conn() as conn:
            row = await (
                await conn.execute(
                    "SELECT * FROM oauth_access_tokens WHERE token = %s",
                    (token,),
                )
            ).fetchone()
        if row is None:
            return None
        at = AccessToken(
            token=row["token"],
            client_id=row["client_id"],
            scopes=row["scopes"] or [],
            expires_at=row["expires_at"],
        )
        return at, row.get("user_id")

    async def delete_access_token(self, token: str) -> None:
        async with await self._conn() as conn:
            await conn.execute("DELETE FROM oauth_access_tokens WHERE token = %s", (token,))

    # -- Refresh Tokens --------------------------------------------------------

    async def save_refresh_token(self, token: RefreshToken) -> None:
        async with await self._conn() as conn:
            await conn.execute(
                """INSERT INTO oauth_refresh_tokens (token, client_id, scopes)
                   VALUES (%s, %s, %s)""",
                (token.token, token.client_id, token.scopes or []),
            )

    async def get_refresh_token(self, token: str, client_id: str) -> RefreshToken | None:
        async with await self._conn() as conn:
            row = await (
                await conn.execute(
                    "SELECT * FROM oauth_refresh_tokens WHERE token = %s AND client_id = %s",
                    (token, client_id),
                )
            ).fetchone()
        if row is None:
            return None
        return RefreshToken(
            token=row["token"],
            client_id=row["client_id"],
            scopes=row["scopes"] or [],
        )

    async def delete_refresh_token(self, token: str) -> None:
        async with await self._conn() as conn:
            await conn.execute("DELETE FROM oauth_refresh_tokens WHERE token = %s", (token,))

    # -- Pending authorizations (sprint 0039) ---------------------------------
    #
    # An in-flight ``/authorize`` request that's been 302'd to the API's
    # ``/oauth/login`` page. Lives in ``oauth_pending_authorizations``
    # (migration 030). Lifecycle:
    #
    #   1. MCP ``BrilliantOAuthProvider.authorize()`` → ``save_pending_authorization``
    #   2. Browser hits API ``/oauth/login?tx=...`` — pending row is SELECTed
    #      (not deleted) so the API can re-validate on POST.
    #   3. API POST /oauth/login → 302 to MCP ``/oauth/continue``.
    #   4. MCP ``/oauth/continue`` → ``get_pending_authorization``,
    #      ``delete_pending_authorization`` atomically consumes the row
    #      and mints the auth code.
    #
    # ``expires_at`` is a unix timestamp (``DOUBLE PRECISION``). Callers must
    # treat an expired row as absent; the in-DB predicate in
    # ``get_pending_authorization`` enforces this to avoid a TOCTOU race
    # against the sweeper.
    # -------------------------------------------------------------------------

    async def save_pending_authorization(
        self,
        tx_id: str,
        *,
        client_id: str,
        scopes: list[str] | None,
        code_challenge: str | None,
        code_challenge_method: str | None,
        redirect_uri: str,
        redirect_uri_provided_explicitly: bool,
        state: str | None,
        resource: str | None,
        expires_at: float,
    ) -> None:
        """Insert a pending-authorization row.

        The caller has already generated ``tx_id`` (expected to be at least
        160 bits of entropy from ``secrets.token_urlsafe``). ``expires_at``
        is an absolute unix timestamp; we keep it as ``DOUBLE PRECISION`` to
        match migration 030's column type and our existing
        ``oauth_auth_codes.expires_at`` convention.
        """
        async with await self._conn() as conn:
            await conn.execute(
                """INSERT INTO oauth_pending_authorizations
                     (tx_id, client_id, scopes, code_challenge, code_challenge_method,
                      redirect_uri, redirect_uri_provided_explicitly, state, resource,
                      expires_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    tx_id,
                    client_id,
                    scopes or [],
                    code_challenge,
                    code_challenge_method,
                    redirect_uri,
                    redirect_uri_provided_explicitly,
                    state,
                    resource,
                    expires_at,
                ),
            )

    async def get_pending_authorization(self, tx_id: str) -> dict[str, Any] | None:
        """Load a pending-authorization row if present and not expired.

        The ``expires_at > now()`` clause is evaluated in-DB against
        ``extract(epoch from now())`` so the API and MCP can't drift against
        each other's clocks — both sides agree on the same Postgres time.

        Returns a plain dict (the standard ``dict_row`` factory output)
        rather than a typed model. The shape matches the table columns in
        migration 030.
        """
        async with await self._conn() as conn:
            row = await (
                await conn.execute(
                    """SELECT tx_id, client_id, scopes, code_challenge,
                              code_challenge_method, redirect_uri,
                              redirect_uri_provided_explicitly, state,
                              resource, expires_at
                       FROM oauth_pending_authorizations
                       WHERE tx_id = %s
                         AND expires_at > extract(epoch from now())""",
                    (tx_id,),
                )
            ).fetchone()
        return row

    async def delete_pending_authorization(self, tx_id: str) -> None:
        """Delete the pending-authorization row, idempotent on missing tx."""
        async with await self._conn() as conn:
            await conn.execute(
                "DELETE FROM oauth_pending_authorizations WHERE tx_id = %s",
                (tx_id,),
            )

    # -- Cleanup ---------------------------------------------------------------

    async def sweep_expired(self) -> int:
        """Delete expired auth codes, access tokens, and pending authz. Returns total rows deleted."""
        now = time.time()
        total = 0
        async with await self._conn() as conn:
            r1 = await conn.execute(
                "DELETE FROM oauth_auth_codes WHERE expires_at <= %s", (now,)
            )
            r2 = await conn.execute(
                "DELETE FROM oauth_access_tokens WHERE expires_at IS NOT NULL AND expires_at <= %s",
                (now,),
            )
            r3 = await conn.execute(
                "DELETE FROM oauth_pending_authorizations WHERE expires_at <= %s",
                (now,),
            )
            total = (r1.rowcount or 0) + (r2.rowcount or 0) + (r3.rowcount or 0)
        if total > 0:
            logger.info("Swept %d expired OAuth rows", total)
        return total
