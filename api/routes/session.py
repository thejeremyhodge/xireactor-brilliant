"""Session initialization endpoint — density manifest for agent session start.

Returns a compact manifest (~≤ 2K tokens) instead of the full tiered index.
The agent is expected to drill down with `get_index(depth=N, path=...)`,
`search_entries`, and `get_entry` once it has seen the manifest. The old
payload inlined every entry, every relationship, every summary, and the
full content of every `system` entry — that routinely ballooned past 40K
tokens on real-sized KBs and broke Claude Code sessions.

New shape (v0.4.0 breaking change, + `tags_top` in v0.4.1):

    {
      "manifest": {
        "total_entries": <int>,
        "last_updated":  <iso8601 | null>,
        "user": {id, display_name, role, department, source},
        "categories":    [{content_type, count}, ...],
        "top_paths":     [{logical_path_prefix, count}, ...],   # up to 15
        "tags_top":      [{tag, count}, ...],                    # up to 20
        "system_entries":[{id, title, logical_path}, ...],       # no content
        "pending_reviews": {count, items[0..5], review_url},
        "hints": ["call get_index(depth=3, path='Projects/') ...", ...]
      }
    }
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from psycopg.rows import dict_row

from auth import UserContext, get_current_user
from database import get_db
from services import lod as lod_service
from services.motifs import count_motifs

router = APIRouter(tags=["session"])

# Manifest versions supported by this endpoint. v1 is the byte-stable
# pre-Sprint-0049 shape; v2 layers structural / heat / motifs blocks on top
# without removing any v1 keys (skill clients pinned to v1 see no diff).
SUPPORTED_MANIFEST_VERSIONS: tuple[int, ...] = (1, 2)

# Hard cap on top_paths rows — the agent can walk past the horizon with
# get_index(path=...), so there is no value in dumping the long tail here.
TOP_PATHS_LIMIT = 15

# Hard cap on tags_top rows. Bounds the manifest's tag-triangulation surface
# to a ~300-token footprint regardless of corpus size; the agent fetches the
# full tag corpus via GET /tags / list_tags() when it needs more.
TAGS_TOP_LIMIT = 20


@router.get("")
async def session_init(
    user: UserContext = Depends(get_current_user),
    manifest_version: Optional[int] = Query(
        None,
        description="Manifest schema version. Supported: 1 (default), 2.",
    ),
    x_manifest_version: Optional[int] = Header(
        None,
        alias="X-Manifest-Version",
        description="Alternative to ?manifest_version= query param.",
    ),
):
    """Return a compact density manifest for agent session start.

    The manifest is designed to fit under ~2K tokens regardless of KB size:
    it carries counts, top-level path buckets, system-entry handles, and
    pending-review previews. Full entry content, summaries, and the
    relationship graph are intentionally excluded — the agent drills down
    via `get_index(depth=N, path=...)`, `search_entries`, and `get_entry`.

    A fresh org with zero published entries returns a well-formed manifest
    with zeroed counts and empty lists, not a crash.

    Manifest version negotiation: query param wins over header; absence of
    both → v1 (byte-identical to the pre-Sprint-0049 shape). v2 layers
    structural / heat / motifs blocks on top without dropping any v1 key.
    """
    # Query param wins over header (explicit beats implicit). Either may be
    # None; fall through to default v1.
    requested_version = manifest_version if manifest_version is not None else x_manifest_version
    if requested_version is None:
        requested_version = 1

    if requested_version not in SUPPORTED_MANIFEST_VERSIONS:
        supported = ", ".join(str(v) for v in SUPPORTED_MANIFEST_VERSIONS)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported manifest_version={requested_version}. "
                f"Supported: {supported}."
            ),
        )

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

        # -------- top tags by published-entry count ---------------------
        # Gives the agent the tag shape of the KB at session start so it
        # can triangulate without fetching entries first. `tags` is TEXT[]
        # with a GIN index (db/migrations/001_core.sql); unnest + GROUP BY
        # on a ~5K-entry KB measures flat at sub-10ms locally.
        #
        # Empty-KB behavior: we ALWAYS emit `tags_top` (empty list when no
        # tags exist) for shape consistency with `categories` / `top_paths`
        # — the existing empty-KB test pattern in tests/test_session_init.py
        # asserts those are `[]`, not missing. Staying consistent means
        # agents / frontends never have to guard on key presence.
        cur = await conn.execute(
            """
            SELECT unnest(tags) AS tag, count(*) AS count
            FROM entries
            WHERE status = 'published'
            GROUP BY tag
            ORDER BY count DESC, tag ASC
            LIMIT %s
            """,
            (TAGS_TOP_LIMIT,),
        )
        cur.row_factory = dict_row
        tags_top_rows = await cur.fetchall()
        tags_top = [
            {"tag": r["tag"], "count": r["count"]}
            for r in tags_top_rows
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
        hints = _build_hints(total, top_paths, tags_top, system_entries, pending_reviews)

        manifest: dict = {
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
            "tags_top": tags_top,
            "system_entries": system_entries,
            "pending_reviews": pending_reviews,
            "hints": hints,
        }

        # v1 path: byte-identical to pre-Sprint-0049 output. No new keys,
        # no manifest_version field. Skill clients pinned to v1 see no diff.
        if requested_version == 1:
            return {"manifest": manifest}

        # v2 path: layer structural/heat/motifs blocks plus an explicit
        # `manifest_version: 2` marker. Reuses the same RLS-scoped conn —
        # all aggregates respect the caller's visibility.
        edge_count = await lod_service.get_edge_count(conn)
        relation_hist = await lod_service.get_relation_type_histogram(conn)
        degree_bins = await lod_service.get_degree_bins(conn)
        orphan_count = await lod_service.get_orphan_count(conn)
        size_dist = await lod_service.get_size_distribution(conn)
        heat_bands = await lod_service.get_heat_bands(conn)
        motifs = await count_motifs(conn)

        manifest["manifest_version"] = 2
        manifest["structural"] = {
            "edge_count": edge_count,
            "relation_type_histogram": relation_hist,
            "degree_bins": degree_bins,
            "orphan_count": orphan_count,
            "size_distribution": size_dist,
        }
        manifest["heat"] = heat_bands
        manifest["motifs"] = motifs

        return {"manifest": manifest}


def _build_hints(
    total: int,
    top_paths: list[dict],
    tags_top: list[dict],
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

    if tags_top:
        first_tag = tags_top[0]["tag"]
        hints.append(
            f"triangulate by tag: call search_entries(tags=['{first_tag}']) — see manifest.tags_top for the top 20"
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
