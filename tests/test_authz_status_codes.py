"""Regression tests for HTTP status codes on RLS-denied write operations.

Background
----------
Routes that rely on row-level security (rather than a route-level role guard)
to reject unauthorized writes were leaking ``psycopg.errors.InsufficientPrivilege``
as HTTP 500. The correct response for an authenticated-but-unauthorized request
is HTTP 403.

These tests pin the contract: viewer-role requests against entry write/update/
delete/link endpoints must return 403, not 500.

Prerequisites
-------------
1. ``docker compose up -d --build``
2. ``pip install -r tests/requirements-dev.txt``

Run::

    pytest tests/test_authz_status_codes.py -v
"""

from __future__ import annotations

import os
import uuid

import pytest
import requests

BASE_URL = os.environ.get("CORTEX_BASE_URL", "http://localhost:8010")
ADMIN_KEY = "bkai_adm1_testkey_admin"
VIEWER_KEY = "bkai_view_testkey_viewer"
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


@pytest.fixture
def seeded_entry():
    """Admin-owned entry the viewer can attempt to mutate."""
    suffix = uuid.uuid4().hex[:10]
    body = {
        "title": f"authz-fixture-{suffix}",
        "content": "fixture body",
        "content_type": "context",
        "logical_path": f"authz/fixture-{suffix}",
        "sensitivity": "shared",
        "tags": ["authz-test"],
    }
    r = requests.post(
        f"{BASE_URL}/entries",
        headers=_headers(ADMIN_KEY),
        json=body,
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 201, f"fixture create failed: {r.status_code} {r.text}"
    entry = r.json()
    yield entry
    requests.delete(
        f"{BASE_URL}/entries/{entry['id']}",
        headers=_headers(ADMIN_KEY),
        timeout=REQUEST_TIMEOUT,
    )


def test_viewer_post_entries_returns_403_not_500(seeded_entry):
    """Viewer attempting to create an entry must get 403, not 500."""
    body = {
        "title": "viewer-attempt",
        "content": "should be denied",
        "content_type": "context",
        "logical_path": f"authz/viewer-{uuid.uuid4().hex[:8]}",
        "sensitivity": "shared",
        "tags": [],
    }
    r = requests.post(
        f"{BASE_URL}/entries",
        headers=_headers(VIEWER_KEY),
        json=body,
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 403, (
        f"Expected 403, got {r.status_code}. "
        "RLS-denied writes must surface as 403, not leak as 500. "
        f"Body: {r.text[:200]}"
    )


def test_viewer_put_entry_returns_403_not_500(seeded_entry):
    """Viewer attempting to update an admin-owned entry must get 403."""
    r = requests.put(
        f"{BASE_URL}/entries/{seeded_entry['id']}",
        headers=_headers(VIEWER_KEY),
        json={"content": "hijacked", "expected_version": seeded_entry["version"]},
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 403, (
        f"Expected 403, got {r.status_code}. Body: {r.text[:200]}"
    )


def test_viewer_delete_entry_returns_403_not_500(seeded_entry):
    """Viewer attempting to delete an admin-owned entry must get 403."""
    r = requests.delete(
        f"{BASE_URL}/entries/{seeded_entry['id']}",
        headers=_headers(VIEWER_KEY),
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 403, (
        f"Expected 403, got {r.status_code}. Body: {r.text[:200]}"
    )


def test_viewer_post_entry_link_returns_403_not_500(seeded_entry):
    """Viewer attempting to create a link must get 403."""
    r = requests.post(
        f"{BASE_URL}/entries/{seeded_entry['id']}/links",
        headers=_headers(VIEWER_KEY),
        json={
            "target_entry_id": seeded_entry["id"],
            "link_type": "relates_to",
        },
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 403, (
        f"Expected 403, got {r.status_code}. Body: {r.text[:200]}"
    )


def test_response_body_is_json_with_detail():
    """403 responses from the global handler must be JSON with a `detail` key."""
    r = requests.post(
        f"{BASE_URL}/entries",
        headers=_headers(VIEWER_KEY),
        json={
            "title": "viewer-shape-check",
            "content": "x",
            "content_type": "context",
            "logical_path": f"authz/shape-{uuid.uuid4().hex[:8]}",
            "sensitivity": "shared",
            "tags": [],
        },
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 403
    assert r.headers.get("content-type", "").startswith("application/json"), (
        f"Expected JSON response, got content-type={r.headers.get('content-type')}"
    )
    body = r.json()
    assert "detail" in body, f"Expected 'detail' key in 403 body, got: {body}"
    assert isinstance(body["detail"], str)
