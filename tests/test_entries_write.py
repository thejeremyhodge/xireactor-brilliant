"""Tests for write-path `entry_links` population (spec 0030, T-0156).

Exercises POST /entries and PUT /entries/{id} against a live API to verify
that `sync_entry_links` populates / updates / removes rows based on
`[[wiki-link]]` references in entry content.

Prerequisites:
  1. docker compose up -d --build   (API on :8010, Postgres on :5442)
  2. pip install -r tests/requirements-dev.txt

Run:
  pytest tests/test_entries_write.py -v
"""

from __future__ import annotations

import os
import uuid

import pytest
import requests

try:
    import psycopg
    _PSYCOPG_AVAILABLE = True
except ImportError:
    _PSYCOPG_AVAILABLE = False


BASE_URL = os.environ.get("CORTEX_BASE_URL", "http://localhost:8010")
DB_DSN = os.environ.get(
    "CORTEX_DB_DSN",
    "postgresql://postgres:dev@localhost:5442/cortex",
)
ADMIN_KEY = "bkai_adm1_testkey_admin"
REQUEST_TIMEOUT = 10.0


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _api_available() -> bool:
    try:
        return requests.get(f"{BASE_URL}/health", timeout=2.0).status_code == 200
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(
        not _api_available(),
        reason=f"Brilliant API not reachable at {BASE_URL} (start `docker compose up -d`).",
    ),
    pytest.mark.skipif(
        not _PSYCOPG_AVAILABLE,
        reason="psycopg not installed; cannot verify entry_links rows",
    ),
]


def _create(title: str, content: str, logical_path: str) -> dict:
    body = {
        "title": title,
        "content": content,
        "content_type": "context",
        "logical_path": logical_path,
        "sensitivity": "shared",
        "tags": ["test", "write-links"],
    }
    r = requests.post(
        f"{BASE_URL}/entries", headers=_headers(ADMIN_KEY), json=body, timeout=REQUEST_TIMEOUT
    )
    assert r.status_code == 201, f"create failed: {r.status_code} {r.text}"
    return r.json()


def _put(entry_id: str, content: str, version: int) -> dict:
    r = requests.put(
        f"{BASE_URL}/entries/{entry_id}",
        headers=_headers(ADMIN_KEY),
        json={"content": content, "expected_version": version},
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 200, f"update failed: {r.status_code} {r.text}"
    return r.json()


def _get(entry_id: str) -> dict:
    r = requests.get(
        f"{BASE_URL}/entries/{entry_id}", headers=_headers(ADMIN_KEY), timeout=REQUEST_TIMEOUT
    )
    assert r.status_code == 200, r.text
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


def _link_count(source_id: str) -> int:
    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM entry_links WHERE source_entry_id = %s",
                (source_id,),
            )
            return cur.fetchone()[0]


@pytest.fixture
def target_entry():
    suffix = uuid.uuid4().hex[:10]
    # Title mimics Meridian seed shape (title == logical_path), so only
    # tail-segment matching will succeed -- mirrors the real-world bug.
    target = _create(
        title=f"writelinks/target-{suffix}",
        content="Target body.",
        logical_path=f"writelinks/target-{suffix}",
    )
    yield target, suffix
    _archive(target["id"])


def test_create_with_wiki_link_writes_entry_links_row(target_entry):
    target, suffix = target_entry
    slug = f"target-{suffix}"
    source = _create(
        title=f"Source A {suffix}",
        content=f"See [[{slug}]] for details.",
        logical_path=f"writelinks/source-a-{suffix}",
    )
    try:
        assert _link_count(source["id"]) == 1
        # read-side rendering verifies the full create -> entry_links -> resolver chain
        rendered = _get(source["id"])["content"]
        assert f"(/kb/{target['id']})" in rendered
        assert "[[" not in rendered
    finally:
        _archive(source["id"])


def test_put_adds_wiki_link(target_entry):
    target, suffix = target_entry
    slug = f"target-{suffix}"
    source = _create(
        title=f"Source B {suffix}",
        content="No links here yet.",
        logical_path=f"writelinks/source-b-{suffix}",
    )
    try:
        assert _link_count(source["id"]) == 0
        updated = _put(source["id"], f"Now referencing [[{slug}]].", source["version"])
        assert _link_count(source["id"]) == 1
        rendered = _get(updated["id"])["content"]
        assert f"(/kb/{target['id']})" in rendered
    finally:
        _archive(source["id"])


def test_put_removes_wiki_link(target_entry):
    target, suffix = target_entry
    slug = f"target-{suffix}"
    source = _create(
        title=f"Source C {suffix}",
        content=f"Initial with [[{slug}]].",
        logical_path=f"writelinks/source-c-{suffix}",
    )
    try:
        assert _link_count(source["id"]) == 1
        _put(source["id"], "No more links.", source["version"])
        assert _link_count(source["id"]) == 0
    finally:
        _archive(source["id"])


def test_unknown_slug_produces_no_rows_without_error(target_entry):
    _target, suffix = target_entry
    source = _create(
        title=f"Source D {suffix}",
        content="Reference to [[does-not-exist-xyz]] here.",
        logical_path=f"writelinks/source-d-{suffix}",
    )
    try:
        assert _link_count(source["id"]) == 0
        # Read returns 200 with literal brackets preserved.
        rendered = _get(source["id"])["content"]
        assert "[[does-not-exist-xyz]]" in rendered
    finally:
        _archive(source["id"])
