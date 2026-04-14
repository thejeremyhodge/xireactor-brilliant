"""Groups CRUD API — permissions v2 (P1).

Groups are named collections of users within an org. They serve as principals
in the unified `permissions` table (principal_type = 'group'). Admin users can
create / delete groups and manage membership; any authenticated user can list
groups in their org and inspect groups they belong to (full membership visible
to members; non-members see name + description only).

RLS is enforced at the DB layer (see db/migrations/018_principals_and_groups.sql):
- `groups`: admin full CRUD; non-admin SELECT all rows in their org.
- `group_members`: admin full CRUD; non-admin SELECT own memberships only.
- `permissions`: admin full CRUD; non-admin SELECT own (user or group) grants.

Mutations emit audit-log rows via `services.audit.record`.
"""

from fastapi import APIRouter, Depends, HTTPException
from psycopg.rows import dict_row

from auth import UserContext, get_current_user
from database import get_db
from services.audit import record_for_user as _audit
from models import (
    GroupCreate,
    GroupDetailResponse,
    GroupMemberGrant,
    GroupMemberResponse,
    GroupResponse,
)


groups_router = APIRouter(tags=["groups"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_admin(user: UserContext) -> None:
    """Raise 403 if the caller is not an admin."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")


def _row_to_group_response(row: dict) -> GroupResponse:
    return GroupResponse(
        id=str(row["id"]),
        org_id=str(row["org_id"]),
        name=row["name"],
        description=row.get("description"),
        created_by=str(row["created_by"]),
        created_at=row["created_at"],
        member_count=row.get("member_count"),
    )


def _row_to_member_response(row: dict) -> GroupMemberResponse:
    return GroupMemberResponse(
        group_id=str(row["group_id"]),
        user_id=str(row["user_id"]),
        org_id=str(row["org_id"]),
        added_by=str(row["added_by"]),
        added_at=row["added_at"],
    )


def _is_duplicate_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "unique" in msg or "duplicate" in msg


# ---------------------------------------------------------------------------
# POST /groups — create (admin)
# ---------------------------------------------------------------------------


@groups_router.post("", response_model=GroupResponse, status_code=201)
async def create_group(
    body: GroupCreate,
    user: UserContext = Depends(get_current_user),
):
    """Create a new group in the caller's org. Admin only.

    Returns 409 if a group with the same name already exists in the org.
    """
    _require_admin(user)

    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Group name is required")

    async with get_db(user) as conn:
        try:
            cur = await conn.execute(
                """
                INSERT INTO groups (org_id, name, description, created_by)
                VALUES (%s, %s, %s, %s)
                RETURNING id, org_id, name, description, created_by, created_at
                """,
                (user.org_id, name, body.description, user.id),
            )
        except Exception as exc:
            if _is_duplicate_error(exc):
                raise HTTPException(
                    status_code=409,
                    detail=f"Group with name '{name}' already exists in this org",
                )
            raise

        cur.row_factory = dict_row
        row = await cur.fetchone()
        await _audit(
            conn,
            user,
            action="group_create",
            target_table="groups",
            target_id=str(row["id"]),
            metadata={
                "name": row["name"],
                "description": row.get("description"),
            },
        )
        return _row_to_group_response(row)


# ---------------------------------------------------------------------------
# GET /groups — list (any authenticated user)
# ---------------------------------------------------------------------------


@groups_router.get("", response_model=list[GroupResponse])
async def list_groups(
    user: UserContext = Depends(get_current_user),
):
    """List all groups in the caller's org.

    Any authenticated user can list groups in their org — RLS policy
    `groups_select` permits SELECT across the org for non-admin roles.
    `member_count` is included for UI convenience.
    """
    async with get_db(user) as conn:
        cur = await conn.execute(
            """
            SELECT
                g.id,
                g.org_id,
                g.name,
                g.description,
                g.created_by,
                g.created_at,
                (
                    SELECT COUNT(*)::int
                    FROM group_members gm
                    WHERE gm.group_id = g.id
                ) AS member_count
            FROM groups g
            WHERE g.org_id = %s
            ORDER BY g.name ASC
            """,
            (user.org_id,),
        )
        cur.row_factory = dict_row
        rows = await cur.fetchall()
        return [_row_to_group_response(r) for r in rows]


# ---------------------------------------------------------------------------
# GET /groups/{id} — detail (members see membership; non-members see meta only)
# ---------------------------------------------------------------------------


@groups_router.get("/{group_id}", response_model=GroupDetailResponse)
async def get_group(
    group_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Return group details.

    - Admins and members see the full member list.
    - Non-members (non-admin) see name + description + metadata only (members=None).

    RLS limits non-admin SELECT on `group_members` to the caller's own rows,
    so we determine "is member" by probing that view and then, for admins only,
    fetching all members via their elevated privileges.
    """
    async with get_db(user) as conn:
        # Fetch the group itself (RLS allows any member of the org to SELECT).
        cur = await conn.execute(
            """
            SELECT id, org_id, name, description, created_by, created_at
            FROM groups
            WHERE id = %s
            """,
            (group_id,),
        )
        cur.row_factory = dict_row
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Group not found")

        # Determine whether the caller is a member. Non-admin SELECT on
        # group_members is already restricted by RLS to own rows, so this
        # query naturally returns rows only when the caller belongs.
        cur = await conn.execute(
            "SELECT 1 FROM group_members WHERE group_id = %s AND user_id = %s",
            (group_id, user.id),
        )
        is_member = (await cur.fetchone()) is not None

        members: list[GroupMemberResponse] | None
        if user.role == "admin" or is_member:
            cur = await conn.execute(
                """
                SELECT group_id, user_id, org_id, added_by, added_at
                FROM group_members
                WHERE group_id = %s
                ORDER BY added_at ASC
                """,
                (group_id,),
            )
            cur.row_factory = dict_row
            member_rows = await cur.fetchall()
            members = [_row_to_member_response(r) for r in member_rows]
        else:
            members = None

        return GroupDetailResponse(
            id=str(row["id"]),
            org_id=str(row["org_id"]),
            name=row["name"],
            description=row.get("description"),
            created_by=str(row["created_by"]),
            created_at=row["created_at"],
            members=members,
        )


# ---------------------------------------------------------------------------
# DELETE /groups/{id} — delete (admin)
# ---------------------------------------------------------------------------


@groups_router.delete("/{group_id}")
async def delete_group(
    group_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Delete a group. Admin only.

    Cascades:
    - `group_members` rows are dropped via ON DELETE CASCADE on the FK.
    - `permissions` rows where `principal_type='group' AND principal_id=group.id`
      have no FK (principal_id is TEXT polymorphic) and must be cleaned up
      explicitly. We do both operations in the same transaction so deletion is
      atomic.
    """
    _require_admin(user)

    async with get_db(user) as conn:
        # Verify the group exists in this org up front so we return a clean 404.
        cur = await conn.execute(
            "SELECT id FROM groups WHERE id = %s",
            (group_id,),
        )
        if (await cur.fetchone()) is None:
            raise HTTPException(status_code=404, detail="Group not found")

        # Explicit cleanup of polymorphic permission grants (no FK cascade possible).
        await conn.execute(
            """
            DELETE FROM permissions
            WHERE org_id = %s
              AND principal_type = 'group'
              AND principal_id = %s
            """,
            (user.org_id, group_id),
        )

        # Delete the group itself; group_members cascades via FK.
        await conn.execute(
            "DELETE FROM groups WHERE id = %s",
            (group_id,),
        )

        await _audit(
            conn,
            user,
            action="group_delete",
            target_table="groups",
            target_id=group_id,
            metadata={"group_id": group_id},
        )
        return {"message": "Group deleted"}


# ---------------------------------------------------------------------------
# POST /groups/{id}/members — add member (admin)
# ---------------------------------------------------------------------------


@groups_router.post(
    "/{group_id}/members",
    response_model=GroupMemberResponse,
    status_code=201,
)
async def add_group_member(
    group_id: str,
    body: GroupMemberGrant,
    user: UserContext = Depends(get_current_user),
):
    """Add a user to a group. Admin only.

    Returns 409 if the user is already a member, 404 if the group or user is
    not found (or not in the caller's org).
    """
    _require_admin(user)

    async with get_db(user) as conn:
        # Verify the group exists in-org.
        cur = await conn.execute(
            "SELECT id FROM groups WHERE id = %s",
            (group_id,),
        )
        if (await cur.fetchone()) is None:
            raise HTTPException(status_code=404, detail="Group not found")

        # Verify the target user exists in the same org. users table has no RLS,
        # so we scope explicitly via org_id.
        cur = await conn.execute(
            "SELECT id FROM users WHERE id = %s AND org_id = %s",
            (body.user_id, user.org_id),
        )
        if (await cur.fetchone()) is None:
            raise HTTPException(
                status_code=404,
                detail="User not found in this organization",
            )

        try:
            cur = await conn.execute(
                """
                INSERT INTO group_members (group_id, user_id, org_id, added_by)
                VALUES (%s, %s, %s, %s)
                RETURNING group_id, user_id, org_id, added_by, added_at
                """,
                (group_id, body.user_id, user.org_id, user.id),
            )
        except Exception as exc:
            if _is_duplicate_error(exc):
                raise HTTPException(
                    status_code=409,
                    detail="User is already a member of this group",
                )
            raise

        cur.row_factory = dict_row
        row = await cur.fetchone()
        await _audit(
            conn,
            user,
            action="group_member_add",
            target_table="group_members",
            target_id=str(row["group_id"]),
            metadata={
                "group_id": str(row["group_id"]),
                "user_id": str(row["user_id"]),
            },
        )
        return _row_to_member_response(row)


# ---------------------------------------------------------------------------
# DELETE /groups/{id}/members/{user_id} — remove member (admin)
# ---------------------------------------------------------------------------


@groups_router.delete("/{group_id}/members/{member_user_id}")
async def remove_group_member(
    group_id: str,
    member_user_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Remove a user from a group. Admin only.

    Group-inherited permissions are resolved live against `group_members` at
    query time, so removing a member immediately revokes any group-granted
    access — no cache invalidation is required.
    """
    _require_admin(user)

    async with get_db(user) as conn:
        cur = await conn.execute(
            """
            DELETE FROM group_members
            WHERE group_id = %s AND user_id = %s
            RETURNING group_id
            """,
            (group_id, member_user_id),
        )
        if (await cur.fetchone()) is None:
            raise HTTPException(
                status_code=404,
                detail="Membership not found",
            )

        await _audit(
            conn,
            user,
            action="group_member_remove",
            target_table="group_members",
            target_id=group_id,
            metadata={
                "group_id": group_id,
                "user_id": member_user_id,
            },
        )
        return {"message": "Member removed"}
