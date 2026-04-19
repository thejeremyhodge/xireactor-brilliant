"""Integration tests for the service-role API key + X-Act-As-User gate.

Exercises the three-case auth gate introduced by sprint 0039, T-0224:

  1. service key + valid X-Act-As-User → UserContext resolves to the target
     user (NOT the service key owner). The /session-init manifest's
     `user.id` field is the single source of truth for identity, so we
     assert on that.
  2. service key + NO X-Act-As-User → UserContext is the service key's
     owner (the service key is a normal self-auth, just with the expanded
     key_type). The owner is returned and the call succeeds.
  3. non-service key + X-Act-As-User → 403 with a clear error detail.

Prerequisites (matches tests/test_permissions_v2.py conventions):
  1. docker compose up -d   (API on :8010, Postgres on :5442)
  2. Migrations applied through 031_service_role_key.sql.
  3. pip install -r tests/requirements-dev.txt

Run:
  pytest tests/test_service_role.py -v

Why /session-init?
------------------
It is the lightest-weight authenticated endpoint that echoes back the
authenticated user's id. We don't care about its KB payload — we just
need to confirm which user the API thinks it's serving. Seed data
(005_seed.sql) guarantees the target user (`usr_editor`) exists with a
stable id independent of the test ordering.
"""

from __future__ import annotations

import os
import secrets
import uuid
from typing import Iterator

import bcrypt
import pytest
import requests

try:
    import psycopg
    _PSYCOPG_AVAILABLE = True
except ImportError:
    _PSYCOPG_AVAILABLE = False


# ---------------------------------------------------------------------------
# Configuration (mirrors tests/test_permissions_v2.py)
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("BRILLIANT_BASE_URL", "http://localhost:8010")
DB_DSN = os.environ.get(
    "BRILLIANT_DB_DSN",
    "postgresql://postgres:dev@localhost:5442/brilliant",
)

# Seed keys (005_seed.sql)
ADMIN_KEY = "bkai_adm1_testkey_admin"
EDITOR_KEY = "bkai_edit_testkey_editor"

# Seed user IDs (005_seed.sql) — users.id is TEXT ('usr_<slug>'), NOT uuid.
USR_ADMIN = "usr_admin"
USR_EDITOR = "usr_editor"

ORG_ID = "org_demo"
REQUEST_TIMEOUT = 10.0

# The endpoint we probe to learn "who does the API think I am?". Any
# authenticated endpoint that surfaces the caller's user id would do; we
# picked /session-init because it's read-only, shallow, and already returns
# user.id in its top-level manifest.
WHO_AM_I_PATH = "/session-init"


# ---------------------------------------------------------------------------
# Skip if infra unavailable
# ---------------------------------------------------------------------------


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
        reason=f"Brilliant API not reachable at {BASE_URL} "
               "(start it with `docker compose up -d`).",
    ),
    pytest.mark.skipif(
        not _db_available(),
        reason=f"Brilliant DB not reachable at {DB_DSN} "
               "(start it with `docker compose up -d`).",
    ),
]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _headers(key: str, act_as: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if act_as is not None:
        h["X-Act-As-User"] = act_as
    return h


def _get(path: str, key: str, *, act_as: str | None = None) -> requests.Response:
    return requests.get(
        f"{BASE_URL}{path}",
        headers=_headers(key, act_as=act_as),
        timeout=REQUEST_TIMEOUT,
    )


# ---------------------------------------------------------------------------
# Service-key fixture
# ---------------------------------------------------------------------------
#
# We insert the service-role API key directly via psycopg rather than
# depending on T-0225's admin_bootstrap changes. This keeps T-0224
# testable in isolation.
#
# The service key's owner is `usr_admin` — in practice the MCP service
# will have its own dedicated user, but for this gate test what matters is
# that the CHECK constraint now accepts `key_type = 'service'` and that
# auth.py branches on that value.


@pytest.fixture
def service_key() -> Iterator[str]:
    """Insert a fresh service-role API key, yield its plaintext, then revoke.

    Constraint exercised: migration 031's expanded api_keys.key_type CHECK.
    If the CHECK still rejects 'service', this INSERT raises and the test
    fails loudly — which is exactly the cross-check we want between the
    migration and the auth-gate tests.
    """
    if not _PSYCOPG_AVAILABLE:
        pytest.skip("psycopg not installed")

    # Build a realistic key: bkai_ + 4 hex chars (prefix) + 32 hex chars.
    suffix = secrets.token_hex(16)
    prefix_rand = secrets.token_hex(2)
    plaintext = f"bkai_{prefix_rand}{suffix}"
    key_prefix = plaintext[:9]
    key_hash = bcrypt.hashpw(
        plaintext.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")

    inserted_id: str | None = None
    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO api_keys (
                    user_id, org_id, key_hash, key_prefix, key_type, label
                )
                VALUES (%s, %s, %s, %s, 'service', %s)
                RETURNING id
                """,
                (
                    USR_ADMIN,
                    ORG_ID,
                    key_hash,
                    key_prefix,
                    f"T-0224 service key {uuid.uuid4().hex[:6]}",
                ),
            )
            inserted_id = str(cur.fetchone()[0])

    try:
        yield plaintext
    finally:
        if inserted_id:
            try:
                with psycopg.connect(DB_DSN, autocommit=True) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE api_keys SET is_revoked = TRUE WHERE id = %s",
                            (inserted_id,),
                        )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Test 1 — service key + valid X-Act-As-User → acts as target user
# ---------------------------------------------------------------------------


def test_service_key_with_act_as_returns_target_user_context(service_key: str):
    """Service key + X-Act-As-User=<editor> → manifest.user.id == usr_editor.

    The service key's owner is usr_admin, so if the gate silently fell
    through on the owner identity the manifest would return usr_admin.
    Asserting on usr_editor proves the act-as path actually switched
    identity downstream.
    """
    r = _get(WHO_AM_I_PATH, service_key, act_as=USR_EDITOR)
    assert r.status_code == 200, f"{r.status_code}: {r.text}"

    body = r.json()
    user = body.get("manifest", {}).get("user", {})
    assert user.get("id") == USR_EDITOR, (
        f"expected acting user to be {USR_EDITOR}, got {user.get('id')!r}; "
        f"full user block: {user}"
    )
    # Target user's role, not the service key owner's
    assert user.get("role") == "editor", (
        f"expected target user's role ('editor'), got {user.get('role')!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — service key + NO X-Act-As-User → acts as service identity
# ---------------------------------------------------------------------------


def test_service_key_without_header_returns_service_owner(service_key: str):
    """Service key without X-Act-As-User → manifest.user.id is the service
    key's *owner* (here usr_admin, per the fixture). This is the fallback
    path — infrequent in practice (the MCP always sends X-Act-As-User)
    but must not error out.
    """
    r = _get(WHO_AM_I_PATH, service_key)
    assert r.status_code == 200, f"{r.status_code}: {r.text}"

    body = r.json()
    user = body.get("manifest", {}).get("user", {})
    assert user.get("id") == USR_ADMIN, (
        f"service-identity fallback should return the key's owner "
        f"({USR_ADMIN}); got {user.get('id')!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — non-service key + X-Act-As-User → 403
# ---------------------------------------------------------------------------


def test_non_service_key_with_act_as_header_returns_403():
    """Any non-service key presenting X-Act-As-User must be rejected with
    403. This is the trust-boundary assertion: adding the header to an
    interactive key must NEVER escalate to another user's identity.

    We use the seeded editor interactive key (005_seed.sql; the admin
    seed key may have been rotated via /auth/login in a shared dev DB,
    but the editor key is never touched by the ceremony and stays
    stable). The target user exists (usr_admin) — so 403 cannot be
    explained by a missing target; it is purely the key_type check
    firing.
    """
    r = _get(WHO_AM_I_PATH, EDITOR_KEY, act_as=USR_ADMIN)
    assert r.status_code == 403, (
        f"expected 403 (non-service key cannot act-as), got "
        f"{r.status_code}: {r.text}"
    )

    # Error detail should mention service-role explicitly so operators
    # debugging "why is my integration failing" don't have to spelunk
    # the source.
    detail = ""
    try:
        detail = r.json().get("detail", "")
    except Exception:
        pass
    assert "service" in detail.lower(), (
        f"error detail should mention 'service' key_type; got {detail!r}"
    )
