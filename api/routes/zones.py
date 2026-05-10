"""Personal-zones router — list + promote endpoints (Sprint 0051).

Two endpoints back the personal-zone safety model:

* ``GET /zone`` — list the calling user's zone entries. Implementation joins
  ``entries`` to ``permissions`` on the caller's zone group id, with the
  same RLS scoping as ``GET /entries``. Pagination matches ``list_entries``
  (``limit`` / ``offset``).

* ``POST /zone/promote`` — additive permission grants on an entry that
  currently sits in the caller's zone. Caller must be admin (or owner) on
  the entry. The zone grant is preserved across any number of promote
  calls — promotion never destroys access. Optional ``new_sensitivity``
  bumps ``entries.sensitivity`` (rejected if it would downgrade a
  non-private entry back to ``private``).

The whole promote path runs inside the single transaction owned by
``get_db(user)`` (see ``api/database.py``) so all permission inserts plus
the optional sensitivity update commit atomically or not at all.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg.rows import dict_row

from auth import UserContext, get_current_user
from database import get_db
from models import (
    EntryList,
    EntryPermissionResponse,
    VALID_SENSITIVITIES,
    ZonePromote,
    ZonePromoteResponse,
)
from services.zones import get_or_create_zone

router = APIRouter(tags=["zones"])


VALID_PROMOTE_ROLES = {"admin", "editor", "commenter", "viewer"}
VALID_PRINCIPAL_TYPES = {"user", "group"}

# Same SELECT shape as routes/entries.py::_SELECT_COLS so EntryResponse
# round-trips identically. Kept inline (not imported) to avoid a module-
# level coupling between routers.
_ENTRY_COLS = """
    id, org_id, title, content, summary, content_hash,
    content_type, logical_path, sensitivity, department,
    owner_id, project_id, tags, domain_meta,
    version, status, source,
    created_by, updated_by, created_at, updated_at,
    claim_type, source_confidence, verification_status, conflict_with
"""

_ENTRY_PERM_COLS = (
    "id, entry_id, principal_type, principal_id, role, granted_by, created_at"
)


def _row_to_entry_response(row: dict) -> dict:
    """Convert a DB row dict to the JSON-friendly shape EntryResponse expects."""
    return {
        "id": str(row["id"]),
        "org_id": str(row["org_id"]),
        "title": row["title"],
        "content": row["content"],
        "summary": row.get("summary"),
        "content_type": row["content_type"],
        "logical_path": row["logical_path"],
        "sensitivity": row["sensitivity"],
        "department": row.get("department"),
        "owner_id": str(row["owner_id"]) if row.get("owner_id") else None,
        "tags": row.get("tags") or [],
        "domain_meta": row.get("domain_meta") or {},
        "version": row["version"],
        "status": row["status"],
        "source": row["source"],
        "created_by": str(row["created_by"]),
        "updated_by": str(row["updated_by"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "claim_type": row.get("claim_type"),
        "source_confidence": row.get("source_confidence"),
        "verification_status": row.get("verification_status"),
        "conflict_with": row.get("conflict_with") or [],
    }


def _perm_row_to_response(row: dict) -> EntryPermissionResponse:
    return EntryPermissionResponse(
        id=str(row["id"]),
        entry_id=str(row["entry_id"]),
        principal_type=row["principal_type"],
        principal_id=str(row["principal_id"]),
        role=row["role"],
        granted_by=str(row["granted_by"]),
        created_at=row["created_at"],
    )


@router.get("", response_model=EntryList)
async def list_zone_entries(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(get_current_user),
):
    """Return the calling user's zone entries.

    Joins ``entries`` to ``permissions`` on the caller's zone group id —
    only entries explicitly bound to the caller's zone surface here. RLS
    further scopes visibility, so admins do *not* see other users' zones
    via this endpoint (zone privacy is an explicit safety property of the
    personal-zones design).
    """
    async with get_db(user) as conn:
        zone_group_id = await get_or_create_zone(conn, user.id, user.org_id)

        cur = await conn.execute(
            """
            SELECT COUNT(*)
            FROM entries e
            JOIN permissions p ON p.entry_id = e.id
            WHERE p.resource_type = 'entry'
              AND p.principal_type = 'group'
              AND p.principal_id = %s
            """,
            (zone_group_id,),
        )
        total = (await cur.fetchone())[0]

        cur = await conn.execute(
            f"""
            SELECT {_ENTRY_COLS}
            FROM entries e
            JOIN permissions p ON p.entry_id = e.id
            WHERE p.resource_type = 'entry'
              AND p.principal_type = 'group'
              AND p.principal_id = %s
            ORDER BY e.updated_at DESC
            LIMIT %s OFFSET %s
            """,
            (zone_group_id, limit, offset),
        )
        cur.row_factory = dict_row
        rows = await cur.fetchall()

        return {
            "entries": [_row_to_entry_response(r) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


async def _require_entry_admin(conn, user: UserContext, entry_id: str) -> dict:
    """Ensure caller has admin on ``entry_id``; return the entry row.

    Admin if any of the following:
      * caller's role is ``admin`` (org-level)
      * caller is ``entries.owner_id`` (entry owner)
      * caller has a direct user-principal ``admin`` permission row
      * caller is a member of a group that holds an ``admin`` permission row

    Returns the entry row dict on success; raises 404 if missing or 403 if
    the caller lacks admin.
    """
    cur = await conn.execute(
        f"SELECT {_ENTRY_COLS} FROM entries WHERE id = %s",
        (entry_id,),
    )
    cur.row_factory = dict_row
    row = await cur.fetchone()
    if row is None:
        # RLS may also hide the entry; treat hidden + missing the same.
        raise HTTPException(status_code=404, detail="Entry not found")

    if user.role == "admin":
        return row
    if row.get("owner_id") and str(row["owner_id"]) == user.id:
        return row

    # Look up admin grant via direct user OR via any group the caller belongs to.
    cur = await conn.execute(
        """
        SELECT 1
        FROM permissions p
        WHERE p.resource_type = 'entry'
          AND p.entry_id = %s
          AND p.role = 'admin'
          AND (
            (p.principal_type = 'user' AND p.principal_id = %s)
            OR (p.principal_type = 'group' AND p.principal_id IN (
                SELECT group_id::text FROM group_members
                WHERE user_id = %s AND org_id = %s
            ))
          )
        LIMIT 1
        """,
        (entry_id, user.id, user.id, user.org_id),
    )
    if (await cur.fetchone()) is None:
        raise HTTPException(
            status_code=403,
            detail="Admin permission required on this entry to promote it",
        )
    return row


@router.post("/promote", response_model=ZonePromoteResponse)
async def promote_zone_entry(
    body: ZonePromote,
    user: UserContext = Depends(get_current_user),
):
    """Promote a zone entry by adding principals (additive) and/or bumping sensitivity.

    Caller must be admin on the entry, and the entry must currently sit in
    the caller's zone (i.e. the caller's zone group holds a permission row
    on it). Existing grants on (principal_type, principal_id, role) tuples
    are deduplicated via ``ON CONFLICT DO NOTHING`` so re-running the same
    promote payload is a no-op.
    """
    async with get_db(user) as conn:
        # Step 1: caller must have admin on the entry.
        entry = await _require_entry_admin(conn, user, body.entry_id)

        # Step 2: entry must be in the caller's zone (zone grant present).
        zone_group_id = await get_or_create_zone(conn, user.id, user.org_id)
        cur = await conn.execute(
            """
            SELECT 1 FROM permissions
            WHERE resource_type = 'entry'
              AND entry_id = %s
              AND principal_type = 'group'
              AND principal_id = %s
            LIMIT 1
            """,
            (body.entry_id, zone_group_id),
        )
        if (await cur.fetchone()) is None:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Entry is not in the caller's zone — promotion only "
                    "applies to entries currently bound to the caller's zone group"
                ),
            )

        # Step 3: validate add_principals up front so we never partially apply.
        for ap in body.add_principals:
            if ap.principal_type not in VALID_PRINCIPAL_TYPES:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Invalid principal_type '{ap.principal_type}'. "
                        f"Must be one of: {sorted(VALID_PRINCIPAL_TYPES)}"
                    ),
                )
            if ap.role not in VALID_PROMOTE_ROLES:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Invalid role '{ap.role}'. "
                        f"Must be one of: {sorted(VALID_PROMOTE_ROLES)}"
                    ),
                )

        # Step 4: validate new_sensitivity (if provided) — reject downgrades.
        new_sensitivity: str | None = None
        if body.new_sensitivity is not None:
            if body.new_sensitivity not in VALID_SENSITIVITIES:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Invalid sensitivity '{body.new_sensitivity}'. "
                        f"Must be one of: {sorted(VALID_SENSITIVITIES)}"
                    ),
                )
            current_sens = entry["sensitivity"]
            # Same value: explicit no-op (don't fire a redundant UPDATE, but
            # don't error either — caller idempotency is friendlier than a 400).
            if body.new_sensitivity == current_sens:
                new_sensitivity = None
            elif (
                current_sens != "private"
                and body.new_sensitivity == "private"
            ):
                # Downgrade safety: refuse to silently re-wall an entry that
                # was already widened. Operators must use direct PATCH paths
                # for that (intentionally awkward).
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Cannot downgrade a non-private entry back to "
                        "'private' via promote — use the entry update path"
                    ),
                )
            else:
                new_sensitivity = body.new_sensitivity

        # Step 5: insert each requested grant (additive, dedup via ON CONFLICT).
        for ap in body.add_principals:
            await conn.execute(
                """
                INSERT INTO permissions (
                    org_id, principal_type, principal_id,
                    resource_type, entry_id, role, granted_by
                ) VALUES (%s, %s, %s, 'entry', %s, %s, %s)
                ON CONFLICT (org_id, principal_type, principal_id,
                             resource_type, entry_id, path_pattern, role)
                DO NOTHING
                """,
                (
                    user.org_id,
                    ap.principal_type,
                    ap.principal_id,
                    body.entry_id,
                    ap.role,
                    user.id,
                ),
            )

        # Step 6: optional sensitivity bump.
        if new_sensitivity is not None:
            await conn.execute(
                "UPDATE entries SET sensitivity = %s, updated_by = %s "
                "WHERE id = %s",
                (new_sensitivity, user.id, body.entry_id),
            )

        # Step 7: re-read the entry + all its permission rows for the response.
        cur = await conn.execute(
            f"SELECT {_ENTRY_COLS} FROM entries WHERE id = %s",
            (body.entry_id,),
        )
        cur.row_factory = dict_row
        updated_entry = await cur.fetchone()

        cur = await conn.execute(
            f"""
            SELECT {_ENTRY_PERM_COLS}
            FROM permissions
            WHERE resource_type = 'entry' AND entry_id = %s
            ORDER BY created_at ASC
            """,
            (body.entry_id,),
        )
        cur.row_factory = dict_row
        perm_rows = await cur.fetchall()

        return {
            "entry": _row_to_entry_response(updated_entry),
            "permissions": [_perm_row_to_response(r) for r in perm_rows],
        }
