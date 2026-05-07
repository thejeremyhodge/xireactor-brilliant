"""GET /lod — multi-axis level-of-detail endpoint (Sprint 0049).

Three axes are envisioned (structural, heat, epistemic). This sprint ships
**structural** + **heat** at LOD0 (corpus), LOD1 (community), LOD2
(community silhouette card), LOD4 (node silhouette), and LOD6
(node section outline). The epistemic axis is deferred (see spec).

Grammar:

    GET /lod?axis=<axis>&scope=<scope>&level=<int>

  axis  ∈ {"structural", "heat"}
  scope ∈ {"corpus", "community:tag:<tag>", "community:path:<prefix>",
           "node:<id>"}
  level ∈ {0, 1, 2, 4, 6}   # 4/6 are node-scoped only

Response shape (corpus, level=0):
  axis=structural → {axis, scope, level, structural: {edges, relation_types,
                     degree_bins, orphans, size_distribution}}
  axis=heat       → {axis, scope, level, heat: {bands: {cold, warm, hot, spiking}}}

Response shape (community:tag / community:path, level=1):
  {axis, scope, level, community_source: "tag" | "path",
   community: {node_count, edge_count, top_tags, dominant_content_types}}

Response shape (community:tag / community:path, level=2 — silhouette card):
  {axis, scope, level, community_source: "tag" | "path",
   silhouette: {node_count, edge_count, top_tags (≤5),
                top_content_types (≤3), community_source}}

Invalid axis or malformed scope → 400 with grammar reminder.

Implementation notes:
- LOD0 corpus reuses `api.services.lod` byte-for-byte so the v2 manifest's
  `structural`/`heat` blocks and `/lod?level=0&scope=corpus` are guaranteed
  identical (same service call, same SQL).
- LOD1 community queries scope by tag membership (`'<tag>' = ANY(tags)`) or
  by `logical_path` first segment (`split_part(logical_path,'/',1) = '<prefix>'`).
- Edge count for a community = `entry_links` rows where BOTH endpoints fall
  inside the community.
- RLS scoping: callers go through `get_db(user)` so visibility matches every
  other read endpoint. Heat data (`entry_access_log`) is admin-only — see
  `api/services/lod.py` for why non-admins see all-cold (intentional).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from psycopg.rows import dict_row

from auth import UserContext, get_current_user
from database import get_db
from services import lod as lod_service
from services import section_outline as section_outline_service

router = APIRouter(tags=["lod"])

VALID_AXES = ("structural", "heat")
SUPPORTED_LEVELS = (0, 1, 2, 4, 6)
DEFERRED_LEVELS: tuple[int, ...] = ()

GRAMMAR_HINT = (
    "Grammar: axis ∈ {structural, heat}; "
    "scope ∈ {corpus, community:tag:<tag>, community:path:<prefix>, node:<id>}; "
    "level ∈ {0, 1, 2, 4, 6}."
)

# LOD4 / LOD6 are node-scoped only.
NODE_REQUIRED_LEVELS = (4, 6)

# Caps on the LOD1 community summary so the response stays bounded
# regardless of community size.
TOP_TAGS_LIMIT = 10
DOMINANT_TYPES_LIMIT = 5

# LOD2 silhouette card — fixed top-N caps so the card shape is bounded
# regardless of community size (spec §0049 step 5).
SILHOUETTE_TOP_TAGS = 5
SILHOUETTE_TOP_CONTENT_TYPES = 3


def _parse_scope(scope: str) -> tuple[str, str | None, str | None]:
    """Return (kind, community_source, value).

    kind ∈ {"corpus", "community", "node"}; community_source ∈ {"tag", "path"}
    when kind == "community", else None; value is the literal tag / path
    prefix / node id (or None for corpus).

    Raises HTTPException(400) on malformed scopes.
    """
    if scope == "corpus":
        return ("corpus", None, None)

    if scope.startswith("community:tag:"):
        tag = scope[len("community:tag:"):]
        if not tag:
            raise HTTPException(
                status_code=400,
                detail=f"scope 'community:tag:<tag>' requires a non-empty tag. {GRAMMAR_HINT}",
            )
        return ("community", "tag", tag)

    if scope.startswith("community:path:"):
        prefix = scope[len("community:path:"):]
        if not prefix:
            raise HTTPException(
                status_code=400,
                detail=f"scope 'community:path:<prefix>' requires a non-empty prefix. {GRAMMAR_HINT}",
            )
        return ("community", "path", prefix)

    if scope.startswith("node:"):
        node_id = scope[len("node:"):]
        if not node_id:
            raise HTTPException(
                status_code=400,
                detail=f"scope 'node:<id>' requires a non-empty id. {GRAMMAR_HINT}",
            )
        return ("node", None, node_id)

    raise HTTPException(
        status_code=400,
        detail=f"Invalid scope '{scope}'. {GRAMMAR_HINT}",
    )


@router.get("")
async def get_lod(
    request: Request,
    axis: str = Query(..., description="structural | heat"),
    scope: str = Query("corpus", description="corpus | community:tag:<tag> | community:path:<prefix>"),
    level: int = Query(0, description="0 | 1 (2/4/6 not yet supported)"),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """Return a level-of-detail aggregate scoped by axis × scope × level."""

    # Tag this request for the request_log middleware so the (axis, level)
    # pair lands in the `endpoint` column. Recoverable via:
    #   SELECT split_part(endpoint, '?', 2) FROM request_log
    #   WHERE endpoint LIKE '/lod?%';
    # (See docs/OBSERVABILITY.md "/lod adoption" section.) The literal
    # cardinality is bounded — axis ∈ {structural,heat} × level ∈ {0,1,2,4,6}
    # → at most 10 distinct endpoint labels, well below dashboard concern.
    request.state.log_endpoint = f"/lod?axis={axis}&level={level}"

    # ---- validate axis ------------------------------------------------------
    if axis not in VALID_AXES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid axis '{axis}'. {GRAMMAR_HINT}",
        )

    # ---- validate level ----------------------------------------------------
    if level in DEFERRED_LEVELS:
        raise HTTPException(
            status_code=400,
            detail=f"level={level} not yet supported (deferred to follow-on tasks). {GRAMMAR_HINT}",
        )
    if level not in SUPPORTED_LEVELS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid level={level}. {GRAMMAR_HINT}",
        )

    # ---- parse scope -------------------------------------------------------
    kind, community_source, value = _parse_scope(scope)

    # LOD4/6 require a node:<id> scope.
    if level in NODE_REQUIRED_LEVELS and kind != "node":
        raise HTTPException(
            status_code=400,
            detail=(
                f"level {level} requires a node:<id> scope; "
                f"got scope='{scope}'. {GRAMMAR_HINT}"
            ),
        )
    # node:<id> scope is only meaningful at LOD4/6.
    if kind == "node" and level not in NODE_REQUIRED_LEVELS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"scope 'node:<id>' requires level 4 or 6; "
                f"got level={level}. {GRAMMAR_HINT}"
            ),
        )

    # ---- dispatch -----------------------------------------------------------
    async with get_db(user) as conn:
        if kind == "node":
            # value is the entry id (validated non-empty by _parse_scope).
            if level == 4:
                return await _node_lod4(conn, axis, scope, level, value)  # type: ignore[arg-type]
            # level == 6
            return await _node_lod6(conn, axis, scope, level, value)  # type: ignore[arg-type]

        if kind == "corpus":
            if level == 2:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "level 2 requires community scope "
                        "(community:tag:<tag> or community:path:<prefix>); "
                        "corpus has no silhouette card."
                    ),
                )
            if level != 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"scope=corpus requires level=0 at this sprint. {GRAMMAR_HINT}",
                )
            return await _corpus_lod0(conn, axis, scope, level)

        # kind == "community"
        if level not in (1, 2):
            raise HTTPException(
                status_code=400,
                detail=f"scope=community:* requires level=1 or level=2 at this sprint. {GRAMMAR_HINT}",
            )
        if level == 1:
            return await _community_lod1(
                conn, axis, scope, level, community_source, value  # type: ignore[arg-type]
            )
        return await _community_lod2(
            conn, axis, scope, level, community_source, value  # type: ignore[arg-type]
        )


# -----------------------------------------------------------------------------
# LOD0 corpus — same service calls as the v2 manifest's structural/heat blocks.
# -----------------------------------------------------------------------------


async def _corpus_lod0(
    conn,
    axis: str,
    scope: str,
    level: int,
) -> dict[str, Any]:
    base: dict[str, Any] = {"axis": axis, "scope": scope, "level": level}

    if axis == "structural":
        base["structural"] = {
            "edges": await lod_service.get_edge_count(conn),
            "relation_types": await lod_service.get_relation_type_histogram(conn),
            "degree_bins": await lod_service.get_degree_bins(conn),
            "orphans": await lod_service.get_orphan_count(conn),
            "size_distribution": await lod_service.get_size_distribution(conn),
        }
    else:  # axis == "heat"
        base["heat"] = {"bands": await lod_service.get_heat_bands(conn)}

    return base


# -----------------------------------------------------------------------------
# LOD1 community — by tag or by logical_path first segment.
# -----------------------------------------------------------------------------


async def _community_aggregate(
    conn,
    community_source: str,  # "tag" | "path"
    value: str,
    *,
    top_tags_limit: int,
    top_content_types_limit: int,
) -> dict[str, Any]:
    """Run the shared community aggregate query plan.

    Returns a dict with keys ``node_count``, ``edge_count``, ``top_tags``
    (list of ``{tag, count}``, capped to ``top_tags_limit``), and
    ``top_content_types`` (list of ``{content_type, count}``, capped to
    ``top_content_types_limit``).

    Both LOD1 and LOD2 call this with different caps; LOD2 adds the
    "silhouette card" framing on top.
    """
    if community_source == "tag":
        member_sql = "SELECT id FROM entries WHERE %s = ANY(tags) AND status = 'published'"
        params = (value,)
    else:  # "path"
        # First-segment match: covers both 'Projects' itself and 'Projects/...'.
        # Using split_part ensures we don't match 'Projects-archive/' by accident.
        member_sql = (
            "SELECT id FROM entries "
            "WHERE (split_part(logical_path, '/', 1) = %s) "
            "  AND status = 'published'"
        )
        params = (value,)

    # ---- node count --------------------------------------------------------
    cur = await conn.execute(
        f"SELECT COUNT(*) FROM ({member_sql}) AS m",
        params,
    )
    row = await cur.fetchone()
    node_count = int(row[0] or 0)

    # ---- edge count (both endpoints in the community) ----------------------
    cur = await conn.execute(
        f"""
        WITH members AS ({member_sql})
        SELECT COUNT(*)
        FROM entry_links l
        WHERE l.source_entry_id IN (SELECT id FROM members)
          AND l.target_entry_id IN (SELECT id FROM members)
        """,
        params,
    )
    row = await cur.fetchone()
    edge_count = int(row[0] or 0)

    # ---- top tags within community ----------------------------------------
    # unnest the array, exclude the community-defining tag itself when
    # source=="tag" so callers see meaningful co-tags rather than a trivial
    # self-match dominating the list.
    if community_source == "tag":
        cur = await conn.execute(
            f"""
            WITH members AS ({member_sql})
            SELECT t AS tag, COUNT(*) AS count
            FROM entries e, unnest(e.tags) AS t
            WHERE e.id IN (SELECT id FROM members)
              AND t <> %s
            GROUP BY t
            ORDER BY count DESC, tag ASC
            LIMIT %s
            """,
            (value, value, top_tags_limit),
        )
    else:
        cur = await conn.execute(
            f"""
            WITH members AS ({member_sql})
            SELECT t AS tag, COUNT(*) AS count
            FROM entries e, unnest(e.tags) AS t
            WHERE e.id IN (SELECT id FROM members)
            GROUP BY t
            ORDER BY count DESC, tag ASC
            LIMIT %s
            """,
            (value, top_tags_limit),
        )
    cur.row_factory = dict_row
    tag_rows = await cur.fetchall()
    top_tags = [{"tag": r["tag"], "count": int(r["count"])} for r in tag_rows]

    # ---- top content_types -------------------------------------------------
    cur = await conn.execute(
        f"""
        WITH members AS ({member_sql})
        SELECT content_type, COUNT(*) AS count
        FROM entries
        WHERE id IN (SELECT id FROM members)
        GROUP BY content_type
        ORDER BY count DESC, content_type ASC
        LIMIT %s
        """,
        (*params, top_content_types_limit),
    )
    cur.row_factory = dict_row
    type_rows = await cur.fetchall()
    top_content_types = [
        {"content_type": r["content_type"], "count": int(r["count"])}
        for r in type_rows
    ]

    return {
        "node_count": node_count,
        "edge_count": edge_count,
        "top_tags": top_tags,
        "top_content_types": top_content_types,
    }


async def _community_lod1(
    conn,
    axis: str,
    scope: str,
    level: int,
    community_source: str,  # "tag" | "path"
    value: str,
) -> dict[str, Any]:
    """Aggregate a community defined by a single tag or a top-level path.

    Scope predicate is applied uniformly to entries; the edge count joins
    against the same predicate on both endpoints (a "community-internal"
    edge has both source and target inside the community).

    Both axes return the same community shape — heat-banding inside a
    community is deferred (the v2 manifest scope is corpus-level).
    """
    agg = await _community_aggregate(
        conn,
        community_source,
        value,
        top_tags_limit=TOP_TAGS_LIMIT,
        top_content_types_limit=DOMINANT_TYPES_LIMIT,
    )

    return {
        "axis": axis,
        "scope": scope,
        "level": level,
        "community_source": community_source,
        "community": {
            "node_count": agg["node_count"],
            "edge_count": agg["edge_count"],
            "top_tags": agg["top_tags"],
            # LOD1 historically named this "dominant_content_types" — preserve
            # the existing response shape for v0.7.x clients.
            "dominant_content_types": agg["top_content_types"],
        },
    }


# -----------------------------------------------------------------------------
# LOD2 community silhouette — fixed-shape "summary card" for a community.
# -----------------------------------------------------------------------------


async def _community_lod2(
    conn,
    axis: str,
    scope: str,
    level: int,
    community_source: str,  # "tag" | "path"
    value: str,
) -> dict[str, Any]:
    """Return a fixed-shape silhouette card for a community.

    Shape (always present, even on an empty community):
        {axis, scope, level, community_source,
         silhouette: {node_count, edge_count,
                      top_tags (≤5), top_content_types (≤3),
                      community_source}}

    Same query plan as LOD1; differences are tighter top-N caps and the
    explicit "silhouette card" framing (top_content_types instead of LOD1's
    legacy ``dominant_content_types`` field name, and ``community_source``
    embedded inside the card so callers can pass the silhouette around as a
    self-describing object).
    """
    agg = await _community_aggregate(
        conn,
        community_source,
        value,
        top_tags_limit=SILHOUETTE_TOP_TAGS,
        top_content_types_limit=SILHOUETTE_TOP_CONTENT_TYPES,
    )

    return {
        "axis": axis,
        "scope": scope,
        "level": level,
        "community_source": community_source,
        "silhouette": {
            "node_count": agg["node_count"],
            "edge_count": agg["edge_count"],
            "top_tags": agg["top_tags"],
            "top_content_types": agg["top_content_types"],
            "community_source": community_source,
        },
    }


# -----------------------------------------------------------------------------
# LOD4 node silhouette + LOD6 section outline (T-0283.3).
#
# Both honor RLS via `get_db(user)`: an entry the caller cannot see returns
# no row and we 404 — never expose existence.
# -----------------------------------------------------------------------------


async def _node_lod4(
    conn,
    axis: str,
    scope: str,
    level: int,
    entry_id: str,
) -> dict[str, Any]:
    """LOD4 node silhouette."""
    silhouette = await lod_service.get_node_silhouette(conn, entry_id)
    if silhouette is None:
        raise HTTPException(status_code=404, detail="node not found")

    return {
        "axis": axis,
        "scope": scope,
        "level": level,
        "silhouette": silhouette,
    }


async def _node_lod6(
    conn,
    axis: str,
    scope: str,
    level: int,
    entry_id: str,
) -> dict[str, Any]:
    """LOD6 section outline — pure function over `entries.content` markdown."""
    cur = await conn.execute(
        "SELECT content, updated_at FROM entries WHERE id = %s",
        (entry_id,),
    )
    cur.row_factory = dict_row
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="node not found")

    sections = section_outline_service.outline_cached(
        entry_id, row["updated_at"], row["content"] or ""
    )

    return {
        "axis": axis,
        "scope": scope,
        "level": level,
        "outline": sections,
    }
