"""Integration tests for Permissions v2 (P1) — spec 0026, T-0139.

Exercises the unified `permissions` table against the live API + database stack.

Prerequisites:
  1. docker compose up -d   (API on :8010, Postgres on :5442)
  2. Migrations applied through 019_permissions_v2_rls.sql.
  3. pip install -r tests/requirements-dev.txt

Run:
  pytest tests/test_permissions_v2.py -v

Coverage
--------
1. User-principal entry grant → grantee gains write access.
2. Group-principal entry grant → group members inherit access; non-members blocked.
3. Removing a user from a group → access revoked immediately (no cache).
4. Multi-principal union → highest role wins (direct editor grant beats group
   viewer grant for the same user).
5. Backfilled legacy row (simulated by direct insert with principal_type='user')
   is still enforced by the RLS helper.
6. Audit rows recorded for every mutation (grant / revoke / group_create /
   group_delete / group_member_add / group_member_remove).

Why editor-write rather than viewer-read?
-----------------------------------------
The entries RLS policies apply a sensitivity ceiling to the ACL path. For
viewer/commenter/agent roles the ceiling matches (or is broader than) what a
grant can unlock, so a grant's observable effect on SELECT is often a no-op.
The write policies (INSERT/UPDATE/DELETE) for kb_editor have no sensitivity
ceiling, which means grants genuinely toggle behavior. We therefore drive
most tests through `PUT /entries/{id}` on admin-owned entries that the editor
user could otherwise read but not write.
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


# ---------------------------------------------------------------------------
# Configuration (mirrors tests/test_comments.py)
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("BRILLIANT_BASE_URL", "http://localhost:8010")
DB_DSN = os.environ.get(
    "BRILLIANT_DB_DSN",
    "postgresql://postgres:dev@localhost:5442/brilliant",
)

# Test API keys from db/migrations/005_seed.sql.
ADMIN_KEY = "bkai_adm1_testkey_admin"
EDITOR_KEY = "bkai_edit_testkey_editor"
COMMENTER_KEY = "bkai_comm_testkey_commenter"
VIEWER_KEY = "bkai_view_testkey_viewer"

# Corresponding user IDs.
USR_ADMIN = "usr_admin"
USR_EDITOR = "usr_editor"
USR_COMMENTER = "usr_commenter"
USR_VIEWER = "usr_viewer"

ORG_ID = "org_demo"
REQUEST_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# HTTP helpers
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


def _put(path: str, key: str, json: dict | None = None) -> requests.Response:
    return requests.put(
        f"{BASE_URL}{path}",
        headers=_headers(key),
        json=json or {},
        timeout=REQUEST_TIMEOUT,
    )


def _delete(path: str, key: str) -> requests.Response:
    return requests.delete(
        f"{BASE_URL}{path}",
        headers=_headers(key),
        timeout=REQUEST_TIMEOUT,
    )


def _api_available() -> bool:
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _api_available(),
    reason=f"Brilliant API not reachable at {BASE_URL} (start it with `docker compose up -d`).",
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _create_admin_entry(*, sensitivity: str = "operational") -> dict:
    """Create an entry owned by admin with no department.

    Editor can SELECT it (sensitivity in the editor ceiling) but cannot UPDATE
    it without an explicit grant — ideal for testing whether permission grants
    actually toggle access.
    """
    suffix = uuid.uuid4().hex[:10]
    title = f"perm2-test-{suffix}"
    body = {
        "title": title,
        "content": f"# {title}\nFixture entry for permissions v2 tests.",
        "content_type": "context",
        "logical_path": f"Tests/perm2/{suffix}",
        "sensitivity": sensitivity,
        "department": None,
        "tags": ["test", "perm2"],
    }
    r = _post("/entries", ADMIN_KEY, body)
    assert r.status_code == 201, f"entry create failed: {r.status_code} {r.text}"
    return r.json()


def _archive(entry_id: str) -> None:
    try:
        _delete(f"/entries/{entry_id}", ADMIN_KEY)
    except Exception:
        pass


def _create_group(name: str, description: str | None = None) -> dict:
    r = _post(
        "/groups",
        ADMIN_KEY,
        {"name": name, "description": description},
    )
    assert r.status_code == 201, f"group create failed: {r.status_code} {r.text}"
    return r.json()


def _delete_group(group_id: str) -> None:
    try:
        _delete(f"/groups/{group_id}", ADMIN_KEY)
    except Exception:
        pass


def _add_member(group_id: str, user_id: str) -> requests.Response:
    return _post(f"/groups/{group_id}/members", ADMIN_KEY, {"user_id": user_id})


def _remove_member(group_id: str, user_id: str) -> requests.Response:
    return _delete(f"/groups/{group_id}/members/{user_id}", ADMIN_KEY)


def _grant_entry_perm(
    entry_id: str,
    *,
    principal_type: str,
    principal_id: str,
    role: str,
) -> requests.Response:
    return _post(
        f"/entries/{entry_id}/permissions",
        ADMIN_KEY,
        {
            "principal_type": principal_type,
            "principal_id": principal_id,
            "role": role,
        },
    )


def _revoke_entry_perm(
    entry_id: str,
    *,
    principal_type: str,
    principal_id: str,
) -> requests.Response:
    return _delete(
        f"/entries/{entry_id}/permissions/{principal_id}?principal_type={principal_type}",
        ADMIN_KEY,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_entry():
    """Admin-owned entry with no department. Archived after the test."""
    e = _create_admin_entry()
    yield e
    _archive(e["id"])


@pytest.fixture
def group():
    """Ad-hoc group; deleted after the test."""
    g = _create_group(name=f"perm2-group-{uuid.uuid4().hex[:8]}")
    yield g
    _delete_group(g["id"])


# ---------------------------------------------------------------------------
# Direct-DB helpers (audit + legacy-backfill assertions)
# ---------------------------------------------------------------------------


def _require_psycopg():
    if not _PSYCOPG_AVAILABLE:
        pytest.skip("psycopg not installed; skipping DB-level assertion")


def _audit_actions(target_id: str, action_prefix: str = "") -> list[str]:
    """Return ordered list of audit_log.action values for a given target_id."""
    _require_psycopg()
    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            if action_prefix:
                cur.execute(
                    """
                    SELECT action FROM audit_log
                    WHERE target_id = %s AND action LIKE %s
                    ORDER BY id ASC
                    """,
                    (target_id, f"{action_prefix}%"),
                )
            else:
                cur.execute(
                    "SELECT action FROM audit_log WHERE target_id = %s ORDER BY id ASC",
                    (target_id,),
                )
            return [row[0] for row in cur.fetchall()]


def _insert_legacy_permission_row(
    *,
    entry_id: str,
    principal_id: str,
    role: str = "editor",
) -> str:
    """Insert a permissions row directly (bypassing the API) to simulate a
    backfilled legacy entry_permissions row. Returns the new permission id.
    """
    _require_psycopg()
    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO permissions (
                    org_id, principal_type, principal_id,
                    resource_type, entry_id, role, granted_by
                ) VALUES (%s, 'user', %s, 'entry', %s, %s, %s)
                RETURNING id
                """,
                (ORG_ID, principal_id, entry_id, role, USR_ADMIN),
            )
            row = cur.fetchone()
            return str(row[0])


def _delete_legacy_permission_row(permission_id: str) -> None:
    if not _PSYCOPG_AVAILABLE:
        return
    try:
        with psycopg.connect(DB_DSN, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM permissions WHERE id = %s", (permission_id,))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Test 1 — User-principal entry grant toggles write access
# ---------------------------------------------------------------------------


def test_user_principal_grant_toggles_write(admin_entry):
    """Editor cannot PUT admin-owned entry by default; a direct user grant
    with role='editor' unlocks write access."""
    entry_id = admin_entry["id"]

    # Baseline: editor can GET (sensitivity ceiling permits read) but cannot PUT.
    assert _get(f"/entries/{entry_id}", EDITOR_KEY).status_code == 200
    denied = _put(
        f"/entries/{entry_id}",
        EDITOR_KEY,
        {"title": admin_entry["title"] + " (editor attempt)"},
    )
    assert denied.status_code in (403, 404), (
        f"expected write block (403/404), got {denied.status_code}: {denied.text}"
    )

    # Grant editor role to usr_editor directly.
    g = _grant_entry_perm(
        entry_id,
        principal_type="user",
        principal_id=USR_EDITOR,
        role="editor",
    )
    assert g.status_code == 201, g.text

    # PUT now succeeds.
    new_title = admin_entry["title"] + " (post-grant)"
    ok = _put(f"/entries/{entry_id}", EDITOR_KEY, {"title": new_title})
    assert ok.status_code == 200, ok.text
    assert ok.json()["title"] == new_title


# ---------------------------------------------------------------------------
# Test 2 — Group-principal grant: members inherit, non-members blocked
# ---------------------------------------------------------------------------


def test_group_principal_grant_members_inherit(admin_entry, group):
    """A group-principal grant gives write access to group members only."""
    entry_id = admin_entry["id"]

    # Editor is not yet in the group — baseline PUT should fail.
    pre = _put(
        f"/entries/{entry_id}",
        EDITOR_KEY,
        {"title": admin_entry["title"] + " (pre-group)"},
    )
    assert pre.status_code in (403, 404), pre.text

    # Grant editor-role to the group.
    gr = _grant_entry_perm(
        entry_id,
        principal_type="group",
        principal_id=group["id"],
        role="editor",
    )
    assert gr.status_code == 201, gr.text

    # Editor is STILL blocked — not a member yet.
    still_blocked = _put(
        f"/entries/{entry_id}",
        EDITOR_KEY,
        {"title": admin_entry["title"] + " (no-member)"},
    )
    assert still_blocked.status_code in (403, 404), still_blocked.text

    # Commenter is also blocked (neither member nor editor role on DB).
    c_blocked = _put(
        f"/entries/{entry_id}",
        COMMENTER_KEY,
        {"title": admin_entry["title"] + " (commenter)"},
    )
    assert c_blocked.status_code in (403, 404), c_blocked.text

    # Add editor to the group.
    add = _add_member(group["id"], USR_EDITOR)
    assert add.status_code == 201, add.text

    # Editor can now PUT via group inheritance.
    new_title = admin_entry["title"] + " (via-group)"
    ok = _put(f"/entries/{entry_id}", EDITOR_KEY, {"title": new_title})
    assert ok.status_code == 200, ok.text
    assert ok.json()["title"] == new_title

    # Viewer (never added to the group) remains blocked from writing. kb_viewer
    # has no UPDATE privilege on entries at all — the DB-level GRANT, not RLS,
    # blocks the write. Any non-2xx status is acceptable here; what matters is
    # that non-members don't inherit group grants.
    v = _put(
        f"/entries/{entry_id}",
        VIEWER_KEY,
        {"title": admin_entry["title"] + " (viewer)"},
    )
    assert v.status_code >= 400, v.text


# ---------------------------------------------------------------------------
# Test 3 — Removing a user from a group revokes access immediately
# ---------------------------------------------------------------------------


def test_remove_from_group_revokes_access(admin_entry, group):
    """After group-based access is granted, removing the user from the group
    immediately revokes access (no cache; next request reflects the change)."""
    entry_id = admin_entry["id"]

    # Set up: group-grant + member.
    assert _grant_entry_perm(
        entry_id, principal_type="group", principal_id=group["id"], role="editor"
    ).status_code == 201
    assert _add_member(group["id"], USR_EDITOR).status_code == 201

    # Confirm editor can write via group.
    t1 = admin_entry["title"] + " (in-group)"
    ok = _put(f"/entries/{entry_id}", EDITOR_KEY, {"title": t1})
    assert ok.status_code == 200, ok.text

    # Remove from group.
    rm = _remove_member(group["id"], USR_EDITOR)
    assert rm.status_code == 200, rm.text

    # Very next request must fail — no materialization / cache.
    blocked = _put(
        f"/entries/{entry_id}",
        EDITOR_KEY,
        {"title": t1 + " (after-remove)"},
    )
    assert blocked.status_code in (403, 404), (
        f"access should be revoked immediately; got {blocked.status_code}: {blocked.text}"
    )


# ---------------------------------------------------------------------------
# Test 4 — Multi-principal union: highest role wins
# ---------------------------------------------------------------------------


def test_multi_principal_union_highest_role_wins(admin_entry, group):
    """User has viewer-via-group AND editor-via-direct grant on the same entry.
    The union should resolve to editor (highest role) — PUT succeeds."""
    entry_id = admin_entry["id"]

    # Group grant: viewer role.
    assert _grant_entry_perm(
        entry_id, principal_type="group", principal_id=group["id"], role="viewer"
    ).status_code == 201
    assert _add_member(group["id"], USR_EDITOR).status_code == 201

    # Baseline: viewer-only via group — editor cannot write yet because the
    # group grant is viewer role (not editor).
    blocked = _put(
        f"/entries/{entry_id}",
        EDITOR_KEY,
        {"title": admin_entry["title"] + " (viewer-only)"},
    )
    assert blocked.status_code in (403, 404), (
        f"viewer-role group grant should not enable write; got {blocked.status_code}"
    )

    # Add direct user grant with editor role — union now includes editor.
    assert _grant_entry_perm(
        entry_id, principal_type="user", principal_id=USR_EDITOR, role="editor"
    ).status_code == 201

    # Editor wins — PUT succeeds.
    new_title = admin_entry["title"] + " (editor-wins)"
    ok = _put(f"/entries/{entry_id}", EDITOR_KEY, {"title": new_title})
    assert ok.status_code == 200, ok.text
    assert ok.json()["title"] == new_title


# ---------------------------------------------------------------------------
# Test 5 — Legacy-backfill row is still enforced
# ---------------------------------------------------------------------------


def test_legacy_backfilled_row_still_enforced(admin_entry):
    """Migration 019 backfills pre-existing entry_permissions rows into the
    unified `permissions` table as principal_type='user'. We simulate one such
    row by inserting directly (bypassing the API / audit helper) and confirm
    the RLS helper honours it just like an API-created grant."""
    entry_id = admin_entry["id"]

    # Before: editor cannot PUT.
    pre = _put(
        f"/entries/{entry_id}",
        EDITOR_KEY,
        {"title": admin_entry["title"] + " (pre-legacy)"},
    )
    assert pre.status_code in (403, 404), pre.text

    # Insert a legacy-style row directly.
    perm_id = _insert_legacy_permission_row(
        entry_id=entry_id, principal_id=USR_EDITOR, role="editor"
    )

    try:
        # Editor can now PUT — RLS helper resolves the row transparently.
        ok = _put(
            f"/entries/{entry_id}",
            EDITOR_KEY,
            {"title": admin_entry["title"] + " (legacy-granted)"},
        )
        assert ok.status_code == 200, (
            f"legacy-backfilled permission should be enforced; got {ok.status_code}: {ok.text}"
        )
    finally:
        _delete_legacy_permission_row(perm_id)


# ---------------------------------------------------------------------------
# Test 6 — Audit rows recorded for each mutation
# ---------------------------------------------------------------------------


def test_audit_rows_for_grant_and_revoke(admin_entry):
    """POST /entries/{id}/permissions writes a 'grant' audit row keyed on the
    permission id; DELETE writes a corresponding 'revoke' row."""
    entry_id = admin_entry["id"]

    r = _grant_entry_perm(
        entry_id, principal_type="user", principal_id=USR_COMMENTER, role="viewer"
    )
    assert r.status_code == 201, r.text
    perm_id = r.json()["id"]

    actions = _audit_actions(perm_id)
    assert "grant" in actions, f"expected 'grant' audit row, got: {actions}"

    rv = _revoke_entry_perm(
        entry_id, principal_type="user", principal_id=USR_COMMENTER
    )
    assert rv.status_code == 200, rv.text

    actions_after = _audit_actions(perm_id)
    assert "revoke" in actions_after, (
        f"expected 'revoke' audit row, got: {actions_after}"
    )


def test_audit_rows_for_group_lifecycle():
    """Creating/deleting a group and adding/removing members emits the full
    set of group_* audit actions."""
    # Create the group and capture its id for audit lookups.
    g = _create_group(name=f"perm2-audit-{uuid.uuid4().hex[:8]}")
    group_id = g["id"]

    try:
        # group_create audit row keyed on group_id.
        create_actions = _audit_actions(group_id, action_prefix="group_")
        assert "group_create" in create_actions, (
            f"expected 'group_create', got: {create_actions}"
        )

        # Add + remove a member — audits are keyed on the group_id
        # (per api/routes/groups.py: target_id=row["group_id"]).
        add = _add_member(group_id, USR_EDITOR)
        assert add.status_code == 201, add.text
        rm = _remove_member(group_id, USR_EDITOR)
        assert rm.status_code == 200, rm.text

        member_actions = _audit_actions(group_id, action_prefix="group_")
        assert "group_member_add" in member_actions, member_actions
        assert "group_member_remove" in member_actions, member_actions
    finally:
        # Delete the group explicitly so we can assert the delete audit row.
        _delete_group(group_id)

    final_actions = _audit_actions(group_id, action_prefix="group_")
    assert "group_delete" in final_actions, (
        f"expected 'group_delete', got: {final_actions}"
    )


def test_audit_row_for_group_grant_on_entry(admin_entry, group):
    """A group-principal permission grant still records a 'grant' audit row."""
    entry_id = admin_entry["id"]
    r = _grant_entry_perm(
        entry_id,
        principal_type="group",
        principal_id=group["id"],
        role="editor",
    )
    assert r.status_code == 201, r.text
    perm_id = r.json()["id"]

    actions = _audit_actions(perm_id)
    assert "grant" in actions, f"expected 'grant' audit row, got: {actions}"
