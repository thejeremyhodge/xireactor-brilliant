"""Tests for the direct link-creation route POST /entries/{id}/links.

Regression guard for the create_link 500: the route did a bare INSERT into
entry_links with no ON CONFLICT clause, so creating a link that already
existed tripped the
`entry_links_org_id_source_entry_id_target_entry_id_link_typ_key` unique
constraint and surfaced as a bare 500. The route now upserts, so a duplicate
link must succeed idempotently (mirrors the staging create_link fix in
tests/test_staging.py).

Prerequisites:
  1. docker compose up -d   (API on :8010, Postgres on :5442)
  2. pip install -r tests/requirements-dev.txt

Run:
  pytest tests/test_links.py -v
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


def _create_entry(title: str, logical_path: str) -> dict:
    r = requests.post(
        f"{BASE_URL}/entries",
        headers=_headers(),
        json={
            "title": title,
            "content": "Body for link regression test.",
            "content_type": "context",
            "logical_path": logical_path,
            "sensitivity": "shared",
            "tags": ["link-fixture"],
        },
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 201, f"create failed: {r.status_code} {r.text}"
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


def test_direct_create_link_is_idempotent_no_500_on_duplicate():
    """POST /entries/{id}/links twice for the same (source, target, link_type)
    must both return 201. The second call previously 500'd on the entry_links
    unique constraint; it now upserts (weight/metadata refreshed)."""
    suffix = uuid.uuid4().hex[:8]
    source = _create_entry(f"link-src-{suffix}", f"link-tests/src-{suffix}")
    target = _create_entry(f"link-tgt-{suffix}", f"link-tests/tgt-{suffix}")

    def _post_link(weight: float) -> requests.Response:
        return requests.post(
            f"{BASE_URL}/entries/{source['id']}/links",
            headers=_headers(),
            json={
                "target_entry_id": target["id"],
                "link_type": "relates_to",
                "weight": weight,
            },
            timeout=REQUEST_TIMEOUT,
        )

    try:
        r1 = _post_link(1.0)
        assert r1.status_code == 201, f"first link failed: {r1.status_code} {r1.text}"
        first_id = r1.json()["id"]

        # The duplicate that previously 500'd on the UniqueViolation.
        # weight stays within the entry_links CHECK (BETWEEN 0 AND 1).
        r2 = _post_link(0.5)
        assert r2.status_code == 201, f"duplicate link 500'd: {r2.status_code} {r2.text}"
        # Upsert: same row id returned, weight refreshed from EXCLUDED.
        assert r2.json()["id"] == first_id, r2.json()
        assert float(r2.json()["weight"]) == 0.5, r2.json()
    finally:
        _archive(source["id"])
        _archive(target["id"])


def test_direct_create_link_out_of_range_weight_returns_422_not_500():
    """weight outside [0,1] must be rejected by the LinkCreate model with a
    422, not leak the entry_links_weight_check CheckViolation as a 500."""
    suffix = uuid.uuid4().hex[:8]
    source = _create_entry(f"link-src-{suffix}", f"link-tests/src-{suffix}")
    target = _create_entry(f"link-tgt-{suffix}", f"link-tests/tgt-{suffix}")
    try:
        r = requests.post(
            f"{BASE_URL}/entries/{source['id']}/links",
            headers=_headers(),
            json={
                "target_entry_id": target["id"],
                "link_type": "relates_to",
                "weight": 2.0,
            },
            timeout=REQUEST_TIMEOUT,
        )
        assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"
    finally:
        _archive(source["id"])
        _archive(target["id"])
