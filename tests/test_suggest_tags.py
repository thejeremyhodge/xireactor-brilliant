"""Integration tests for the `suggest_tags` endpoint (spec 0037, T-0210).

Exercises `POST /tags/suggest` against the live API + DB stack:

  * blog-post-length content against the seeded demo KB returns
    >= 3 suggestions with ``score > 0`` and ``usage_count`` populated
  * empty-corpus org (fresh org with zero published entries) returns
    ``{"suggestions": []}`` — never a 500
  * RLS isolation: a user in org A never sees tags that only exist
    in org B's entries, even when the content matches exactly

Prerequisites
-------------
  1. ``docker compose up -d``   (API on :8010, Postgres on :5442)
  2. ``pip install -r tests/requirements-dev.txt``
  3. Migrations applied through 005_seed.sql (for ``org_demo`` + admin key).

Run
---
  pytest tests/test_suggest_tags.py -v
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
# Configuration (mirrors tests/test_entries_write.py)
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


def _suggest(content: str, *, key: str, limit: int = 10) -> requests.Response:
    return requests.post(
        f"{BASE_URL}/tags/suggest",
        headers=_auth(key),
        json={"content": content, "limit": limit},
        timeout=REQUEST_TIMEOUT,
    )


# ---------------------------------------------------------------------------
# Second-org fixture — provisions ``org_suggesttags_<suffix>`` with an admin
# user + bcrypt-hashed API key directly in the DB. Mirrors the pattern in
# tests/test_attachments.py. Teardown drops everything we wrote so re-runs
# are idempotent.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_org():
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_suggesttags_{suffix}"
    user_id = f"usr_suggesttags_{suffix}"
    # The API's auth handler keys lookup by the first 9 chars of the token
    # (``bkai_XXXX``), so we need a unique 9-char prefix per test org.
    key_prefix = f"bkai_{suffix[:4]}"
    token = f"{key_prefix}_testkey_sugg_{suffix}"

    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO organizations (id, name, settings) "
                "VALUES (%s, %s, '{}')",
                (org_id, f"Suggest Tags Test Org {suffix}"),
            )
            cur.execute(
                """
                INSERT INTO users (id, org_id, display_name, email_hash, role)
                VALUES (%s, %s, %s,
                        encode(digest(%s, 'sha256'), 'hex'),
                        'admin')
                """,
                (user_id, org_id, f"Suggest Admin {suffix}",
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
# Case 1 — Blog-post-length content against the seeded demo KB returns
# at least 3 suggestions with ``score > 0`` and populated ``usage_count``.
#
# The content below deliberately namechecks several tags seeded in
# 005_seed.sql (api, mission, rls, strategy, q3, architecture, auth) so
# the substring ranker has plenty to match on. Word count ~500.
# ---------------------------------------------------------------------------


_BLOG_POST_CONTENT = """\
Building a Multi-Tenant Knowledge Base: Architecture Decisions

When designing a multi-tenant knowledge base, the mission is clear — every
team member needs governed access to institutional context without leaking
data across org boundaries. Our architecture leans on PostgreSQL row-level
security (RLS) as the primary isolation boundary, which keeps the surface
area small and auditable.

The API layer is a thin FastAPI shell over a connection pool. Every request
carries a Bearer token, and auth middleware resolves the token into a
user-scoped session that sets org_id, user_id, role, and department as
session variables. RLS policies reference those session variables, so
tenant isolation is enforced at the query level regardless of which route
you hit. This matters for our mission: an agent writing via the MCP server
must never see another org's rows, even on a hand-written SQL query.

We chose RLS over per-client instances for three reasons. First, the
operational overhead of running N databases doesn't scale with a lean team.
Second, RLS composes cleanly with our governance tiers — Tier 3+ items
stay pending until human review regardless of tenancy. Third, the multi-
tenant shape lines up with our Q3 strategy: onboard teams fast, charge
per-seat, and let the database do the heavy lifting on isolation.

Content types in our schema are registered, not hardcoded — the
content_type_registry table lets admins extend the taxonomy without code
changes. This supports the broader API strategy of making the knowledge
base a living structure, not a rigid schema. Every entry carries tags, and
those tags drive search facets, graph traversal, and now tag suggestion.

Tag suggestion is a first-class tool. Given free-form content, we rank the
org's existing tag corpus by how well each tag matches the input. The
ranker is deterministic — case-insensitive substring match with a whole-
word bonus, weighted by usage count. No LLM call, no vector search. This
keeps the latency predictable and the architecture simple.

Auth flows are uniform across the API: the /auth/login endpoint returns a
full API key (no JWT), and every subsequent request uses Bearer auth. The
auth layer and the RLS policies are the only two places where tenant
isolation is enforced, which makes security review tractable. Agents use
dedicated agent keys that route writes through the staging pipeline — no
direct writes from an agent-scoped key.

Our architecture exercises this thoroughly: the API, the staging pipeline,
the governance ladder, and the MCP surface all share the same auth and RLS
primitives. The mission holds — every team, every agent, the same context.
"""


def test_suggest_tags_returns_at_least_three_for_blog_post():
    """Blog-post-length content returns >= 3 ranked suggestions against the
    seeded demo KB — each with ``score > 0`` and ``usage_count`` populated."""
    resp = _suggest(_BLOG_POST_CONTENT, key=ADMIN_KEY, limit=10)
    assert resp.status_code == 200, f"{resp.status_code} {resp.text}"

    body = resp.json()
    assert "suggestions" in body, body
    suggestions = body["suggestions"]
    assert isinstance(suggestions, list)
    assert len(suggestions) >= 3, (
        f"expected >= 3 suggestions against the seeded demo KB, got "
        f"{len(suggestions)}: {suggestions}"
    )

    # Every suggestion must carry the documented shape with positive score.
    seen_tags: set[str] = set()
    for s in suggestions:
        assert set(s.keys()) >= {"tag", "score", "usage_count"}, s
        assert isinstance(s["tag"], str) and s["tag"], s
        assert s["score"] > 0, s
        assert isinstance(s["usage_count"], int)
        assert s["usage_count"] >= 1, s
        # No duplicate tags in the response.
        assert s["tag"] not in seen_tags, f"duplicate tag in suggestions: {s['tag']}"
        seen_tags.add(s["tag"])

    # Results must be sorted by score descending.
    scores = [s["score"] for s in suggestions]
    assert scores == sorted(scores, reverse=True), (
        f"suggestions not sorted by score desc: {scores}"
    )


# ---------------------------------------------------------------------------
# Case 2 — Empty-corpus org returns {"suggestions": []} without crashing.
#
# The ``isolated_org`` fixture provisions a fresh org with zero entries, so
# the ``SELECT unnest(tags)`` aggregate returns no rows. The endpoint must
# respond 200 with an empty suggestions array — not a 500, not a 404.
# ---------------------------------------------------------------------------


def test_suggest_tags_empty_corpus_returns_empty_list(isolated_org):
    resp = _suggest(
        _BLOG_POST_CONTENT, key=isolated_org["token"], limit=10
    )
    assert resp.status_code == 200, f"{resp.status_code} {resp.text}"
    body = resp.json()
    assert body == {"suggestions": []}, body


# ---------------------------------------------------------------------------
# Case 3 — RLS isolation. A user in org B must never see tags that only
# exist in org A's entries, even when the content matches verbatim.
#
# We seed a uniquely-tagged entry in ``isolated_org`` (org A) with a tag
# that doesn't appear anywhere in ``org_demo``'s corpus, then call
# /tags/suggest from ``org_demo`` (org B) with content that embeds that
# exact tag. The org B response must not include the org-A-only tag.
# ---------------------------------------------------------------------------


def test_suggest_tags_respects_rls_isolation(isolated_org):
    # Pick a tag that is extremely unlikely to exist in org_demo's seed.
    suffix = uuid.uuid4().hex[:8]
    private_tag = f"xorgprivate-{suffix}"

    # Seed a published entry in org A carrying the private tag. Use the
    # org-A admin's token so the write lands via the normal API path —
    # which exercises RLS rather than bypassing it.
    create_body = {
        "title": f"Org A private tag carrier {suffix}",
        "content": "Body for RLS isolation test.",
        "content_type": "context",
        "logical_path": f"tests/rls-suggest-{suffix}",
        "sensitivity": "shared",
        "tags": [private_tag, "common"],
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
        # Sanity: org A sees its own tag.
        sanity = _suggest(
            f"Reference to {private_tag} and common tooling.",
            key=isolated_org["token"],
            limit=20,
        )
        assert sanity.status_code == 200, sanity.text
        sanity_tags = {s["tag"] for s in sanity.json()["suggestions"]}
        assert private_tag in sanity_tags, (
            f"org A should see its own private tag; got {sanity_tags}"
        )

        # Isolation: org B (``org_demo``) must NOT see the private tag even
        # when the content literally contains it. This is the RLS check.
        leak_probe = _suggest(
            f"Content deliberately name-checking {private_tag} plus "
            f"mission and api for ballast.",
            key=ADMIN_KEY,
            limit=50,
        )
        assert leak_probe.status_code == 200, leak_probe.text
        leaked_tags = {s["tag"] for s in leak_probe.json()["suggestions"]}
        assert private_tag not in leaked_tags, (
            f"RLS leak: org_demo saw org A's private tag {private_tag!r} "
            f"in suggestions {leaked_tags}"
        )
    finally:
        # Teardown: archive the org-A entry. The fixture's DB-level cleanup
        # removes the rows anyway, but calling DELETE keeps the API-side
        # invariants clean in case the fixture teardown order changes.
        entry_id = create_resp.json().get("id")
        if entry_id:
            requests.delete(
                f"{BASE_URL}/entries/{entry_id}",
                headers=_auth(isolated_org["token"]),
                timeout=REQUEST_TIMEOUT,
            )
