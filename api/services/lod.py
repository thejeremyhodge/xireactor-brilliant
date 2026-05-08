"""Aggregate LOD (level-of-detail) data layer for Sprint 0049.

Pure async aggregate functions over an open RLS-scoped psycopg connection.
Caller is responsible for setting the RLS session context (see
`api.database.get_db`); these functions take an already-scoped connection
and run read-only aggregates against `entries`, `entry_links`, and
`entry_access_log`.

No HTTP, no auth context, no write paths. Caller-owned connection means
unit tests can drive these with any open async connection (see T-0285).

Heat banding thresholds are module constants (see spec §"Locked Decisions
Heat banding"). Admin override path is noted in the spec but NOT in this
sprint — defaults are global.
"""

from __future__ import annotations

from statistics import median
from typing import Any

import psycopg
from psycopg.rows import dict_row

# -----------------------------------------------------------------------------
# Heat banding thresholds — see spec §"Locked Decisions Heat banding"
# (.xireactor/specs/0049--2026-05-07--multi-lod-3-axis.md)
#
# Spiking = >SPIKING_READS_PER_24H reads in the last 24h
# Hot     = entry touched within HOT_WINDOW
# Warm    = entry touched within WARM_WINDOW (and not already hot)
# Cold    = older than WARM_WINDOW or never accessed
# -----------------------------------------------------------------------------
SPIKING_READS_PER_24H: int = 5
HOT_WINDOW: str = "24 hours"
WARM_WINDOW: str = "7 days"


# -----------------------------------------------------------------------------
# Structural axis aggregates
# -----------------------------------------------------------------------------


async def get_edge_count(conn: psycopg.AsyncConnection) -> int:
    """Total edges (rows in `entry_links`) visible to the caller's RLS scope."""
    cur = await conn.execute("SELECT COUNT(*) FROM entry_links")
    row = await cur.fetchone()
    return int(row[0] or 0)


async def get_relation_type_histogram(
    conn: psycopg.AsyncConnection,
) -> dict[str, int]:
    """Return a histogram of edges keyed by relation type (`link_type`).

    The schema column is `link_type` (002_relationships.sql); the spec
    refers to the conceptual axis as "relation_type". We expose the raw
    DB values as the histogram keys so consumers see exactly the set of
    types currently in the corpus. Empty corpus → `{}`.
    """
    cur = await conn.execute(
        """
        SELECT link_type, count(*) AS count
        FROM entry_links
        GROUP BY link_type
        ORDER BY count DESC, link_type ASC
        """
    )
    rows = await cur.fetchall()
    return {r[0]: int(r[1]) for r in rows}


async def get_degree_bins(conn: psycopg.AsyncConnection) -> dict[str, Any]:
    """Return {"avg", "median", "max"} of total degree (in + out) per entry.

    Empty corpus → {"avg": 0.0, "median": 0.0, "max": 0}.
    """
    # Per-entry degree = inbound + outbound edges. LEFT JOIN keeps zero-degree
    # entries in the distribution (they pull `avg` and `median` toward 0,
    # which is the honest signal — orphans are part of the corpus shape).
    cur = await conn.execute(
        """
        SELECT
          COALESCE(
            (SELECT COUNT(*) FROM entry_links l
              WHERE l.source_entry_id = e.id), 0
          )
          +
          COALESCE(
            (SELECT COUNT(*) FROM entry_links l
              WHERE l.target_entry_id = e.id), 0
          ) AS degree
        FROM entries e
        """
    )
    rows = await cur.fetchall()
    degrees = [int(r[0]) for r in rows]

    if not degrees:
        return {"avg": 0.0, "median": 0.0, "max": 0}

    return {
        "avg": round(sum(degrees) / len(degrees), 2),
        "median": float(median(degrees)),
        "max": max(degrees),
    }


async def get_orphan_count(conn: psycopg.AsyncConnection) -> int:
    """Count entries with degree 0 (no inbound, no outbound links)."""
    cur = await conn.execute(
        """
        SELECT COUNT(*)
        FROM entries e
        WHERE NOT EXISTS (
            SELECT 1 FROM entry_links l WHERE l.source_entry_id = e.id
        )
        AND NOT EXISTS (
            SELECT 1 FROM entry_links l WHERE l.target_entry_id = e.id
        )
        """
    )
    row = await cur.fetchone()
    return int(row[0] or 0)


async def get_size_distribution(
    conn: psycopg.AsyncConnection,
) -> dict[str, int]:
    """Bucket entries by `content` byte-length.

    Buckets: <1KB, 1-10KB, 10-100KB, >100KB. NULL/empty content → <1KB.
    Empty corpus → all zeros.
    """
    cur = await conn.execute(
        """
        SELECT
          SUM(CASE WHEN octet_length(COALESCE(content, '')) < 1024 THEN 1 ELSE 0 END) AS lt_1kb,
          SUM(CASE WHEN octet_length(COALESCE(content, '')) >= 1024
                    AND octet_length(COALESCE(content, '')) < 10240 THEN 1 ELSE 0 END) AS kb_1_10,
          SUM(CASE WHEN octet_length(COALESCE(content, '')) >= 10240
                    AND octet_length(COALESCE(content, '')) < 102400 THEN 1 ELSE 0 END) AS kb_10_100,
          SUM(CASE WHEN octet_length(COALESCE(content, '')) >= 102400 THEN 1 ELSE 0 END) AS gt_100kb
        FROM entries
        """
    )
    cur.row_factory = dict_row
    row = await cur.fetchone()
    if row is None:
        return {"<1KB": 0, "1-10KB": 0, "10-100KB": 0, ">100KB": 0}
    return {
        "<1KB": int(row["lt_1kb"] or 0),
        "1-10KB": int(row["kb_1_10"] or 0),
        "10-100KB": int(row["kb_10_100"] or 0),
        ">100KB": int(row["gt_100kb"] or 0),
    }


# -----------------------------------------------------------------------------
# Heat axis aggregate
# -----------------------------------------------------------------------------


async def get_heat_bands(conn: psycopg.AsyncConnection) -> dict[str, int]:
    """Classify each visible entry into one of {cold, warm, hot, spiking}.

    Bands are mutually exclusive and assigned in priority order:
      1. spiking — >SPIKING_READS_PER_24H reads in the last HOT_WINDOW
      2. hot     — at least one read in the last HOT_WINDOW (not spiking)
      3. warm    — at least one read in the last WARM_WINDOW (not hot)
      4. cold    — no read in the last WARM_WINDOW (or never accessed)

    Source data: `entry_access_log.ts` (db/migrations/023_access_log.sql).
    Note: only kb_admin sees rows of `entry_access_log` (RLS); for other
    roles the table appears empty and every visible entry classifies as
    cold. That's the intentional behavior — non-admin agents shouldn't
    derive read-traffic signal from other actors.

    Empty corpus → all four bands at 0.
    """
    # Single roundtrip: per-entry max(ts) + 24h read-count, then bucket.
    # LEFT JOIN keeps entries with zero accesses (→ cold).
    cur = await conn.execute(
        f"""
        WITH per_entry AS (
            SELECT
              e.id AS entry_id,
              MAX(a.ts)                          AS last_ts,
              COUNT(a.id) FILTER (
                  WHERE a.ts >= now() - INTERVAL '{HOT_WINDOW}'
              )                                  AS reads_24h,
              COUNT(a.id) FILTER (
                  WHERE a.ts >= now() - INTERVAL '{WARM_WINDOW}'
              )                                  AS reads_7d
            FROM entries e
            LEFT JOIN entry_access_log a ON a.entry_id = e.id
            GROUP BY e.id
        )
        SELECT
          SUM(CASE WHEN reads_24h > %s THEN 1 ELSE 0 END) AS spiking,
          SUM(CASE
                WHEN reads_24h > %s THEN 0
                WHEN last_ts IS NOT NULL
                 AND last_ts >= now() - INTERVAL '{HOT_WINDOW}' THEN 1
                ELSE 0
              END) AS hot,
          SUM(CASE
                WHEN last_ts IS NOT NULL
                 AND last_ts >= now() - INTERVAL '{HOT_WINDOW}' THEN 0
                WHEN last_ts IS NOT NULL
                 AND last_ts >= now() - INTERVAL '{WARM_WINDOW}' THEN 1
                ELSE 0
              END) AS warm,
          SUM(CASE
                WHEN last_ts IS NULL
                  OR last_ts <  now() - INTERVAL '{WARM_WINDOW}' THEN 1
                ELSE 0
              END) AS cold
        FROM per_entry
        """,
        (SPIKING_READS_PER_24H, SPIKING_READS_PER_24H),
    )
    cur.row_factory = dict_row
    row = await cur.fetchone()
    if row is None:
        return {"cold": 0, "warm": 0, "hot": 0, "spiking": 0}
    return {
        "cold": int(row["cold"] or 0),
        "warm": int(row["warm"] or 0),
        "hot": int(row["hot"] or 0),
        "spiking": int(row["spiking"] or 0),
    }


# -----------------------------------------------------------------------------
# Node silhouette (LOD4) — single-row + degree query for one entry.
# -----------------------------------------------------------------------------

# Tag-cluster shape: anything matching `<prefix>:<value>` is a "cluster tag"
# (e.g. `project:atlas`, `task:completed`). Plain tags ("review", "urgent")
# are excluded — they're not addressable as cluster scopes.
_CLUSTER_TAG_RE = r"^[^:]+:[^:].*$"


async def get_node_silhouette(
    conn: psycopg.AsyncConnection,
    entry_id: str,
) -> dict[str, Any] | None:
    """Return a node silhouette dict for ``entry_id`` or ``None`` if invisible.

    Shape::

        {
          id, title, tags, length,
          degree_in, degree_out,
          tag_clusters: [...],   # tags matching `<prefix>:<value>`
          path_cluster: str,     # logical_path first segment
        }

    ``length`` uses ``octet_length(content)`` (byte length) — consistent
    with ``get_size_distribution`` so the silhouette aligns with the
    corpus size-distribution buckets.

    RLS: the caller's connection is already RLS-scoped via
    ``get_db(user)``. If the entry is not visible the SELECT returns
    no row and we return ``None`` — never expose existence.
    """
    cur = await conn.execute(
        """
        SELECT
          e.id::text                                  AS id,
          e.title                                     AS title,
          e.tags                                      AS tags,
          octet_length(COALESCE(e.content, ''))       AS length,
          (SELECT COUNT(*) FROM entry_links l
            WHERE l.target_entry_id = e.id)           AS degree_in,
          (SELECT COUNT(*) FROM entry_links l
            WHERE l.source_entry_id = e.id)           AS degree_out,
          split_part(e.logical_path, '/', 1)          AS path_cluster
        FROM entries e
        WHERE e.id = %s
        """,
        (entry_id,),
    )
    cur.row_factory = dict_row
    row = await cur.fetchone()
    if row is None:
        return None

    tags = list(row["tags"] or [])
    # Cluster tags: shape `<prefix>:<value>` (matches grammar used by
    # community:tag:<tag> scope). Filter is in Python rather than SQL
    # because the tag list is already small.
    import re as _re

    cluster_re = _re.compile(_CLUSTER_TAG_RE)
    tag_clusters = [t for t in tags if cluster_re.match(t)]

    return {
        "id": row["id"],
        "title": row["title"],
        "tags": tags,
        "length": int(row["length"] or 0),
        "degree_in": int(row["degree_in"] or 0),
        "degree_out": int(row["degree_out"] or 0),
        "tag_clusters": tag_clusters,
        "path_cluster": row["path_cluster"] or "",
    }


# -----------------------------------------------------------------------------
# Node heat (LOD4, axis=heat) — single-entry heat chip (T-0289 / GH #73)
#
# Returns the heat band for one entry plus reads_24h / reads_7d / last_ts.
# Same banding rules and thresholds as `get_heat_bands` (LOD0 corpus).
#
# RLS subtlety: `entry_access_log` is admin-only. A non-admin act-as reader
# sees zero rows even when the entry has been read frequently — every
# non-admin LOD4 heat chip would otherwise read as "cold" with no signal
# distinguishing "actually cold" from "RLS-filtered cold". Caller passes in
# the entry's degree (already computed for the silhouette) so we can surface
# `rls_filtered: true` when count is zero AND degree is non-zero (a cheap
# heuristic: a connected entry that *looks* cold is probably just hidden).
# -----------------------------------------------------------------------------


async def get_node_heat(
    conn: psycopg.AsyncConnection,
    entry_id: str,
    *,
    degree: int | None = None,
) -> dict[str, Any] | None:
    """Return heat chip for one entry or ``None`` if invisible.

    Shape::

        {
          "band": "cold" | "warm" | "hot" | "spiking",
          "reads_24h": int,
          "reads_7d": int,
          "last_ts": str | None,        # ISO 8601 of the last access, or None
          "rls_filtered": bool          # ONLY present + true when band==cold,
                                        # reads_7d==0, and degree>0
        }

    The ``rls_filtered`` hint is documented in skill/references/api-reference.md
    and is intentionally a *hint* — non-admin act-as readers never see
    `entry_access_log` rows (RLS), so a cold band on a connected entry is
    probably RLS-induced silence rather than a true read-traffic signal.

    ``degree`` is optional; when omitted we compute it inline. If the entry
    is not visible (RLS-filtered at the entry level, not the access-log
    level), returns ``None``.
    """
    # Confirm entry visibility under the caller's RLS scope. Mirrors the
    # `get_node_silhouette` contract — invisible entries return None so the
    # route can 404 cleanly.
    cur = await conn.execute(
        "SELECT 1 FROM entries WHERE id = %s",
        (entry_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None

    if degree is None:
        cur = await conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM entry_links l
                 WHERE l.target_entry_id = %s)
              +
              (SELECT COUNT(*) FROM entry_links l
                 WHERE l.source_entry_id = %s)
            """,
            (entry_id, entry_id),
        )
        drow = await cur.fetchone()
        degree = int(drow[0] or 0) if drow else 0

    # Aggregate this entry's access log in a single query. Same thresholds
    # and window definitions as `get_heat_bands` (LOD0 corpus heat) so the
    # banding is consistent across LODs.
    cur = await conn.execute(
        f"""
        SELECT
          MAX(ts) AS last_ts,
          COUNT(*) FILTER (WHERE ts >= now() - INTERVAL '{HOT_WINDOW}')
            AS reads_24h,
          COUNT(*) FILTER (WHERE ts >= now() - INTERVAL '{WARM_WINDOW}')
            AS reads_7d
        FROM entry_access_log
        WHERE entry_id = %s
        """,
        (entry_id,),
    )
    cur.row_factory = dict_row
    arow = await cur.fetchone()
    reads_24h = int((arow or {}).get("reads_24h") or 0)
    reads_7d = int((arow or {}).get("reads_7d") or 0)
    last_ts = (arow or {}).get("last_ts") if arow else None

    # Banding: same priority order as `get_heat_bands`.
    if reads_24h > SPIKING_READS_PER_24H:
        band = "spiking"
    elif last_ts is not None and reads_24h > 0:
        band = "hot"
    elif reads_7d > 0:
        band = "warm"
    else:
        band = "cold"

    out: dict[str, Any] = {
        "band": band,
        "reads_24h": reads_24h,
        "reads_7d": reads_7d,
        "last_ts": last_ts.isoformat() if last_ts is not None else None,
    }
    # rls_filtered hint: only meaningful when the chip looks cold AND the
    # entry is connected. A non-admin act-as reader sees this as a signal
    # that "all-cold" is RLS-induced rather than an actual read-traffic
    # absence. Documented in skill/references/api-reference.md (T-0289).
    if band == "cold" and reads_7d == 0 and (degree or 0) > 0:
        out["rls_filtered"] = True

    return out


# -----------------------------------------------------------------------------
# Community heat (LOD1 / LOD2, axis=heat) — per-band counts over a community.
#
# Same banding rules as `get_heat_bands`, but the entry set is restricted to
# the community membership predicate (matches `_community_aggregate` in
# `api/routes/lod.py`). Returns the same `{cold, warm, hot, spiking}` shape
# the corpus heat block uses, so callers see a uniform contract across LOD0
# and LOD1/2.
# -----------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# Epistemic axis (LOD0 / LOD2 / LOD4) — T-0291 / Sprint 0050.
#
# Aggregate-on-read over the four epistemic columns added by migration 033:
#   claim_type, source_confidence, verification_status, conflict_with.
#
# LOD0 / LOD2 → 2D histogram of (claim_type × verification_status). The result
# always carries the full 4×4 = 16 cells with zero counts where empty so
# callers can iterate the grid without conditionals.
#
# LOD4 → per-node "epistemic chip": the four epistemic fields for one entry,
# nothing else. Title/content explicitly NOT in this payload (T-0291
# acceptance #2: no extra columns from `entries`).
#
# Histogram queries use index `entries_epistemic_histogram_idx`
# (db/migrations/033_epistemic_axis.sql) — verified by the EXPLAIN-based test
# in tests/test_lod.py.
# -----------------------------------------------------------------------------

# Full enum value sets (mirror api.models + the Postgres enums in 033). The
# histogram pre-populates every (claim_type × verification_status) cell using
# these so the response shape is always 4×4=16 cells regardless of corpus
# contents. Order matches the enum declaration in the migration.
EPISTEMIC_CLAIM_TYPES: tuple[str, ...] = (
    "event",
    "observation",
    "claim",
    "rule",
)
EPISTEMIC_VERIFICATION_STATUSES: tuple[str, ...] = (
    "verified",
    "pending",
    "disputed",
    "superseded",
)


async def get_epistemic_histogram(
    conn: psycopg.AsyncConnection,
    scope_kind: str,  # "corpus" | "community"
    *,
    community_source: str | None = None,  # "tag" | "path" when scope_kind=="community"
    value: str | None = None,
) -> dict[str, dict[str, int]]:
    """Return ``{claim_type: {verification_status: count}}`` over the scope.

    Single query: ``GROUP BY claim_type, verification_status`` over `entries`
    with an optional membership predicate (community-by-tag or
    community-by-path, matching the existing community aggregate grammar).

    Every (claim_type × verification_status) cell is present in the result
    with count 0 when empty — callers get a fixed 4×4 grid.

    Uses ``entries_epistemic_histogram_idx`` (claim_type, verification_status)
    from migration 033. When the scope is community-bounded the planner falls
    back to a tag/path-filtered scan; the index still helps the GROUP BY.
    """
    # Build the membership predicate. We always scope to status='published'
    # to keep the histogram aligned with the user-visible corpus (mirrors
    # `_community_aggregate` in api/routes/lod.py).
    if scope_kind == "corpus":
        where_clause = "WHERE status = 'published'"
        params: tuple[Any, ...] = ()
    elif scope_kind == "community":
        if community_source == "tag":
            where_clause = "WHERE %s = ANY(tags) AND status = 'published'"
            params = (value,)
        elif community_source == "path":
            where_clause = (
                "WHERE split_part(logical_path, '/', 1) = %s "
                "AND status = 'published'"
            )
            params = (value,)
        else:  # pragma: no cover — guarded by route
            raise ValueError(
                f"unknown community_source: {community_source!r}"
            )
    else:  # pragma: no cover — guarded by route
        raise ValueError(f"unknown scope_kind: {scope_kind!r}")

    cur = await conn.execute(
        f"""
        SELECT claim_type::text   AS claim_type,
               verification_status::text AS verification_status,
               COUNT(*) AS cnt
        FROM entries
        {where_clause}
        GROUP BY claim_type, verification_status
        """,
        params,
    )
    cur.row_factory = dict_row
    rows = await cur.fetchall()

    # Pre-populate the full grid with zeros so callers get a stable shape
    # even on an empty corpus / community.
    grid: dict[str, dict[str, int]] = {
        ct: {vs: 0 for vs in EPISTEMIC_VERIFICATION_STATUSES}
        for ct in EPISTEMIC_CLAIM_TYPES
    }
    for r in rows:
        ct = r["claim_type"]
        vs = r["verification_status"]
        # Defensive: if a future migration adds a value not in the constants
        # above, surface it rather than silently dropping. Add the row to a
        # synthesized inner dict if the outer key is unknown.
        if ct not in grid:
            grid[ct] = {vs2: 0 for vs2 in EPISTEMIC_VERIFICATION_STATUSES}
        grid[ct][vs] = int(r["cnt"] or 0)
    return grid


async def get_node_epistemic(
    conn: psycopg.AsyncConnection,
    entry_id: str,
) -> dict[str, Any] | None:
    """Return the four-field epistemic chip for one entry or ``None``.

    Shape::

        {
          "claim_type":          "event" | "observation" | "claim" | "rule",
          "source_confidence":   "verified" | "reported" | "inferred" | "rumor",
          "verification_status": "verified" | "pending" | "disputed" | "superseded",
          "conflict_with":       [<entry_id>, ...]   # uuid[] cast to str
        }

    Title / content / tags / anything else are intentionally NOT included
    (T-0291 acceptance #2 — keep the chip tight). Callers wanting the full
    silhouette plus epistemic should issue both LOD4 axis=structural and
    axis=epistemic.

    Returns ``None`` when the entry is invisible to the caller's RLS scope —
    same contract as `get_node_silhouette` so the route can 404 cleanly.
    """
    cur = await conn.execute(
        """
        SELECT
          claim_type::text          AS claim_type,
          source_confidence::text   AS source_confidence,
          verification_status::text AS verification_status,
          conflict_with             AS conflict_with
        FROM entries
        WHERE id = %s
        """,
        (entry_id,),
    )
    cur.row_factory = dict_row
    row = await cur.fetchone()
    if row is None:
        return None

    # `conflict_with` is uuid[]; psycopg returns list[UUID]. Coerce to str
    # for JSON-friendly response shape.
    conflicts = row["conflict_with"] or []
    return {
        "claim_type": row["claim_type"],
        "source_confidence": row["source_confidence"],
        "verification_status": row["verification_status"],
        "conflict_with": [str(c) for c in conflicts],
    }


async def get_community_heat_bands(
    conn: psycopg.AsyncConnection,
    community_source: str,  # "tag" | "path"
    value: str,
) -> dict[str, int]:
    """Return ``{cold, warm, hot, spiking}`` over the community's entries.

    ``community_source`` is the same grammar as the LOD1/LOD2 community
    aggregate: ``"tag"`` matches ``%s = ANY(tags)``; ``"path"`` matches
    ``split_part(logical_path, '/', 1) = %s``. Empty community → all bands 0.
    """
    if community_source == "tag":
        member_predicate = "%s = ANY(tags) AND status = 'published'"
    else:  # "path"
        member_predicate = (
            "split_part(logical_path, '/', 1) = %s AND status = 'published'"
        )

    cur = await conn.execute(
        f"""
        WITH members AS (
            SELECT id FROM entries WHERE {member_predicate}
        ),
        per_entry AS (
            SELECT
              m.id AS entry_id,
              MAX(a.ts) AS last_ts,
              COUNT(a.id) FILTER (
                  WHERE a.ts >= now() - INTERVAL '{HOT_WINDOW}'
              ) AS reads_24h,
              COUNT(a.id) FILTER (
                  WHERE a.ts >= now() - INTERVAL '{WARM_WINDOW}'
              ) AS reads_7d
            FROM members m
            LEFT JOIN entry_access_log a ON a.entry_id = m.id
            GROUP BY m.id
        )
        SELECT
          SUM(CASE WHEN reads_24h > %s THEN 1 ELSE 0 END) AS spiking,
          SUM(CASE
                WHEN reads_24h > %s THEN 0
                WHEN last_ts IS NOT NULL
                 AND last_ts >= now() - INTERVAL '{HOT_WINDOW}' THEN 1
                ELSE 0
              END) AS hot,
          SUM(CASE
                WHEN last_ts IS NOT NULL
                 AND last_ts >= now() - INTERVAL '{HOT_WINDOW}' THEN 0
                WHEN last_ts IS NOT NULL
                 AND last_ts >= now() - INTERVAL '{WARM_WINDOW}' THEN 1
                ELSE 0
              END) AS warm,
          SUM(CASE
                WHEN last_ts IS NULL
                  OR last_ts <  now() - INTERVAL '{WARM_WINDOW}' THEN 1
                ELSE 0
              END) AS cold
        FROM per_entry
        """,
        (value, SPIKING_READS_PER_24H, SPIKING_READS_PER_24H),
    )
    cur.row_factory = dict_row
    row = await cur.fetchone()
    if row is None:
        return {"cold": 0, "warm": 0, "hot": 0, "spiking": 0}
    return {
        "cold": int(row["cold"] or 0),
        "warm": int(row["warm"] or 0),
        "hot": int(row["hot"] or 0),
        "spiking": int(row["spiking"] or 0),
    }
