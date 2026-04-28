"""Integration tests for ``GET /import/visualize`` (Sprint 0047, T-0273/T-0275).

Four scenarios mandated by the spec:

  1. happy_path  — seeded org → 200 text/html with substituted graph data
  2. unauthorized — no Bearer → 401
  3. empty       — 0 entries → "Graph not ready" card
  4. oversize    — > 3000 entries → "exceeds the demo visualization limits" card

Each test uses an isolated org/user/api_key triple so we don't perturb the
seeded ``org_demo`` knowledge base. Cleanup is best-effort in teardown.

Prerequisites
-------------
  1. ``docker compose up -d --build``  (API on :8010, Postgres on :5442)
  2. ``pip install -r tests/requirements-dev.txt``

Run
---
  pytest tests/test_visualize.py -v
"""

from __future__ import annotations

import os
import uuid

import pytest


try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _REQUESTS_AVAILABLE = False

try:
    import psycopg
    _PSYCOPG_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PSYCOPG_AVAILABLE = False


BASE_URL = os.environ.get("BRILLIANT_BASE_URL", "http://localhost:8010")
DB_DSN = os.environ.get(
    "BRILLIANT_DB_DSN",
    "postgresql://postgres:dev@localhost:5442/brilliant",
)
REQUEST_TIMEOUT = 30.0


def _api_available() -> bool:
    if not _REQUESTS_AVAILABLE:
        return False
    try:
        return requests.get(f"{BASE_URL}/health", timeout=2.0).status_code == 200
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(
        not _REQUESTS_AVAILABLE,
        reason="requests not installed; pip install -r tests/requirements-dev.txt",
    ),
    pytest.mark.skipif(
        not _PSYCOPG_AVAILABLE,
        reason="psycopg not installed; pip install -r tests/requirements-dev.txt",
    ),
    pytest.mark.skipif(
        not _api_available(),
        reason=f"Brilliant API not reachable at {BASE_URL} "
        f"(start `docker compose up -d`).",
    ),
]


def _provision_org(entry_count: int) -> tuple[str, str, str, str]:
    """Create a fresh org + admin user + api_key, optionally seed entries.

    Returns ``(org_id, user_id, key_prefix, plaintext_token)``. The caller
    should pass ``key_prefix`` to ``_cleanup_org`` in a finally block.
    """
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_viz_{suffix}"
    user_id = f"usr_viz_{suffix}"
    key_prefix = f"bkai_{suffix[:4]}"
    token = f"{key_prefix}_testkey_viz_{suffix}"

    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO organizations (id, name) VALUES (%s, %s)",
                (org_id, f"viz-test-{suffix}"),
            )
            cur.execute(
                """
                INSERT INTO users (id, org_id, display_name, email_hash, role)
                VALUES (%s, %s, %s, %s, 'admin')
                """,
                (user_id, org_id, f"viz-{suffix}", suffix),
            )
            cur.execute(
                """
                INSERT INTO api_keys (user_id, org_id, key_hash, key_prefix,
                                      key_type, label)
                VALUES (%s, %s, crypt(%s, gen_salt('bf')), %s,
                        'interactive', %s)
                """,
                (user_id, org_id, token, key_prefix, f"viz-test-{suffix}"),
            )
            if entry_count > 0:
                # Bulk insert via generate_series — fast even at 3001 rows.
                cur.execute(
                    """
                    INSERT INTO entries (
                        org_id, title, content, content_hash, content_type,
                        logical_path, created_by, updated_by, source
                    )
                    SELECT
                        %s,
                        'viz-' || gs::text,
                        'body ' || gs::text,
                        encode(sha256(('viz-' || gs::text)::bytea), 'hex'),
                        'context',
                        %s || '/' || gs::text,
                        %s, %s, 'api'
                    FROM generate_series(1, %s) AS gs
                    """,
                    (org_id, f"viz-{suffix}", user_id, user_id, entry_count),
                )

    return org_id, user_id, key_prefix, token


def _cleanup_org(org_id: str, key_prefix: str) -> None:
    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM api_keys WHERE key_prefix = %s", (key_prefix,))
            cur.execute("DELETE FROM entry_links WHERE source_entry_id IN "
                        "(SELECT id FROM entries WHERE org_id = %s)", (org_id,))
            cur.execute("DELETE FROM entries WHERE org_id = %s", (org_id,))
            cur.execute("DELETE FROM users WHERE org_id = %s", (org_id,))
            cur.execute("DELETE FROM organizations WHERE id = %s", (org_id,))


@pytest.fixture
def fresh_org():
    """Factory fixture: callers request entry_count, get back a token."""
    created: list[tuple[str, str]] = []

    def _make(entry_count: int) -> str:
        org_id, _user_id, key_prefix, token = _provision_org(entry_count)
        created.append((org_id, key_prefix))
        return token

    yield _make

    for org_id, key_prefix in created:
        try:
            _cleanup_org(org_id, key_prefix)
        except Exception:
            pass


def _get_visualize(token: str | None) -> requests.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return requests.get(
        f"{BASE_URL}/import/visualize",
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )


def test_unauthorized_returns_401(fresh_org):
    resp = _get_visualize(token=None)
    assert resp.status_code == 401


def test_happy_path_renders_template_with_substituted_data(fresh_org):
    token = fresh_org(5)
    resp = _get_visualize(token)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    # Placeholder must be fully substituted.
    assert "__GRAPH_DATA_JSON__" not in body
    # Graph payload must be present.
    assert '"nodes"' in body
    assert '"edges"' in body
    # Brand chrome stays.
    assert "Brilliant" in body or "xiReactor" in body


def test_empty_org_renders_not_ready_card(fresh_org):
    token = fresh_org(0)
    resp = _get_visualize(token)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Graph not ready" in resp.text


def test_oversize_org_renders_demo_limits_card(fresh_org):
    # 3001 > _VIZ_NODE_CAP (3000) → server short-circuits to OVERSIZE card
    # before ever loading the heavy template.
    token = fresh_org(3001)
    resp = _get_visualize(token)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "exceeds the demo visualization limits" in resp.text
    # OVERSIZE branch must NOT render the heavy template.
    assert "__GRAPH_DATA_JSON__" not in resp.text
    assert '"nodes"' not in resp.text
