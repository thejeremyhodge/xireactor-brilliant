"""Session initialization endpoint — density manifest for agent session start.

Returns a compact manifest (~≤ 2K tokens) instead of the full tiered index.
The agent is expected to drill down with `get_index(depth=N, path=...)`,
`search_entries`, and `get_entry` once it has seen the manifest. The old
payload inlined every entry, every relationship, every summary, and the
full content of every `system` entry — that routinely ballooned past 40K
tokens on real-sized KBs and broke Claude Code sessions.

New shape (v0.4.0 breaking change):

    {
      "manifest": {
        "total_entries": <int>,
        "last_updated":  <iso8601 | null>,
        "user": {id, display_name, role, department, source},
        "categories":    [{content_type, count}, ...],
        "top_paths":     [{logical_path_prefix, count}, ...],   # up to 15
        "system_entries":[{id, title, logical_path}, ...],       # no content
        "pending_reviews": {count, items[0..5], review_url},
        "hints": ["call get_index(depth=3, path='Projects/') ...", ...]
      }
    }
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from psycopg.rows import dict_row

from auth import UserContext, get_current_user
from database import get_db

router = APIRouter(tags=["session"])

# Hard cap on top_paths rows — the agent can walk past the horizon with
# get_index(path=...), so there is no value in dumping the long tail here.
TOP_PATHS_LIMIT = 15


@router.get("")
async def session_init(
    user: UserContext = Depends(get_current_user),
):
    """Return a compact density manifest for agent session start.

    The manifest is designed to fit under ~2K tokens regardless of KB size:
    it carries counts, top-level path buckets, system-entry handles, and
    pending-review previews. Full entry content, summaries, and the
    relationship graph are intentionally excluded — the agent drills down
    via `get_index(depth=N, path=...)`, `search_entries`, and `get_entry`.

    A fresh org with zero published entries returns a well-formed manifest
    with zeroed counts and empty lists, not a crash.
    """
    async with get_db(user) as conn:
        # -------- counts + last_updated ---------------------------------
        cur = await conn.execute(
            "SELECT COUNT(*), MAX(updated_at) FROM entries WHERE status = 'published'"
        )
        row = await cur.fetchone()
        total = row[0] or 0
        last_updated = row[1].isoformat() if row[1] else None

        # -------- category counts ---------------------------------------
        cur = await conn.execute(
            """
            SELECT content_type, count(*) AS count
            FROM entries WHERE status = 'published'
            GROUP BY content_type ORDER BY count DESC
            """
        )
        cur.row_factory = dict_row
        categories = await cur.fetchall()

        # -------- top-level logical_path buckets ------------------------
        # Bucket by first path segment (everything up to the first '/').
        # Entries with no slash bucket under their full logical_path.
        # Ordered by count desc, capped at TOP_PATHS_LIMIT.
        cur = await conn.execute(
            """
            SELECT
              CASE
                WHEN position('/' IN logical_path) > 0
                  THEN split_part(logical_path, '/', 1)
                ELSE logical_path
              END AS prefix,
              count(*) AS count
            FROM entries
            WHERE status = 'published' AND logical_path IS NOT NULL
            GROUP BY prefix
            ORDER BY count DESC, prefix ASC
            LIMIT %s
            """,
            (TOP_PATHS_LIMIT,),
        )
        cur.row_factory = dict_row
        top_path_rows = await cur.fetchall()
        top_paths = [
            {"logical_path_prefix": r["prefix"], "count": r["count"]}
            for r in top_path_rows
        ]

        # -------- system entries (handles only, no content) -------------
        cur = await conn.execute(
            """
            SELECT id, title, logical_path
            FROM entries
            WHERE status = 'published' AND content_type = 'system'
            ORDER BY logical_path
            """
        )
        cur.row_factory = dict_row
        system_rows = await cur.fetchall()
        system_entries = [
            {
                "id": str(r["id"]),
                "title": r["title"],
                "logical_path": r["logical_path"],
            }
            for r in system_rows
        ]

        # -------- pending reviews (unchanged from prior shape) ----------
        cur = await conn.execute(
            """
            SELECT id, target_path, change_type, submission_category,
                   governance_tier, priority, created_at, submitted_by
            FROM staging
            WHERE status = 'pending'
              AND governance_tier >= 3
              AND org_id = %s
            ORDER BY priority ASC, created_at ASC
            LIMIT 20
            """,
            (user.org_id,),
        )
        cur.row_factory = dict_row
        pending_rows = await cur.fetchall()

        now = datetime.now(timezone.utc)
        pending_reviews = {
            "count": len(pending_rows),
            "items": [
                {
                    "id": str(r["id"]),
                    "target_path": r["target_path"],
                    "change_type": r["change_type"],
                    "governance_tier": r["governance_tier"],
                    "submitted_by": str(r["submitted_by"]),
                    "age_hours": round(
                        (now - r["created_at"].replace(tzinfo=timezone.utc)).total_seconds() / 3600,
                        1,
                    ),
                }
                for r in pending_rows[:5]
            ],
            "review_url": "/staging?status=pending&tier_gte=3",
        }

        # -------- hints -------------------------------------------------
        hints = _build_hints(total, top_paths, system_entries, pending_reviews)

        return {
            "manifest": {
                "total_entries": total,
                "last_updated": last_updated,
                "user": {
                    "id": user.id,
                    "display_name": user.display_name,
                    "role": user.role,
                    "department": user.department,
                    "source": user.source,
                },
                "categories": categories,
                "top_paths": top_paths,
                "system_entries": system_entries,
                "pending_reviews": pending_reviews,
                "hints": hints,
            }
        }


def _build_hints(
    total: int,
    top_paths: list[dict],
    system_entries: list[dict],
    pending_reviews: dict,
) -> list[str]:
    """Produce a short, budgeted list of drill-down suggestions.

    Hints are strings (not structured actions) so the agent sees them
    inline with the manifest. Keep each hint under one sentence.
    """
    hints: list[str] = []

    if total == 0:
        hints.append(
            "KB is empty — create entries with create_entry or import a vault via import_vault."
        )
        return hints

    if top_paths:
        first = top_paths[0]["logical_path_prefix"]
        hints.append(
            f"call get_index(depth=3, path='{first}/') to see titles and relationships under '{first}/'"
        )

    hints.append(
        "call search_entries(q=...) for keyword lookup; get_entry(id) for full content"
    )

    if system_entries:
        hints.append(
            "read system rules with get_entry(id) on manifest.system_entries — content is omitted here"
        )

    if pending_reviews.get("count", 0) > 0:
        hints.append(
            "surface manifest.pending_reviews in your standup — there are Tier 3+ items awaiting review"
        )

    return hints
