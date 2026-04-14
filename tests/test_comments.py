"""Integration tests for the comments subsystem (spec 0026, T-0133).

These tests exercise the live API + database stack:

  Prerequisites:
    1. docker compose up -d   (API on :8010, Postgres on :5442)
    2. Migrations applied through 017_comments.sql (and ideally 019).
    3. pip install -r tests/requirements-dev.txt

  Run:
    pytest tests/test_comments.py -v

Pattern: roughly mirrors tests/demo_e2e.sh's HTTP-driven approach but as
pytest functions with proper fixtures + isolation. We create a fresh entry
per test (under a unique logical_path) so tests don't interfere with each
other or the seed data.

Audit-log assertions
--------------------
T-0138 will wire the audit_log writes inside the comments handlers. Until
that lands, the create/resolve/escalate INSERTs into audit_log do not
happen (the handlers carry TODO(T-0138) markers). The audit-test cases
below are written and ready, but skipped via `pytest.mark.skip` linked to
T-0138. Flip the skip to xfail->pass once T-0138 lands.

Worker chose option (a) from the task brief: keep the test code in this
file so T-0138 only has to remove a single decorator to activate them,
rather than adding a new file later.
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


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("CORTEX_BASE_URL", "http://localhost:8010")
DB_DSN = os.environ.get(
    "CORTEX_DB_DSN",
    "postgresql://postgres:dev@localhost:5442/cortex",
)

# Test API keys from db/migrations/005_seed.sql.
ADMIN_KEY = "bkai_adm1_testkey_admin"
EDITOR_KEY = "bkai_edit_testkey_editor"
COMMENTER_KEY = "bkai_comm_testkey_commenter"
VIEWER_KEY = "bkai_view_testkey_viewer"
AGENT_KEY = "bkai_agnt_testkey_agent"

# Corresponding user IDs (also from seed).
USR_ADMIN = "usr_admin"
USR_EDITOR = "usr_editor"
USR_COMMENTER = "usr_commenter"
USR_VIEWER = "usr_viewer"

REQUEST_TIMEOUT = 10.0

# Skip reason used by the audit-log tests until T-0138 lands.
AUDIT_SKIP_REASON = (
    "Audit-log writes for comment_create/resolve/escalate land in T-0138; "
    "remove this skip once that task ships."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _get(path: str, key: str) -> requests.Response:
    return requests.get(f"{BASE_URL}{path}", headers=_headers(key), timeout=REQUEST_TIMEOUT)


def _post(path: str, key: str, json: dict | None = None) -> requests.Response:
    return requests.post(
        f"{BASE_URL}{path}",
        headers=_headers(key),
        json=json or {},
        timeout=REQUEST_TIMEOUT,
    )


def _patch(path: str, key: str, json: dict | None = None) -> requests.Response:
    return requests.patch(
        f"{BASE_URL}{path}",
        headers=_headers(key),
        json=json or {},
        timeout=REQUEST_TIMEOUT,
    )


def _api_available() -> bool:
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


# Skip the entire module if the API isn't reachable. Surfaces as a clear
# message rather than a wall of connection errors.
pytestmark = pytest.mark.skipif(
    not _api_available(),
    reason=f"Brilliant API not reachable at {BASE_URL} (start it with `docker compose up -d`).",
)


def _create_entry(
    *,
    key: str = ADMIN_KEY,
    title: str | None = None,
    sensitivity: str = "shared",
    path_prefix: str = "Tests/comments",
) -> dict:
    """Create a fresh entry and return its JSON body."""
    suffix = uuid.uuid4().hex[:10]
    title = title or f"comments-test-{suffix}"
    body = {
        "title": title,
        "content": f"# {title}\nFixture entry for comment tests.",
        "content_type": "context",
        "logical_path": f"{path_prefix}/{suffix}",
        "sensitivity": sensitivity,
        "tags": ["test", "comments"],
    }
    r = _post("/entries", key, body)
    assert r.status_code == 201, f"entry create failed: {r.status_code} {r.text}"
    return r.json()


def _archive_entry(entry_id: str, key: str = ADMIN_KEY) -> None:
    """Best-effort cleanup. Errors are swallowed."""
    try:
        requests.delete(
            f"{BASE_URL}/entries/{entry_id}",
            headers=_headers(key),
            timeout=REQUEST_TIMEOUT,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def entry():
    """A fresh entry owned by admin. Archived after the test."""
    e = _create_entry(key=ADMIN_KEY)
    yield e
    _archive_entry(e["id"])


@pytest.fixture
def editor_entry():
    """A fresh entry owned by the editor. Archived after the test."""
    e = _create_entry(key=EDITOR_KEY)
    yield e
    _archive_entry(e["id"])


# ---------------------------------------------------------------------------
# Tests — author-kind, round-trip, list
# ---------------------------------------------------------------------------


def test_create_and_list_round_trip(entry):
    """Admin creates a comment, then lists it back with correct fields."""
    create = _post(
        f"/entries/{entry['id']}/comments",
        ADMIN_KEY,
        {"body": "first comment"},
    )
    assert create.status_code == 201, create.text
    created = create.json()

    assert created["entry_id"] == entry["id"]
    assert created["author_id"] == USR_ADMIN
    assert created["author_kind"] == "user"
    assert created["body"] == "first comment"
    assert created["status"] == "open"
    assert created["resolved_at"] is None
    assert created["resolved_by"] is None
    assert created["escalated_to"] is None
    assert created["parent_comment_id"] is None

    listed = _get(f"/entries/{entry['id']}/comments", ADMIN_KEY)
    assert listed.status_code == 200
    rows = listed.json()
    ids = [r["id"] for r in rows]
    assert created["id"] in ids


def test_agent_authored_flag(entry):
    """A comment posted with an agent key gets author_kind='agent'."""
    r = _post(
        f"/entries/{entry['id']}/comments",
        AGENT_KEY,
        {"body": "agent observation"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["author_kind"] == "agent"
    # The agent test key in seed maps to usr_editor — confirm the API records
    # the underlying user, not a synthetic agent identity.
    assert body["author_id"] == USR_EDITOR


def test_commenter_can_post(entry):
    """commenter role is allowed to POST a comment."""
    r = _post(
        f"/entries/{entry['id']}/comments",
        COMMENTER_KEY,
        {"body": "commenter chiming in"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["author_id"] == USR_COMMENTER


# ---------------------------------------------------------------------------
# Tests — viewer permission boundaries
# ---------------------------------------------------------------------------


def test_viewer_can_read_comments(entry):
    """Viewer GETs the list successfully (even if empty)."""
    # Seed a comment so the list is non-trivial.
    _post(f"/entries/{entry['id']}/comments", ADMIN_KEY, {"body": "visible to viewer"})
    r = _get(f"/entries/{entry['id']}/comments", VIEWER_KEY)
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)


def test_viewer_cannot_post_comment(entry):
    """Viewer POST is rejected — viewer role lacks INSERT on comments."""
    r = _post(
        f"/entries/{entry['id']}/comments",
        VIEWER_KEY,
        {"body": "viewer should not be able to post"},
    )
    # Viewer might be blocked at the GRANT layer (mapped to 403 by handler) or
    # at RLS WITH CHECK. Either is acceptable; what's not acceptable is 201/200.
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"


# ---------------------------------------------------------------------------
# Tests — entry visibility enforcement
# ---------------------------------------------------------------------------


def test_comment_on_unknown_entry_404():
    """Posting against a non-existent entry returns 404."""
    fake_id = str(uuid.uuid4())
    r = _post(f"/entries/{fake_id}/comments", ADMIN_KEY, {"body": "ghost"})
    assert r.status_code == 404


def test_list_on_unknown_entry_404():
    fake_id = str(uuid.uuid4())
    r = _get(f"/entries/{fake_id}/comments", ADMIN_KEY)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Tests — status transitions / authorization
# ---------------------------------------------------------------------------


def test_owner_can_resolve_others_comment(editor_entry):
    """The entry owner can resolve a comment authored by someone else."""
    # Commenter posts on editor-owned entry.
    r = _post(
        f"/entries/{editor_entry['id']}/comments",
        COMMENTER_KEY,
        {"body": "needs attention"},
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    # Editor (owner) resolves it.
    r2 = _patch(f"/comments/{cid}", EDITOR_KEY, {"status": "resolved"})
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == "resolved"
    assert body["resolved_by"] == USR_EDITOR
    assert body["resolved_at"] is not None


def test_non_author_non_owner_cannot_resolve(editor_entry):
    """A third-party (commenter) cannot resolve admin's comment on editor's entry."""
    r = _post(
        f"/entries/{editor_entry['id']}/comments",
        ADMIN_KEY,
        {"body": "admin says hi"},
    )
    assert r.status_code == 201
    cid = r.json()["id"]

    # Commenter is not the author, not the owner, not an admin.
    r2 = _patch(f"/comments/{cid}", COMMENTER_KEY, {"status": "resolved"})
    assert r2.status_code == 403, f"expected 403, got {r2.status_code}: {r2.text}"


def test_author_can_resolve_own_comment(entry):
    """The author can resolve their own comment."""
    r = _post(
        f"/entries/{entry['id']}/comments",
        COMMENTER_KEY,
        {"body": "my own comment"},
    )
    assert r.status_code == 201
    cid = r.json()["id"]

    r2 = _patch(f"/comments/{cid}", COMMENTER_KEY, {"status": "dismissed"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "dismissed"


def test_escalate_records_escalated_to(entry):
    """PATCH status=escalated stores the escalated_to user id."""
    r = _post(
        f"/entries/{entry['id']}/comments",
        ADMIN_KEY,
        {"body": "escalation candidate"},
    )
    cid = r.json()["id"]

    r2 = _patch(
        f"/comments/{cid}",
        ADMIN_KEY,
        {"status": "escalated", "escalated_to": USR_EDITOR},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == "escalated"
    assert body["escalated_to"] == USR_EDITOR
    # Escalation should NOT stamp resolved_*.
    assert body["resolved_at"] is None
    assert body["resolved_by"] is None


def test_escalate_requires_escalated_to(entry):
    """status=escalated without escalated_to is a 422."""
    r = _post(f"/entries/{entry['id']}/comments", ADMIN_KEY, {"body": "x"})
    cid = r.json()["id"]
    r2 = _patch(f"/comments/{cid}", ADMIN_KEY, {"status": "escalated"})
    assert r2.status_code == 422


def test_resolved_comment_cannot_reopen(entry):
    """resolved is terminal in P1 — re-resolving returns 409."""
    r = _post(f"/entries/{entry['id']}/comments", ADMIN_KEY, {"body": "x"})
    cid = r.json()["id"]
    assert _patch(f"/comments/{cid}", ADMIN_KEY, {"status": "resolved"}).status_code == 200
    r3 = _patch(f"/comments/{cid}", ADMIN_KEY, {"status": "resolved"})
    assert r3.status_code == 409, r3.text


def test_escalated_can_be_resolved(entry):
    """escalated → resolved is allowed."""
    r = _post(f"/entries/{entry['id']}/comments", ADMIN_KEY, {"body": "x"})
    cid = r.json()["id"]
    _patch(
        f"/comments/{cid}",
        ADMIN_KEY,
        {"status": "escalated", "escalated_to": USR_EDITOR},
    )
    r3 = _patch(f"/comments/{cid}", ADMIN_KEY, {"status": "resolved"})
    assert r3.status_code == 200, r3.text
    assert r3.json()["status"] == "resolved"


# ---------------------------------------------------------------------------
# Tests — reply threading
# ---------------------------------------------------------------------------


def test_reply_threads_under_parent(entry):
    parent = _post(f"/entries/{entry['id']}/comments", ADMIN_KEY, {"body": "parent"})
    parent_id = parent.json()["id"]

    reply = _post(f"/comments/{parent_id}/replies", EDITOR_KEY, {"body": "child"})
    assert reply.status_code == 201, reply.text
    reply_body = reply.json()
    assert reply_body["parent_comment_id"] == parent_id
    assert reply_body["entry_id"] == entry["id"]

    # List should contain both, with the reply pointing at the parent.
    listed = _get(f"/entries/{entry['id']}/comments", ADMIN_KEY).json()
    by_id = {r["id"]: r for r in listed}
    assert parent_id in by_id and reply_body["id"] in by_id
    assert by_id[reply_body["id"]]["parent_comment_id"] == parent_id


def test_create_with_parent_in_different_entry_422(entry, editor_entry):
    """parent_comment_id must reference a comment on the same entry."""
    other = _post(
        f"/entries/{editor_entry['id']}/comments",
        ADMIN_KEY,
        {"body": "elsewhere"},
    )
    other_id = other.json()["id"]

    r = _post(
        f"/entries/{entry['id']}/comments",
        ADMIN_KEY,
        {"body": "wrong parent", "parent_comment_id": other_id},
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# Tests — audit log (skipped pending T-0138)
# ---------------------------------------------------------------------------


def _audit_rows_for_target(target_id: str, action_prefix: str = "comment_") -> list[dict]:
    """Read audit_log rows for a given target_id directly from Postgres.

    Uses a privileged DSN so we can bypass per-role grants on audit_log.
    """
    if not _PSYCOPG_AVAILABLE:
        pytest.skip("psycopg not installed; skipping DB-level audit assertions")

    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT action, actor_id, target_table, target_id
                FROM audit_log
                WHERE target_id = %s AND action LIKE %s
                ORDER BY id ASC
                """,
                (target_id, f"{action_prefix}%"),
            )
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def test_audit_row_on_create(entry):
    """Creating a comment writes a `comment_create` audit row."""
    r = _post(f"/entries/{entry['id']}/comments", ADMIN_KEY, {"body": "audit me"})
    cid = r.json()["id"]

    rows = _audit_rows_for_target(cid)
    actions = [row["action"] for row in rows]
    assert "comment_create" in actions, f"missing comment_create in {actions}"
    create_row = next(r for r in rows if r["action"] == "comment_create")
    assert create_row["actor_id"] == USR_ADMIN
    assert create_row["target_table"] == "comments"


def test_audit_row_on_resolve(entry):
    """Resolving a comment writes a `comment_resolve` audit row."""
    r = _post(f"/entries/{entry['id']}/comments", ADMIN_KEY, {"body": "x"})
    cid = r.json()["id"]
    _patch(f"/comments/{cid}", ADMIN_KEY, {"status": "resolved"})

    actions = [row["action"] for row in _audit_rows_for_target(cid)]
    assert "comment_create" in actions
    assert "comment_resolve" in actions


def test_audit_row_on_escalate(entry):
    """Escalating a comment writes a `comment_escalate` audit row."""
    r = _post(f"/entries/{entry['id']}/comments", ADMIN_KEY, {"body": "x"})
    cid = r.json()["id"]
    _patch(
        f"/comments/{cid}",
        ADMIN_KEY,
        {"status": "escalated", "escalated_to": USR_EDITOR},
    )

    actions = [row["action"] for row in _audit_rows_for_target(cid)]
    assert "comment_escalate" in actions
