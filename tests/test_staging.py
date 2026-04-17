"""Tests for the staging / governance pipeline (issue #12, T-0196).

Covers metadata-only staging submissions: a `change_type: update` with
`proposed_meta` but no `proposed_content` must succeed (201) and apply the
new meta fields without disturbing `content`. Previously the NOT NULL
constraint on `staging.proposed_content` surfaced as a bare 500.

Prerequisites:
  1. docker compose up -d   (API on :8010, Postgres on :5442)
     Migrations through 024 must be applied (fresh volume if upgrading).
  2. pip install -r tests/requirements-dev.txt

Run:
  pytest tests/test_staging.py -v
"""

from __future__ import annotations

import os
import uuid

import pytest
import requests


BASE_URL = os.environ.get("CORTEX_BASE_URL", "http://localhost:8010")
ADMIN_KEY = "bkai_adm1_testkey_admin"
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
# Helpers
# ---------------------------------------------------------------------------


def _create_entry(title: str, content: str, logical_path: str, tags=None) -> dict:
    body = {
        "title": title,
        "content": content,
        "content_type": "context",
        "logical_path": logical_path,
        "sensitivity": "shared",
        "tags": tags or ["test", "staging-fixture"],
    }
    r = requests.post(
        f"{BASE_URL}/entries",
        headers=_headers(),
        json=body,
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 201, f"create failed: {r.status_code} {r.text}"
    return r.json()


def _get_entry(entry_id: str) -> dict:
    r = requests.get(
        f"{BASE_URL}/entries/{entry_id}",
        headers=_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _archive(entry_id: str) -> None:
    try:
        requests.delete(
            f"{BASE_URL}/entries/{entry_id}",
            headers=_headers(),
            timeout=REQUEST_TIMEOUT,
        )
    except Exception:
        pass


def _submit_staging(payload: dict) -> requests.Response:
    return requests.post(
        f"{BASE_URL}/staging",
        headers=_headers(),
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )


@pytest.fixture
def fresh_entry():
    suffix = uuid.uuid4().hex[:10]
    entry = _create_entry(
        title=f"staging-target-{suffix}",
        content="Original body — must remain unchanged after meta-only update.",
        logical_path=f"staging-tests/target-{suffix}",
        tags=["original-tag"],
    )
    yield entry
    _archive(entry["id"])


# ---------------------------------------------------------------------------
# Tests — issue #12
# ---------------------------------------------------------------------------


def test_meta_only_update_returns_201_and_preserves_content(fresh_entry):
    """An update with only proposed_meta (no proposed_content) must succeed
    and leave the entry's content untouched after auto-promotion."""
    original_content = fresh_entry["content"]
    new_tags = ["seed", "test-data", uuid.uuid4().hex[:6]]

    r = _submit_staging({
        "target_entry_id": fresh_entry["id"],
        "target_path": fresh_entry["logical_path"],
        "change_type": "update",
        "proposed_meta": {"tags": new_tags},
    })
    assert r.status_code == 201, f"expected 201, got {r.status_code}: {r.text}"
    body = r.json()
    # Admin web_ui keys auto-promote at Tier 1.
    assert body["status"] == "auto_approved", body
    assert body["promoted_entry_id"] == fresh_entry["id"], body

    # Entry now has the new tags but the original content.
    refreshed = _get_entry(fresh_entry["id"])
    assert refreshed["content"] == original_content, (
        f"content mutated by meta-only update: {refreshed['content']!r}"
    )
    assert set(new_tags).issubset(set(refreshed["tags"])), refreshed["tags"]


def test_update_with_neither_content_nor_meta_returns_422(fresh_entry):
    """A no-op update (no content, no title, no meta, no content_type)
    has nothing to apply and must be rejected as 422 — never 500."""
    r = _submit_staging({
        "target_entry_id": fresh_entry["id"],
        "target_path": fresh_entry["logical_path"],
        "change_type": "update",
    })
    assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"
    detail = r.json().get("detail", "")
    assert "update requires at least one" in detail, detail


def test_bulk_meta_only_update_across_ten_entries():
    """Bulk-tagging 10 distinct entries with meta-only updates must all
    succeed without any 500s. Regression test for the original NotNull
    crash that landed when callers tried to script this workflow."""
    suffix = uuid.uuid4().hex[:8]
    created: list[dict] = []
    try:
        for i in range(10):
            created.append(_create_entry(
                title=f"bulk-tag-{suffix}-{i}",
                content=f"Body {i} for bulk-tag regression test.",
                logical_path=f"staging-tests/bulk-{suffix}/entry-{i}",
            ))

        common_tag = f"bulk-{suffix}"
        for entry in created:
            r = _submit_staging({
                "target_entry_id": entry["id"],
                "target_path": entry["logical_path"],
                "change_type": "update",
                "proposed_meta": {"tags": [common_tag, "bulk-fixture"]},
            })
            assert r.status_code == 201, (
                f"bulk meta-only update failed for {entry['id']}: "
                f"{r.status_code} {r.text}"
            )
            assert r.json()["status"] == "auto_approved"

        # All 10 entries now carry the common tag, content unchanged.
        for entry in created:
            refreshed = _get_entry(entry["id"])
            assert common_tag in refreshed["tags"], refreshed["tags"]
            assert refreshed["content"].startswith("Body "), refreshed["content"]
    finally:
        for entry in created:
            _archive(entry["id"])


def test_update_with_content_still_works(fresh_entry):
    """Regression: existing update-with-content path must still succeed
    and overwrite content as before."""
    new_body = f"Replacement body {uuid.uuid4().hex[:6]}"
    r = _submit_staging({
        "target_entry_id": fresh_entry["id"],
        "target_path": fresh_entry["logical_path"],
        "change_type": "update",
        "proposed_content": new_body,
    })
    assert r.status_code == 201, f"{r.status_code} {r.text}"
    assert r.json()["status"] == "auto_approved"

    refreshed = _get_entry(fresh_entry["id"])
    assert refreshed["content"] == new_body, refreshed["content"]


def test_meta_only_update_does_not_collide_on_empty_hash(fresh_entry):
    """Regression: prior to the fix every meta-only update hashed the
    empty string, causing the second one to (a) match other meta-only
    submissions on content_hash and (b) potentially escalate via the
    Tier 2 dup check. With the fix, content_hash for meta-only is NULL
    and successive meta-only updates auto-approve cleanly."""
    suffix = uuid.uuid4().hex[:6]
    other = _create_entry(
        title=f"sibling-{suffix}",
        content="Sibling body — different from fresh_entry.",
        logical_path=f"staging-tests/sibling-{suffix}",
    )
    try:
        r1 = _submit_staging({
            "target_entry_id": fresh_entry["id"],
            "target_path": fresh_entry["logical_path"],
            "change_type": "update",
            "proposed_meta": {"tags": [f"first-{suffix}"]},
        })
        assert r1.status_code == 201, r1.text
        assert r1.json()["status"] == "auto_approved", r1.json()

        r2 = _submit_staging({
            "target_entry_id": other["id"],
            "target_path": other["logical_path"],
            "change_type": "update",
            "proposed_meta": {"tags": [f"second-{suffix}"]},
        })
        assert r2.status_code == 201, r2.text
        # Must NOT be escalated to pending due to a fake hash collision.
        assert r2.json()["status"] == "auto_approved", r2.json()
    finally:
        _archive(other["id"])
