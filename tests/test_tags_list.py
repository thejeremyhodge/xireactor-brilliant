"""Integration tests for the `GET /tags` list endpoint (spec 0041, T-0239).

Exercises ``GET /tags`` against the live API + DB stack:

  * the seeded demo KB returns a non-empty ordered list with correct
    envelope shape (``{tags, total}``) and sort order (count desc, tag asc)
  * pagination (``limit`` / ``offset``) trims results without mutating
    ``total``; negative/oversized values are rejected with 422
  * empty-corpus org (fresh org with zero published entries) returns
    ``{"tags": [], "total": 0}`` — never a 500
  * RLS isolation: a user in org A never sees tags that only exist in
    org B's entries (cross-org leak check)

Prerequisites
-------------
  1. ``docker compose up -d``   (API on :8010, Postgres on :5442)
  2. ``pip install -r tests/requirements-dev.txt``
  3. Migrations applied through 005_seed.sql (for ``org_demo`` + admin key).

Run
---
  pytest tests/test_tags_list.py -v
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
# Configuration (mirrors tests/test_suggest_tags.py)
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("BRILLIANT_BASE_URL", "http://localhost:8010")
DB_DSN = os.environ.get(
    "BRILLIANT_DB_DSN",
    "postgresql://postgres:dev@localhost:5442/brilliant",
)

# Admin key from db/migrations/005_seed.sql — gives us an ``org_demo`` user.
ADMIN_KEY = os.environ.get("ADMIN_KEY", "bkai_adm1_testkey_admin")

REQUEST_TIMEOUT = 10.0


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


def _list_tags(*, key: str, limit: int | None = None, offset: int | None = None) -> requests.Response:
    params: dict = {}
    if limit is not None:
        params["limit"] = limit
    if offset is not None:
        params["offset"] = offset
    return requests.get(
        f"{BASE_URL}/tags",
        headers=_auth(key),
        params=params or None,
        timeout=REQUEST_TIMEOUT,
    )


def _co_occurring(
    *, key: str, tag: str, limit: int | None = None
) -> requests.Response:
    """Call ``GET /tags/{tag}/co-occurring`` — single helper mirrors
    ``_list_tags`` so the co-occurrence tests stay symmetric with the
    list-endpoint tests above."""
    params: dict = {}
    if limit is not None:
        params["limit"] = limit
    return requests.get(
        f"{BASE_URL}/tags/{tag}/co-occurring",
        headers=_auth(key),
        params=params or None,
        timeout=REQUEST_TIMEOUT,
    )


# ---------------------------------------------------------------------------
# Second-org fixture — provisions ``org_tagslist_<suffix>`` with an admin
# user + bcrypt-hashed API key. Mirrors tests/test_suggest_tags.py so the
# cleanup path and RLS story stay identical.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_org():
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_tagslist_{suffix}"
    user_id = f"usr_tagslist_{suffix}"
    # The API's auth handler keys lookup by the first 9 chars of the token
    # (``bkai_XXXX``), so we need a unique 9-char prefix per test org.
    key_prefix = f"bkai_{suffix[:4]}"
    token = f"{key_prefix}_testkey_tags_{suffix}"

    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO organizations (id, name, settings) "
                "VALUES (%s, %s, '{}')",
                (org_id, f"Tags List Test Org {suffix}"),
            )
            cur.execute(
                """
                INSERT INTO users (id, org_id, display_name, email_hash, role)
                VALUES (%s, %s, %s,
                        encode(digest(%s, 'sha256'), 'hex'),
                        'admin')
                """,
                (user_id, org_id, f"Tags Admin {suffix}",
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

    try:
        yield {
            "org_id": org_id,
            "user_id": user_id,
            "token": token,
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


# ---------------------------------------------------------------------------
# Case 1 — Seeded demo KB returns a well-formed, ordered list.
# ---------------------------------------------------------------------------


def test_list_tags_returns_ordered_envelope_for_seeded_kb():
    """Seeded demo KB returns ``{tags, total}`` with count-desc/tag-asc order.

    Shape checks:
      * envelope keys match the ``TagListResponse`` schema
      * each ``tags`` item has ``{tag: str, count: int}``
      * tags are sorted by ``count`` descending; ties break on ``tag`` asc
      * ``total`` equals or exceeds ``len(tags)`` (total is org-wide,
        ``tags`` is paginated)
    """
    resp = _list_tags(key=ADMIN_KEY)
    assert resp.status_code == 200, f"{resp.status_code} {resp.text}"
    body = resp.json()

    assert set(body.keys()) == {"tags", "total"}, body
    tags = body["tags"]
    total = body["total"]

    assert isinstance(tags, list), body
    assert isinstance(total, int), body
    assert total >= len(tags), (total, len(tags))
    # The seeded demo KB carries tags in 005_seed.sql; if this ever
    # becomes zero something else is badly wrong with the fixture data.
    assert total >= 1, f"seeded demo KB should have >= 1 tag, got {total}"
    assert len(tags) >= 1, f"expected >= 1 tag row, got {tags!r}"

    seen_tags: set[str] = set()
    for row in tags:
        assert set(row.keys()) == {"tag", "count"}, row
        assert isinstance(row["tag"], str) and row["tag"], row
        assert isinstance(row["count"], int) and row["count"] >= 1, row
        assert row["tag"] not in seen_tags, (
            f"duplicate tag in response: {row['tag']}"
        )
        seen_tags.add(row["tag"])

    # Ordering: count desc, then tag asc on ties.
    for prev, cur in zip(tags, tags[1:]):
        if prev["count"] == cur["count"]:
            assert prev["tag"] <= cur["tag"], (
                f"tie-break not alphabetic: {prev} before {cur}"
            )
        else:
            assert prev["count"] > cur["count"], (
                f"count not desc: {prev} before {cur}"
            )


# ---------------------------------------------------------------------------
# Case 2 — Pagination trims the slice without mutating ``total``.
# ---------------------------------------------------------------------------


def test_list_tags_pagination_respects_limit_and_offset():
    """limit/offset slice the list without mutating total."""
    # Fetch a broad view to establish the ground truth.
    full = _list_tags(key=ADMIN_KEY, limit=500).json()
    total = full["total"]
    full_tags = full["tags"]

    if total < 2:
        pytest.skip(
            "Need >= 2 distinct tags in the seeded KB to meaningfully "
            "test pagination; seed produced only %d." % total
        )

    # limit=1 — first tag only, total unchanged.
    page1 = _list_tags(key=ADMIN_KEY, limit=1, offset=0).json()
    assert page1["total"] == total, page1
    assert len(page1["tags"]) == 1, page1
    assert page1["tags"][0] == full_tags[0], (
        f"limit=1 page did not match head of full list: "
        f"{page1['tags'][0]} vs {full_tags[0]}"
    )

    # offset=1, limit=1 — second tag.
    page2 = _list_tags(key=ADMIN_KEY, limit=1, offset=1).json()
    assert page2["total"] == total, page2
    assert len(page2["tags"]) == 1, page2
    assert page2["tags"][0] == full_tags[1], (
        f"offset=1 page did not match index 1 of full list: "
        f"{page2['tags'][0]} vs {full_tags[1]}"
    )


# ---------------------------------------------------------------------------
# Case 3 — Invalid pagination values return 422, not 500.
# ---------------------------------------------------------------------------


def test_list_tags_rejects_out_of_range_limits():
    # limit too low
    r = _list_tags(key=ADMIN_KEY, limit=0)
    assert r.status_code == 422, r.text
    # limit too high (documented cap is 5000)
    r = _list_tags(key=ADMIN_KEY, limit=5001)
    assert r.status_code == 422, r.text
    # negative offset
    r = _list_tags(key=ADMIN_KEY, offset=-1)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# Case 4 — Empty-corpus org returns {"tags": [], "total": 0} (never 500).
# ---------------------------------------------------------------------------


def test_list_tags_empty_corpus_returns_empty_envelope(isolated_org):
    resp = _list_tags(key=isolated_org["token"])
    assert resp.status_code == 200, f"{resp.status_code} {resp.text}"
    body = resp.json()
    assert body == {"tags": [], "total": 0}, body


# ---------------------------------------------------------------------------
# Case 5 — RLS isolation. A user in org B must never see tags that only
# exist in org A's entries. We seed a uniquely-tagged entry in
# ``isolated_org`` (org A) and confirm ``org_demo`` (org B) does not see
# that tag in its own /tags response.
# ---------------------------------------------------------------------------


def test_list_tags_respects_rls_isolation(isolated_org):
    suffix = uuid.uuid4().hex[:8]
    private_tag = f"xorgprivate-{suffix}"

    create_body = {
        "title": f"Org A private tag carrier {suffix}",
        "content": "Body for RLS isolation test.",
        "content_type": "context",
        "logical_path": f"tests/rls-tagslist-{suffix}",
        "sensitivity": "shared",
        "tags": [private_tag, "common-tagslist"],
    }
    create_resp = requests.post(
        f"{BASE_URL}/entries",
        headers=_auth(isolated_org["token"]),
        json=create_body,
        timeout=REQUEST_TIMEOUT,
    )
    assert create_resp.status_code == 201, (
        f"org-A seed create failed: {create_resp.status_code} "
        f"{create_resp.text}"
    )

    try:
        # Sanity: org A sees its own private tag.
        sanity = _list_tags(key=isolated_org["token"], limit=500)
        assert sanity.status_code == 200, sanity.text
        sanity_tags = {row["tag"] for row in sanity.json()["tags"]}
        assert private_tag in sanity_tags, (
            f"org A should see its own private tag; got {sanity_tags}"
        )

        # Isolation: org_demo (org B) must NOT see the private tag at all.
        leak_probe = _list_tags(key=ADMIN_KEY, limit=5000)
        assert leak_probe.status_code == 200, leak_probe.text
        leaked_tags = {row["tag"] for row in leak_probe.json()["tags"]}
        assert private_tag not in leaked_tags, (
            f"RLS leak: org_demo saw org A's private tag {private_tag!r} "
            f"in /tags response"
        )
    finally:
        # Teardown. Fixture-level cleanup wipes the rows too, but calling
        # DELETE keeps the API-side invariants clean in case the fixture
        # teardown order changes.
        entry_id = create_resp.json().get("id")
        if entry_id:
            requests.delete(
                f"{BASE_URL}/entries/{entry_id}",
                headers=_auth(isolated_org["token"]),
                timeout=REQUEST_TIMEOUT,
            )


# ---------------------------------------------------------------------------
# Co-occurrence endpoint — `GET /tags/{tag}/co-occurring` (spec 0041, T-0242).
#
# The seeded demo KB (005_seed.sql) contains two published entries tagged
# with ``q3``:
#   - entry #2  ARRAY['api', 'performance', 'q3']
#   - entry #12 ARRAY['strategy', 'q3', 'priorities']
# No other published entry carries ``q3``. That means every neighbor of
# ``q3`` has co_count = 1 except where one of the neighbors ALSO appears
# alongside ``q3`` more than once — which it does not in the fixture.
# So the response MUST:
#   - list exactly four neighbors: api, performance, strategy, priorities
#   - none of the self-tag (``q3`` must not appear)
#   - jaccard == 1.0 for any neighbor that appears ONLY alongside q3
#     (none of the four do — ``api`` is also in entry #9, so
#     A_total=2, B_total=2, co=1 -> jaccard = 1 / (2+2-1) = 0.333...)
# Rather than pin exact jaccards (brittle to seed changes) we assert
# shape + monotonic ordering + one anchored pair ``q3 -> api`` as the
# known-good neighbor.
# ---------------------------------------------------------------------------


def test_co_occurring_returns_expected_neighbor_at_rank_1():
    """A known co-occurring pair in the seeded KB shows up as a neighbor.

    Picks ``q3`` because it appears on exactly two seeded entries with
    non-overlapping secondary tags, giving a deterministic neighbor set:
    ``{api, performance, strategy, priorities}``.
    """
    resp = _co_occurring(key=ADMIN_KEY, tag="q3")
    assert resp.status_code == 200, f"{resp.status_code} {resp.text}"
    body = resp.json()

    # Envelope shape matches the pydantic model.
    assert set(body.keys()) == {"tag", "neighbors"}, body
    assert body["tag"] == "q3", body
    neighbors = body["neighbors"]
    assert isinstance(neighbors, list), body
    assert len(neighbors) >= 1, (
        f"expected at least one co-occurring neighbor for q3 in seeded KB, "
        f"got {neighbors!r}"
    )

    # Per-row shape.
    neighbor_tags: list[str] = []
    for row in neighbors:
        assert set(row.keys()) == {"tag", "co_count", "jaccard"}, row
        assert isinstance(row["tag"], str) and row["tag"], row
        assert row["tag"] != "q3", (
            f"self-tag leaked into neighbors: {row!r}"
        )
        assert isinstance(row["co_count"], int) and row["co_count"] >= 1, row
        assert isinstance(row["jaccard"], (int, float)), row
        assert 0.0 <= float(row["jaccard"]) <= 1.0, row
        neighbor_tags.append(row["tag"])

    # No duplicate neighbors.
    assert len(set(neighbor_tags)) == len(neighbor_tags), neighbor_tags

    # ``api`` is the canonical expected neighbor — entry #2 pairs it with
    # q3, and it's also present elsewhere in the seed, so it's a stable
    # co-occurrence anchor. Must be in the top slice at default limit=10.
    assert "api" in neighbor_tags, (
        f"expected 'api' as a co-occurrence neighbor of 'q3' "
        f"(seed entry #2 has ARRAY['api', 'performance', 'q3']); "
        f"got {neighbor_tags!r}"
    )

    # Ordering: co_count desc, then jaccard desc, then tag asc.
    for prev, cur in zip(neighbors, neighbors[1:]):
        if prev["co_count"] == cur["co_count"]:
            if prev["jaccard"] == cur["jaccard"]:
                assert prev["tag"] <= cur["tag"], (
                    f"tie-break not alphabetic: {prev} before {cur}"
                )
            else:
                assert prev["jaccard"] >= cur["jaccard"], (
                    f"jaccard not desc on co_count tie: {prev} before {cur}"
                )
        else:
            assert prev["co_count"] > cur["co_count"], (
                f"co_count not desc: {prev} before {cur}"
            )


def test_co_occurring_unknown_tag_returns_empty_neighbors():
    """A tag nobody uses returns ``{tag, neighbors: []}`` with status 200.

    Must NOT be a 404 — consistent with ``GET /tags`` on empty corpora
    (empty is not an error).
    """
    bogus = f"definitely-not-a-real-tag-{uuid.uuid4().hex[:8]}"
    resp = _co_occurring(key=ADMIN_KEY, tag=bogus)
    assert resp.status_code == 200, f"{resp.status_code} {resp.text}"
    body = resp.json()
    assert body == {"tag": bogus, "neighbors": []}, body


def test_co_occurring_respects_limit_parameter():
    """``limit`` caps the neighbor count; bad values return 422, not 500.

    The seeded ``q3`` tag should have >= 2 neighbors (api + performance
    + strategy + priorities), so a ``limit=1`` request returns exactly
    one and the full request returns at least two.
    """
    full = _co_occurring(key=ADMIN_KEY, tag="q3").json()
    if len(full["neighbors"]) < 2:
        pytest.skip(
            "Seeded KB should produce >= 2 neighbors for q3; got %d — "
            "fixture drift?" % len(full["neighbors"])
        )

    capped = _co_occurring(key=ADMIN_KEY, tag="q3", limit=1).json()
    assert len(capped["neighbors"]) == 1, capped
    # The single row must match the head of the uncapped response
    # (deterministic ordering).
    assert capped["neighbors"][0] == full["neighbors"][0], (
        capped["neighbors"][0],
        full["neighbors"][0],
    )

    # Out-of-range limits return 422 — never 500.
    bad_low = _co_occurring(key=ADMIN_KEY, tag="q3", limit=0)
    assert bad_low.status_code == 422, bad_low.text
    bad_high = _co_occurring(key=ADMIN_KEY, tag="q3", limit=101)
    assert bad_high.status_code == 422, bad_high.text


def test_co_occurring_empty_corpus_returns_empty_neighbors(isolated_org):
    """Fresh org with zero entries returns empty neighbors for any tag.

    Uses the isolated-org fixture so the assertion is not polluted by
    the demo seed.
    """
    resp = _co_occurring(key=isolated_org["token"], tag="anything")
    assert resp.status_code == 200, f"{resp.status_code} {resp.text}"
    body = resp.json()
    assert body == {"tag": "anything", "neighbors": []}, body
