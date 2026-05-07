"""Markdown section outline parser for LOD6 (Sprint 0049).

Pure-stdlib function over `entries.content`. Extracts ATX headings
(`#`, `##`, ..., `######`) while ignoring `#` characters that fall
inside fenced code blocks (``` or ~~~).

Memoization: entries larger than ``LARGE_ENTRY_THRESHOLD`` go through
an LRU cache keyed on ``(entry_id, updated_at_iso)`` so a row update
naturally invalidates the cached outline. Smaller entries skip the
cache (the parse is cheap and cache pressure isn't worth it).

No I/O, no LLM, no network — safe to call from any context.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

# Entries above this byte-length get the cached parse path.
LARGE_ENTRY_THRESHOLD: int = 10 * 1024  # 10 KB

# Up to 6 leading '#' followed by a space and the heading text.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")

# Fenced code block opener/closer: ``` or ~~~ (optionally with info string).
_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")


def outline(content: str) -> list[dict[str, Any]]:
    """Return a list of ATX heading descriptors for ``content``.

    Each entry is ``{"level": int, "text": str, "line": int}`` where
    ``line`` is 1-indexed.

    Headings inside fenced code blocks (``` or ~~~) are ignored —
    fence state is tracked line-by-line so a single fence opens and
    the matching fence closes regardless of fence character.
    """
    if not content:
        return []

    headings: list[dict[str, Any]] = []
    in_fence = False
    fence_marker: str | None = None

    for idx, line in enumerate(content.splitlines(), start=1):
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = None
            # A fence line is never itself a heading, regardless.
            continue

        if in_fence:
            continue

        m = _HEADING_RE.match(line)
        if not m:
            continue

        hashes, text = m.group(1), m.group(2).strip()
        if not text:
            # Empty heading text ("# ") — skip; pollutes outlines.
            continue
        headings.append({"level": len(hashes), "text": text, "line": idx})

    return headings


@lru_cache(maxsize=128)
def _outline_cached_impl(
    entry_id: str, updated_at_iso: str, content: str
) -> tuple[tuple[int, str, int], ...]:
    """LRU-cached parse. Returns a tuple so the cached value is hashable
    and immutable; ``outline_cached`` re-materializes the dict shape.
    """
    return tuple((h["level"], h["text"], h["line"]) for h in outline(content))


def outline_cached(
    entry_id: str, updated_at: Any, content: str
) -> list[dict[str, Any]]:
    """Outline an entry, using the LRU cache for entries >10KB.

    Cache key is ``(entry_id, updated_at_iso)`` so a row update — which
    bumps ``updated_at`` — naturally invalidates the prior outline.

    For small entries the parse is cheap; we skip the cache to avoid
    eviction pressure on more valuable large-entry parses.
    """
    if content is None:
        return []

    if len(content) <= LARGE_ENTRY_THRESHOLD:
        return outline(content)

    # Normalize updated_at to a stable string. datetime → isoformat();
    # anything else → str() — we only need a deterministic cache key.
    if hasattr(updated_at, "isoformat"):
        updated_at_iso = updated_at.isoformat()
    else:
        updated_at_iso = str(updated_at)

    rows = _outline_cached_impl(str(entry_id), updated_at_iso, content)
    return [{"level": lvl, "text": txt, "line": ln} for (lvl, txt, ln) in rows]
