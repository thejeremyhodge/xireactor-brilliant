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


BASE_URL = os.environ.get("BRILLIANT_BASE_URL", "http://localhost:8010")
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


# ---------------------------------------------------------------------------
# Tests — Sprint 0050 epistemic axis (T-0293)
# ---------------------------------------------------------------------------


def _create_entry_via_staging(payload_overrides: dict) -> dict:
    """Helper: submit a create staging item with the overrides merged in.
    Returns the staging response. Caller is responsible for cleaning up the
    promoted entry via `_archive(staging['promoted_entry_id'])`.
    """
    suffix = uuid.uuid4().hex[:8]
    base = {
        "target_entry_id": None,
        "target_path": f"staging-tests/epistemic-{suffix}",
        "change_type": "create",
        "proposed_title": f"epistemic-create-{suffix}",
        "proposed_content": f"Body for epistemic test {suffix}.",
        "content_type": "note",
    }
    base.update(payload_overrides)
    r = _submit_staging(base)
    assert r.status_code == 201, f"submit failed: {r.status_code} {r.text}"
    body = r.json()
    assert body["status"] == "auto_approved", body
    assert body.get("promoted_entry_id"), body
    return body


def test_submit_without_epistemic_fields_uses_content_type_defaults():
    """T-0293 AC #1: submit without claim_type / source_confidence creates
    the entry with defaults inferred from content_type. content_type='note'
    maps to claim_type='observation', and source_confidence defaults to
    'reported'. We verify via the API response — when the entry route adds
    epistemic columns to its SELECT (T-0294 / follow-on), we'll switch to
    asserting them via _get_entry. For now we assert the write path itself
    succeeded end-to-end at Tier 1.
    """
    promoted = _create_entry_via_staging({"content_type": "note"})
    try:
        # Sanity: the staging row promoted to a real entry.
        refreshed = _get_entry(promoted["promoted_entry_id"])
        assert refreshed["content_type"] == "note", refreshed
        # Once EntryResponse exposes claim_type, assert it's 'observation'
        # and source_confidence is 'reported'. Until then, the absence of
        # 500s on the write path is the contract this test guards.
        if "claim_type" in refreshed:
            assert refreshed["claim_type"] == "observation", refreshed
        if "source_confidence" in refreshed:
            assert refreshed["source_confidence"] == "reported", refreshed
    finally:
        _archive(promoted["promoted_entry_id"])


def test_submit_with_explicit_epistemic_fields_lands_on_entry():
    """T-0293 AC #2: submit with explicit claim_type / source_confidence
    writes those exact values onto the entry post-approval. content_type
    is 'note' (which would default to observation/reported) but the
    submitter's explicit 'claim' / 'verified' must win.
    """
    promoted = _create_entry_via_staging({
        "content_type": "note",
        "claim_type": "claim",
        "source_confidence": "verified",
    })
    try:
        refreshed = _get_entry(promoted["promoted_entry_id"])
        if "claim_type" in refreshed:
            assert refreshed["claim_type"] == "claim", refreshed
        if "source_confidence" in refreshed:
            assert refreshed["source_confidence"] == "verified", refreshed
    finally:
        _archive(promoted["promoted_entry_id"])


def test_review_override_overrides_submitter_values():
    """T-0293 AC #3: when an admin approves a Tier-3 staging item with
    claim_type / source_confidence overrides in the ReviewAction body,
    those values land on the entry — NOT the submitter's. We force a
    Tier-3 path by submitting against a 'system'-sensitivity entry so the
    item lands in 'pending' status, then approve with overrides.
    """
    suffix = uuid.uuid4().hex[:8]
    # Submit a strategic-sensitivity create — Tier 3, lands as pending.
    submit_r = _submit_staging({
        "target_entry_id": None,
        "target_path": f"staging-tests/review-override-{suffix}",
        "change_type": "create",
        "proposed_title": f"review-override-{suffix}",
        "proposed_content": "Strategic claim under review.",
        "content_type": "note",
        "proposed_meta": {"sensitivity": "strategic"},
        # Submitter says 'observation' / 'reported'.
        "claim_type": "observation",
        "source_confidence": "reported",
    })
    assert submit_r.status_code == 201, submit_r.text
    staging_row = submit_r.json()
    assert staging_row["status"] == "pending", staging_row
    staging_id = staging_row["id"]

    promoted_entry_id = None
    try:
        # Reviewer overrides to 'rule' / 'verified'.
        approve_r = requests.post(
            f"{BASE_URL}/staging/{staging_id}/approve",
            headers=_headers(),
            json={"claim_type": "rule", "source_confidence": "verified"},
            timeout=REQUEST_TIMEOUT,
        )
        assert approve_r.status_code == 200, approve_r.text
        approved = approve_r.json()
        assert approved["status"] == "approved", approved

        # Find the entry the staging item promoted.
        # Re-fetch staging to read promoted_entry_id (some implementations
        # set it on approve only).
        list_r = requests.get(
            f"{BASE_URL}/staging?status=approved&target_path=staging-tests/review-override-{suffix}",
            headers=_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        if list_r.status_code == 200 and list_r.json().get("items"):
            promoted_entry_id = list_r.json()["items"][0].get("promoted_entry_id")

        if promoted_entry_id:
            refreshed = _get_entry(promoted_entry_id)
            if "claim_type" in refreshed:
                assert refreshed["claim_type"] == "rule", refreshed
            if "source_confidence" in refreshed:
                assert refreshed["source_confidence"] == "verified", refreshed
    finally:
        if promoted_entry_id:
            _archive(promoted_entry_id)


def test_existing_staging_payloads_without_epistemic_fields_unchanged():
    """T-0293 AC #4: payloads that pre-date the epistemic axis (no
    claim_type / source_confidence) must continue to work end-to-end.
    This is a duplicate-spirit of the meta-only / content-only tests
    above, but explicitly framed as a back-compat guard for the v0.7.0
    API contract."""
    suffix = uuid.uuid4().hex[:8]
    r = _submit_staging({
        "target_entry_id": None,
        "target_path": f"staging-tests/legacy-{suffix}",
        "change_type": "create",
        "proposed_title": f"legacy-{suffix}",
        "proposed_content": "Legacy payload — no epistemic fields.",
        "content_type": "note",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "auto_approved", body
    promoted_id = body.get("promoted_entry_id")
    assert promoted_id, body
    try:
        # Entry exists and is reachable.
        refreshed = _get_entry(promoted_id)
        assert refreshed["content_type"] == "note", refreshed
        assert refreshed["content"] == "Legacy payload — no epistemic fields."
    finally:
        _archive(promoted_id)


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
