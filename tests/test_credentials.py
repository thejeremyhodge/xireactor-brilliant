"""Integration tests for ``GET /credentials`` recovery route.

Sprint 0043, T-0254 — close Issue #45 Option B. Operators who lose
``brilliant-credentials.txt`` can re-fetch the six-field payload with
their admin API key. The payload shape matches the installer file
byte-for-byte so a user can ``curl ... > brilliant-credentials.txt``
the response and have a drop-in replacement.

Three cases exercised:

  1. Valid admin Bearer → 200 JSON with all six fields present.
  2. No Authorization header → 401 (enforced by
     :func:`auth._extract_bearer_token`).
  3. Non-admin user API key → 403 (enforced by ``_require_admin``
     inside the handler; auth succeeds, role check fails).

Prerequisites (matches tests/test_service_role.py conventions):
  1. docker compose up -d   (API on :8010, Postgres on :5442)
     Migrations through 032 (api_public_url) applied.
  2. Seed data loaded (005_seed.sql equivalent — demo.sql in v0.5.1+).
  3. pip install -r tests/requirements-dev.txt

Run:
  pytest tests/test_credentials.py -v
"""

from __future__ import annotations

import json
import os
import secrets
import time
from typing import Iterator

import pytest
import requests

try:
    import psycopg
    _PSYCOPG_AVAILABLE = True
except ImportError:
    _PSYCOPG_AVAILABLE = False


# ---------------------------------------------------------------------------
# Configuration (mirrors tests/test_service_role.py)
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("BRILLIANT_BASE_URL", "http://localhost:8010")
DB_DSN = os.environ.get(
    "BRILLIANT_DB_DSN",
    "postgresql://postgres:dev@localhost:5442/brilliant",
)
REQUEST_TIMEOUT = 10.0

# Seed keys (005_seed.sql / demo.sql) — stable across test runs because
# /auth/login rotation only touches the admin key, not the editor key.
ADMIN_KEY = "bkai_adm1_testkey_admin"
EDITOR_KEY = "bkai_edit_testkey_editor"


# ---------------------------------------------------------------------------
# Skip gates
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
        reason=(
            f"Brilliant API not reachable at {BASE_URL} "
            "(start it with `docker compose up -d`)."
        ),
    ),
    pytest.mark.skipif(
        not _db_available(),
        reason=(
            f"Brilliant DB not reachable at {DB_DSN} "
            "(start it with `docker compose up -d`)."
        ),
    ),
]


# ---------------------------------------------------------------------------
# OAuth client fixture — seed DB has no oauth_clients row; the recovery
# route depends on one. Insert a throwaway client for the test, clean up
# after. Production installs always have exactly one row minted by
# admin_bootstrap.
# ---------------------------------------------------------------------------


@pytest.fixture
def oauth_client_row() -> Iterator[tuple[str, str]]:
    """Insert a test oauth_clients row, yield ``(client_id, client_secret)``.

    If the caller's admin_bootstrap has already populated the table
    (fresh docker compose up flows through ensure_admin_user on first
    boot), we yield the existing row instead of inserting a duplicate.
    Teardown only deletes rows we inserted.
    """
    if not _PSYCOPG_AVAILABLE:
        pytest.skip("psycopg not installed")

    inserted = False
    client_id = f"brilliant_test_{secrets.token_hex(8)}"
    client_secret = secrets.token_hex(32)
    client_id_issued_at = int(time.time())

    yield_id: str
    yield_secret: str

    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Prefer the most recently created production row if one
            # already exists — matches the handler's ORDER BY created_at
            # DESC LIMIT 1 source-of-truth.
            cur.execute(
                """
                SELECT client_id, client_secret
                FROM oauth_clients
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()

            if row is not None:
                yield_id, yield_secret = str(row[0]), str(row[1])
            else:
                cur.execute(
                    """
                    INSERT INTO oauth_clients (
                        client_id, client_secret, client_id_issued_at, client_info
                    )
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        client_id,
                        client_secret,
                        client_id_issued_at,
                        json.dumps({
                            "client_id": client_id,
                            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                        }),
                    ),
                )
                inserted = True
                yield_id, yield_secret = client_id, client_secret

    try:
        yield (yield_id, yield_secret)
    finally:
        if inserted:
            try:
                with psycopg.connect(DB_DSN, autocommit=True) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM oauth_clients WHERE client_id = %s",
                            (client_id,),
                        )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Test 1 — 200 with valid admin Bearer, all six fields present
# ---------------------------------------------------------------------------


def test_credentials_admin_bearer_returns_six_field_json(
    oauth_client_row: tuple[str, str],
):
    """``GET /credentials`` with a valid admin Bearer must return 200 JSON
    containing all six canonical fields in the expected shape.

    The six-field contract (see ``api/admin_bootstrap.py``'s credential
    block + install.sh's writer) is:

      admin_email
      admin_api_key
      oauth_client_id
      oauth_client_secret
      mcp_url
      login_url

    admin_api_key should echo the Bearer we presented — that's the only
    way to put plaintext on the wire, since the DB stores the bcrypt
    hash. The handler pulls it from request.headers.
    """
    r = requests.get(
        f"{BASE_URL}/credentials",
        headers={
            "Authorization": f"Bearer {ADMIN_KEY}",
            "Accept": "application/json",
        },
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 200, f"{r.status_code}: {r.text}"

    ctype = r.headers.get("Content-Type", "").lower()
    assert "application/json" in ctype, (
        f"expected JSON response, got Content-Type={ctype!r}; body: {r.text!r}"
    )

    body = r.json()
    expected_keys = {
        "admin_email",
        "admin_api_key",
        "oauth_client_id",
        "oauth_client_secret",
        "mcp_url",
        "login_url",
    }
    assert set(body.keys()) == expected_keys, (
        f"expected exactly {sorted(expected_keys)}; got {sorted(body.keys())}"
    )

    # admin_api_key must echo the Bearer we presented (the DB only has
    # the bcrypt hash, so this is the only way the plaintext can land
    # in the response).
    assert body["admin_api_key"] == ADMIN_KEY, (
        f"expected admin_api_key to echo the Bearer token; got "
        f"{body['admin_api_key']!r}"
    )

    # oauth_client_id / oauth_client_secret must match the fixture's
    # ORDER BY created_at DESC LIMIT 1 pick.
    expected_client_id, expected_client_secret = oauth_client_row
    assert body["oauth_client_id"] == expected_client_id, (
        f"oauth_client_id mismatch: got {body['oauth_client_id']!r}, "
        f"expected {expected_client_id!r}"
    )
    assert body["oauth_client_secret"] == expected_client_secret, (
        "oauth_client_secret mismatch (plaintext stored in DB per migration 006)"
    )

    # mcp_url + login_url: loose shape checks — we don't pin values
    # because they depend on env (RENDER_EXTERNAL_URL) / DB state.
    assert isinstance(body["mcp_url"], str) and body["mcp_url"], (
        "mcp_url must be a non-empty string"
    )
    assert isinstance(body["login_url"], str) and body["login_url"], (
        "login_url must be a non-empty string"
    )
    assert body["login_url"].endswith("/auth/login"), (
        f"login_url must end with /auth/login; got {body['login_url']!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — 401 without Authorization header
# ---------------------------------------------------------------------------


def test_credentials_without_bearer_returns_401():
    """``GET /credentials`` with no Authorization header must return 401.

    Enforced by :func:`auth._extract_bearer_token`, which raises
    HTTPException(401) when the header is absent. This is the same
    gate every authenticated API route relies on — no special case
    for /credentials.
    """
    r = requests.get(
        f"{BASE_URL}/credentials",
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 401, (
        f"expected 401 (no Bearer), got {r.status_code}: {r.text}"
    )


# ---------------------------------------------------------------------------
# Test 3 — 403 with non-admin user API key
# ---------------------------------------------------------------------------


def test_credentials_non_admin_user_returns_403():
    """``GET /credentials`` with a valid but non-admin user key must
    return 403.

    Auth succeeds (the editor seed key bcrypt-verifies), but
    ``_require_admin`` rejects because role != 'admin'. Error detail
    should mention "admin" so an operator debugging a failed recovery
    attempt can tell this from a transient auth failure.
    """
    r = requests.get(
        f"{BASE_URL}/credentials",
        headers={"Authorization": f"Bearer {EDITOR_KEY}"},
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 403, (
        f"expected 403 (non-admin key), got {r.status_code}: {r.text}"
    )

    # Error detail should surface "admin" so callers can differentiate
    # from auth failures.
    detail = ""
    try:
        detail = r.json().get("detail", "")
    except Exception:
        pass
    assert "admin" in detail.lower(), (
        f"expected 'admin' in error detail for clarity; got {detail!r}"
    )
