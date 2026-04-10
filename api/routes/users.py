"""User management routes: list members, change roles, deactivate, remove."""

from fastapi import APIRouter, Depends, HTTPException
from psycopg.rows import dict_row

from auth import UserContext, get_current_user
from database import get_pool
from models import UserResponse, UserRoleUpdate

VALID_ROLES = {"admin", "editor", "commenter", "viewer"}


def _require_admin(user: UserContext) -> None:
    """Raise 403 if user is not an admin."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")


def _row_to_response(row: dict) -> UserResponse:
    """Convert a database row dict to a UserResponse."""
    return UserResponse(
        id=row["id"],
        org_id=row["org_id"],
        display_name=row["display_name"],
        email=row.get("email"),
        role=row["role"],
        department=row.get("department"),
        is_active=row["is_active"],
    )


# ---------------------------------------------------------------------------
# Members router — mounts at /org
# ---------------------------------------------------------------------------

members_router = APIRouter(tags=["users"])


@members_router.get("/members", response_model=list[UserResponse])
async def list_members(
    user: UserContext = Depends(get_current_user),
):
    """List all users in the caller's organization (admin only).

    Uses raw pool since users table has no RLS.
    """
    _require_admin(user)

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT id, org_id, display_name, email, role, department, is_active
            FROM users
            WHERE org_id = %s
            ORDER BY created_at ASC
            """,
            (user.org_id,),
        )
        cur.row_factory = dict_row
        rows = await cur.fetchall()
        return [_row_to_response(r) for r in rows]


# ---------------------------------------------------------------------------
# Users router — mounts at /users
# ---------------------------------------------------------------------------

users_router = APIRouter(tags=["users"])


@users_router.patch("/{user_id}/role", response_model=UserResponse)
async def change_role(
    user_id: str,
    body: UserRoleUpdate,
    user: UserContext = Depends(get_current_user),
):
    """Change a user's role (admin only). Cannot demote yourself."""
    _require_admin(user)

    if user_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot change your own role")

    if body.role not in VALID_ROLES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid role '{body.role}'. Must be one of: {sorted(VALID_ROLES)}",
        )

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            UPDATE users SET role = %s, updated_at = NOW()
            WHERE id = %s AND org_id = %s
            RETURNING id, org_id, display_name, email, role, department, is_active
            """,
            (body.role, user_id, user.org_id),
        )
        cur.row_factory = dict_row
        row = await cur.fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="User not found")

        return _row_to_response(row)


@users_router.patch("/{user_id}/deactivate", response_model=UserResponse)
async def deactivate_user(
    user_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Deactivate a user account (admin only). Cannot deactivate yourself."""
    _require_admin(user)

    if user_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            UPDATE users SET is_active = FALSE, updated_at = NOW()
            WHERE id = %s AND org_id = %s
            RETURNING id, org_id, display_name, email, role, department, is_active
            """,
            (user_id, user.org_id),
        )
        cur.row_factory = dict_row
        row = await cur.fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="User not found")

        return _row_to_response(row)


@users_router.delete("/{user_id}")
async def remove_user(
    user_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Remove a user: deactivate + revoke all API keys (admin only). Cannot remove yourself."""
    _require_admin(user)

    if user_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot remove yourself")

    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            # Deactivate user
            cur = await conn.execute(
                """
                UPDATE users SET is_active = FALSE, updated_at = NOW()
                WHERE id = %s AND org_id = %s
                RETURNING id
                """,
                (user_id, user.org_id),
            )
            row = await cur.fetchone()

            if row is None:
                raise HTTPException(status_code=404, detail="User not found")

            # Revoke all API keys
            await conn.execute(
                "UPDATE api_keys SET is_revoked = TRUE WHERE user_id = %s",
                (user_id,),
            )

            return {"message": "User deactivated and all API keys revoked"}
