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
