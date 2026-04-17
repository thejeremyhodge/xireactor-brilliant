"""Bulk org-wide graph endpoint for the frontend /graph page.

Returns all nodes + deduped edges in a single response so the client can render
a force-directed graph without an N+1 fan-out over /entries/{id}/links.

Permission parity with /entries and /entries/{id}/links is enforced by RLS
(get_db sets LOCAL ROLE to the user's pg role); the user only sees nodes and
edges on entries they could read individually.
"""

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg.rows import dict_row

from auth import UserContext, get_current_user
from database import get_db
from models import GraphEdge, GraphNode, GraphResponse
from services.access_log import log_entry_reads

router = APIRouter(tags=["graph"])

_CACHE_TTL_SECONDS = 45
_LIMIT_NODES_CAP = 5000
_cache: dict[tuple, tuple[float, GraphResponse]] = {}


def _cache_get(key: tuple) -> GraphResponse | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    expires_at, payload = entry
    if expires_at < time.monotonic():
        _cache.pop(key, None)
        return None
    return payload


def _cache_put(key: tuple, payload: GraphResponse) -> None:
    _cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, payload)
    # Opportunistic eviction of expired entries to keep the dict bounded.
    if len(_cache) > 256:
        now = time.monotonic()
        stale = [k for k, (exp, _) in _cache.items() if exp < now]
        for k in stale:
            _cache.pop(k, None)


@router.get("", response_model=GraphResponse)
async def get_graph(
    scope: str = Query("org", pattern="^(org|path)$"),
    path: str | None = Query(None, description="Logical path prefix (required when scope=path)"),
    include_archived: bool = Query(False),
    limit_nodes: int = Query(1000, ge=1, le=_LIMIT_NODES_CAP),
    user: UserContext = Depends(get_current_user),
) -> GraphResponse:
    """Return the full permission-filtered graph for the caller's org.

    - Nodes are ordered by updated_at DESC so truncation keeps the most recent.
    - Edges are restricted to the returned node set so the graph stays
      self-consistent when truncated.
    - Edges are server-side deduped using a canonical (min_id, max_id, link_type)
      key; the smaller id is emitted as `source`.
    - Results are cached for ~45s per (org, user, query_params) tuple.
    """
    if scope == "path" and not path:
        raise HTTPException(
            status_code=422,
            detail="scope=path requires the 'path' query parameter",
        )

    cache_key = (user.org_id, user.id, scope, path, include_archived, limit_nodes)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    async with get_db(user) as conn:
        node_sql = f"""
            SELECT id, title, content_type, logical_path, summary, updated_at
            FROM entries
            WHERE 1=1
              {"AND status != 'archived'" if not include_archived else ""}
              {"AND logical_path LIKE %(path_prefix)s" if scope == "path" else ""}
            ORDER BY updated_at DESC
            LIMIT %(limit)s
        """
        params: dict = {"limit": limit_nodes + 1}
        if scope == "path":
            params["path_prefix"] = f"{path}%"

        cur = await conn.execute(node_sql, params)
        cur.row_factory = dict_row
        node_rows = await cur.fetchall()

        truncated = len(node_rows) > limit_nodes
        if truncated:
            node_rows = node_rows[:limit_nodes]

        # total_nodes under the same filters (bounded by RLS so this is cheap
        # for the target org size; a 500-entry vault is a single index scan).
        count_sql = f"""
            SELECT COUNT(*) AS n FROM entries
            WHERE 1=1
              {"AND status != 'archived'" if not include_archived else ""}
              {"AND logical_path LIKE %(path_prefix)s" if scope == "path" else ""}
        """
        cur = await conn.execute(
            count_sql,
            {"path_prefix": f"{path}%"} if scope == "path" else {},
        )
        total_nodes = int((await cur.fetchone())[0])

        node_ids = [str(r["id"]) for r in node_rows]

        if node_ids:
            cur = await conn.execute(
                """
                SELECT source_entry_id, target_entry_id, link_type, weight
                FROM entry_links
                WHERE source_entry_id = ANY(%s)
                  AND target_entry_id = ANY(%s)
                """,
                (node_ids, node_ids),
            )
            cur.row_factory = dict_row
            edge_rows = await cur.fetchall()
        else:
            edge_rows = []

        # Observability: single batched INSERT for all returned node ids.
        # Inside the conn block so the INSERT runs under the caller's RLS
        # context; helper swallows its own errors.
        await log_entry_reads(conn, user, node_ids)

    nodes = [
        GraphNode(
            id=str(r["id"]),
            title=r["title"],
            content_type=r["content_type"],
            logical_path=r["logical_path"],
            summary=r.get("summary"),
            updated_at=r["updated_at"],
        )
        for r in node_rows
    ]

    # Server-side dedup: canonical key = (min_id, max_id, link_type).
    # Emit the smaller id as source per the task spec. Weight collisions keep
    # the max observed weight (deterministic, favors stronger signal).
    edge_map: dict[tuple[str, str, str], float] = {}
    for r in edge_rows:
        a = str(r["source_entry_id"])
        b = str(r["target_entry_id"])
        lo, hi = (a, b) if a <= b else (b, a)
        key = (lo, hi, r["link_type"])
        w = float(r["weight"])
        prev = edge_map.get(key)
        if prev is None or w > prev:
            edge_map[key] = w

    edges = [
        GraphEdge(source=lo, target=hi, link_type=lt, weight=w)
        for (lo, hi, lt), w in edge_map.items()
    ]

    response = GraphResponse(
        nodes=nodes,
        edges=edges,
        total_nodes=total_nodes,
        total_edges=len(edges),
        truncated=truncated,
        generated_at=datetime.now(timezone.utc),
    )
    _cache_put(cache_key, response)
    return response
