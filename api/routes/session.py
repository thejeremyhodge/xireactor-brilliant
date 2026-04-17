"""Session initialization endpoint — context bundle for agent session start."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from psycopg.rows import dict_row

from auth import UserContext, get_current_user
from database import get_db

router = APIRouter(tags=["session"])


@router.get("")
async def session_init(
    user: UserContext = Depends(get_current_user),
):
    """Return a pre-assembled context bundle for agent session start.

    Dynamically selects index depth based on KB size:
    - <=50 entries  -> L4 (summaries)
    - <=500 entries -> L3 (relationships)
    - <=5000 entries -> L2 (document index)
    - >5000 entries -> L1 (category counts only)

    Always includes system entries and KB metadata.
    """
    async with get_db(user) as conn:
        # Count total entries
        cur = await conn.execute(
            "SELECT COUNT(*) FROM entries WHERE status = 'published'"
        )
        total = (await cur.fetchone())[0]

        # Select depth based on size
        if total <= 50:
            depth = 4
        elif total <= 500:
            depth = 3
        elif total <= 5000:
            depth = 2
        else:
            depth = 1

        # Get category counts (L1 — always)
        cur = await conn.execute(
            """
            SELECT content_type, count(*) AS count
            FROM entries WHERE status = 'published'
            GROUP BY content_type ORDER BY count DESC
            """
        )
        cur.row_factory = dict_row
        categories = await cur.fetchall()

        # Build index based on depth
        index = {
            "depth": depth,
            "total_entries": total,
            "categories": categories,
        }

        if depth >= 2:
            # L2: document index
            if depth >= 4:
                select = "id, title, content_type, logical_path, summary, updated_at"
            else:
                select = "id, title, content_type, logical_path, updated_at"

            cur = await conn.execute(
                f"""
                SELECT {select}
                FROM entries WHERE status = 'published'
                ORDER BY logical_path
                """
            )
            cur.row_factory = dict_row
            entries = await cur.fetchall()

            # Convert UUIDs to strings
            index["entries"] = [
                {**e, "id": str(e["id"]), "updated_at": e["updated_at"].isoformat()}
                for e in entries
            ]

            if depth >= 4:
                index["summaries"] = {
                    str(e["id"]): e.get("summary") or "" for e in entries
                }

        if depth >= 3:
            # L3: relationships
            cur = await conn.execute(
                """
                SELECT el.source_entry_id, el.target_entry_id, el.link_type
                FROM entry_links el
                JOIN entries e1 ON e1.id = el.source_entry_id AND e1.status = 'published'
                JOIN entries e2 ON e2.id = el.target_entry_id AND e2.status = 'published'
                """
            )
            cur.row_factory = dict_row
            links = await cur.fetchall()
            index["relationships"] = [
                {
                    "source_id": str(r["source_entry_id"]),
                    "target_id": str(r["target_entry_id"]),
                    "link_type": r["link_type"],
                }
                for r in links
            ]

        # Always include user-authored system entries (rules, conventions under System/*).
        # NOTE: the content-type registry is NOT here — it lives in its own table (see /types).
        cur = await conn.execute(
            """
            SELECT id, title, content, content_type, logical_path
            FROM entries
            WHERE status = 'published' AND content_type = 'system'
            ORDER BY logical_path
            """
        )
        cur.row_factory = dict_row
        system_entries = await cur.fetchall()
        system = [
            {**e, "id": str(e["id"])} for e in system_entries
        ]

        # Get last updated timestamp
        cur = await conn.execute(
            "SELECT MAX(updated_at) FROM entries WHERE status = 'published'"
        )
        last_updated_row = await cur.fetchone()
        last_updated = last_updated_row[0].isoformat() if last_updated_row[0] else None

        # Query pending Tier 3+ staging items for escalation preamble
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

        return {
            "index": index,
            "system_entries": system,
            "pending_reviews": pending_reviews,
            "metadata": {
                "total_entries": total,
                "last_updated": last_updated,
                "user": {
                    "id": user.id,
                    "display_name": user.display_name,
                    "role": user.role,
                    "department": user.department,
                    "source": user.source,
                },
            },
        }
