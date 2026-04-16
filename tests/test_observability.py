"""Integration tests for the observability stack (spec 0034c, T-0193).

Exercises the full observability pipeline against a live API:
- request_log middleware (logs every non-health request with duration/status).
- entry_access_log tracking on read paths (single batched INSERT per request).
- Admin analytics rollup endpoints (/analytics/top-entries, top-endpoints).
- RLS denial on log tables for non-admin roles.

Prerequisites:
  1. Observability worktree stack up:
       COMPOSE_PROJECT_NAME=brilliant-obs docker compose up -d
     (API on :8030, Postgres on :5462)
  2. pip install -r tests/requirements-dev.txt

Run:
  pytest tests/test_observability.py -v

Env overrides (keep tests runnable from either worktree after merge):
  CORTEX_BASE_URL  -- default http://localhost:8030
  CORTEX_DB_DSN    -- default postgresql://postgres:dev@localhost:5462/cortex
"""

from __future__ import annotations

import os
import time
import uuid

import pytest
import requests

try:
    import psycopg
    _PSYCOPG_AVAILABLE = True
except ImportError:
    _PSYCOPG_AVAILABLE = False


# The observability worktree runs on 8030/:5462; the primary tree runs on
# 8010/:5442. Default to the observability ports so this file works without
# env overrides when executed from this worktree, but allow override so the
# same tests can run from the primary tree after merge.
BASE_URL = os.environ.get("CORTEX_BASE_URL", "http://localhost:8030")
DB_DSN = os.environ.get(
    "CORTEX_DB_DSN",
    "postgresql://postgres:dev@localhost:5462/cortex",
)

ADMIN_KEY = "bkai_adm1_testkey_admin"
EDITOR_KEY = "bkai_edit_testkey_editor"
VIEWER_KEY = "bkai_view_testkey_viewer"
AGENT_KEY = "bkai_agnt_testkey_agent"

ORG_ID = "org_demo"
ADMIN_USER_ID = "usr_admin"

REQUEST_TIMEOUT = 10.0

# Seeded entries (see db/migrations/005_seed.sql). Use a stable, known id
# so we don't have to create+teardown an entry just to read one.
SEED_ENTRY_ID = "a0000000-0000-0000-0000-000000000001"

# Async background inserts should land well under 2s; polling bounds the
# wait so a failing test fails fast rather than stalling the suite.
_MAX_POLL_SECONDS = 3.0
_POLL_INTERVAL = 0.1


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _api_available() -> bool:
    try:
        return requests.get(f"{BASE_URL}/health", timeout=2.0).status_code == 200
    except Exception:
        return False


def _db_available() -> bool:
    if not _PSYCOPG_AVAILABLE:
        return False
    try:
        with psycopg.connect(DB_DSN, connect_timeout=2) as _:
            return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(
        not _api_available(),
        reason=f"Brilliant API not reachable at {BASE_URL} (start `docker compose up -d`).",
    ),
    pytest.mark.skipif(
        not _PSYCOPG_AVAILABLE,
        reason="psycopg not installed; cannot verify observability rows",
    ),
    pytest.mark.skipif(
        not _db_available(),
        reason=f"Postgres not reachable at {DB_DSN}",
    ),
]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _admin_cursor(conn):
    """Configure an RLS-scoped cursor that sees the admin's view of the logs.

    Raw postgres connections run under FORCE RLS — without role+org_id set,
    SELECTs return 0 rows regardless of actual content. Mirror what the API
    does for admin-scoped reads.
    """
    cur = conn.cursor()
    cur.execute("SET ROLE kb_admin")
    cur.execute(f"SET app.org_id = '{ORG_ID}'")
    return cur


def _truncate_logs() -> bool:
    """TRUNCATE both log tables as the postgres superuser.

    kb_admin only has SELECT/INSERT, so TRUNCATE requires superuser. We gate
    row-counting tests on this succeeding (if the DSN isn't superuser, those
    tests skip rather than produce nondeterministic counts).
    """
    try:
        with psycopg.connect(DB_DSN, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE entry_access_log, request_log")
        return True
    except Exception:
        return False


def _can_truncate() -> bool:
    # Used as a skip gate; don't actually wipe anything here.
    try:
        with psycopg.connect(DB_DSN, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT has_table_privilege(current_user, 'entry_access_log', 'TRUNCATE')")
                return bool(cur.fetchone()[0])
    except Exception:
        return False


def _wait_for(predicate, max_seconds: float = _MAX_POLL_SECONDS):
    """Poll `predicate()` until it returns truthy. Returns last value."""
    deadline = time.time() + max_seconds
    value = predicate()
    while not value and time.time() < deadline:
        time.sleep(_POLL_INTERVAL)
        value = predicate()
    return value


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_middleware_records_read():
    """GET /entries should land exactly one row in request_log with the
    matched path template, 200 status, and a non-negative duration.
    """
    if not _truncate_logs():
        pytest.skip("DB user lacks TRUNCATE on log tables; cannot isolate counts.")

    r = requests.get(
        f"{BASE_URL}/entries?limit=1",
        headers=_headers(ADMIN_KEY),
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 200, r.text

    def _look():
        with psycopg.connect(DB_DSN, autocommit=True) as conn:
            cur = _admin_cursor(conn)
            cur.execute(
                """
                SELECT endpoint, status, duration_ms
                FROM request_log
                WHERE endpoint = '/entries'
                ORDER BY ts DESC
                LIMIT 1
                """
            )
            return cur.fetchone()

    row = _wait_for(_look)
    assert row is not None, "middleware did not record a row for GET /entries"
    endpoint, status, duration_ms = row
    assert endpoint == "/entries"
    assert status == 200
    # duration_ms is rounded to int so a sub-millisecond response can legitimately
    # be 0. Require >= 0 rather than > 0 to avoid flakes on fast hardware.
    assert duration_ms is not None and duration_ms >= 0


def test_health_not_logged():
    """/health is skipped by the middleware — hit it 5 times, expect 0 rows."""
    if not _truncate_logs():
        pytest.skip("DB user lacks TRUNCATE on log tables; cannot isolate counts.")

    for _ in range(5):
        r = requests.get(f"{BASE_URL}/health", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200

    # Give any stray async tasks a moment to flush before asserting absence.
    time.sleep(0.5)

    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        cur = _admin_cursor(conn)
        cur.execute("SELECT COUNT(*) FROM request_log WHERE endpoint = '/health'")
        assert cur.fetchone()[0] == 0


def test_entry_get_writes_one_access_row():
    """GET /entries/{id} should write exactly 1 row to entry_access_log."""
    if not _truncate_logs():
        pytest.skip("DB user lacks TRUNCATE on log tables; cannot isolate counts.")

    r = requests.get(
        f"{BASE_URL}/entries/{SEED_ENTRY_ID}",
        headers=_headers(ADMIN_KEY),
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 200, r.text

    def _look():
        with psycopg.connect(DB_DSN, autocommit=True) as conn:
            cur = _admin_cursor(conn)
            cur.execute(
                "SELECT COUNT(*) FROM entry_access_log WHERE entry_id = %s",
                (SEED_ENTRY_ID,),
            )
            return cur.fetchone()[0]

    count = _wait_for(lambda: _look() or None) or 0
    assert count == 1, f"expected 1 access_log row, got {count}"


def test_entries_list_batched_insert():
    """GET /entries?limit=5 writes 5 rows that share a single `ts` (proxy for
    single-statement INSERT — all rows get the same statement_timestamp)."""
    if not _truncate_logs():
        pytest.skip("DB user lacks TRUNCATE on log tables; cannot isolate counts.")

    r = requests.get(
        f"{BASE_URL}/entries?limit=5",
        headers=_headers(ADMIN_KEY),
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 200, r.text
    returned = len(r.json()["entries"])
    assert returned == 5, f"expected 5 entries from seed data, got {returned}"

    def _look():
        with psycopg.connect(DB_DSN, autocommit=True) as conn:
            cur = _admin_cursor(conn)
            cur.execute("SELECT COUNT(*), COUNT(DISTINCT ts) FROM entry_access_log")
            total, distinct_ts = cur.fetchone()
            return (total, distinct_ts) if total >= 5 else None

    result = _wait_for(_look)
    assert result is not None, "entry_access_log never reached 5 rows"
    total, distinct_ts = result
    assert total == 5, f"expected exactly 5 access rows, got {total}"
    assert distinct_ts == 1, (
        f"batched insert should share one ts across all rows, got {distinct_ts}"
    )


def test_graph_batched_insert():
    """/graph?scope=org&limit_nodes=N writes exactly N rows to entry_access_log.

    The /graph response is cached 45s keyed by (org, user, scope, path,
    include_archived, limit_nodes). A cached hit short-circuits before the
    access-log INSERT. Pick a `limit_nodes` value derived from the current
    wall clock so back-to-back runs of this test always miss the cache.
    """
    if not _truncate_logs():
        pytest.skip("DB user lacks TRUNCATE on log tables; cannot isolate counts.")

    # Wall-clock-seeded limit_nodes (in [3, 30]) avoids cache collision on rerun.
    unique_limit = (int(time.time()) % 28) + 3

    r = requests.get(
        f"{BASE_URL}/graph?scope=org&limit_nodes={unique_limit}",
        headers=_headers(ADMIN_KEY),
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 200, r.text
    nodes = r.json()["nodes"]
    n = len(nodes)
    assert n > 0, "graph returned no nodes — cannot verify batched insert"

    def _look():
        with psycopg.connect(DB_DSN, autocommit=True) as conn:
            cur = _admin_cursor(conn)
            cur.execute("SELECT COUNT(*) FROM entry_access_log")
            total = cur.fetchone()[0]
            return total if total >= n else None

    total = _wait_for(_look)
    assert total is not None, f"entry_access_log never reached {n} rows"
    assert total == n, f"expected {n} rows (one per returned node), got {total}"


def test_viewer_denied_on_analytics():
    """Viewer key gets 403 on every analytics endpoint (admin-only gate)."""
    r = requests.get(
        f"{BASE_URL}/analytics/top-entries?since=24h",
        headers=_headers(VIEWER_KEY),
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 403, f"expected 403 for viewer, got {r.status_code}: {r.text}"

    # Sanity-check the other two rollups too so a missing gate anywhere
    # gets caught by this test.
    r2 = requests.get(
        f"{BASE_URL}/analytics/top-endpoints?since=24h",
        headers=_headers(VIEWER_KEY),
        timeout=REQUEST_TIMEOUT,
    )
    assert r2.status_code == 403

    r3 = requests.get(
        f"{BASE_URL}/analytics/session-depth?actor_id={ADMIN_USER_ID}&since=24h",
        headers=_headers(VIEWER_KEY),
        timeout=REQUEST_TIMEOUT,
    )
    assert r3.status_code == 403


def test_admin_top_entries_shape():
    """Admin GET /analytics/top-entries returns the stable rollup shape.

    Downstream MCP tool (T-0192) depends on these exact keys.
    """
    r = requests.get(
        f"{BASE_URL}/analytics/top-entries?since=24h&limit=5",
        headers=_headers(ADMIN_KEY),
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # Top-level shape contract.
    assert set(body.keys()) >= {"items", "limit", "offset", "since"}, body.keys()
    assert body["limit"] == 5
    assert body["offset"] == 0
    assert body["since"] == "24h"
    assert isinstance(body["items"], list)

    # If any items are present, they must have (entry_id, title, reads).
    for item in body["items"]:
        assert set(item.keys()) >= {"entry_id", "title", "reads"}, item
        assert isinstance(item["reads"], int)


def test_admin_top_endpoints_p95():
    """Admin GET /analytics/top-endpoints returns numeric avg + p95 duration.

    Seed an entry into the request log (via at least one logged request) so
    the rollup has something to report.
    """
    # Ensure there's at least one logged request in the window.
    requests.get(
        f"{BASE_URL}/entries?limit=1",
        headers=_headers(ADMIN_KEY),
        timeout=REQUEST_TIMEOUT,
    )
    # Give the fire-and-forget insert time to land before reading.
    time.sleep(0.3)

    r = requests.get(
        f"{BASE_URL}/analytics/top-endpoints?since=24h&limit=5",
        headers=_headers(ADMIN_KEY),
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert set(body.keys()) >= {"items", "limit", "offset", "since"}
    assert body["limit"] == 5
    assert isinstance(body["items"], list)
    assert len(body["items"]) >= 1, "expected at least one endpoint in rollup"

    for item in body["items"]:
        assert set(item.keys()) >= {
            "endpoint",
            "count",
            "avg_duration_ms",
            "p95_duration_ms",
        }, item
        assert isinstance(item["count"], int) and item["count"] > 0
        # Both duration fields should be numeric (float) when count > 0.
        assert isinstance(item["avg_duration_ms"], (int, float)), item
        assert isinstance(item["p95_duration_ms"], (int, float)), item
        assert item["avg_duration_ms"] >= 0
        assert item["p95_duration_ms"] >= 0


def test_since_parser_rejects_bogus():
    """?since=bogus returns 422 across all analytics endpoints."""
    r = requests.get(
        f"{BASE_URL}/analytics/top-entries?since=bogus",
        headers=_headers(ADMIN_KEY),
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"

    r2 = requests.get(
        f"{BASE_URL}/analytics/top-endpoints?since=forever",
        headers=_headers(ADMIN_KEY),
        timeout=REQUEST_TIMEOUT,
    )
    assert r2.status_code == 422


def test_rls_viewer_cannot_read_access_log():
    """Direct psycopg connection as kb_viewer sees 0 rows from log tables
    even when admin sees non-zero. Verifies FORCE RLS + admin-only SELECT
    policy are both in effect.
    """
    # Generate a guaranteed row before we check.
    requests.get(
        f"{BASE_URL}/entries/{SEED_ENTRY_ID}",
        headers=_headers(ADMIN_KEY),
        timeout=REQUEST_TIMEOUT,
    )
    # Let the access-log INSERT land.
    time.sleep(0.3)

    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Admin perspective: should see rows.
            cur.execute("SET ROLE kb_admin")
            cur.execute(f"SET app.org_id = '{ORG_ID}'")
            cur.execute("SELECT COUNT(*) FROM entry_access_log")
            admin_seen = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM request_log")
            admin_seen_req = cur.fetchone()[0]

            assert admin_seen > 0, (
                "admin sees 0 access-log rows; cannot test viewer denial "
                "(the test expects recent activity to exist)."
            )

            # Swap to viewer. RESET ROLE first because SET ROLE is cumulative
            # in the same session.
            cur.execute("RESET ROLE")
            cur.execute("SET ROLE kb_viewer")
            cur.execute(f"SET app.org_id = '{ORG_ID}'")

            cur.execute("SELECT COUNT(*) FROM entry_access_log")
            viewer_seen = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM request_log")
            viewer_seen_req = cur.fetchone()[0]

    assert viewer_seen == 0, (
        f"viewer should see 0 entry_access_log rows under FORCE RLS, "
        f"saw {viewer_seen} (admin saw {admin_seen})."
    )
    assert viewer_seen_req == 0, (
        f"viewer should see 0 request_log rows, saw {viewer_seen_req} "
        f"(admin saw {admin_seen_req})."
    )


def test_actor_type_filter_rejects_bogus():
    """?actor_type=bogus returns 422 (not 500, not silent empty)."""
    r = requests.get(
        f"{BASE_URL}/analytics/top-entries?since=24h&actor_type=martian",
        headers=_headers(ADMIN_KEY),
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"
