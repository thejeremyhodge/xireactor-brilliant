"""Share/unshare endpoints for entry-level and path-level permissions.

Permissions v2 (P1): grants are written to the unified `permissions` table
with a polymorphic principal — `(principal_type, principal_id)` where
`principal_type ∈ {'user', 'group'}`. Route paths are unchanged from v1; only
the request body shape differs (accepts `principal_type` + `principal_id`,
with `user_id` still accepted as an alias for `principal_id`).

Audit-log rows for grant/revoke are emitted via `services.audit.record`.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg.rows import dict_row

from auth import UserContext, get_current_user
from database import get_db
from services.audit import record_for_user as _audit
from models import (
    EntryPermissionResponse,
    PathPermissionGrant,
    PathPermissionResponse,
    PermissionGrant,
)

VALID_PERMISSION_ROLES = {"admin", "editor", "commenter", "viewer"}
VALID_PRINCIPAL_TYPES = {"user", "group"}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _validate_role(role: str) -> None:
    """Raise 422 if role is not a valid permission role."""
    if role not in VALID_PERMISSION_ROLES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid role '{role}'. Must be one of: {sorted(VALID_PERMISSION_ROLES)}",
        )


def _validate_principal_type(principal_type: str) -> None:
    """Raise 422 if principal_type is not user|group."""
    if principal_type not in VALID_PRINCIPAL_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid principal_type '{principal_type}'. "
                f"Must be one of: {sorted(VALID_PRINCIPAL_TYPES)}"
            ),
        )


async def _validate_group_principal(conn, user: UserContext, group_id: str) -> None:
    """For principal_type='group', confirm the group exists in the caller's org.

    Returns silently on success; raises 422 otherwise (per spec: invalid group
    ID must be 422, not 500).
    """
    cur = await conn.execute(
        "SELECT 1 FROM groups WHERE id = %s AND org_id = %s",
        (group_id, user.org_id),
    )
    if (await cur.fetchone()) is None:
        raise HTTPException(
            status_code=422,
            detail=f"Group '{group_id}' does not exist in this organization",
        )


# ---------------------------------------------------------------------------
# Entry-level permissions router — mounts at /entries
# ---------------------------------------------------------------------------

entry_perms_router = APIRouter(tags=["permissions"])


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
        principal_type=row["principal_type"],
        principal_id=str(row["principal_id"]),
        role=row["role"],
        granted_by=str(row["granted_by"]),
        created_at=row["created_at"],
    )


_ENTRY_PERM_COLS = (
    "id, entry_id, principal_type, principal_id, role, granted_by, created_at"
)


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
    """Grant a principal (user or group) access to a specific entry.

    Owner-or-admin only. Body accepts `principal_type` (default 'user') +
    `principal_id`; legacy `user_id` is still accepted as an alias.
    """
    _validate_role(body.role)
    _validate_principal_type(body.principal_type)

    async with get_db(user) as conn:
        await _require_admin_or_owner(conn, user, entry_id)

        if body.principal_type == "group":
            await _validate_group_principal(conn, user, body.principal_id)

        try:
            cur = await conn.execute(
                f"""
                INSERT INTO permissions (
                    org_id, principal_type, principal_id,
                    resource_type, entry_id, role, granted_by
                ) VALUES (%s, %s, %s, 'entry', %s, %s, %s)
                RETURNING {_ENTRY_PERM_COLS}
                """,
                (
                    user.org_id,
                    body.principal_type,
                    body.principal_id,
                    entry_id,
                    body.role,
                    user.id,
                ),
            )
        except Exception as exc:
            # Unique constraint violation — duplicate grant
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Permission grant already exists for this principal "
                        "on this entry"
                    ),
                )
            raise

        cur.row_factory = dict_row
        row = await cur.fetchone()
        await _audit(
            conn,
            user,
            action="grant",
            target_table="permissions",
            target_id=str(row["id"]),
            target_path=None,
            metadata={
                "resource_type": "entry",
                "entry_id": entry_id,
                "principal_type": body.principal_type,
                "principal_id": body.principal_id,
                "role": body.role,
            },
        )
        return _entry_perm_to_response(row)


@entry_perms_router.delete("/{entry_id}/permissions/{target_principal_id}")
async def revoke_entry_permission(
    entry_id: str,
    target_principal_id: str,
    principal_type: str = Query(
        "user",
        description="Principal type to revoke ('user' or 'group'). Defaults to 'user'.",
    ),
    user: UserContext = Depends(get_current_user),
):
    """Revoke a principal's access to a specific entry (owner or admin only).

    `principal_type` is a query param defaulting to `'user'` for back-compat.
    """
    _validate_principal_type(principal_type)

    async with get_db(user) as conn:
        await _require_admin_or_owner(conn, user, entry_id)

        cur = await conn.execute(
            """
            DELETE FROM permissions
            WHERE resource_type = 'entry'
              AND entry_id = %s
              AND principal_type = %s
              AND principal_id = %s
            RETURNING id
            """,
            (entry_id, principal_type, target_principal_id),
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail="Permission grant not found",
            )
        await _audit(
            conn,
            user,
            action="revoke",
            target_table="permissions",
            target_id=str(row[0]),
            metadata={
                "resource_type": "entry",
                "entry_id": entry_id,
                "principal_type": principal_type,
                "principal_id": target_principal_id,
            },
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
            FROM permissions
            WHERE resource_type = 'entry'
              AND entry_id = %s
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
        principal_type=row["principal_type"],
        principal_id=str(row["principal_id"]),
        role=row["role"],
        granted_by=str(row["granted_by"]),
        created_at=row["created_at"],
    )


_PATH_PERM_COLS = (
    "id, path_pattern, principal_type, principal_id, role, granted_by, created_at"
)


@path_perms_router.post(
    "/permissions",
    response_model=PathPermissionResponse,
    status_code=201,
)
async def grant_path_permission(
    body: PathPermissionGrant,
    user: UserContext = Depends(get_current_user),
):
    """Grant a principal (user or group) access on a path pattern (admin only)."""
    _require_admin(user)
    _validate_role(body.role)
    _validate_principal_type(body.principal_type)

    async with get_db(user) as conn:
        if body.principal_type == "group":
            await _validate_group_principal(conn, user, body.principal_id)

        try:
            cur = await conn.execute(
                f"""
                INSERT INTO permissions (
                    org_id, principal_type, principal_id,
                    resource_type, path_pattern, role, granted_by
                ) VALUES (%s, %s, %s, 'path', %s, %s, %s)
                RETURNING {_PATH_PERM_COLS}
                """,
                (
                    user.org_id,
                    body.principal_type,
                    body.principal_id,
                    body.path_pattern,
                    body.role,
                    user.id,
                ),
            )
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Permission grant already exists for this principal "
                        "on this path pattern"
                    ),
                )
            raise

        cur.row_factory = dict_row
        row = await cur.fetchone()
        await _audit(
            conn,
            user,
            action="grant",
            target_table="permissions",
            target_id=str(row["id"]),
            target_path=body.path_pattern,
            metadata={
                "resource_type": "path",
                "path_pattern": body.path_pattern,
                "principal_type": body.principal_type,
                "principal_id": body.principal_id,
                "role": body.role,
            },
        )
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
            """
            DELETE FROM permissions
            WHERE id = %s AND resource_type = 'path'
            RETURNING id
            """,
            (permission_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail="Path permission not found",
            )
        await _audit(
            conn,
            user,
            action="revoke",
            target_table="permissions",
            target_id=str(row[0]),
            metadata={
                "resource_type": "path",
                "permission_id": permission_id,
            },
        )
        return {"message": "Path permission revoked"}


@path_perms_router.get(
    "/permissions",
    response_model=list[PathPermissionResponse],
)
async def list_path_permissions(
    principal_id: str | None = Query(
        None,
        description="Filter by principal ID (user or group)",
        alias="principal_id",
    ),
    principal_type: str | None = Query(
        None,
        description="Filter by principal type ('user' or 'group')",
    ),
    user_id: str | None = Query(
        None,
        description="DEPRECATED: alias for principal_id (assumes principal_type='user')",
    ),
    user: UserContext = Depends(get_current_user),
):
    """List path permission grants (admin only).

    Supports optional `principal_id` + `principal_type` filters. The legacy
    `user_id` query param is still accepted and implies `principal_type='user'`.
    """
    _require_admin(user)

    # Back-compat: collapse user_id into principal_id with implicit user type.
    if user_id and not principal_id:
        principal_id = user_id
        if not principal_type:
            principal_type = "user"

    if principal_type:
        _validate_principal_type(principal_type)

    conditions = ["resource_type = 'path'"]
    params: list = []

    if principal_id:
        conditions.append("principal_id = %s")
        params.append(principal_id)
    if principal_type:
        conditions.append("principal_type = %s")
        params.append(principal_type)

    where_clause = "WHERE " + " AND ".join(conditions)

    async with get_db(user) as conn:
        cur = await conn.execute(
            f"""
            SELECT {_PATH_PERM_COLS}
            FROM permissions
            {where_clause}
            ORDER BY created_at DESC
            """,
            params,
        )
        cur.row_factory = dict_row
        rows = await cur.fetchall()
        return [_path_perm_to_response(r) for r in rows]
