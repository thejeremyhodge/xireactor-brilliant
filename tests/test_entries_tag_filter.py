"""Tests for multi-tag AND filter on GET /entries (spec 0041, T-0240).

Covers:
  1. Multi-tag AND semantics — entry tagged [a, b, c] matches tags=[a, b]
     but not tags=[a, d] (missing tag in entry).
  2. Back-compat — the pre-existing `?tag=X` single filter continues to
     behave exactly as it did before T-0240 landed.
  3. Mutual exclusion — sending both `tag` and `tags` simultaneously
     returns 422 with a message naming both params.

Prerequisites:
  1. docker compose up -d --build   (API on :8010, Postgres on :5442)
  2. pip install -r tests/requirements-dev.txt

Run:
  pytest tests/test_entries_tag_filter.py -v
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


def _create(title: str, logical_path: str, tags: list[str]) -> dict:
    body = {
        "title": title,
        "content": f"Tag-filter test entry {title}.",
        "content_type": "context",
        "logical_path": logical_path,
        "sensitivity": "shared",
        "tags": tags,
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


def _search(params: dict) -> tuple[int, dict]:
    """GET /entries with the given params; return (status_code, json)."""
    r = requests.get(
        f"{BASE_URL}/entries",
        headers=_headers(ADMIN_KEY),
        params=params,
        timeout=REQUEST_TIMEOUT,
    )
    try:
        body = r.json()
    except Exception:
        body = {"_raw": r.text}
    return r.status_code, body


@pytest.fixture
def tagged_entries():
    """Seed three entries with overlapping tag sets.

    Uses a unique-per-run suffix on every tag so the filter is guaranteed to
    return ONLY our seeded rows (no pollution from the shared KB). This keeps
    the test deterministic against any demo seed or prior-run state.
    """
    suffix = uuid.uuid4().hex[:10]
    t_a = f"tag-a-{suffix}"
    t_b = f"tag-b-{suffix}"
    t_c = f"tag-c-{suffix}"
    t_d = f"tag-d-{suffix}"

    abc = _create(
        title=f"ABC entry {suffix}",
        logical_path=f"tagfilter/abc-{suffix}",
        tags=[t_a, t_b, t_c],
    )
    ab = _create(
        title=f"AB entry {suffix}",
        logical_path=f"tagfilter/ab-{suffix}",
        tags=[t_a, t_b],
    )
    a_only = _create(
        title=f"A-only entry {suffix}",
        logical_path=f"tagfilter/a-{suffix}",
        tags=[t_a],
    )

    try:
        yield {
            "suffix": suffix,
            "tags": {"a": t_a, "b": t_b, "c": t_c, "d": t_d},
            "entries": {"abc": abc, "ab": ab, "a_only": a_only},
        }
    finally:
        for e in (abc, ab, a_only):
            _archive(e["id"])


def test_tags_and_semantics_matches_superset(tagged_entries):
    """tags=[a, b] must match entries containing BOTH tags (superset semantics)."""
    t = tagged_entries["tags"]
    e = tagged_entries["entries"]

    status, body = _search({"tags": [t["a"], t["b"]]})
    assert status == 200, f"expected 200, got {status} body={body}"

    ids = {row["id"] for row in body["entries"]}
    # Both [a, b, c] and [a, b] are supersets of {a, b} → included.
    assert e["abc"]["id"] in ids, f"ABC (tags a,b,c) must match tags=[a,b]; got ids={ids}"
    assert e["ab"]["id"] in ids, f"AB (tags a,b) must match tags=[a,b]; got ids={ids}"
    # [a] alone is NOT a superset of {a, b} → excluded.
    assert e["a_only"]["id"] not in ids, (
        f"A-only (tags [a]) must NOT match tags=[a,b] under AND semantics; got ids={ids}"
    )


def test_tags_and_semantics_excludes_missing_tag(tagged_entries):
    """tags=[a, d] must return zero of our seeded entries — none contain `d`."""
    t = tagged_entries["tags"]
    e = tagged_entries["entries"]

    status, body = _search({"tags": [t["a"], t["d"]]})
    assert status == 200, f"expected 200, got {status} body={body}"

    seeded_ids = {e["abc"]["id"], e["ab"]["id"], e["a_only"]["id"]}
    returned_ids = {row["id"] for row in body["entries"]}
    overlap = seeded_ids & returned_ids
    assert overlap == set(), (
        f"No seeded entry has tag `d`, so tags=[a,d] must return none of them; "
        f"got overlap={overlap}"
    )


def test_singular_tag_back_compat(tagged_entries):
    """The pre-existing `?tag=X` single filter must keep working unchanged."""
    t = tagged_entries["tags"]
    e = tagged_entries["entries"]

    status, body = _search({"tag": t["b"]})
    assert status == 200, f"expected 200, got {status} body={body}"

    ids = {row["id"] for row in body["entries"]}
    # Both ABC and AB carry tag `b`; A-only does not.
    assert e["abc"]["id"] in ids
    assert e["ab"]["id"] in ids
    assert e["a_only"]["id"] not in ids


def test_tag_and_tags_mutually_exclusive_returns_422(tagged_entries):
    """Sending both `tag` and `tags` simultaneously must fail fast (422)."""
    t = tagged_entries["tags"]
    status, body = _search({"tag": t["a"], "tags": [t["a"], t["b"]]})
    assert status == 422, f"expected 422 on mutual exclusion, got {status} body={body}"

    # The error message must name BOTH params so the caller can fix their
    # request without guessing which one to drop.
    detail = str(body.get("detail", body)).lower()
    assert "tag" in detail and "tags" in detail, (
        f"422 detail must name both `tag` and `tags`; got detail={body.get('detail')!r}"
    )
