"""PostgreSQL-backed OAuth store for persistent token survival across container restarts."""

from __future__ import annotations

import json
import logging
import os
import time

import psycopg
from psycopg.rows import dict_row

from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull

logger = logging.getLogger("cortex.oauth_store")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:dev@localhost:5432/cortex",
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

    async def save_auth_code(self, code: AuthorizationCode) -> None:
        async with await self._conn() as conn:
            await conn.execute(
                """INSERT INTO oauth_auth_codes
                     (code, client_id, scopes, expires_at, code_challenge, redirect_uri,
                      redirect_uri_provided_explicitly, resource)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    code.code,
                    code.client_id,
                    code.scopes or [],
                    code.expires_at,
                    code.code_challenge,
                    str(code.redirect_uri) if code.redirect_uri else None,
                    code.redirect_uri_provided_explicitly,
                    str(code.resource) if code.resource else None,
                ),
            )

    async def get_auth_code(self, code: str, client_id: str) -> AuthorizationCode | None:
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
        return AuthorizationCode(
            code=row["code"],
            scopes=row["scopes"] or [],
            expires_at=row["expires_at"],
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=row["redirect_uri_provided_explicitly"],
            resource=row["resource"],
        )

    async def delete_auth_code(self, code: str) -> None:
        async with await self._conn() as conn:
            await conn.execute("DELETE FROM oauth_auth_codes WHERE code = %s", (code,))

    # -- Access Tokens ---------------------------------------------------------

    async def save_access_token(self, token: AccessToken) -> None:
        async with await self._conn() as conn:
            await conn.execute(
                """INSERT INTO oauth_access_tokens (token, client_id, scopes, expires_at)
                   VALUES (%s, %s, %s, %s)""",
                (token.token, token.client_id, token.scopes or [], token.expires_at),
            )

    async def get_access_token(self, token: str) -> AccessToken | None:
        async with await self._conn() as conn:
            row = await (
                await conn.execute(
                    "SELECT * FROM oauth_access_tokens WHERE token = %s",
                    (token,),
                )
            ).fetchone()
        if row is None:
            return None
        return AccessToken(
            token=row["token"],
            client_id=row["client_id"],
            scopes=row["scopes"] or [],
            expires_at=row["expires_at"],
        )

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

    # -- Cleanup ---------------------------------------------------------------

    async def sweep_expired(self) -> int:
        """Delete expired auth codes and access tokens. Returns total rows deleted."""
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
            total = (r1.rowcount or 0) + (r2.rowcount or 0)
        if total > 0:
            logger.info("Swept %d expired OAuth rows", total)
        return total
