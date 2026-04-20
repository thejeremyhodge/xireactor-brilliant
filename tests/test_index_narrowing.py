"""Integration tests for the `get_index` scale guard (spec 0041, T-0241).

The guard: at ``depth >= 2``, if the caller's visible published-entry total
exceeds 200 AND no narrowing filter (``path``, ``content_type``, or ``tag``)
is supplied, ``GET /index`` returns 422 with body
``{"error": "index_too_large", "total": N, "hint": "..."}``. L1
(``depth=1``, counts only) is never guarded and always returns 200
regardless of KB size.

Covers:
  1. depth=2 + no filter on a > 200-entry KB → 422 with the documented body.
  2. depth=2 + path="..." → 200 (guard clears when narrowing is present).
  3. depth=1 always works regardless of KB size (no guard ever).
  4. depth=2 + tag=... clears the guard and returns 200.

The test provisions an isolated org and bulk-inserts 201 published entries
directly via psycopg (skipping the API write path so the setup stays
deterministic and fast — 201 HTTP round-trips would swamp the test run).
All rows carry a unique per-run suffix on the ``logical_path`` and one
shared ``tag`` so the narrowing cases have something to filter on.

Prerequisites
-------------
  1. ``docker compose up -d``   (API on :8010, Postgres on :5442)
  2. ``pip install -r tests/requirements-dev.txt``
  3. Migrations applied through 005_seed.sql.

Run
---
  pytest tests/test_index_narrowing.py -v
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


# ---------------------------------------------------------------------------
# Configuration (mirrors tests/test_tags_list.py)
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("BRILLIANT_BASE_URL", "http://localhost:8010")
DB_DSN = os.environ.get(
    "BRILLIANT_DB_DSN",
    "postgresql://postgres:dev@localhost:5442/brilliant",
)

REQUEST_TIMEOUT = 15.0

# Guard triggers when total > 200, so we need at least 201 rows to push the
# isolated org past the threshold.
GUARD_THRESHOLD = 200
SEED_ENTRY_COUNT = 201


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


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


# ---------------------------------------------------------------------------
# Oversized-KB fixture — isolated org with >200 published entries. Direct
# psycopg inserts so setup stays fast; full RLS still applies on the read
# path under the returned API key.
# ---------------------------------------------------------------------------


@pytest.fixture
def large_org():
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_idxguard_{suffix}"
    user_id = f"usr_idxguard_{suffix}"
    # The API's auth handler keys lookup by the first 9 chars of the token
    # (``bkai_XXXX``), so we need a unique 9-char prefix per test org.
    key_prefix = f"bkai_{suffix[:4]}"
    token = f"{key_prefix}_testkey_idxg_{suffix}"

    # The shared tag every seeded entry carries — the guard-cleared
    # tag-filter case greps for this to prove narrowing brings the guard
    # back under the limit.
    shared_tag = f"guardtest-{suffix}"
    path_prefix = f"Projects/guardtest-{suffix}/"

    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO organizations (id, name, settings) "
                "VALUES (%s, %s, '{}')",
                (org_id, f"Index Guard Test Org {suffix}"),
            )
            cur.execute(
                """
                INSERT INTO users (id, org_id, display_name, email_hash, role)
                VALUES (%s, %s, %s,
                        encode(digest(%s, 'sha256'), 'hex'),
                        'admin')
                """,
                (user_id, org_id, f"Guard Admin {suffix}",
                 f"{user_id}@test.local"),
            )
            cur.execute(
                """
                INSERT INTO api_keys (user_id, org_id, key_hash, key_prefix,
                                      key_type, label)
                VALUES (%s, %s, crypt(%s, gen_salt('bf')), %s, 'interactive',
                        %s)
                """,
                (user_id, org_id, token, key_prefix, f"test-{suffix}"),
            )

            # Bulk-insert SEED_ENTRY_COUNT published entries. content_hash is
            # NOT NULL so we compute md5 of the content inline; source must
            # be one of the registered values (web_ui is the safest pick for
            # interactive-key provisioning).
            for i in range(SEED_ENTRY_COUNT):
                content = f"Guard test entry {i} for org {suffix}."
                cur.execute(
                    """
                    INSERT INTO entries (
                        org_id, title, content, content_hash,
                        content_type, logical_path, sensitivity, department,
                        owner_id, tags, source,
                        created_by, updated_by, status
                    ) VALUES (
                        %s, %s, %s, md5(%s),
                        'context', %s, 'shared', NULL,
                        %s, %s, 'web_ui',
                        %s, %s, 'published'
                    )
                    """,
                    (
                        org_id,
                        f"Guard Entry {i:03d} {suffix}",
                        content,
                        content,
                        f"{path_prefix}entry-{i:03d}",
                        user_id,
                        [shared_tag],
                        user_id,
                        user_id,
                    ),
                )

    try:
        yield {
            "org_id": org_id,
            "user_id": user_id,
            "token": token,
            "shared_tag": shared_tag,
            "path_prefix": path_prefix,
            "entry_count": SEED_ENTRY_COUNT,
        }
    finally:
        with psycopg.connect(DB_DSN, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM entry_versions WHERE org_id = %s", (org_id,)
                )
                cur.execute(
                    "DELETE FROM entry_links WHERE org_id = %s", (org_id,)
                )
                cur.execute("DELETE FROM entries WHERE org_id = %s", (org_id,))
                cur.execute(
                    "DELETE FROM api_keys WHERE user_id = %s", (user_id,)
                )
                cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
                cur.execute(
                    "DELETE FROM organizations WHERE id = %s", (org_id,)
                )


def _get_index(*, key: str, **params: object) -> requests.Response:
    # Drop None-valued params so they never hit the wire.
    clean = {k: v for k, v in params.items() if v is not None}
    return requests.get(
        f"{BASE_URL}/index",
        headers=_auth(key),
        params=clean or None,
        timeout=REQUEST_TIMEOUT,
    )


# ---------------------------------------------------------------------------
# Case 1 — depth >= 2 + no filter on a > 200-entry KB → 422 with the
# documented body shape.
# ---------------------------------------------------------------------------


def test_index_depth2_no_filter_over_threshold_returns_422(large_org):
    resp = _get_index(key=large_org["token"], depth=2)

    assert resp.status_code == 422, (
        f"expected 422 on depth=2 + no filter at {large_org['entry_count']} "
        f"entries; got {resp.status_code} {resp.text}"
    )
    body = resp.json()
    # The body must carry top-level keys (not FastAPI's default
    # `{"detail": ...}` wrapper) so agents can dispatch on `error` directly.
    assert set(body.keys()) >= {"error", "total", "hint"}, body
    assert body["error"] == "index_too_large", body
    assert isinstance(body["total"], int) and body["total"] > GUARD_THRESHOLD, body
    assert body["total"] >= large_org["entry_count"], body

    hint = body["hint"]
    assert isinstance(hint, str) and hint, body
    # The hint must name the narrowing options and point the caller at the
    # fallback tool so agents can auto-recover without guessing.
    for token in ("path=", "content_type=", "tag=", "search_entries"):
        assert token in hint, f"hint must mention {token!r}; got {hint!r}"


# ---------------------------------------------------------------------------
# Case 2 — depth=2 + path=... returns 200 (guard clears with any narrowing
# filter). The seeded org's entries all live under `path_prefix`, so the
# filter matches every one of them and the endpoint returns its normal
# IndexResponse shape.
# ---------------------------------------------------------------------------


def test_index_depth2_with_path_filter_clears_guard(large_org):
    resp = _get_index(
        key=large_org["token"],
        depth=2,
        path=large_org["path_prefix"],
    )

    assert resp.status_code == 200, (
        f"expected 200 with path filter; got {resp.status_code} {resp.text}"
    )
    body = resp.json()
    assert body["depth"] == 2, body
    assert isinstance(body.get("entries"), list), body
    assert len(body["entries"]) == large_org["entry_count"], (
        f"path filter should return every seeded entry; got "
        f"{len(body['entries'])} vs seeded {large_org['entry_count']}"
    )


# ---------------------------------------------------------------------------
# Case 3 — depth=1 (counts only) always works regardless of KB size. No
# filter, 201 entries, should still return 200 with the full
# categories/total shape.
# ---------------------------------------------------------------------------


def test_index_depth1_always_works_regardless_of_size(large_org):
    resp = _get_index(key=large_org["token"], depth=1)

    assert resp.status_code == 200, (
        f"L1 must always work; got {resp.status_code} {resp.text}"
    )
    body = resp.json()
    assert body["depth"] == 1, body
    assert body["total_entries"] >= large_org["entry_count"], body
    # L1 must NOT inline entries — that's what the guard exists to prevent.
    # `entries` is optional on the response schema, so either omitted or
    # empty/None is acceptable.
    assert not body.get("entries"), (
        f"L1 must not return entries; got {len(body.get('entries') or [])}"
    )


# ---------------------------------------------------------------------------
# Case 4 — depth=2 + tag=... clears the guard. Every seeded entry carries
# `shared_tag`, so the filter matches the full set but the presence of the
# tag= param alone is enough to bypass the guard.
# ---------------------------------------------------------------------------


def test_index_depth2_with_tag_filter_clears_guard(large_org):
    resp = _get_index(
        key=large_org["token"],
        depth=2,
        tag=large_org["shared_tag"],
    )

    assert resp.status_code == 200, (
        f"expected 200 with tag filter; got {resp.status_code} {resp.text}"
    )
    body = resp.json()
    assert body["depth"] == 2, body
    assert isinstance(body.get("entries"), list), body
    assert len(body["entries"]) == large_org["entry_count"], (
        f"tag filter should return every seeded entry; got "
        f"{len(body['entries'])} vs seeded {large_org['entry_count']}"
    )
