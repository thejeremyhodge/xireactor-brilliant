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
    key_type: str  # interactive | agent | api_integration | service


# Map key_type to source
_KEY_TYPE_TO_SOURCE = {
    "interactive": "web_ui",
    "agent": "agent",
    "api_integration": "api",
    # 'service' keys always act on behalf of a user via X-Act-As-User; the
    # effective source is therefore the target user's downstream context.
    # The fallback mapping below only applies when the service key is used
    # without an X-Act-As-User header (service-identity calls, rare).
    "service": "api",
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
    5. If X-Act-As-User header is present:
         - key_type must be 'service' (else 403)
         - load the target user row and return UserContext for *that* user
           (RLS downstream scopes to the target user via app.user_id +
           kb_* role); the service key owner's identity is intentionally
           dropped so per-user RLS is enforced end-to-end.
    6. Map key_type to source
    7. Update last_used_at
    8. Return UserContext
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

        # ------------------------------------------------------------------
        # X-Act-As-User handling (service-role gate)
        # ------------------------------------------------------------------
        # A service-role key may present X-Act-As-User: <user_id> to act as
        # a different end user — the MCP layer uses this to thread the
        # OAuth-bound user_id into every API call so per-user RLS applies.
        # Any non-service key presenting the header is a client-side bug
        # or an abuse attempt; reject with 403.
        act_as_user_id = request.headers.get("X-Act-As-User")
        if act_as_user_id is not None:
            act_as_user_id = act_as_user_id.strip()
            if not act_as_user_id:
                raise HTTPException(
                    status_code=400,
                    detail="X-Act-As-User header present but empty",
                )

            if key_type != "service":
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "X-Act-As-User header is only honored on service-role "
                        "API keys"
                    ),
                )

            # Load the target user. Must be active and in the same org as the
            # service key's owner (belt-and-suspenders tenant isolation).
            cur = await conn.execute(
                """
                SELECT id, org_id, display_name, role, department
                FROM users
                WHERE id = %s
                  AND is_active = TRUE
                """,
                (act_as_user_id,),
            )
            target = await cur.fetchone()
            if target is None:
                raise HTTPException(
                    status_code=404,
                    detail="X-Act-As-User target user not found or inactive",
                )

            (
                target_id,
                target_org_id,
                target_display_name,
                target_role,
                target_department,
            ) = target

            if str(target_org_id) != str(org_id):
                raise HTTPException(
                    status_code=403,
                    detail="X-Act-As-User target belongs to a different org",
                )

            # Stash *target* identity on request.state so middleware (e.g.
            # request_log + RLS session setup) scopes downstream queries to
            # the acting user, not the service key owner.
            request.state.user_org_id = str(target_org_id)
            request.state.user_id = str(target_id)

            # source: still reflects that the call came in on a service
            # channel but the acting identity is the target user's.
            return UserContext(
                id=str(target_id),
                org_id=str(target_org_id),
                display_name=target_display_name,
                role=target_role,
                department=target_department,
                source="api",
                # Preserve key_type='service' so downstream code can tell
                # this request rode in on a service channel (useful for
                # audit + debugging) without changing authorization — role
                # is what gates access, and role is the target user's.
                key_type="service",
            )

        # ------------------------------------------------------------------
        # No X-Act-As-User header → normal self-auth path.
        # ------------------------------------------------------------------
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
