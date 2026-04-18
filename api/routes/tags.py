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

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from auth import UserContext, get_current_user
from database import get_db


router = APIRouter(tags=["tags"])


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
