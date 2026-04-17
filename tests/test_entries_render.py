"""Tests for wiki-link resolution at read time (spec 0028, T-0142/T-0143).

Exercises the GET /entries/{id} rendering pipeline end-to-end (API + DB):

  Prerequisites:
    1. docker compose up -d   (API on :8010, Postgres on :5442)
    2. Migrations applied at least through 002_relationships.sql.
    3. pip install -r tests/requirements-dev.txt

  Run:
    pytest tests/test_entries_render.py -v

Tests 1-3 hit the live API and rely on direct psycopg INSERTs into
`entry_links` (there is no public links API at time of writing). Test 4
is a pure unit test against `resolve_wiki_links` using a fake connection
that counts `.execute()` calls -- this is how we prove the short-circuit
path never issues a DB query when `[[` is absent from content.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import requests

try:
    import psycopg
    _PSYCOPG_AVAILABLE = True
except ImportError:
    _PSYCOPG_AVAILABLE = False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("BRILLIANT_BASE_URL", "http://localhost:8010")
DB_DSN = os.environ.get(
    "BRILLIANT_DB_DSN",
    "postgresql://postgres:dev@localhost:5442/brilliant",
)

# Seed API key + user id (see db/migrations/005_seed.sql).
ADMIN_KEY = "bkai_adm1_testkey_admin"
USR_ADMIN = "usr_admin"
ORG_ID = os.environ.get("BRILLIANT_TEST_ORG_ID", "org_demo")  # seeded org

REQUEST_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _post(path: str, key: str, json: dict | None = None) -> requests.Response:
    return requests.post(
        f"{BASE_URL}{path}", headers=_headers(key), json=json or {}, timeout=REQUEST_TIMEOUT
    )


def _get(path: str, key: str) -> requests.Response:
    return requests.get(f"{BASE_URL}{path}", headers=_headers(key), timeout=REQUEST_TIMEOUT)


def _delete(path: str, key: str) -> requests.Response:
    return requests.delete(f"{BASE_URL}{path}", headers=_headers(key), timeout=REQUEST_TIMEOUT)


def _api_available() -> bool:
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


# Module-level skip when the API isn't up. Test 4 is pure-Python and doesn't
# need the API, so it overrides this with its own marker below.
_api_up = _api_available()
pytestmark = pytest.mark.skipif(
    not _api_up,
    reason=f"Brilliant API not reachable at {BASE_URL} (start it with `docker compose up -d`).",
)


def _create_entry(
    *,
    title: str,
    content: str,
    logical_path: str,
    key: str = ADMIN_KEY,
) -> dict:
    body = {
        "title": title,
        "content": content,
        "content_type": "context",
        "logical_path": logical_path,
        "sensitivity": "shared",
        "tags": ["test", "render"],
    }
    r = _post("/entries", key, body)
    assert r.status_code == 201, f"entry create failed: {r.status_code} {r.text}"
    return r.json()


def _archive(entry_id: str) -> None:
    try:
        _delete(f"/entries/{entry_id}", ADMIN_KEY)
    except Exception:
        pass


def _insert_link(source_id: str, target_id: str, link_type: str = "relates_to") -> None:
    """Insert an entry_links row via a privileged DSN (bypasses role grants)."""
    if not _PSYCOPG_AVAILABLE:
        pytest.skip("psycopg not installed; cannot seed entry_links")
    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO entry_links (
                    org_id, source_entry_id, target_entry_id,
                    link_type, weight, created_by, source
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (ORG_ID, source_id, target_id, link_type, 1.0, USR_ADMIN, "api"),
            )


# ---------------------------------------------------------------------------
# Fixture: one source entry + one target entry + a link between them.
# The source entry's content is set per-test (we update it in place) so each
# test can exercise a different `[[...]]` pattern.
# ---------------------------------------------------------------------------


@pytest.fixture
def linked_pair():
    """(source, target, slug) triple where the target's logical_path tail
    is unique per run so write-path resolution (spec 0030) can't
    mis-match an orphan from a prior test.

    Target title is 'Title A'. Under spec 0030, PUT /entries/{id} re-syncs
    `entry_links` from content via `[[slug]]` resolution, so the direct
    `_insert_link` below is redundant for tests that update content — but
    kept so the resolver has a row even if the source's content never
    references the target (historical test design).
    """
    suffix = uuid.uuid4().hex[:10]
    slug = f"slug-{suffix}"
    target = _create_entry(
        title="Title A",
        content="Target body.",
        logical_path=f"Tests/render/{suffix}/{slug}",
    )
    # Source created with placeholder content; each test rewrites it.
    source = _create_entry(
        title=f"Source {suffix}",
        content="placeholder",
        logical_path=f"Tests/render/{suffix}/source",
    )
    _insert_link(source["id"], target["id"])
    yield source, target, slug
    _archive(source["id"])
    _archive(target["id"])


def _set_content(entry_id: str, new_content: str, expected_version: int) -> dict:
    """PUT /entries/{id} to swap in new content for the test case."""
    r = requests.put(
        f"{BASE_URL}/entries/{entry_id}",
        headers=_headers(ADMIN_KEY),
        json={"content": new_content, "expected_version": expected_version},
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 200, f"update failed: {r.status_code} {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# Test 1: [[slug-a]] resolves with link row present.
# ---------------------------------------------------------------------------


def test_wiki_link_resolves_to_markdown_link(linked_pair):
    source, target, slug = linked_pair
    _set_content(source["id"], f"See [[{slug}]] for details.", source["version"])

    r = _get(f"/entries/{source['id']}", ADMIN_KEY)
    assert r.status_code == 200, r.text
    content = r.json()["content"]

    expected = f"[Title A](/kb/{target['id']})"
    assert expected in content, f"expected {expected!r} in {content!r}"
    assert "[[" not in content, f"literal brackets leaked through: {content!r}"


# ---------------------------------------------------------------------------
# Test 2: [[slug-a|Custom Alias]] uses the alias as label.
# ---------------------------------------------------------------------------


def test_wiki_link_alias_renders_as_label(linked_pair):
    source, target, slug = linked_pair
    _set_content(
        source["id"],
        f"Check [[{slug}|Custom Alias]] please.",
        source["version"],
    )

    r = _get(f"/entries/{source['id']}", ADMIN_KEY)
    assert r.status_code == 200, r.text
    content = r.json()["content"]

    expected = f"[Custom Alias](/kb/{target['id']})"
    assert expected in content, f"expected {expected!r} in {content!r}"
    assert "[[" not in content
    # Title must not sneak in when an alias is provided.
    assert "Title A" not in content


# ---------------------------------------------------------------------------
# Test 3: [[nonexistent]] with no matching link row -> literal preserved.
# ---------------------------------------------------------------------------


def test_wiki_link_unresolved_stays_literal(linked_pair):
    source, _target, _slug = linked_pair
    original = "See [[nonexistent]] — should stay literal."
    _set_content(source["id"], original, source["version"])

    r = _get(f"/entries/{source['id']}", ADMIN_KEY)
    assert r.status_code == 200, r.text
    content = r.json()["content"]

    assert "[[nonexistent]]" in content, f"literal not preserved: {content!r}"


# ---------------------------------------------------------------------------
# Test 4: Short-circuit — no `[[` means no DB query.
#
# Pure unit test against resolve_wiki_links with a fake async connection.
# Doesn't need the API or DB, so override the module-level skip.
# ---------------------------------------------------------------------------


class _CountingConn:
    """Minimal async conn stub that records .execute() invocations."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def execute(self, sql, params=None):  # pragma: no cover - see assertion
        self.calls.append((sql, params))

        class _FakeCur:
            async def fetchall(self_inner):
                return []

        return _FakeCur()


@pytest.mark.skipif(False, reason="pure unit test; always runs")
def test_no_wiki_token_short_circuits_db_query():
    # Import inside the test to keep collection-time imports minimal and to
    # not require the API to be up.
    import sys
    import pathlib

    api_dir = pathlib.Path(__file__).resolve().parents[1] / "api"
    if str(api_dir) not in sys.path:
        sys.path.insert(0, str(api_dir))

    from services.render import resolve_wiki_links  # type: ignore

    conn = _CountingConn()
    content = "This content has no wiki tokens — just plain markdown."
    result = asyncio.run(
        resolve_wiki_links(content, conn, "00000000-0000-0000-0000-000000000000")
    )

    assert result == content, "content with no [[ must pass through unchanged"
    assert conn.calls == [], (
        f"resolver must not issue a DB query when '[[' is absent; got {conn.calls!r}"
    )
