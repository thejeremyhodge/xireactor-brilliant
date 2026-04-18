"""Tests for the pg_trgm fuzzy-fallback flag on GET /entries (spec 0037, T-0208).

Covers:
  1. Exact-match (fuzzy=false / omitted) returns byte-identical results to
     the pre-migration FTS path — regression guard on the default surface.
  2. Near-miss query ("klaude" against an entry with title/content
     containing "claude") returns ≥ 1 hit with fuzzy=true.
  3. Fuzzy is a PURE fallback: when FTS already has hits, the trigram
     branch is NOT engaged (result set must equal the FTS result set).

Prerequisites:
  1. docker compose up -d --build   (API on :8010, Postgres on :5442)
  2. Migration 026 applied (pg_trgm extension + trigram GIN indexes)
  3. pip install -r tests/requirements-dev.txt

Run:
  pytest tests/test_entries_fuzzy.py -v
"""

from __future__ import annotations

import os
import uuid

import pytest
import requests


BASE_URL = os.environ.get("BRILLIANT_BASE_URL", "http://localhost:8010")
ADMIN_KEY = "bkai_adm1_testkey_admin"
REQUEST_TIMEOUT = 10.0


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _api_available() -> bool:
    try:
        return requests.get(f"{BASE_URL}/health", timeout=2.0).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _api_available(),
    reason=f"Brilliant API not reachable at {BASE_URL} (start `docker compose up -d`).",
)


def _create(title: str, content: str, logical_path: str) -> dict:
    body = {
        "title": title,
        "content": content,
        "content_type": "context",
        "logical_path": logical_path,
        "sensitivity": "shared",
        "tags": ["test", "fuzzy"],
    }
    r = requests.post(
        f"{BASE_URL}/entries",
        headers=_headers(ADMIN_KEY),
        json=body,
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 201, f"create failed: {r.status_code} {r.text}"
    return r.json()


def _archive(entry_id: str) -> None:
    try:
        requests.delete(
            f"{BASE_URL}/entries/{entry_id}",
            headers=_headers(ADMIN_KEY),
            timeout=REQUEST_TIMEOUT,
        )
    except Exception:
        pass


def _search(q: str, *, fuzzy: bool | None = None) -> dict:
    params: dict = {"q": q}
    if fuzzy is not None:
        params["fuzzy"] = "true" if fuzzy else "false"
    r = requests.get(
        f"{BASE_URL}/entries",
        headers=_headers(ADMIN_KEY),
        params=params,
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 200, f"search failed: {r.status_code} {r.text}"
    return r.json()


@pytest.fixture
def claude_entry():
    """Seed an entry whose title + content both mention 'claude' (lowercased
    to match pg_trgm's case-sensitive default). The unique suffix lets us
    run alongside other tests without collisions."""
    suffix = uuid.uuid4().hex[:10]
    entry = _create(
        title=f"Working with claude {suffix}",
        content=(
            "This entry documents how our team uses claude "
            "for pair programming. Claude is an AI assistant."
        ),
        logical_path=f"fuzzy/claude-{suffix}",
    )
    yield entry, suffix
    _archive(entry["id"])


def test_exact_match_default_behavior_unchanged(claude_entry):
    """fuzzy omitted — FTS returns the entry for the exact token 'claude'."""
    entry, _suffix = claude_entry
    result = _search("claude")  # fuzzy omitted -> default False
    ids = {e["id"] for e in result["entries"]}
    assert entry["id"] in ids, (
        "FTS must still find 'claude' entries when fuzzy is omitted; "
        f"got ids={ids}"
    )


def test_exact_match_with_fuzzy_false_unchanged(claude_entry):
    """fuzzy=false is byte-identical to fuzzy omitted for an exact hit."""
    entry, _suffix = claude_entry
    default = _search("claude")
    off = _search("claude", fuzzy=False)
    assert default == off, (
        "fuzzy=false must return byte-identical payload to fuzzy omitted; "
        f"default={default!r} off={off!r}"
    )
    assert entry["id"] in {e["id"] for e in off["entries"]}


def test_near_miss_without_fuzzy_returns_empty(claude_entry):
    """'klaude' is not a recognised token — FTS alone returns zero rows."""
    _entry, _suffix = claude_entry
    result = _search("klaude")
    assert result["total"] == 0, (
        "FTS path must return 0 rows for 'klaude' without fuzzy; "
        f"got total={result['total']}"
    )
    assert result["entries"] == []


def test_near_miss_with_fuzzy_true_finds_claude(claude_entry):
    """fuzzy=true engages the pg_trgm fallback and surfaces the 'claude' entry."""
    entry, _suffix = claude_entry
    result = _search("klaude", fuzzy=True)
    assert result["total"] >= 1, (
        "fuzzy=true must surface 'klaude' → 'claude' near-misses; "
        f"got total={result['total']}"
    )
    ids = {e["id"] for e in result["entries"]}
    assert entry["id"] in ids, (
        f"Expected seeded claude entry {entry['id']} in fuzzy results, "
        f"got ids={ids}"
    )


def test_fuzzy_true_with_exact_hit_does_not_trigger_fallback(claude_entry):
    """When FTS has ≥1 hit, fuzzy=true must return the FTS result set
    (fallback is skipped). Guards against the trigram branch merging or
    reordering exact-match results."""
    entry, _suffix = claude_entry
    fts = _search("claude", fuzzy=False)
    fuzzy = _search("claude", fuzzy=True)
    assert fts == fuzzy, (
        "fuzzy=true must be a pure fallback when FTS has hits; "
        f"fts={fts!r} fuzzy={fuzzy!r}"
    )
    assert entry["id"] in {e["id"] for e in fuzzy["entries"]}
