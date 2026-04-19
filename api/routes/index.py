"""Tiered index map endpoint (L1-L5) for agent session context."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row

from auth import UserContext, get_current_user
from database import get_db
from models import (
    IndexCategory,
    IndexEntry,
    IndexRelationship,
    IndexResponse,
)

router = APIRouter(tags=["index"])

# Depth >= 2 returns full entry rows — at ~100 tokens/entry (id, title, path,
# timestamp) a 500-entry KB already blows past 50K tokens, which breaks
# Claude sessions. If the caller's visible published-entry count exceeds
# this threshold AND no narrowing filter is supplied, we fail fast with a
# 422 that points them at search_entries. L1 (counts-only) is always safe.
INDEX_SCALE_GUARD_THRESHOLD = 200


@router.get("", response_model=IndexResponse)
async def get_index(
    depth: int = Query(1, ge=1, le=5, description="Index depth level (1-5)"),
    path: str | None = Query(None, description="Filter by logical_path prefix"),
    content_type: str | None = Query(None, description="Filter by content_type"),
    tag: str | None = Query(
        None,
        description=(
            "Filter by a single tag (array contains). Accepted for parity "
            "with search_entries and to satisfy the depth>=2 narrowing "
            "guard. For multi-tag AND filtering use `search_entries` — "
            "`get_index` only supports a single tag."
        ),
    ),
    user: UserContext = Depends(get_current_user),
):
    """Return a permission-filtered, tiered index map of the knowledge base.

    Depth levels:
    - L1: Category counts (content_type groupings) — always safe, no scale guard
    - L2: Document index (titles, paths, timestamps)
    - L3: Relationships between visible entries
    - L4: Summaries for each entry
    - L5: Full content for each entry

    Optional filters:
    - path: Filter entries whose logical_path starts with this prefix
    - content_type: Filter entries by exact content_type match
    - tag: Filter entries whose tags array contains this tag (single tag only)

    Scale guard: at depth >= 2, if the caller's visible published-entry
    total exceeds 200 AND no narrowing filter (``path``, ``content_type``,
    or ``tag``) is supplied, the endpoint returns 422 with body
    ``{"error": "index_too_large", "total": N, "hint": "..."}``. L1 (counts
    only) is unconstrained. Callers wanting multi-tag AND should use
    ``search_entries(tags=[...])``.
    """
    # Build reusable filter clause and params
    filter_clause = "status = 'published'"
    filter_params: list = []
    if path is not None:
        filter_clause += " AND logical_path LIKE %s"
        filter_params.append(f"{path}%")
    if content_type is not None:
        filter_clause += " AND content_type = %s"
        filter_params.append(content_type)
    if tag is not None:
        filter_clause += " AND tags @> %s::text[]"
        filter_params.append([tag])

    async with get_db(user) as conn:
        # L1 (always): Category counts
        cur = await conn.execute(
            f"""
            SELECT content_type, count(*) AS count
            FROM entries
            WHERE {filter_clause}
            GROUP BY content_type
            ORDER BY count DESC
            """,
            filter_params,
        )
        cur.row_factory = dict_row
        cat_rows = await cur.fetchall()

        categories = [
            IndexCategory(content_type=r["content_type"], count=r["count"])
            for r in cat_rows
        ]
        total_entries = sum(c.count for c in categories)

        response = IndexResponse(
            depth=depth,
            total_entries=total_entries,
            categories=categories,
        )

        # Scale guard — depth >= 2 on a large KB without a narrowing filter
        # would return unbounded rows and blow past the agent's token budget.
        # Fail fast with a hint pointing the caller at the right next tool.
        # L1 (counts only) is always safe and bypasses this check.
        if depth >= 2 and path is None and content_type is None and tag is None:
            if total_entries > INDEX_SCALE_GUARD_THRESHOLD:
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": "index_too_large",
                        "total": total_entries,
                        "hint": (
                            "narrow with path=, content_type=, tag=, "
                            "or use search_entries"
                        ),
                    },
                )

        # L2+: Document index
        if depth >= 2:
            if depth >= 5:
                # L5: include full content in the same query
                select_cols = "id, title, content_type, logical_path, updated_at, summary, content"
            elif depth >= 4:
                # L4: include summary
                select_cols = "id, title, content_type, logical_path, updated_at, summary"
            else:
                select_cols = "id, title, content_type, logical_path, updated_at"

            cur = await conn.execute(
                f"""
                SELECT {select_cols}
                FROM entries
                WHERE {filter_clause}
                ORDER BY logical_path
                """,
                filter_params,
            )
            cur.row_factory = dict_row
            entry_rows = await cur.fetchall()

            response.entries = [
                IndexEntry(
                    id=str(r["id"]),
                    title=r["title"],
                    content_type=r["content_type"],
                    logical_path=r["logical_path"],
                    updated_at=r["updated_at"],
                )
                for r in entry_rows
            ]

            # L4+: Summaries
            if depth >= 4:
                response.summaries = {
                    str(r["id"]): r.get("summary") or ""
                    for r in entry_rows
                }

            # L5+: Full content
            if depth >= 5:
                response.contents = {
                    str(r["id"]): r.get("content") or ""
                    for r in entry_rows
                }

        # L3+: Relationships between visible entries
        if depth >= 3:
            # Only include links where both endpoints are visible (RLS on entries table)
            cur = await conn.execute(
                f"""
                SELECT el.source_entry_id, el.target_entry_id, el.link_type
                FROM entry_links el
                JOIN entries e1 ON e1.id = el.source_entry_id AND e1.{filter_clause}
                JOIN entries e2 ON e2.id = el.target_entry_id AND e2.{filter_clause}
                """,
                filter_params + filter_params,
            )
            cur.row_factory = dict_row
            link_rows = await cur.fetchall()

            response.relationships = [
                IndexRelationship(
                    source_id=str(r["source_entry_id"]),
                    target_id=str(r["target_entry_id"]),
                    link_type=r["link_type"],
                )
                for r in link_rows
            ]

        return response
