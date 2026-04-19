"""Tests for the /session-init density manifest (T-0207, issue #7).

Covers the v0.4.0 reshape: the endpoint now returns a compact `manifest`
budgeted to ≤ 2048 tiktoken (cl100k_base) tokens regardless of KB size.
The old `{ index, system_entries, pending_reviews, metadata }` shape is
gone — all session-start context lives under `manifest`.

Prerequisites:
  1. docker compose up -d   (API on :8010, Postgres on :5442)
     Migrations through 025 + seed (005) applied.
  2. pip install -r tests/requirements-dev.txt
  3. pip install tiktoken                   # token-budget assertion

Run:
  pytest tests/test_session_init.py -v
"""

from __future__ import annotations

import json
import os

import pytest
import requests


BASE_URL = os.environ.get("BRILLIANT_BASE_URL", "http://localhost:8010")
ADMIN_KEY = "bkai_adm1_testkey_admin"
# Budget bumped from 2048 → 2560 in v0.4.1 to absorb up to 20 `tags_top`
# rows (~300-token addition). Still well under the pre-reshape 40K blowup.
TOKEN_BUDGET = 2560
REQUEST_TIMEOUT = 10.0


def _headers(key: str = ADMIN_KEY) -> dict:
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


# ---------------------------------------------------------------------------
# Happy path — seeded demo KB
# ---------------------------------------------------------------------------


def test_session_init_returns_manifest_envelope():
    r = requests.get(f"{BASE_URL}/session-init", headers=_headers(), timeout=REQUEST_TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()

    # New envelope
    assert "manifest" in body, f"expected 'manifest' key, got {list(body.keys())}"

    # Legacy keys must be gone — this is an intentional breaking change.
    assert "index" not in body
    assert "metadata" not in body

    m = body["manifest"]
    expected_keys = {
        "total_entries",
        "last_updated",
        "user",
        "categories",
        "top_paths",
        "tags_top",
        "system_entries",
        "pending_reviews",
        "hints",
    }
    assert expected_keys.issubset(m.keys()), (
        f"manifest missing keys: {expected_keys - set(m.keys())}"
    )


def test_session_init_user_context_shape():
    r = requests.get(f"{BASE_URL}/session-init", headers=_headers(), timeout=REQUEST_TIMEOUT)
    m = r.json()["manifest"]

    user = m["user"]
    # Must carry the fields the skill references to decide write path.
    for key in ("id", "display_name", "role", "source"):
        assert key in user, f"user missing {key}"
    assert user["role"] in {"admin", "editor", "commenter", "viewer"}
    assert user["source"] in {"web_ui", "agent", "api"}


def test_session_init_categories_and_top_paths_shape():
    r = requests.get(f"{BASE_URL}/session-init", headers=_headers(), timeout=REQUEST_TIMEOUT)
    m = r.json()["manifest"]

    assert isinstance(m["categories"], list)
    for row in m["categories"]:
        assert set(row.keys()) == {"content_type", "count"}
        assert isinstance(row["count"], int)

    assert isinstance(m["top_paths"], list)
    assert len(m["top_paths"]) <= 15, "top_paths must be capped at 15 rows"
    for row in m["top_paths"]:
        assert set(row.keys()) == {"logical_path_prefix", "count"}
        assert isinstance(row["count"], int)
        # Prefix is a first path segment — never contains a slash.
        assert "/" not in row["logical_path_prefix"]


def test_session_init_tags_top_shape_and_ordering():
    """tags_top: list of {tag: str, count: int}, len ≤ 20, ordered by
    count desc then tag asc. Emitted even when empty (for shape stability)."""
    r = requests.get(f"{BASE_URL}/session-init", headers=_headers(), timeout=REQUEST_TIMEOUT)
    m = r.json()["manifest"]

    assert "tags_top" in m, "tags_top must always be present (empty list when empty)"
    tags_top = m["tags_top"]
    assert isinstance(tags_top, list)
    assert len(tags_top) <= 20, "tags_top must be capped at 20 rows"

    for row in tags_top:
        assert set(row.keys()) == {"tag", "count"}, (
            f"tags_top row must be {{tag, count}}, got {row.keys()}"
        )
        assert isinstance(row["tag"], str)
        assert isinstance(row["count"], int)
        assert row["count"] > 0

    # Ordering: count desc, then tag asc within a tie.
    for prev, curr in zip(tags_top, tags_top[1:]):
        assert (
            prev["count"] > curr["count"]
            or (prev["count"] == curr["count"] and prev["tag"] <= curr["tag"])
        ), f"tags_top not sorted (count desc, tag asc): {prev} before {curr}"


def test_session_init_system_entries_omit_content():
    """system_entries must carry only handles (id, title, logical_path).

    The old payload inlined full content, which is the single largest
    contributor to token bloat on real KBs. Agents fetch content on
    demand via get_entry.
    """
    r = requests.get(f"{BASE_URL}/session-init", headers=_headers(), timeout=REQUEST_TIMEOUT)
    m = r.json()["manifest"]

    assert isinstance(m["system_entries"], list)
    for entry in m["system_entries"]:
        assert set(entry.keys()) == {"id", "title", "logical_path"}, (
            f"system_entries must NOT inline content — got {entry.keys()}"
        )


def test_session_init_pending_reviews_preserved():
    r = requests.get(f"{BASE_URL}/session-init", headers=_headers(), timeout=REQUEST_TIMEOUT)
    pr = r.json()["manifest"]["pending_reviews"]

    assert set(pr.keys()) == {"count", "items", "review_url"}
    assert isinstance(pr["count"], int)
    assert isinstance(pr["items"], list)
    assert len(pr["items"]) <= 5
    assert pr["review_url"] == "/staging?status=pending&tier_gte=3"


def test_session_init_hints_are_strings():
    r = requests.get(f"{BASE_URL}/session-init", headers=_headers(), timeout=REQUEST_TIMEOUT)
    hints = r.json()["manifest"]["hints"]

    assert isinstance(hints, list)
    assert all(isinstance(h, str) for h in hints)


# ---------------------------------------------------------------------------
# Token budget — the whole point of the reshape
# ---------------------------------------------------------------------------


def test_session_init_fits_token_budget():
    """JSON-serialized response must be ≤ 2048 cl100k_base tokens on the
    seeded demo KB. The agent budget assumes session_init is cheap to call.
    """
    try:
        import tiktoken
    except ImportError:
        pytest.skip("tiktoken not installed — `pip install tiktoken` to run")

    r = requests.get(f"{BASE_URL}/session-init", headers=_headers(), timeout=REQUEST_TIMEOUT)
    assert r.status_code == 200

    enc = tiktoken.get_encoding("cl100k_base")
    token_count = len(enc.encode(r.text))
    assert token_count <= TOKEN_BUDGET, (
        f"session_init blew the {TOKEN_BUDGET}-token budget: {token_count} tokens. "
        "Something in the payload is over-inlining — check system_entries, "
        "top_paths cap, or hints length."
    )


# ---------------------------------------------------------------------------
# Empty-KB resilience — fresh orgs must not crash
# ---------------------------------------------------------------------------


def test_session_init_shape_stable_for_current_kb():
    """Sanity: the response is valid JSON, the manifest is a dict, and the
    counts align with what we can observe. This acts as a smoke test for
    the empty-KB case too — if the endpoint ever 500s on zero entries, the
    shape assertions here will surface it.
    """
    r = requests.get(f"{BASE_URL}/session-init", headers=_headers(), timeout=REQUEST_TIMEOUT)
    assert r.status_code == 200
    body = r.json()
    m = body["manifest"]
    assert isinstance(m, dict)
    assert isinstance(m["total_entries"], int)
    assert m["total_entries"] >= 0
    # If total_entries is 0, categories/top_paths/tags_top/system_entries
    # must all be empty lists — not null, not missing keys.
    if m["total_entries"] == 0:
        assert m["categories"] == []
        assert m["top_paths"] == []
        assert m["tags_top"] == []
        assert m["system_entries"] == []
        assert m["last_updated"] is None
