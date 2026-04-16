"""Admin-only usage analytics endpoints.

Rolls up the observability tables (`entry_access_log`, `request_log`) into
top-N shapes for admin consumption. All endpoints are:

- admin-only (checked explicitly before any DB call, so non-admins get a
  clean 403 rather than a silent 0-row result via RLS);
- org-scoped by RLS (we run under the admin's RLS-scoped connection);
- paginated (limit 1..200, offset >= 0).

Response shapes are intentionally stable — downstream MCP tool (T-0192)
and tests (T-0193) depend on them. Do not add extra keys without bumping
the consumers.
"""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg.rows import dict_row

from auth import UserContext, get_current_user
from database import get_db

router = APIRouter(tags=["analytics"])


# Map of accepted `since` tokens -> timedelta. Extend here if product asks
# for e.g. `90d`; keep the set small so the surface stays predictable.
_SINCE_MAP = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


def _parse_since(since: str) -> timedelta:
    """Parse a `since` query param into a timedelta. 422 on unknown tokens."""
    delta = _SINCE_MAP.get(since)
    if delta is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid 'since' value: {since!r}. "
                f"Must be one of: {sorted(_SINCE_MAP.keys())}"
            ),
        )
    return delta


def _require_admin(user: UserContext) -> None:
    """Gate: analytics endpoints are admin-only. 403 for everyone else."""
    if user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="analytics endpoints are admin-only",
        )


# Valid actor_type filter values for top-entries — matches the CHECK
# constraint on `entry_access_log.actor_type`.
_VALID_ACTOR_TYPES = {"user", "agent", "api"}


@router.get("/top-entries")
async def top_entries(
    actor_type: str | None = Query(
        None,
        description="Filter by actor type: user, agent, or api",
    ),
    since: str = Query("24h", description="Time window: 1h, 24h, 7d, or 30d"),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(get_current_user),
):
    """Top entries by read count within the window, org-scoped.

    Single GROUP BY query joining `entry_access_log` to `entries` so the
    response includes titles without an N+1 fanout.
    """
    _require_admin(user)
    delta = _parse_since(since)

    if actor_type is not None and actor_type not in _VALID_ACTOR_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid 'actor_type': {actor_type!r}. "
                f"Must be one of: {sorted(_VALID_ACTOR_TYPES)}"
            ),
        )

    # Build the WHERE clause. The `ts >= now() - interval` bound is applied
    # on the access log side where the idx_entry_access_org_ts index helps.
    where_parts = ["eal.ts >= now() - %s::interval"]
    params: list = [f"{int(delta.total_seconds())} seconds"]

    if actor_type is not None:
        where_parts.append("eal.actor_type = %s")
        params.append(actor_type)

    where_clause = " AND ".join(where_parts)

    query = f"""
        SELECT
            eal.entry_id::text AS entry_id,
            e.title AS title,
            COUNT(*) AS reads
        FROM entry_access_log eal
        JOIN entries e ON e.id = eal.entry_id
        WHERE {where_clause}
        GROUP BY eal.entry_id, e.title
        ORDER BY reads DESC, eal.entry_id
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])

    async with get_db(user) as conn:
        cur = await conn.execute(query, params)
        cur.row_factory = dict_row
        rows = await cur.fetchall()

    # Coerce COUNT(*) (psycopg returns Decimal/int) to int for JSON clarity.
    items = [
        {
            "entry_id": r["entry_id"],
            "title": r["title"],
            "reads": int(r["reads"]),
        }
        for r in rows
    ]

    return {
        "items": items,
        "limit": limit,
        "offset": offset,
        "since": since,
    }


@router.get("/top-endpoints")
async def top_endpoints(
    since: str = Query("24h", description="Time window: 1h, 24h, 7d, or 30d"),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(get_current_user),
):
    """Top endpoints by request count within the window, with avg + p95 duration."""
    _require_admin(user)
    delta = _parse_since(since)

    query = """
        SELECT
            endpoint,
            COUNT(*) AS count,
            AVG(duration_ms)::float AS avg_duration_ms,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)::float
                AS p95_duration_ms
        FROM request_log
        WHERE ts >= now() - %s::interval
        GROUP BY endpoint
        ORDER BY count DESC, endpoint
        LIMIT %s OFFSET %s
    """
    params = [f"{int(delta.total_seconds())} seconds", limit, offset]

    async with get_db(user) as conn:
        cur = await conn.execute(query, params)
        cur.row_factory = dict_row
        rows = await cur.fetchall()

    items = [
        {
            "endpoint": r["endpoint"],
            "count": int(r["count"]),
            "avg_duration_ms": (
                float(r["avg_duration_ms"]) if r["avg_duration_ms"] is not None else None
            ),
            "p95_duration_ms": (
                float(r["p95_duration_ms"]) if r["p95_duration_ms"] is not None else None
            ),
        }
        for r in rows
    ]

    return {
        "items": items,
        "limit": limit,
        "offset": offset,
        "since": since,
    }


@router.get("/session-depth")
async def session_depth(
    actor_id: str = Query(..., description="The actor_id to profile"),
    since: str = Query("24h", description="Time window: 1h, 24h, 7d, or 30d"),
    user: UserContext = Depends(get_current_user),
):
    """Bucket an actor's activity into 15-minute windows.

    A window row reports how many requests the actor issued, how many
    distinct entries they touched (via entry_access_log joined on actor_id),
    and the wall-clock span of requests within that window.
    """
    _require_admin(user)
    delta = _parse_since(since)

    # 15-minute bucket: truncate to the minute, then subtract (minute % 15).
    # Same bucket expression used on both sides so the LEFT JOIN lines up.
    bucket_expr = (
        "date_trunc('minute', {ts}) "
        "- (EXTRACT(MINUTE FROM {ts})::int %% 15) * interval '1 minute'"
    )
    req_bucket = bucket_expr.format(ts="rl.ts")
    eal_bucket = bucket_expr.format(ts="eal.ts")

    query = f"""
        WITH req AS (
            SELECT
                {req_bucket} AS window_start,
                COUNT(*) AS requests,
                EXTRACT(EPOCH FROM (MAX(rl.ts) - MIN(rl.ts)))::int AS duration_s
            FROM request_log rl
            WHERE rl.actor_id = %s
              AND rl.ts >= now() - %s::interval
            GROUP BY window_start
        ),
        touched AS (
            SELECT
                {eal_bucket} AS window_start,
                COUNT(DISTINCT eal.entry_id) AS entries_touched
            FROM entry_access_log eal
            WHERE eal.actor_id = %s
              AND eal.ts >= now() - %s::interval
            GROUP BY window_start
        )
        SELECT
            COALESCE(req.window_start, touched.window_start) AS window_start,
            COALESCE(req.requests, 0) AS requests,
            COALESCE(touched.entries_touched, 0) AS entries_touched,
            COALESCE(req.duration_s, 0) AS duration_s
        FROM req
        FULL OUTER JOIN touched ON touched.window_start = req.window_start
        ORDER BY window_start
    """

    interval_str = f"{int(delta.total_seconds())} seconds"
    params = [actor_id, interval_str, actor_id, interval_str]

    async with get_db(user) as conn:
        cur = await conn.execute(query, params)
        cur.row_factory = dict_row
        rows = await cur.fetchall()

    windows = [
        {
            "window_start": (
                r["window_start"].isoformat() if r["window_start"] is not None else None
            ),
            "requests": int(r["requests"]),
            "entries_touched": int(r["entries_touched"]),
            "duration_s": int(r["duration_s"]),
        }
        for r in rows
    ]

    return {
        "actor_id": actor_id,
        "windows": windows,
        "since": since,
    }
