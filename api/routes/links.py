"""Link management and CTE-based graph traversal endpoints."""

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg.rows import dict_row

from auth import UserContext, get_current_user
from database import get_db
from models import (
    LinkCreate,
    LinkNeighbor,
    LinkResponse,
    TraversalResponse,
    VALID_LINK_TYPES,
)

router = APIRouter(tags=["links"])

# Bidirectional link types: traversal follows both directions
BIDIRECTIONAL_TYPES = {"relates_to", "contradicts"}


@router.post("/{entry_id}/links", response_model=LinkResponse, status_code=201)
async def create_link(
    entry_id: str,
    body: LinkCreate,
    user: UserContext = Depends(get_current_user),
):
    """Create a typed link from entry_id to target_entry_id."""
    if user.source == "agent":
        raise HTTPException(
            status_code=403,
            detail="Agent keys cannot create links directly. Use submit_staging with change_type='create_link' and proposed_meta containing source_entry_id, target_entry_id, link_type, weight.",
        )

    if body.link_type not in VALID_LINK_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid link_type '{body.link_type}'. Must be one of: {sorted(VALID_LINK_TYPES)}",
        )

    async with get_db(user) as conn:
        # Verify source entry exists (RLS filters automatically)
        cur = await conn.execute(
            "SELECT id FROM entries WHERE id = %s",
            (entry_id,),
        )
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Source entry not found")

        # Verify target entry exists
        cur = await conn.execute(
            "SELECT id FROM entries WHERE id = %s",
            (body.target_entry_id,),
        )
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Target entry not found")

        # Insert the link
        cur = await conn.execute(
            """
            INSERT INTO entry_links (
                org_id, source_entry_id, target_entry_id,
                link_type, weight, metadata,
                created_by, source
            ) VALUES (
                %(org_id)s, %(source_entry_id)s, %(target_entry_id)s,
                %(link_type)s, %(weight)s, %(metadata)s,
                %(created_by)s, %(source)s
            )
            RETURNING id, source_entry_id, target_entry_id,
                      link_type, weight, metadata,
                      created_by, source, created_at
            """,
            {
                "org_id": user.org_id,
                "source_entry_id": entry_id,
                "target_entry_id": body.target_entry_id,
                "link_type": body.link_type,
                "weight": body.weight,
                "metadata": json.dumps(body.metadata),
                "created_by": user.id,
                "source": user.source,
            },
        )
        cur.row_factory = dict_row
        row = await cur.fetchone()

        return LinkResponse(
            id=str(row["id"]),
            source_entry_id=str(row["source_entry_id"]),
            target_entry_id=str(row["target_entry_id"]),
            link_type=row["link_type"],
            weight=float(row["weight"]),
            metadata=row["metadata"] or {},
            created_by=str(row["created_by"]),
            source=row["source"],
            created_at=row["created_at"],
        )


@router.get("/{entry_id}/links", response_model=TraversalResponse)
async def get_links(
    entry_id: str,
    depth: int = Query(1, ge=1, le=3, description="Traversal depth (1-3)"),
    user: UserContext = Depends(get_current_user),
):
    """Get neighbors of an entry via link traversal.

    - depth=1: direct neighbors (outgoing + incoming for bidirectional types)
    - depth=2-3: recursive CTE traversal with cycle prevention
    """
    async with get_db(user) as conn:
        # Verify the entry exists
        cur = await conn.execute(
            "SELECT id FROM entries WHERE id = %s",
            (entry_id,),
        )
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Entry not found")

        if depth == 1:
            # Simple JOIN for 1-hop neighbors
            cur = await conn.execute(
                """
                SELECT
                    el.target_entry_id AS entry_id,
                    el.link_type,
                    el.weight,
                    1 AS depth,
                    e.title,
                    e.summary,
                    e.content_type
                FROM entry_links el
                JOIN entries e ON e.id = el.target_entry_id
                WHERE el.source_entry_id = %s

                UNION

                SELECT
                    el.source_entry_id AS entry_id,
                    el.link_type,
                    el.weight,
                    1 AS depth,
                    e.title,
                    e.summary,
                    e.content_type
                FROM entry_links el
                JOIN entries e ON e.id = el.source_entry_id
                WHERE el.target_entry_id = %s
                  AND el.link_type IN ('relates_to', 'contradicts')
                """,
                (entry_id, entry_id),
            )
        else:
            # Recursive CTE for multi-hop traversal with cycle prevention
            cur = await conn.execute(
                """
                WITH RECURSIVE graph AS (
                    -- Anchor: direct outgoing links
                    SELECT el.target_entry_id AS entry_id, el.link_type, el.weight, 1 AS depth,
                           ARRAY[el.source_entry_id, el.target_entry_id] AS path
                    FROM entry_links el
                    WHERE el.source_entry_id = %s
                    UNION ALL
                    -- Anchor: direct incoming links (bidirectional types)
                    SELECT el.source_entry_id AS entry_id, el.link_type, el.weight, 1 AS depth,
                           ARRAY[el.target_entry_id, el.source_entry_id] AS path
                    FROM entry_links el
                    WHERE el.target_entry_id = %s
                      AND el.link_type IN ('relates_to', 'contradicts')
                    UNION ALL
                    -- Recursive step
                    SELECT el.target_entry_id, el.link_type, el.weight, g.depth + 1,
                           g.path || el.target_entry_id
                    FROM graph g
                    JOIN entry_links el ON el.source_entry_id = g.entry_id
                    WHERE g.depth < %s AND el.target_entry_id != ALL(g.path)
                )
                SELECT DISTINCT ON (g.entry_id)
                    g.entry_id, g.link_type, g.weight, g.depth,
                    e.title, e.summary, e.content_type
                FROM graph g
                JOIN entries e ON e.id = g.entry_id
                ORDER BY g.entry_id, g.depth
                """,
                (entry_id, entry_id, depth),
            )

        cur.row_factory = dict_row
        rows = await cur.fetchall()

        neighbors = [
            LinkNeighbor(
                entry_id=str(row["entry_id"]),
                title=row["title"],
                summary=row.get("summary"),
                content_type=row["content_type"],
                link_type=row["link_type"],
                weight=float(row["weight"]),
                depth=row["depth"],
            )
            for row in rows
        ]

        return TraversalResponse(
            origin_id=entry_id,
            depth=depth,
            neighbors=neighbors,
        )
