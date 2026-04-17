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


BASE_URL = os.environ.get("BRILLIANT_BASE_URL", "http://localhost:8010")
DB_DSN = os.environ.get(
    "BRILLIANT_DB_DSN",
    "postgresql://postgres:dev@localhost:5442/brilliant",
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


# ---------------------------------------------------------------------------
# Markdown-link extraction (issue #16, T-0195).
#
# `sync_entry_links` historically only matched `[[wiki-link]]` references.
# These tests cover the four cases from the issue: markdown links to existing
# entries resolve, markdown links to unknown targets are silently skipped,
# mixed wiki + markdown links both resolve, and same-target dedup across forms
# produces exactly one entry_links row. URLs / anchors / images must NOT be
# extracted.
# ---------------------------------------------------------------------------


def test_create_with_markdown_link_writes_entry_links_row(target_entry):
    """[label](slug) to an existing entry produces one entry_links row."""
    target, suffix = target_entry
    slug = f"target-{suffix}"
    source = _create(
        title=f"Source MD-A {suffix}",
        content=f"See [the target]({slug}) for details.",
        logical_path=f"writelinks/source-md-a-{suffix}",
    )
    try:
        assert _link_count(source["id"]) == 1
        rendered = _get(source["id"])["content"]
        # The markdown form is preserved as-is in stored content; what matters
        # is that the entry_links row exists for downstream graph queries.
        assert f"({slug})" in rendered or f"(/kb/{target['id']})" in rendered
    finally:
        _archive(source["id"])


def test_markdown_link_to_unknown_target_is_silently_skipped(target_entry):
    """Markdown link to a non-existent slug writes no row and does not error."""
    _target, suffix = target_entry
    source = _create(
        title=f"Source MD-B {suffix}",
        content="See [missing thing](does-not-exist-xyz-md) for details.",
        logical_path=f"writelinks/source-md-b-{suffix}",
    )
    try:
        assert _link_count(source["id"]) == 0
        # Body should round-trip without 500 / mangling.
        rendered = _get(source["id"])["content"]
        assert "does-not-exist-xyz-md" in rendered
    finally:
        _archive(source["id"])


def test_mixed_wiki_and_markdown_links_both_resolve():
    """A source referencing two distinct targets via wiki + markdown forms
    produces exactly two entry_links rows."""
    suffix = uuid.uuid4().hex[:10]
    target_a = _create(
        title=f"writelinks/mix-target-a-{suffix}",
        content="Target A.",
        logical_path=f"writelinks/mix-target-a-{suffix}",
    )
    target_b = _create(
        title=f"writelinks/mix-target-b-{suffix}",
        content="Target B.",
        logical_path=f"writelinks/mix-target-b-{suffix}",
    )
    slug_a = f"mix-target-a-{suffix}"
    slug_b = f"mix-target-b-{suffix}"
    source = _create(
        title=f"Source MIX {suffix}",
        content=(
            f"Wiki reference to [[{slug_a}]] and markdown reference to "
            f"[the other]({slug_b}).\n\n"
            # Image syntax — must be ignored.
            f"![an image](https://example.com/foo.png)\n\n"
            # External URL — must be ignored.
            f"See [docs](https://example.com/docs) for details.\n\n"
            # Anchor — must be ignored.
            f"Jump to [section](#somewhere)."
        ),
        logical_path=f"writelinks/source-mix-{suffix}",
    )
    try:
        assert _link_count(source["id"]) == 2
    finally:
        _archive(source["id"])
        _archive(target_a["id"])
        _archive(target_b["id"])


def test_same_target_via_wiki_and_markdown_dedupes_to_one_row(target_entry):
    """Linking to the same target as both [[slug]] and [label](slug) writes
    exactly one entry_links row (cross-form dedup)."""
    target, suffix = target_entry
    slug = f"target-{suffix}"
    source = _create(
        title=f"Source DEDUP {suffix}",
        content=(
            f"First reference: [[{slug}]].\n\n"
            f"Second reference (markdown): [same target]({slug})."
        ),
        logical_path=f"writelinks/source-dedup-{suffix}",
    )
    try:
        assert _link_count(source["id"]) == 1
    finally:
        _archive(source["id"])
