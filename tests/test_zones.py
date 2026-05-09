"""Integration tests for Personal Zones (Sprint 0051, spec 0051).

Exercises the personal-zones surface end-to-end against the live API +
database stack:

  * provision_user_zone trigger + backfill
  * default-write-to-zone behavior on POST /entries
  * RLS isolation between users (zone privacy)
  * additive promotion via POST /zone/promote
  * zone-group immutability through the API surface
  * provision_user_zone idempotency
  * MCP-path happy case (promote_entry through mcp/tools.py)

Prerequisites:
  1. docker compose up -d   (API on :8010, Postgres on :5442)
  2. Migrations applied through 034_personal_zones.sql.
  3. db/seed/demo.sql applied (users usr_admin/usr_editor/usr_commenter).
  4. pip install -r tests/requirements-dev.txt

Run:
  pytest tests/test_zones.py -v

Modeled on tests/test_permissions_v2.py — same fixture style, same seed
users, same DB-DSN/BASE-URL env knobs.
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
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("BRILLIANT_BASE_URL", "http://localhost:8010")
DB_DSN = os.environ.get(
    "BRILLIANT_DB_DSN",
    "postgresql://postgres:dev@localhost:5442/brilliant",
)

# Seed test API keys from db/seed/demo.sql.
ADMIN_KEY = "bkai_adm1_testkey_admin"
EDITOR_KEY = "bkai_edit_testkey_editor"
COMMENTER_KEY = "bkai_comm_testkey_commenter"

# Corresponding user IDs.
USR_ADMIN = "usr_admin"
USR_EDITOR = "usr_editor"
USR_COMMENTER = "usr_commenter"

ORG_ID = "org_demo"
REQUEST_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _get(path: str, key: str, params: dict | None = None) -> requests.Response:
    return requests.get(
        f"{BASE_URL}{path}",
        headers=_headers(key),
        params=params or {},
        timeout=REQUEST_TIMEOUT,
    )


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
# Direct-DB helpers
# ---------------------------------------------------------------------------


def _require_psycopg():
    if not _PSYCOPG_AVAILABLE:
        pytest.skip("psycopg not installed; skipping DB-level assertion")


def _zone_group_id(user_id: str, org_id: str = ORG_ID) -> str | None:
    """Return the zone group id for a user, or None if missing."""
    _require_psycopg()
    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text FROM groups
                WHERE org_id = %s AND owner_user_id = %s AND is_zone = TRUE
                """,
                (org_id, user_id),
            )
            row = cur.fetchone()
            return row[0] if row else None


def _zone_member_count(group_id: str) -> int:
    _require_psycopg()
    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM group_members WHERE group_id = %s",
                (group_id,),
            )
            return cur.fetchone()[0]


def _zone_count(user_id: str, org_id: str = ORG_ID) -> int:
    _require_psycopg()
    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM groups
                WHERE org_id = %s AND owner_user_id = %s AND is_zone = TRUE
                """,
                (org_id, user_id),
            )
            return cur.fetchone()[0]


def _entry_zone_perm_exists(entry_id: str, zone_group_id: str) -> bool:
    _require_psycopg()
    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM permissions
                WHERE resource_type = 'entry'
                  AND entry_id = %s
                  AND principal_type = 'group'
                  AND principal_id = %s
                  AND role = 'admin'
                LIMIT 1
                """,
                (entry_id, zone_group_id),
            )
            return cur.fetchone() is not None


def _call_provision_user_zone(user_id: str, org_id: str = ORG_ID) -> str:
    """Invoke the SECURITY DEFINER function directly (as superuser)."""
    _require_psycopg()
    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT provision_user_zone(%s, %s)::text", (user_id, org_id))
            return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Entry helpers
# ---------------------------------------------------------------------------


def _create_entry(key: str, **overrides) -> dict:
    """Create an entry as the given API key. ``sensitivity`` defaults to None
    (omitted) so the personal-zones default-write path triggers."""
    suffix = uuid.uuid4().hex[:10]
    body = {
        "title": overrides.pop("title", f"zone-test-{suffix}"),
        "content": overrides.pop("content", f"# zone-test-{suffix}\nFixture body."),
        "content_type": overrides.pop("content_type", "context"),
        "logical_path": overrides.pop("logical_path", f"Tests/zones/{suffix}"),
        "tags": overrides.pop("tags", ["test", "zones"]),
    }
    if "sensitivity" in overrides:
        body["sensitivity"] = overrides.pop("sensitivity")
    body.update(overrides)
    r = _post("/entries", key, body)
    assert r.status_code == 201, f"entry create failed: {r.status_code} {r.text}"
    return r.json()


def _archive(entry_id: str) -> None:
    try:
        _delete(f"/entries/{entry_id}", ADMIN_KEY)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Test 1 — Provisioning: every seed user has a zone group + member row.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("user_id", [USR_ADMIN, USR_EDITOR, USR_COMMENTER])
def test_seed_users_have_zone_groups(user_id):
    """The trigger + backfill in migration 034 ensures every existing user
    has exactly one zone group, with the user as sole member."""
    zone_id = _zone_group_id(user_id)
    assert zone_id is not None, f"user {user_id} has no zone group"
    assert _zone_count(user_id) == 1, (
        f"user {user_id} has multiple zone groups (expected exactly 1)"
    )
    # Only the owner is a member.
    assert _zone_member_count(zone_id) == 1, (
        f"zone {zone_id} for {user_id} has unexpected membership count"
    )


# ---------------------------------------------------------------------------
# Test 2 — Backfill smoke: seed user count == zone group count.
# ---------------------------------------------------------------------------


def test_backfill_covers_all_seed_users():
    """Backfill loop in migration 034 hits every users row."""
    _require_psycopg()
    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users WHERE org_id = %s", (ORG_ID,))
            user_count = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM groups WHERE org_id = %s AND is_zone = TRUE",
                (ORG_ID,),
            )
            zone_count = cur.fetchone()[0]
    assert zone_count == user_count, (
        f"expected {user_count} zone groups (one per user); found {zone_count}"
    )


# ---------------------------------------------------------------------------
# Test 3 — Default-write-to-zone: POST /entries without sensitivity → private
# entry plus a permissions row binding the caller's zone group as admin.
# ---------------------------------------------------------------------------


def test_default_write_lands_in_zone():
    """POST /entries with no sensitivity field defaults to 'private' and a
    zone-group admin grant is written in the same transaction."""
    entry = _create_entry(ADMIN_KEY)
    try:
        assert entry["sensitivity"] == "private", (
            f"expected sensitivity='private', got {entry['sensitivity']!r}"
        )
        zone_id = _zone_group_id(USR_ADMIN)
        assert zone_id is not None
        assert _entry_zone_perm_exists(entry["id"], zone_id), (
            f"expected zone-group admin permission row for entry {entry['id']}"
        )
    finally:
        _archive(entry["id"])


# ---------------------------------------------------------------------------
# Test 4 — Explicit non-private sensitivity bypasses the zone grant.
# ---------------------------------------------------------------------------


def test_explicit_shared_sensitivity_skips_zone_grant():
    """When the caller passes sensitivity='shared' (or any non-private value)
    the personal-zones path does NOT add a zone permission row — old
    behavior preserved verbatim."""
    entry = _create_entry(ADMIN_KEY, sensitivity="shared")
    try:
        assert entry["sensitivity"] == "shared"
        zone_id = _zone_group_id(USR_ADMIN)
        assert zone_id is not None
        assert not _entry_zone_perm_exists(entry["id"], zone_id), (
            f"unexpected zone grant on shared entry {entry['id']}"
        )
    finally:
        _archive(entry["id"])


# ---------------------------------------------------------------------------
# Test 5 — RLS isolation: User B cannot SELECT User A's zone entry.
# ---------------------------------------------------------------------------


def test_rls_isolation_between_users():
    """User A (admin) creates a private/zone entry; User B (editor, same org)
    cannot SELECT it via GET /entries/{id} or GET /entries."""
    entry = _create_entry(ADMIN_KEY)
    try:
        # Direct GET as another user must 403/404.
        r = _get(f"/entries/{entry['id']}", EDITOR_KEY)
        assert r.status_code in (403, 404), (
            f"expected 403/404 for cross-user zone read; got {r.status_code}: {r.text}"
        )

        # GET /entries must not enumerate the entry to the other user.
        r = _get("/entries", EDITOR_KEY, params={"limit": 200})
        assert r.status_code == 200, r.text
        ids = {e["id"] for e in r.json().get("entries", [])}
        assert entry["id"] not in ids, (
            f"editor list incorrectly contains admin's zone entry {entry['id']}"
        )
    finally:
        _archive(entry["id"])


# ---------------------------------------------------------------------------
# Test 6 — Promotion (additive) makes the entry visible to a wider principal
# while preserving the caller's zone grant.
# ---------------------------------------------------------------------------


@pytest.fixture
def shared_group():
    """A non-zone ad-hoc group with usr_editor as a member.

    Created by the admin (only role allowed to manage groups). Cleaned up
    after the test.
    """
    name = f"zone-promote-target-{uuid.uuid4().hex[:8]}"
    r = _post("/groups", ADMIN_KEY, {"name": name, "description": "promote target"})
    assert r.status_code == 201, r.text
    g = r.json()
    add = _post(
        f"/groups/{g['id']}/members", ADMIN_KEY, {"user_id": USR_EDITOR}
    )
    assert add.status_code == 201, add.text
    yield g
    try:
        _delete(f"/groups/{g['id']}", ADMIN_KEY)
    except Exception:
        pass


def test_promotion_grants_target_group_and_preserves_zone(shared_group):
    """User A promotes a zone entry to a group containing User B.

    After promotion:
      * User B can SELECT the entry.
      * User A's zone grant is still present.
      * The promoted group permission row exists.
    """
    entry = _create_entry(ADMIN_KEY)
    entry_id = entry["id"]
    try:
        # Pre-flight: user B (editor) cannot see it.
        pre = _get(f"/entries/{entry_id}", EDITOR_KEY)
        assert pre.status_code in (403, 404), pre.text

        # Admin promotes to the shared group as viewer. We also bump
        # sensitivity to 'shared' here — kb_editor's SELECT policy applies
        # a sensitivity ceiling on the ACL path (see migration 019), so
        # promoting a *private* entry by ACL alone leaves it invisible to
        # editor-tier readers. Real-world callers will pair the principal
        # add with a sensitivity bump for cross-user reads to actually work.
        promote = _post(
            "/zone/promote",
            ADMIN_KEY,
            {
                "entry_id": entry_id,
                "add_principals": [
                    {
                        "principal_type": "group",
                        "principal_id": shared_group["id"],
                        "role": "viewer",
                    }
                ],
                "new_sensitivity": "shared",
            },
        )
        assert promote.status_code == 200, promote.text

        # User B can now SELECT.
        post = _get(f"/entries/{entry_id}", EDITOR_KEY)
        assert post.status_code == 200, (
            f"expected 200 after promotion, got {post.status_code}: {post.text}"
        )

        # Zone grant still present.
        zone_id = _zone_group_id(USR_ADMIN)
        assert zone_id is not None
        assert _entry_zone_perm_exists(entry_id, zone_id), (
            "zone grant must persist across promotion (additive only)"
        )

        # Promoted group permission row exists in the response.
        principal_ids = {
            p["principal_id"] for p in promote.json()["permissions"]
        }
        assert shared_group["id"] in principal_ids
        assert zone_id in principal_ids
    finally:
        _archive(entry_id)


# ---------------------------------------------------------------------------
# Test 7 — Promote 403: a user without admin on the entry cannot promote it.
# ---------------------------------------------------------------------------


def test_promote_without_admin_returns_403(shared_group):
    """User A creates a zone entry, then User B (editor — no admin grant on
    the entry, not in admin's zone) tries to promote it. Must 403."""
    entry = _create_entry(ADMIN_KEY)
    try:
        r = _post(
            "/zone/promote",
            EDITOR_KEY,
            {
                "entry_id": entry["id"],
                "add_principals": [
                    {
                        "principal_type": "group",
                        "principal_id": shared_group["id"],
                        "role": "viewer",
                    }
                ],
            },
        )
        # Either the entry is invisible to editor (404) or the admin check
        # fires (403) — both are acceptable; the safety property is "non-admin
        # callers cannot promote".
        assert r.status_code in (403, 404), (
            f"expected 403/404 promoting another user's entry; got {r.status_code}"
        )
    finally:
        _archive(entry["id"])


# ---------------------------------------------------------------------------
# Test 8 — new_sensitivity bumps the entry's sensitivity column.
# ---------------------------------------------------------------------------


def test_promote_with_new_sensitivity_bumps_entry(shared_group):
    """new_sensitivity='shared' on /zone/promote updates entries.sensitivity."""
    entry = _create_entry(ADMIN_KEY)
    try:
        assert entry["sensitivity"] == "private"
        r = _post(
            "/zone/promote",
            ADMIN_KEY,
            {
                "entry_id": entry["id"],
                "add_principals": [
                    {
                        "principal_type": "group",
                        "principal_id": shared_group["id"],
                        "role": "viewer",
                    }
                ],
                "new_sensitivity": "shared",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["entry"]["sensitivity"] == "shared"

        # Re-read and confirm the sensitivity is persisted.
        check = _get(f"/entries/{entry['id']}", ADMIN_KEY)
        assert check.status_code == 200, check.text
        assert check.json()["sensitivity"] == "shared"
    finally:
        _archive(entry["id"])


# ---------------------------------------------------------------------------
# Test 9 — Downgrade rejected: cannot promote-back-to-private from non-private.
# ---------------------------------------------------------------------------


def test_promote_downgrade_to_private_rejected(shared_group):
    """new_sensitivity='private' on a non-private entry → 400.

    First widen to 'shared' (via a normal promote), then attempt a downgrade
    promote — must be rejected.
    """
    entry = _create_entry(ADMIN_KEY)
    try:
        # Step 1: widen.
        widen = _post(
            "/zone/promote",
            ADMIN_KEY,
            {
                "entry_id": entry["id"],
                "add_principals": [],
                "new_sensitivity": "shared",
            },
        )
        assert widen.status_code == 200, widen.text

        # Step 2: attempt downgrade — must 400.
        down = _post(
            "/zone/promote",
            ADMIN_KEY,
            {
                "entry_id": entry["id"],
                "add_principals": [],
                "new_sensitivity": "private",
            },
        )
        assert down.status_code == 400, (
            f"expected 400 on downgrade-to-private; got {down.status_code}: {down.text}"
        )
    finally:
        _archive(entry["id"])


# ---------------------------------------------------------------------------
# Test 10 — Zone immutability via the API surface.
# ---------------------------------------------------------------------------


_ZONE_IMMUTABLE_DETAIL = "Personal zone groups are immutable via the API"


def test_zone_group_delete_blocked_for_admin_via_api():
    """Even an org admin cannot DELETE a zone group through the API — the
    `_assert_not_zone` guard in api/routes/groups.py returns 403 with a
    descriptive detail. Migration-034 BEFORE triggers bypass `kb_admin`
    (intentional escape hatch for psql superuser maintenance), so the
    immutability invariant has to be enforced at the API layer for admins."""
    zone_id = _zone_group_id(USR_EDITOR)
    assert zone_id is not None

    r = _delete(f"/groups/{zone_id}", ADMIN_KEY)
    assert r.status_code == 403, (
        f"admin must get 403 deleting a zone group; got {r.status_code}: {r.text}"
    )
    assert r.json().get("detail") == _ZONE_IMMUTABLE_DETAIL, (
        f"unexpected detail: {r.json()}"
    )

    # Zone still present.
    assert _zone_group_id(USR_EDITOR) is not None


def test_zone_member_add_blocked_for_admin_via_api():
    """Admin cannot add a member to someone else's zone group via the API.
    The `_assert_not_zone` guard runs before the INSERT and returns 403."""
    zone_id = _zone_group_id(USR_EDITOR)
    assert zone_id is not None

    r = _post(
        f"/groups/{zone_id}/members", ADMIN_KEY, {"user_id": USR_COMMENTER}
    )
    assert r.status_code == 403, (
        f"admin must get 403 adding to zone group; got {r.status_code}: {r.text}"
    )
    assert r.json().get("detail") == _ZONE_IMMUTABLE_DETAIL, (
        f"unexpected detail: {r.json()}"
    )

    # Membership unchanged (still 1 — owner only).
    assert _zone_member_count(zone_id) == 1


def test_zone_member_remove_blocked_for_admin_via_api():
    """Admin cannot remove the owner from a zone group via the API. The
    `_assert_not_zone` guard short-circuits the DELETE."""
    zone_id = _zone_group_id(USR_EDITOR)
    assert zone_id is not None

    r = _delete(f"/groups/{zone_id}/members/{USR_EDITOR}", ADMIN_KEY)
    assert r.status_code == 403, (
        f"admin must get 403 removing zone member; got {r.status_code}: {r.text}"
    )
    assert r.json().get("detail") == _ZONE_IMMUTABLE_DETAIL, (
        f"unexpected detail: {r.json()}"
    )

    # Membership unchanged (still 1 — owner only).
    assert _zone_member_count(zone_id) == 1


# ---------------------------------------------------------------------------
# Test 10b — T-0301.1 verification: non-admin write path provisions zone.
# ---------------------------------------------------------------------------


def test_editor_post_entry_without_sensitivity_provisions_zone():
    """A kb_editor user POSTing /entries without sensitivity must succeed
    (200/201) and result in a zone permission row for the editor's zone.

    Before the GRANT widening in migration 034, this would 500 with
    InsufficientPrivilege because get_or_create_zone runs as the caller's
    PG role inside the request transaction and only kb_admin had EXECUTE
    on provision_user_zone. T-0301.1 widens the grant to all kb_* roles
    (the function is SECURITY DEFINER so the surface stays bounded)."""
    entry = _create_entry(EDITOR_KEY)
    try:
        assert entry["sensitivity"] == "private", (
            f"expected sensitivity='private', got {entry['sensitivity']!r}"
        )
        zone_id = _zone_group_id(USR_EDITOR)
        assert zone_id is not None
        assert _entry_zone_perm_exists(entry["id"], zone_id), (
            f"expected zone-group admin permission row for editor entry {entry['id']}"
        )
    finally:
        _archive(entry["id"])


# ---------------------------------------------------------------------------
# Test 11 — provision_user_zone is idempotent: calling it twice does not
# create a duplicate zone group.
# ---------------------------------------------------------------------------


def test_provision_user_zone_idempotent():
    """Calling provision_user_zone twice for the same user returns the same
    group id and leaves exactly one zone row."""
    before = _zone_count(USR_EDITOR)
    assert before == 1

    id1 = _call_provision_user_zone(USR_EDITOR)
    id2 = _call_provision_user_zone(USR_EDITOR)
    assert id1 == id2, "provision_user_zone must return the same group id on re-call"

    after = _zone_count(USR_EDITOR)
    assert after == 1, (
        f"idempotency violated: zone count went {before} -> {after} after re-provision"
    )


# ---------------------------------------------------------------------------
# Test 12 — MCP-path happy case: exercise mcp/tools.py::promote_entry through
# its registered callable with a real BrilliantClient pointed at the test API.
#
# The MCP module imports FastMCP at module scope, so guard the import and
# skip cleanly on environments where the SDK isn't available.
# ---------------------------------------------------------------------------


def test_mcp_promote_entry_happy_path(shared_group):
    """Register tools on a fresh FastMCP, fish out the promote_entry callable,
    and run it against the live API. Local-stdio path: ``act_as`` resolves to
    None and the BrilliantClient authenticates with the env-supplied key
    (we point it at admin's key for the duration of the test)."""
    fastmcp_mod = pytest.importorskip("mcp.server.fastmcp")
    FastMCP = fastmcp_mod.FastMCP

    # Make the mcp/ source tree importable without needing it on PYTHONPATH.
    import os as _os
    import sys as _sys
    repo_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    mcp_dir = _os.path.join(repo_root, "mcp")
    if mcp_dir not in _sys.path:
        _sys.path.insert(0, mcp_dir)

    # Configure BrilliantClient via env. Local stdio mode (no OAuth context)
    # → tools call api.{get,post}(act_as=None) and the client sends the env
    # key without an X-Act-As-User header. Using the admin seed key is the
    # cleanest way to drive the happy path without setting up a service key.
    prev_base = _os.environ.get("BRILLIANT_BASE_URL")
    prev_key = _os.environ.get("BRILLIANT_SERVICE_API_KEY")
    _os.environ["BRILLIANT_BASE_URL"] = BASE_URL
    _os.environ["BRILLIANT_SERVICE_API_KEY"] = ADMIN_KEY

    try:
        # Late imports — we just put mcp/ on sys.path above.
        import client as mcp_client  # type: ignore[import-not-found]
        import tools as mcp_tools  # type: ignore[import-not-found]

        api = mcp_client.BrilliantClient()
        mcp_app = FastMCP(name="zones-test")
        mcp_tools.register_tools(mcp_app, api)

        # Locate the registered promote_entry tool callable. FastMCP's tool
        # manager exposes the underlying function via `.fn` on each tool.
        registered = mcp_app._tool_manager._tools  # noqa: SLF001 — test access
        assert "promote_entry" in registered, (
            f"promote_entry not registered; have: {list(registered)}"
        )
        promote_fn = registered["promote_entry"].fn

        # Create a zone entry to promote.
        entry = _create_entry(ADMIN_KEY)
        try:
            import asyncio
            result = asyncio.run(
                promote_fn(
                    entry_id=entry["id"],
                    add_principals=[
                        {
                            "principal_type": "group",
                            "principal_id": shared_group["id"],
                            "role": "viewer",
                        }
                    ],
                )
            )

            # Either an error dict or the {"entry","permissions","summary"} shape.
            assert isinstance(result, dict), f"unexpected return type: {type(result)}"
            assert result.get("error") is not True, (
                f"promote_entry returned error: {result}"
            )
            assert "summary" in result, (
                f"missing 'summary' in promote_entry response: {result.keys()}"
            )
            assert "entry" in result and result["entry"]["id"] == entry["id"]
            principal_ids = {p["principal_id"] for p in result["permissions"]}
            assert shared_group["id"] in principal_ids
        finally:
            _archive(entry["id"])
    finally:
        if prev_base is None:
            _os.environ.pop("BRILLIANT_BASE_URL", None)
        else:
            _os.environ["BRILLIANT_BASE_URL"] = prev_base
        if prev_key is None:
            _os.environ.pop("BRILLIANT_SERVICE_API_KEY", None)
        else:
            _os.environ["BRILLIANT_SERVICE_API_KEY"] = prev_key
