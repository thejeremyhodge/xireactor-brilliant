"""Personal-zone resolver service (Sprint 0051, ADR personal-zones-sprint-1).

Every user gets an auto-created walled-off "zone" group at user-creation time
via the trigger installed in `db/migrations/034_personal_zones.sql`. Writes
default into the caller's zone for safety — `POST /entries` without an
explicit non-private sensitivity grants the caller's zone group `admin` on
the new entry id.

This module exposes a single helper used by the entry-write paths:

    get_or_create_zone(conn, user_id, org_id) -> str

The helper is "defensive": it calls `provision_user_zone(...)` (SECURITY
DEFINER, idempotent) before SELECTing the row so any historical user that
slipped past the trigger still ends up with a zone. Returns the zone group
id as a TEXT string suitable for use as `permissions.principal_id` (which is
TEXT — see migration 018; UUIDs are cast to TEXT at the boundary).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def get_or_create_zone(conn, user_id: str, org_id: str) -> str:
    """Return the caller's zone group id (as TEXT), creating it if missing.

    Calls `provision_user_zone(p_user_id, p_org_id)` (SECURITY DEFINER,
    idempotent — see migration 034). Re-SELECTs the canonical row to be
    explicit about what we return; raises RuntimeError on the (impossible)
    case where the function returns no row, so callers fail loudly rather
    than writing an entry with no zone grant.

    Returned value is the TEXT representation of the group UUID — ready to
    use as `permissions.principal_id`, which is `TEXT` per migration 018.
    """
    cur = await conn.execute(
        "SELECT provision_user_zone(%s, %s)::text",
        (user_id, org_id),
    )
    row = await cur.fetchone()
    if row is None or row[0] is None:
        # provision_user_zone is idempotent and returns the group id on every
        # path. A NULL here means a hard schema/permission issue — surface it
        # rather than silently dropping the zone grant.
        raise RuntimeError(
            f"provision_user_zone returned no row for user={user_id} org={org_id}"
        )
    return row[0]
