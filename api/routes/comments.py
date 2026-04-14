"""Comments API — first-class comments subsystem (spec 0026, task T-0132).

Endpoints:
- POST /entries/{entry_id}/comments           → create a comment on an entry
- GET  /entries/{entry_id}/comments           → list comments on an entry
- PATCH /comments/{comment_id}                → update status (resolve/dismiss/escalate)
- POST /comments/{comment_id}/replies         → thread reply (shorthand for create w/ parent)

Authorship:
- author_kind is derived from the session (UserContext), NEVER from the client body.
- Agent-key sessions (key_type == 'agent') are recorded as author_kind='agent'.

Permissions:
- Visibility: RLS filters comments to those on entries the caller can read. If the
  entry itself is unreadable we return 404 (matches entries.py behaviour).
- Create: viewer role is blocked at the GRANT layer (INSERT permission missing).
  We surface RLS/GRANT denials as HTTP 403 rather than 500.
- PATCH: application-level check enforces author-OR-entry-owner-OR-admin, with RLS
  as defence-in-depth.
- Status transitions allowed: open → resolved|dismissed|escalated,
                              escalated → resolved|dismissed. No reopen in P1.

Audit-log wiring is T-0138's responsibility; this module structures writes so
that adding audit rows is a single insert in the same transaction.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg.rows import dict_row
from psycopg import errors as pg_errors

from auth import UserContext, get_current_user
from database import get_db
from services.audit import record_for_user as _audit
from models import (
    CommentCreate,
    CommentResponse,
    CommentUpdate,
    VALID_COMMENT_STATUSES,
    VALID_COMMENT_UPDATE_STATUSES,
)


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
# entries_comments_router mounts under /entries (create + list on an entry)
# comments_router        mounts under /comments (update + reply on a comment)

entries_comments_router = APIRouter(tags=["comments"])
comments_router = APIRouter(tags=["comments"])


_COMMENT_COLS = """
    id, org_id, entry_id, author_id, author_kind, body, status,
    escalated_to, parent_comment_id, created_at, resolved_at, resolved_by
"""


def _row_to_response(row: dict) -> CommentResponse:
    """Convert a DB row dict to a CommentResponse."""
    return CommentResponse(
        id=str(row["id"]),
        org_id=str(row["org_id"]),
        entry_id=str(row["entry_id"]),
        author_id=str(row["author_id"]),
        author_kind=row["author_kind"],
        body=row["body"],
        status=row["status"],
        escalated_to=str(row["escalated_to"]) if row.get("escalated_to") else None,
        parent_comment_id=(
            str(row["parent_comment_id"]) if row.get("parent_comment_id") else None
        ),
        created_at=row["created_at"],
        resolved_at=row.get("resolved_at"),
        resolved_by=str(row["resolved_by"]) if row.get("resolved_by") else None,
    )


def _author_kind(user: UserContext) -> str:
    """Agent keys produce agent-authored comments; everything else is user."""
    return "agent" if user.key_type == "agent" else "user"


async def _require_entry_visible(conn, entry_id: str) -> None:
    """Raise 404 if the entry is not readable by the current RLS context.

    Mirrors entries.py: we don't leak existence to users who can't see the entry.
    """
    cur = await conn.execute(
        "SELECT 1 FROM entries WHERE id = %s",
        (entry_id,),
    )
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Entry not found")


async def _insert_comment(
    conn,
    *,
    user: UserContext,
    entry_id: str,
    body: str,
    parent_comment_id: str | None,
) -> dict:
    """Insert a comment row under the current RLS session. Returns the row dict.

    Raises 403 on RLS/GRANT denial (viewer role, etc.).
    """
    # If replying, validate the parent belongs to the same entry (single SELECT).
    if parent_comment_id is not None:
        cur = await conn.execute(
            "SELECT entry_id FROM comments WHERE id = %s",
            (parent_comment_id,),
        )
        parent = await cur.fetchone()
        if parent is None:
            raise HTTPException(status_code=404, detail="Parent comment not found")
        if str(parent[0]) != str(entry_id):
            raise HTTPException(
                status_code=422,
                detail="parent_comment_id belongs to a different entry",
            )

    try:
        cur = await conn.execute(
            f"""
            INSERT INTO comments (
                org_id, entry_id, author_id, author_kind, body, parent_comment_id
            ) VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING {_COMMENT_COLS}
            """,
            (
                user.org_id,
                entry_id,
                user.id,
                _author_kind(user),
                body,
                parent_comment_id,
            ),
        )
    except pg_errors.InsufficientPrivilege as exc:
        raise HTTPException(
            status_code=403,
            detail="Your role does not permit commenting on this entry",
        ) from exc
    except pg_errors.CheckViolation as exc:
        # RLS WITH CHECK failure
        raise HTTPException(
            status_code=403,
            detail="Commenting on this entry is not permitted",
        ) from exc

    cur.row_factory = dict_row
    row = await cur.fetchone()
    await _audit(
        conn,
        user,
        action="comment_create",
        target_table="comments",
        target_id=str(row["id"]),
        metadata={
            "entry_id": str(row["entry_id"]),
            "parent_comment_id": (
                str(row["parent_comment_id"]) if row.get("parent_comment_id") else None
            ),
            "author_kind": row["author_kind"],
        },
    )
    return row


# ---------------------------------------------------------------------------
# POST /entries/{entry_id}/comments  — create
# ---------------------------------------------------------------------------
@entries_comments_router.post(
    "/{entry_id}/comments",
    response_model=CommentResponse,
    status_code=201,
)
async def create_comment(
    entry_id: str,
    body: CommentCreate,
    user: UserContext = Depends(get_current_user),
):
    """Create a comment on an entry. Agent-role sessions author as 'agent'."""
    if not body.body or not body.body.strip():
        raise HTTPException(status_code=422, detail="Comment body cannot be empty")

    async with get_db(user) as conn:
        await _require_entry_visible(conn, entry_id)
        row = await _insert_comment(
            conn,
            user=user,
            entry_id=entry_id,
            body=body.body,
            parent_comment_id=body.parent_comment_id,
        )
        return _row_to_response(row)


# ---------------------------------------------------------------------------
# GET /entries/{entry_id}/comments  — list
# ---------------------------------------------------------------------------
@entries_comments_router.get(
    "/{entry_id}/comments",
    response_model=list[CommentResponse],
)
async def list_comments(
    entry_id: str,
    status: str | None = Query(
        None,
        description=(
            "Filter by status (open | resolved | escalated | dismissed). "
            "Defaults to all statuses."
        ),
    ),
    user: UserContext = Depends(get_current_user),
):
    """List comments on an entry, ordered by created_at ASC."""
    if status is not None and status not in VALID_COMMENT_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid status '{status}'. "
                f"Must be one of: {sorted(VALID_COMMENT_STATUSES)}"
            ),
        )

    async with get_db(user) as conn:
        await _require_entry_visible(conn, entry_id)

        params: list = [entry_id]
        where = "WHERE entry_id = %s"
        if status is not None:
            where += " AND status = %s"
            params.append(status)

        cur = await conn.execute(
            f"""
            SELECT {_COMMENT_COLS}
            FROM comments
            {where}
            ORDER BY created_at ASC
            """,
            params,
        )
        cur.row_factory = dict_row
        rows = await cur.fetchall()
        return [_row_to_response(r) for r in rows]


# ---------------------------------------------------------------------------
# PATCH /comments/{comment_id}  — resolve | dismiss | escalate
# ---------------------------------------------------------------------------
@comments_router.patch(
    "/{comment_id}",
    response_model=CommentResponse,
)
async def update_comment_status(
    comment_id: str,
    body: CommentUpdate,
    user: UserContext = Depends(get_current_user),
):
    """Update a comment's status. Author, entry-owner, or admin only.

    Allowed transitions: open → resolved|dismissed|escalated,
                         escalated → resolved|dismissed.
    """
    if body.status not in VALID_COMMENT_UPDATE_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid target status '{body.status}'. "
                f"Must be one of: {sorted(VALID_COMMENT_UPDATE_STATUSES)}"
            ),
        )
    if body.status == "escalated" and not body.escalated_to:
        raise HTTPException(
            status_code=422,
            detail="escalated_to is required when status is 'escalated'",
        )

    async with get_db(user) as conn:
        # Load the comment + the owning entry's owner_id in one round-trip.
        cur = await conn.execute(
            f"""
            SELECT {_COMMENT_COLS},
                   (SELECT owner_id FROM entries WHERE id = comments.entry_id) AS entry_owner_id
            FROM comments
            WHERE id = %s
            """,
            (comment_id,),
        )
        cur.row_factory = dict_row
        current = await cur.fetchone()

        if current is None:
            # Either doesn't exist or RLS hid it. 404 either way.
            raise HTTPException(status_code=404, detail="Comment not found")

        # Application-level author/owner/admin check (defence-in-depth over RLS).
        entry_owner_id = (
            str(current["entry_owner_id"]) if current.get("entry_owner_id") else None
        )
        is_author = str(current["author_id"]) == user.id
        is_owner = entry_owner_id == user.id
        is_admin = user.role == "admin"
        if not (is_author or is_owner or is_admin):
            raise HTTPException(
                status_code=403,
                detail="Only the comment author, the entry owner, or an admin "
                "can change a comment's status",
            )

        # Status transition rules (no reopen in P1).
        cur_status = current["status"]
        if cur_status == "open":
            pass  # any of resolved/dismissed/escalated is fine
        elif cur_status == "escalated":
            if body.status not in ("resolved", "dismissed"):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Cannot transition escalated comment to '{body.status}'. "
                        "Allowed: resolved, dismissed."
                    ),
                )
        else:
            # resolved / dismissed are terminal in P1.
            raise HTTPException(
                status_code=409,
                detail=f"Comment is already '{cur_status}' and cannot be re-opened in P1",
            )

        # Validate escalated_to is a user in the same org (single SELECT).
        escalated_to_val: str | None = None
        if body.status == "escalated":
            cur = await conn.execute(
                "SELECT 1 FROM users WHERE id = %s AND org_id = %s",
                (body.escalated_to, user.org_id),
            )
            if await cur.fetchone() is None:
                raise HTTPException(
                    status_code=422,
                    detail="escalated_to must be a user in the same organization",
                )
            escalated_to_val = body.escalated_to

        # Build the UPDATE. resolved/dismissed stamp resolved_at + resolved_by;
        # escalated records escalated_to and does NOT stamp resolved_* fields.
        if body.status in ("resolved", "dismissed"):
            sql = f"""
                UPDATE comments
                SET status = %s,
                    resolved_at = now(),
                    resolved_by = %s,
                    escalated_to = NULL
                WHERE id = %s
                RETURNING {_COMMENT_COLS}
            """
            params = (body.status, user.id, comment_id)
        else:  # escalated
            sql = f"""
                UPDATE comments
                SET status = 'escalated',
                    escalated_to = %s
                WHERE id = %s
                RETURNING {_COMMENT_COLS}
            """
            params = (escalated_to_val, comment_id)

        try:
            cur = await conn.execute(sql, params)
        except pg_errors.InsufficientPrivilege as exc:
            raise HTTPException(
                status_code=403,
                detail="Your role does not permit updating this comment",
            ) from exc
        cur.row_factory = dict_row
        row = await cur.fetchone()
        if row is None:
            # RLS filtered the UPDATE to zero rows.
            raise HTTPException(
                status_code=403,
                detail="You are not permitted to change this comment's status",
            )
        action_map = {
            "resolved": "comment_resolve",
            "dismissed": "comment_dismiss",
            "escalated": "comment_escalate",
        }
        await _audit(
            conn,
            user,
            action=action_map[body.status],
            target_table="comments",
            target_id=str(row["id"]),
            metadata={
                "entry_id": str(row["entry_id"]),
                "previous_status": cur_status,
                "new_status": body.status,
                "escalated_to": escalated_to_val,
            },
        )
        return _row_to_response(row)


# ---------------------------------------------------------------------------
# POST /comments/{comment_id}/replies  — thread reply
# ---------------------------------------------------------------------------
@comments_router.post(
    "/{comment_id}/replies",
    response_model=CommentResponse,
    status_code=201,
)
async def reply_to_comment(
    comment_id: str,
    body: CommentCreate,
    user: UserContext = Depends(get_current_user),
):
    """Reply to an existing comment. Equivalent to POST /entries/{id}/comments
    with parent_comment_id set, but looks up the entry from the parent so the
    caller doesn't have to re-specify it.
    """
    if not body.body or not body.body.strip():
        raise HTTPException(status_code=422, detail="Comment body cannot be empty")

    async with get_db(user) as conn:
        cur = await conn.execute(
            "SELECT entry_id FROM comments WHERE id = %s",
            (comment_id,),
        )
        parent = await cur.fetchone()
        if parent is None:
            raise HTTPException(status_code=404, detail="Parent comment not found")
        entry_id = str(parent[0])

        # Ensure the entry is still visible to the caller.
        await _require_entry_visible(conn, entry_id)

        row = await _insert_comment(
            conn,
            user=user,
            entry_id=entry_id,
            body=body.body,
            parent_comment_id=comment_id,
        )
        return _row_to_response(row)
