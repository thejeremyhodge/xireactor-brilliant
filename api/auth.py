"""API key authentication middleware for FastAPI."""

from dataclasses import dataclass
from datetime import datetime, timezone

import bcrypt
from fastapi import Depends, HTTPException, Request

from database import get_pool


@dataclass
class UserContext:
    """Authenticated user context, injected into route handlers."""

    id: str
    org_id: str
    display_name: str
    role: str  # admin | editor | commenter | viewer
    department: str | None
    source: str  # web_ui | agent | api
    key_type: str  # interactive | agent | api_integration


# Map key_type to source
_KEY_TYPE_TO_SOURCE = {
    "interactive": "web_ui",
    "agent": "agent",
    "api_integration": "api",
}


def _extract_bearer_token(request: Request) -> str:
    """Extract Bearer token from Authorization header."""
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header format")
    return parts[1]


async def get_current_user(request: Request) -> UserContext:
    """FastAPI dependency that authenticates via API key and returns UserContext.

    Auth flow:
    1. Extract Bearer token from Authorization header
    2. Lookup api_keys by key_prefix (first 8 chars)
    3. bcrypt verify full token against key_hash
    4. Join to users table for role, department, org_id, display_name
    5. Map key_type to source
    6. Update last_used_at
    7. Return UserContext
    """
    token = _extract_bearer_token(request)

    if len(token) < 9:
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Key prefix format: bkai_XXXX (9 chars)
    key_prefix = token[:9]

    # Use a raw connection from the pool (not RLS-scoped) for auth queries
    pool = get_pool()
    async with pool.connection() as conn:
        # Look up the API key by prefix
        row = await conn.execute(
            """
            SELECT
                ak.id AS key_id,
                ak.key_hash,
                ak.key_type,
                ak.user_id,
                u.org_id,
                u.display_name,
                u.role,
                u.department
            FROM api_keys ak
            JOIN users u ON u.id = ak.user_id
            WHERE ak.key_prefix = %s
              AND ak.is_revoked = FALSE
              AND (ak.expires_at IS NULL OR ak.expires_at > NOW())
            """,
            (key_prefix,),
        )
        result = await row.fetchone()

        if result is None:
            raise HTTPException(status_code=401, detail="Invalid or expired API key")

        (
            key_id,
            key_hash,
            key_type,
            user_id,
            org_id,
            display_name,
            role,
            department,
        ) = result

        # bcrypt verify the full token against stored hash
        if not bcrypt.checkpw(token.encode("utf-8"), key_hash.encode("utf-8")):
            raise HTTPException(status_code=401, detail="Invalid API key")

        # Update last_used_at
        await conn.execute(
            "UPDATE api_keys SET last_used_at = %s WHERE id = %s",
            (datetime.now(timezone.utc), key_id),
        )

        source = _KEY_TYPE_TO_SOURCE.get(key_type, "api")

        # Stash on request.state so downstream middleware (e.g. request_log)
        # can read org and actor IDs after the handler returns. Safe to set
        # even if no middleware reads them.
        request.state.user_org_id = str(org_id)
        request.state.user_id = str(user_id)

        return UserContext(
            id=str(user_id),
            org_id=str(org_id),
            display_name=display_name,
            role=role,
            department=department,
            source=source,
            key_type=key_type,
        )
