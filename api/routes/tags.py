"""Tag suggestion endpoints (spec 0037, T-0210).

Provides `POST /tags/suggest` — a deterministic ranker over the caller's
existing tag corpus (drawn from `entries.tags`). No LLM, no pgvector. RLS
handles tenant scoping through `get_db(user)`, so callers only ever see
their own org's vocabulary.

Ranking is case-insensitive substring match with a whole-word bonus,
multiplied by `log(1 + usage_count)` so well-used tags outrank rarities
when both are present in the content. Tags with score 0 are dropped;
ties break by usage_count then alphabetically for stability.
"""

from __future__ import annotations

import math
import re

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from auth import UserContext, get_current_user
from database import get_db
from models import (
    TagCoOccurrence,
    TagCoOccurrenceResponse,
    TagListResponse,
    TagWithCount,
)


router = APIRouter(tags=["tags"])

# Default/max page sizes for GET /tags. Defaults generous because the
# full corpus is typically small (hundreds), but the max keeps the
# endpoint from being weaponized on very large KBs.
_TAGS_LIST_DEFAULT_LIMIT = 500
_TAGS_LIST_MAX_LIMIT = 5000

# Default/max cap for the co-occurrence endpoint. 10 is enough for a
# triangulation "pick your next drill axis" UX without bloating the
# response; 100 keeps the absolute ceiling sane on very broad tag
# neighborhoods.
_CO_OCCURRING_DEFAULT_LIMIT = 10
_CO_OCCURRING_MAX_LIMIT = 100


class SuggestTagsRequest(BaseModel):
    content: str
    limit: int = Field(default=10, ge=1, le=100)


class TagSuggestion(BaseModel):
    tag: str
    score: float
    usage_count: int


class SuggestTagsResponse(BaseModel):
    suggestions: list[TagSuggestion]


def _score_tag(tag: str, usage_count: int, content_lc: str) -> float:
    """Score a single candidate tag against the (lowercased) content.

    Scoring rules (cheap, deterministic, no LLM):
      * substring match (case-insensitive) contributes 1.0
      * a whole-word match (bounded by non-word chars) adds a 1.0 bonus
      * tags that are empty or not substrings contribute 0
      * final score is the match signal multiplied by log(1 + usage_count)
        so popular tags outrank rarities when both fire

    The whole-word check uses ``re.escape`` so tags containing regex
    metacharacters (e.g. ``c++``, ``q3.5``) do not blow up.
    """
    tag_lc = tag.strip().lower()
    if not tag_lc:
        return 0.0

    if tag_lc not in content_lc:
        return 0.0

    # Substring hit baseline.
    base = 1.0

    # Whole-word bonus — ``\b`` is weak for tags ending in punctuation, so
    # we flank with an explicit non-word class. Anchors at string edges
    # also count as word boundaries.
    pattern = r"(?:^|[^A-Za-z0-9_])" + re.escape(tag_lc) + r"(?:$|[^A-Za-z0-9_])"
    if re.search(pattern, content_lc):
        base += 1.0

    return base * math.log(1 + max(usage_count, 0))


@router.get("", response_model=TagListResponse)
async def list_tags(
    limit: int = Query(
        default=_TAGS_LIST_DEFAULT_LIMIT,
        ge=1,
        le=_TAGS_LIST_MAX_LIMIT,
    ),
    offset: int = Query(default=0, ge=0),
    user: UserContext = Depends(get_current_user),
) -> TagListResponse:
    """List the caller's full tag corpus with per-tag usage counts.

    Paginated via ``limit`` (default 500, max 5000) and ``offset``.
    Sorted by ``count`` descending, then ``tag`` ascending (stable across
    calls). RLS scopes the corpus to the caller's org — cross-org tags
    never leak into the response.

    The ``total`` field reflects the number of **distinct tags** visible
    to the caller, independent of pagination. Empty-corpus orgs (no
    published entries, or no entries with tags) return 200 with
    ``{"tags": [], "total": 0}`` — never a 500.

    Complements ``session_init.manifest.tags_top`` (capped at 20): use the
    manifest for the initial snapshot and this endpoint to drill into the
    long tail of the vocabulary.
    """
    async with get_db(user) as conn:
        # Distinct-tag total first so pagination math doesn't depend on
        # slicing the aggregate. Two queries keep the plan simple and
        # leverage the GIN index on ``tags`` (db/migrations/001_core.sql).
        cur = await conn.execute(
            """
            SELECT count(DISTINCT tag) AS total
            FROM (
              SELECT unnest(tags) AS tag
              FROM entries
              WHERE status = 'published'
            ) AS t
            WHERE tag IS NOT NULL AND tag <> ''
            """
        )
        total_row = await cur.fetchone()
        total = int(total_row[0]) if total_row and total_row[0] is not None else 0

        # Empty-corpus short-circuit: skip the paginated query entirely so
        # the endpoint cannot 500 on a zero-tag org. This also avoids
        # returning empty result sets with stale pagination metadata.
        if total == 0:
            return TagListResponse(tags=[], total=0)

        cur = await conn.execute(
            """
            SELECT tag, count(*) AS count
            FROM (
              SELECT unnest(tags) AS tag
              FROM entries
              WHERE status = 'published'
            ) AS t
            WHERE tag IS NOT NULL AND tag <> ''
            GROUP BY tag
            ORDER BY count DESC, tag ASC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        rows = await cur.fetchall()

    tags = [
        TagWithCount(tag=row[0], count=int(row[1]))
        for row in rows
        if row[0]  # belt-and-suspenders: filter blank tags
    ]
    return TagListResponse(tags=tags, total=total)


@router.get("/{tag}/co-occurring", response_model=TagCoOccurrenceResponse)
async def co_occurring_tags(
    tag: str,
    limit: int = Query(
        default=_CO_OCCURRING_DEFAULT_LIMIT,
        ge=1,
        le=_CO_OCCURRING_MAX_LIMIT,
    ),
    user: UserContext = Depends(get_current_user),
) -> TagCoOccurrenceResponse:
    """List tags that frequently co-occur with ``tag`` on the same entry.

    For every published entry that carries the target tag ``A``, unnest
    every other tag on that same entry and count how often each one
    appears. Returns up to ``limit`` neighbors ranked by:

      1. ``co_count`` desc (raw overlap with ``A``)
      2. ``jaccard`` desc (overlap normalised by union size, so a rare
         tag that always co-occurs with ``A`` can outrank a common tag
         that happens to be everywhere)
      3. ``tag`` asc (stable tie-break)

    ``jaccard`` is computed as ``|A ∩ B| / |A ∪ B|``, i.e.
    ``co_count / (A_total + B_total - co_count)`` where ``A_total`` is
    the number of entries with ``A`` and ``B_total`` the number of
    entries with ``B``. The value is always in ``[0.0, 1.0]``.

    Unknown tag / empty corpus
    --------------------------
    A tag that no entry carries returns
    ``{"tag": "<tag>", "neighbors": []}`` with status 200 — treated as
    an empty co-occurrence set, consistent with ``GET /tags`` on empty
    orgs. No 404.

    RLS scopes both the target-tag set and the candidate-tag set to the
    caller's org, so cross-org neighbors never leak. Drafts and
    archived entries are excluded.
    """
    # Computation strategy
    # --------------------
    # We want a single query so the neighbor aggregation, the A_total
    # count, and the B_total counts all share the same RLS-scoped
    # snapshot of ``entries``. A multi-query approach would be simpler
    # to read but would need explicit `SET LOCAL ROLE` coordination
    # across each statement — the single-query path inherits the
    # session's role once.
    #
    # The query has three CTEs:
    #   1. ``target`` — entries carrying the target tag (``A``) plus
    #      their full tag arrays. Also gives us ``A_total``.
    #   2. ``co`` — unnest ``target.tags``, drop the target tag itself,
    #      count by candidate tag. ``co_count`` per candidate.
    #   3. ``b_totals`` — for each candidate tag, count how many
    #      published entries (org-wide) carry it. This is ``B_total``.
    #
    # Jaccard is computed inline in the final SELECT. Division-by-zero
    # is impossible here because if a candidate tag shows up in the
    # ``co`` CTE at all, there is at least one entry (the target one)
    # that carries it, so ``B_total >= 1`` and the denominator
    # ``A_total + B_total - co_count >= 1``.
    async with get_db(user) as conn:
        cur = await conn.execute(
            """
            WITH target AS (
                SELECT id, tags
                FROM entries
                WHERE status = 'published'
                  AND tags @> ARRAY[%s]::text[]
            ),
            a_total AS (
                SELECT count(*)::bigint AS n FROM target
            ),
            co AS (
                SELECT other_tag, count(*)::bigint AS co_count
                FROM (
                    SELECT unnest(tags) AS other_tag
                    FROM target
                ) AS u
                WHERE other_tag IS NOT NULL
                  AND other_tag <> ''
                  AND other_tag <> %s
                GROUP BY other_tag
            ),
            b_totals AS (
                SELECT tag AS other_tag, count(*)::bigint AS b_total
                FROM (
                    SELECT unnest(tags) AS tag
                    FROM entries
                    WHERE status = 'published'
                ) AS t
                WHERE tag IS NOT NULL AND tag <> ''
                GROUP BY tag
            )
            SELECT
                co.other_tag,
                co.co_count,
                (a_total.n + b_totals.b_total - co.co_count) AS union_size,
                b_totals.b_total
            FROM co
            JOIN b_totals USING (other_tag)
            CROSS JOIN a_total
            ORDER BY
                co.co_count DESC,
                -- Jaccard desc as secondary sort so a rare-but-strongly-
                -- linked neighbor outranks a ubiquitous tag with the same
                -- co_count. Computed inline to keep the ORDER BY in sync
                -- with the ranking contract documented on the endpoint.
                (co.co_count::numeric /
                 NULLIF(a_total.n + b_totals.b_total - co.co_count, 0)) DESC,
                co.other_tag ASC
            LIMIT %s
            """,
            (tag, tag, limit),
        )
        rows = await cur.fetchall()

    neighbors: list[TagCoOccurrence] = []
    for row in rows:
        other_tag = row[0]
        co_count = int(row[1])
        union_size = int(row[2]) if row[2] is not None else 0
        # Defensive: union_size should always be >= co_count >= 1 for
        # any row we get back. Guard against a pathological zero-union
        # anyway so a bad SQL plan can never 500 the endpoint.
        jaccard = (co_count / union_size) if union_size > 0 else 0.0
        # Clamp to [0.0, 1.0] so numeric drift can never embarrass us
        # downstream (e.g. a 1.0000000001 would fail strict typing in
        # some clients).
        if jaccard < 0.0:
            jaccard = 0.0
        elif jaccard > 1.0:
            jaccard = 1.0
        neighbors.append(
            TagCoOccurrence(
                tag=other_tag,
                co_count=co_count,
                jaccard=round(jaccard, 4),
            )
        )

    return TagCoOccurrenceResponse(tag=tag, neighbors=neighbors)


@router.post("/suggest", response_model=SuggestTagsResponse)
async def suggest_tags(
    body: SuggestTagsRequest,
    user: UserContext = Depends(get_current_user),
) -> SuggestTagsResponse:
    """Rank the caller's existing tag vocabulary against ``body.content``.

    Returns up to ``limit`` tags with score > 0, sorted by score desc.
    Empty-corpus orgs get ``{"suggestions": []}`` — never a 500.
    """
    content_lc = (body.content or "").lower()
    if not content_lc.strip():
        return SuggestTagsResponse(suggestions=[])

    async with get_db(user) as conn:
        # RLS scopes this to the caller's org automatically; no explicit
        # org_id filter needed. ``status='published'`` keeps drafts and
        # archived noise out of the suggestion pool.
        cur = await conn.execute(
            """
            SELECT unnest(tags) AS tag, count(*) AS usage_count
            FROM entries
            WHERE status = 'published'
            GROUP BY tag
            """
        )
        rows = await cur.fetchall()

    scored: list[tuple[float, int, str]] = []
    for tag, usage_count in rows:
        if not tag:
            continue
        score = _score_tag(tag, int(usage_count), content_lc)
        if score > 0:
            scored.append((score, int(usage_count), tag))

    # Sort: highest score first, then heaviest usage, then alphabetical.
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))

    suggestions = [
        TagSuggestion(tag=t, score=round(s, 4), usage_count=u)
        for s, u, t in scored[: body.limit]
    ]
    return SuggestTagsResponse(suggestions=suggestions)
