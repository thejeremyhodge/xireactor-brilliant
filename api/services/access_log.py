"""Batched per-entry read tracking helper for `entry_access_log`.

Called from read-path endpoints in `routes/entries.py`, `routes/graph.py`, and
`routes/staging.py` to record which entries the caller actually saw in the
response. Writes participate in the caller's existing RLS-scoped transaction
so the INSERT runs under the same pg role / `app.org_id` that served the read.

Design notes:
- One multi-row INSERT per request (even for 50-entry list pages or big graphs)
  so we don't amplify query count. See spec 0034c / T-0190.
- Failure is non-fatal: if logging errors, we warn and swallow. A logging
  outage must NEVER break a read for the user.
- `actor_type` maps from `UserContext.key_type`: agent keys → `agent`, human
  (interactive) keys → `user`, machine/integration keys → `api`. The DB has a
  CHECK constraint on those three values.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# UserContext.key_type -> entry_access_log.actor_type
_ACTOR_TYPE_MAP = {
    "agent": "agent",
    "interactive": "user",
    "api_integration": "api",
}


async def log_entry_reads(
    conn: Any,
    user: Any,
    entry_ids: Iterable[str],
) -> None:
    """Record one `entry_access_log` row per distinct entry id surfaced.

    Args:
        conn: An RLS-scoped async psycopg connection from `get_db(user)`. The
            caller's transaction is reused — do NOT open a new connection.
        user: The authenticated `UserContext` for this request.
        entry_ids: Entry ids that were returned to the caller. Duplicates are
            collapsed while preserving first-seen order.

    Never raises. Errors are logged at WARNING and swallowed so read paths
    remain resilient to logging outages.
    """
    try:
        # De-duplicate while preserving order
        seen: set[str] = set()
        unique_ids: list[str] = []
        for eid in entry_ids:
            if eid is None:
                continue
            sid = str(eid)
            if sid in seen:
                continue
            seen.add(sid)
            unique_ids.append(sid)

        if not unique_ids:
            return

        actor_type = _ACTOR_TYPE_MAP.get(getattr(user, "key_type", None), "api")
        org_id = user.org_id
        actor_id = user.id
        source = getattr(user, "source", None)

        # One multi-row INSERT. Build VALUES placeholders dynamically so we
        # flush all ids in a single round-trip regardless of list size.
        placeholders = ",".join(["(%s, %s, %s, %s, %s)"] * len(unique_ids))
        params: list[Any] = []
        for eid in unique_ids:
            params.extend([org_id, actor_type, actor_id, eid, source])

        await conn.execute(
            f"""
            INSERT INTO entry_access_log
                (org_id, actor_type, actor_id, entry_id, source)
            VALUES {placeholders}
            """,
            params,
        )
    except Exception as exc:  # noqa: BLE001 — log-write must never break the read
        logger.warning("entry_access_log write failed: %s", exc)
