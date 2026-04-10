"""Share/unshare endpoints for entry-level and path-level permissions."""

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg.rows import dict_row

from auth import UserContext, get_current_user
from database import get_db
from models import (
    EntryPermissionResponse,
    PathPermissionResponse,
    PermissionGrant,
    PathPermissionGrant,
)

VALID_PERMISSION_ROLES = {"admin", "editor", "commenter", "viewer"}

# ---------------------------------------------------------------------------
# Entry-level permissions router — mounts at /entries
# ---------------------------------------------------------------------------

entry_perms_router = APIRouter(tags=["permissions"])


def _validate_role(role: str) -> None:
    """Raise 422 if role is not a valid permission role."""
    if role not in VALID_PERMISSION_ROLES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid role '{role}'. Must be one of: {sorted(VALID_PERMISSION_ROLES)}",
        )


async def _require_admin_or_owner(
    conn, user: UserContext, entry_id: str
) -> None:
    """Raise 403 unless the user is an admin or owns the entry."""
    if user.role == "admin":
        return
    cur = await conn.execute(
        "SELECT owner_id FROM entries WHERE id = %s",
        (entry_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    owner_id = str(row[0]) if row[0] else None
    if owner_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="Only the entry owner or an admin can manage permissions on this entry",
        )


def _entry_perm_to_response(row: dict) -> EntryPermissionResponse:
    """Convert a database row dict to an EntryPermissionResponse."""
    return EntryPermissionResponse(
        id=str(row["id"]),
        entry_id=str(row["entry_id"]),
        user_id=str(row["user_id"]),
        role=row["role"],
        granted_by=str(row["granted_by"]),
        created_at=row["created_at"],
    )


_ENTRY_PERM_COLS = "id, entry_id, user_id, role, granted_by, created_at"


@entry_perms_router.post(
    "/{entry_id}/permissions",
    response_model=EntryPermissionResponse,
    status_code=201,
)
async def grant_entry_permission(
    entry_id: str,
    body: PermissionGrant,
    user: UserContext = Depends(get_current_user),
):
    """Grant a user access to a specific entry (owner or admin only)."""
    _validate_role(body.role)

    async with get_db(user) as conn:
        await _require_admin_or_owner(conn, user, entry_id)

        try:
            cur = await conn.execute(
                f"""
                INSERT INTO entry_permissions (
                    org_id, entry_id, user_id, role, granted_by
                ) VALUES (%s, %s, %s, %s, %s)
                RETURNING {_ENTRY_PERM_COLS}
                """,
                (user.org_id, entry_id, body.user_id, body.role, user.id),
            )
        except Exception as exc:
            # Unique constraint violation — duplicate grant
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise HTTPException(
                    status_code=409,
                    detail="Permission grant already exists for this user on this entry",
                )
            raise

        cur.row_factory = dict_row
        row = await cur.fetchone()
        return _entry_perm_to_response(row)


@entry_perms_router.delete("/{entry_id}/permissions/{target_user_id}")
async def revoke_entry_permission(
    entry_id: str,
    target_user_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Revoke a user's access to a specific entry (owner or admin only)."""
    async with get_db(user) as conn:
        await _require_admin_or_owner(conn, user, entry_id)

        cur = await conn.execute(
            "DELETE FROM entry_permissions WHERE entry_id = %s AND user_id = %s RETURNING id",
            (entry_id, target_user_id),
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail="Permission grant not found",
            )
        return {"message": "Permission revoked"}


@entry_perms_router.get(
    "/{entry_id}/permissions",
    response_model=list[EntryPermissionResponse],
)
async def list_entry_permissions(
    entry_id: str,
    user: UserContext = Depends(get_current_user),
):
    """List all permission grants on an entry (owner or admin only)."""
    async with get_db(user) as conn:
        await _require_admin_or_owner(conn, user, entry_id)

        cur = await conn.execute(
            f"""
            SELECT {_ENTRY_PERM_COLS}
            FROM entry_permissions
            WHERE entry_id = %s
            ORDER BY created_at DESC
            """,
            (entry_id,),
        )
        cur.row_factory = dict_row
        rows = await cur.fetchall()
        return [_entry_perm_to_response(r) for r in rows]


# ---------------------------------------------------------------------------
# Path-level permissions router — mounts at /paths
# ---------------------------------------------------------------------------

path_perms_router = APIRouter(tags=["permissions"])


def _require_admin(user: UserContext) -> None:
    """Raise 403 if user is not an admin."""
    if user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Only admins can manage path permissions",
        )


def _path_perm_to_response(row: dict) -> PathPermissionResponse:
    """Convert a database row dict to a PathPermissionResponse."""
    return PathPermissionResponse(
        id=str(row["id"]),
        path_pattern=row["path_pattern"],
        user_id=str(row["user_id"]),
        role=row["role"],
        granted_by=str(row["granted_by"]),
        created_at=row["created_at"],
    )


_PATH_PERM_COLS = "id, path_pattern, user_id, role, granted_by, created_at"


@path_perms_router.post(
    "/permissions",
    response_model=PathPermissionResponse,
    status_code=201,
)
async def grant_path_permission(
    body: PathPermissionGrant,
    user: UserContext = Depends(get_current_user),
):
    """Grant a user access on a path pattern (admin only)."""
    _require_admin(user)
    _validate_role(body.role)

    async with get_db(user) as conn:
        try:
            cur = await conn.execute(
                f"""
                INSERT INTO path_permissions (
                    org_id, path_pattern, user_id, role, granted_by
                ) VALUES (%s, %s, %s, %s, %s)
                RETURNING {_PATH_PERM_COLS}
                """,
                (user.org_id, body.path_pattern, body.user_id, body.role, user.id),
            )
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise HTTPException(
                    status_code=409,
                    detail="Permission grant already exists for this user on this path pattern",
                )
            raise

        cur.row_factory = dict_row
        row = await cur.fetchone()
        return _path_perm_to_response(row)


@path_perms_router.delete("/permissions/{permission_id}")
async def revoke_path_permission(
    permission_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Revoke a path permission grant by ID (admin only)."""
    _require_admin(user)

    async with get_db(user) as conn:
        cur = await conn.execute(
            "DELETE FROM path_permissions WHERE id = %s RETURNING id",
            (permission_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail="Path permission not found",
            )
        return {"message": "Path permission revoked"}


@path_perms_router.get(
    "/permissions",
    response_model=list[PathPermissionResponse],
)
async def list_path_permissions(
    user_id: str | None = Query(None, description="Filter by user ID"),
    user: UserContext = Depends(get_current_user),
):
    """List path permission grants (admin only). Optional user_id filter."""
    _require_admin(user)

    conditions = []
    params: list = []

    if user_id:
        conditions.append("user_id = %s")
        params.append(user_id)

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    async with get_db(user) as conn:
        cur = await conn.execute(
            f"""
            SELECT {_PATH_PERM_COLS}
            FROM path_permissions
            {where_clause}
            ORDER BY created_at DESC
            """,
            params,
        )
        cur.row_factory = dict_row
        rows = await cur.fetchall()
        return [_path_perm_to_response(r) for r in rows]
