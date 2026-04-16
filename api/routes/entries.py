"""CRUD and full-text search endpoints for knowledge base entries."""

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg.rows import dict_row

from auth import UserContext, get_current_user
from database import get_db
from models import (
    AttachmentResponse,
    BlobResponse,
    EntryAppend,
    EntryCreate,
    EntryList,
    EntryResponse,
    EntryUpdate,
    VALID_SENSITIVITIES,
)
from services.links import sync_entry_links
from services.render import resolve_wiki_links

router = APIRouter(tags=["entries"])


async def _validate_content_type(conn, content_type: str) -> None:
    """Validate content_type against the DB registry, with alias resolution."""
    cur = await conn.execute(
        "SELECT name, alias_of FROM content_type_registry WHERE name = %s AND is_active = true",
        (content_type,),
    )
    row = await cur.fetchone()
    if row is None:
        # Not found -- get valid types for error message
        cur = await conn.execute(
            "SELECT name FROM content_type_registry WHERE alias_of IS NULL AND is_active = true ORDER BY name"
        )
        valid = [r[0] for r in await cur.fetchall()]
        raise HTTPException(
            status_code=422,
            detail=f"Invalid content_type '{content_type}'. Must be one of: {valid}",
        )
    if row[1] is not None:  # alias_of
        raise HTTPException(
            status_code=422,
            detail=f"'{content_type}' is an alias — use '{row[1]}' instead",
        )


def _row_to_response(row: dict) -> EntryResponse:
    """Convert a database row dict to an EntryResponse."""
    return EntryResponse(
        id=str(row["id"]),
        org_id=str(row["org_id"]),
        title=row["title"],
        content=row["content"],
        summary=row.get("summary"),
        content_type=row["content_type"],
        logical_path=row["logical_path"],
        sensitivity=row["sensitivity"],
        department=row.get("department"),
        owner_id=str(row["owner_id"]) if row.get("owner_id") else None,
        tags=row.get("tags") or [],
        domain_meta=row.get("domain_meta") or {},
        version=row["version"],
        status=row["status"],
        source=row["source"],
        created_by=str(row["created_by"]),
        updated_by=str(row["updated_by"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# Columns to SELECT for entry responses (excludes search_vector, embedding, word_count)
_SELECT_COLS = """
    id, org_id, title, content, summary, content_hash,
    content_type, logical_path, sensitivity, department,
    owner_id, project_id, tags, domain_meta,
    version, status, source,
    created_by, updated_by, created_at, updated_at
"""


def _require_non_agent(user: UserContext) -> None:
    """Raise 403 if the caller is using an agent key (agents must use staging)."""
    if user.key_type == "agent":
        raise HTTPException(
            status_code=403,
            detail=(
                "Agent keys cannot write to entries directly. "
                "Use the staging pipeline: POST /staging to submit, "
                "then an authorized user can approve via POST /staging/{id}/review."
            ),
        )


@router.post("", response_model=EntryResponse, status_code=201)
async def create_entry(
    body: EntryCreate,
    user: UserContext = Depends(get_current_user),
):
    """Create a new knowledge base entry."""
    _require_non_agent(user)

    # Validate sensitivity
    if body.sensitivity not in VALID_SENSITIVITIES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid sensitivity '{body.sensitivity}'. Must be one of: {sorted(VALID_SENSITIVITIES)}",
        )

    content_hash = hashlib.sha256(body.content.encode()).hexdigest()

    async with get_db(user) as conn:
        # Validate content_type against DB registry
        await _validate_content_type(conn, body.content_type)

        cur = await conn.execute(
            f"""
            INSERT INTO entries (
                org_id, title, content, summary, content_hash,
                content_type, logical_path, sensitivity, department,
                owner_id, project_id, tags, domain_meta,
                source, created_by, updated_by
            ) VALUES (
                %(org_id)s, %(title)s, %(content)s, %(summary)s, %(content_hash)s,
                %(content_type)s, %(logical_path)s, %(sensitivity)s, %(department)s,
                %(owner_id)s, %(project_id)s, %(tags)s, %(domain_meta)s,
                %(source)s, %(created_by)s, %(updated_by)s
            )
            RETURNING {_SELECT_COLS}
            """,
            {
                "org_id": user.org_id,
                "title": body.title,
                "content": body.content,
                "summary": body.summary,
                "content_hash": content_hash,
                "content_type": body.content_type,
                "logical_path": body.logical_path,
                "sensitivity": body.sensitivity,
                "department": body.department,
                "owner_id": user.id,
                "project_id": body.project_id,
                "tags": body.tags,
                "domain_meta": json.dumps(body.domain_meta),
                "source": user.source,
                "created_by": user.id,
                "updated_by": user.id,
            },
        )
        cur.row_factory = dict_row
        row = await cur.fetchone()

        # Populate entry_links from [[wiki-links]] in content so the read-time
        # resolver (services/render.py) has rows to join. Without this, MCP/UI
        # writes render brackets literally. See spec 0030.
        await sync_entry_links(
            conn,
            row["id"],
            body.content,
            user.org_id,
            user.id,
            user.source,
        )

        return _row_to_response(row)


@router.get("/{entry_id}", response_model=EntryResponse)
async def get_entry(
    entry_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Get a single entry by ID. RLS handles permission scoping."""
    async with get_db(user) as conn:
        cur = await conn.execute(
            f"SELECT {_SELECT_COLS} FROM entries WHERE id = %s",
            (entry_id,),
        )
        cur.row_factory = dict_row
        row = await cur.fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="Entry not found")

        # Resolve [[wiki-links]] in content at read time (spec 0028).
        # Short-circuits internally when content has no '[['.
        row["content"] = await resolve_wiki_links(
            row["content"], conn, str(row["id"])
        )

        return _row_to_response(row)


@router.get("", response_model=EntryList)
async def list_entries(
    q: str | None = Query(None, description="Full-text search query"),
    content_type: str | None = Query(None, description="Filter by content type"),
    logical_path: str | None = Query(None, description="Filter by path prefix"),
    department: str | None = Query(None, description="Filter by department"),
    tag: str | None = Query(None, description="Filter by tag (array contains)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(get_current_user),
):
    """List entries with optional full-text search and filters."""
    conditions = []
    params: list = []

    if q:
        conditions.append(
            "search_vector @@ websearch_to_tsquery('english', %s)"
        )
        params.append(q)

    if content_type:
        conditions.append("content_type = %s")
        params.append(content_type)

    if logical_path:
        conditions.append("logical_path LIKE %s")
        params.append(f"{logical_path}%")

    if department:
        conditions.append("department = %s")
        params.append(department)

    if tag:
        conditions.append("tags @> %s::text[]")
        params.append([tag])

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    # Determine ordering: rank by relevance if searching, otherwise by updated_at
    if q:
        order_clause = "ORDER BY ts_rank(search_vector, websearch_to_tsquery('english', %s)) DESC"
        order_params = [q]
    else:
        order_clause = "ORDER BY updated_at DESC"
        order_params = []

    async with get_db(user) as conn:
        # Get total count
        count_query = f"SELECT COUNT(*) FROM entries {where_clause}"
        cur = await conn.execute(count_query, params)
        total = (await cur.fetchone())[0]

        # Get entries
        data_query = f"""
            SELECT {_SELECT_COLS}
            FROM entries
            {where_clause}
            {order_clause}
            LIMIT %s OFFSET %s
        """
        data_params = params + order_params + [limit, offset]

        cur = await conn.execute(data_query, data_params)
        cur.row_factory = dict_row
        rows = await cur.fetchall()

        return EntryList(
            entries=[_row_to_response(r) for r in rows],
            total=total,
            limit=limit,
            offset=offset,
        )


@router.put("/{entry_id}", response_model=EntryResponse)
async def update_entry(
    entry_id: str,
    body: EntryUpdate,
    user: UserContext = Depends(get_current_user),
):
    """Update an entry. Creates a version snapshot before applying changes."""
    _require_non_agent(user)

    async with get_db(user) as conn:
        # Fetch current entry
        cur = await conn.execute(
            f"SELECT {_SELECT_COLS} FROM entries WHERE id = %s",
            (entry_id,),
        )
        cur.row_factory = dict_row
        current = await cur.fetchone()

        if current is None:
            raise HTTPException(status_code=404, detail="Entry not found")

        # Optimistic concurrency check
        if body.expected_version is not None and body.expected_version != current["version"]:
            raise HTTPException(
                status_code=409,
                detail=f"Stale version: expected {body.expected_version} but entry is at version {current['version']}. Re-read and resubmit.",
            )

        # Snapshot current state into entry_versions before updating
        # ON CONFLICT DO NOTHING: if a prior attempt already wrote this snapshot
        # (e.g. client retry after network error), skip safely — same data.
        await conn.execute(
            """
            INSERT INTO entry_versions (
                entry_id, org_id, version, title, content, content_hash,
                domain_meta, tags, status,
                changed_by, source, change_summary, governance_action
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s
            )
            ON CONFLICT (entry_id, version) DO NOTHING
            """,
            (
                current["id"],
                current["org_id"],
                current["version"],
                current["title"],
                current["content"],
                current["content_hash"],
                json.dumps(current["domain_meta"] or {}),
                current["tags"] or [],
                current["status"],
                user.id,
                user.source,
                None,  # change_summary
                "updated",  # governance_action
            ),
        )

        # Build UPDATE SET clause from non-None fields
        updates = {}
        if body.title is not None:
            updates["title"] = body.title
        if body.content is not None:
            updates["content"] = body.content
            updates["content_hash"] = hashlib.sha256(body.content.encode()).hexdigest()
        if body.summary is not None:
            updates["summary"] = body.summary
        if body.content_type is not None:
            await _validate_content_type(conn, body.content_type)
            updates["content_type"] = body.content_type
        if body.logical_path is not None:
            updates["logical_path"] = body.logical_path
        if body.sensitivity is not None:
            if body.sensitivity not in VALID_SENSITIVITIES:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid sensitivity '{body.sensitivity}'.",
                )
            updates["sensitivity"] = body.sensitivity
        if body.department is not None:
            updates["department"] = body.department
        if body.tags is not None:
            updates["tags"] = body.tags
        if body.domain_meta is not None:
            updates["domain_meta"] = json.dumps(body.domain_meta)

        # Always bump version and audit fields
        new_version = current["version"] + 1
        updates["version"] = new_version
        updates["updated_by"] = user.id
        updates["source"] = user.source

        # Build parameterized SET clause
        set_parts = []
        set_params = []
        for col, val in updates.items():
            set_parts.append(f"{col} = %s")
            set_params.append(val)

        set_clause = ", ".join(set_parts)
        set_params.append(entry_id)

        cur = await conn.execute(
            f"""
            UPDATE entries
            SET {set_clause}
            WHERE id = %s
            RETURNING {_SELECT_COLS}
            """,
            set_params,
        )
        cur.row_factory = dict_row
        row = await cur.fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="Entry not found")

        # Re-sync entry_links whenever content changed (delete-then-insert
        # inside this txn so removing a `[[...]]` drops its row). See spec 0030.
        if body.content is not None:
            await sync_entry_links(
                conn,
                row["id"],
                body.content,
                user.org_id,
                user.id,
                user.source,
            )

        return _row_to_response(row)


@router.patch("/{entry_id}/append", response_model=EntryResponse)
async def append_entry(
    entry_id: str,
    body: EntryAppend,
    user: UserContext = Depends(get_current_user),
):
    """Append content to an existing entry. Creates a version snapshot before applying."""
    _require_non_agent(user)

    async with get_db(user) as conn:
        # Fetch current entry
        cur = await conn.execute(
            f"SELECT {_SELECT_COLS} FROM entries WHERE id = %s",
            (entry_id,),
        )
        cur.row_factory = dict_row
        current = await cur.fetchone()

        if current is None:
            raise HTTPException(status_code=404, detail="Entry not found")

        # Optimistic concurrency check
        if body.expected_version is not None and body.expected_version != current["version"]:
            raise HTTPException(
                status_code=409,
                detail=f"Stale version: expected {body.expected_version} but entry is at version {current['version']}. Re-read and resubmit.",
            )

        # Snapshot current state into entry_versions before updating
        # ON CONFLICT DO NOTHING: safe on retry — same snapshot data
        await conn.execute(
            """
            INSERT INTO entry_versions (
                entry_id, org_id, version, title, content, content_hash,
                domain_meta, tags, status,
                changed_by, source, change_summary, governance_action
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s
            )
            ON CONFLICT (entry_id, version) DO NOTHING
            """,
            (
                current["id"],
                current["org_id"],
                current["version"],
                current["title"],
                current["content"],
                current["content_hash"],
                json.dumps(current["domain_meta"] or {}),
                current["tags"] or [],
                current["status"],
                user.id,
                user.source,
                None,  # change_summary
                "appended",  # governance_action
            ),
        )

        # Concatenate new content
        new_content = current["content"] + body.separator + body.content
        content_hash = hashlib.sha256(new_content.encode()).hexdigest()
        new_version = current["version"] + 1

        cur = await conn.execute(
            f"""
            UPDATE entries
            SET content = %s,
                content_hash = %s,
                version = %s,
                updated_by = %s,
                source = %s
            WHERE id = %s
            RETURNING {_SELECT_COLS}
            """,
            (
                new_content,
                content_hash,
                new_version,
                user.id,
                user.source,
                entry_id,
            ),
        )
        cur.row_factory = dict_row
        row = await cur.fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="Entry not found")

        # Appended content may contain new `[[...]]` — re-sync so the read
        # resolver sees them. Scope extension consistent with spec 0030 intent
        # ("every write path").
        await sync_entry_links(
            conn,
            row["id"],
            new_content,
            user.org_id,
            user.id,
            user.source,
        )

        return _row_to_response(row)


@router.delete("/{entry_id}")
async def delete_entry(
    entry_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Soft-delete an entry by setting status to 'archived'."""
    _require_non_agent(user)

    async with get_db(user) as conn:
        cur = await conn.execute(
            "UPDATE entries SET status = 'archived', updated_by = %s WHERE id = %s RETURNING id",
            (user.id, entry_id),
        )
        row = await cur.fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="Entry not found")

        return {"message": "Entry archived"}


@router.get("/{entry_id}/attachments", response_model=list[AttachmentResponse])
async def list_entry_attachments(
    entry_id: str,
    user: UserContext = Depends(get_current_user),
):
    """List attachments on an entry, filtered by RLS.

    Entry visibility is enforced by the entries RLS policies; the
    `entry_attachments_select_via_entry` policy further hides attachment
    rows whose owning entry the caller can't see. A caller with no access
    to the entry will get an empty list — the same shape as an entry with
    no attachments, which keeps cross-org probing quiet.
    """
    async with get_db(user) as conn:
        cur = await conn.execute(
            """
            SELECT
                ea.id            AS attachment_id,
                ea.entry_id      AS entry_id,
                ea.blob_id       AS blob_id,
                ea.role          AS role,
                ea.created_at    AS attachment_created_at,
                b.sha256         AS sha256,
                b.content_type   AS content_type,
                b.size_bytes     AS size_bytes,
                b.uploaded_at    AS uploaded_at
            FROM entry_attachments ea
            JOIN blobs b ON b.id = ea.blob_id
            WHERE ea.entry_id = %s
            ORDER BY ea.created_at ASC
            """,
            (entry_id,),
        )
        cur.row_factory = dict_row
        rows = await cur.fetchall()

    return [
        AttachmentResponse(
            id=str(r["attachment_id"]),
            entry_id=str(r["entry_id"]),
            blob_id=str(r["blob_id"]),
            role=r["role"],
            created_at=r["attachment_created_at"],
            blob=BlobResponse(
                id=str(r["blob_id"]),
                sha256=r["sha256"],
                content_type=r["content_type"],
                size_bytes=int(r["size_bytes"]),
                uploaded_at=r["uploaded_at"],
            ),
        )
        for r in rows
    ]
