"""Integration tests for the friendly HTML 404 on ``GET /oauth/login``.

Sprint 0042, T-0250 — before this change, landing on ``/oauth/login``
with a missing or expired ``tx`` query param returned raw JSON
``{"detail":"Not found"}``. The MCP's ``/authorize`` hop redirects a
browser here, so the user needs a human-readable recovery page, not a
JSON error body.

The GET handler now returns ``HTMLResponse(status_code=404)`` with a
body mentioning "expired" and guidance to click Connect in Claude again.
The POST handler still raises JSON 404 — it is machine-driven, not
user-facing.

Prerequisites (matches tests/test_session_init.py conventions):
  1. docker compose up -d   (API on :8010, Postgres on :5442)
     Migrations through 030 (oauth user binding) applied.
  2. pip install -r tests/requirements-dev.txt

Run:
  pytest tests/test_oauth_login.py -v
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


# ---------------------------------------------------------------------------
# Skip gates
# ---------------------------------------------------------------------------


def _api_available() -> bool:
    try:
        return (
            requests.get(f"{BASE_URL}/health", timeout=2.0).status_code == 200
        )
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
]


# ---------------------------------------------------------------------------
# Fixture — seed a valid pending-authz row (and the oauth_client it FKs to)
# ---------------------------------------------------------------------------


@pytest.fixture
def pending_tx() -> Iterator[str]:
    """Insert a short-lived oauth_client + oauth_pending_authorizations row.

    Yields the ``tx_id`` plaintext. On teardown, both rows are deleted so
    subsequent test runs on a shared dev DB don't accumulate cruft.
    """
    if not _db_available():
        pytest.skip(
            f"Brilliant DB not reachable at {DB_DSN} "
            "(start it with `docker compose up -d`)."
        )

    client_id = f"test-client-{secrets.token_hex(4)}"
    tx_id = f"tx_{secrets.token_hex(12)}"
    # Expires 5 minutes from now — well within the GET request window.
    expires_at = time.time() + 300.0

    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO oauth_clients (
                    client_id, client_secret, client_id_issued_at, client_info
                )
                VALUES (%s, %s, %s, %s)
                """,
                (
                    client_id,
                    "test-secret",
                    int(time.time()),
                    json.dumps({"client_id": client_id}),
                ),
            )
            cur.execute(
                """
                INSERT INTO oauth_pending_authorizations (
                    tx_id, client_id, scopes, redirect_uri, expires_at
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    tx_id,
                    client_id,
                    [],
                    "http://localhost/callback",
                    expires_at,
                ),
            )

    try:
        yield tx_id
    finally:
        try:
            with psycopg.connect(DB_DSN, autocommit=True) as conn:
                with conn.cursor() as cur:
                    # pending-authz is CASCADE on oauth_clients, but delete
                    # explicitly for clarity and to survive partial-cleanup
                    # races on shared dev DBs.
                    cur.execute(
                        "DELETE FROM oauth_pending_authorizations "
                        "WHERE tx_id = %s",
                        (tx_id,),
                    )
                    cur.execute(
                        "DELETE FROM oauth_clients WHERE client_id = %s",
                        (client_id,),
                    )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Test 1 — missing tx → friendly HTML 404
# ---------------------------------------------------------------------------


def test_oauth_login_missing_tx_returns_html_404():
    """``GET /oauth/login`` (no query string) must return 404 with a
    human-readable HTML body mentioning "expired", NOT raw
    ``{"detail":"Not found"}`` JSON.

    The content-type assertion guards against a regression where someone
    reverts to ``raise HTTPException`` (FastAPI's default renders those
    as ``application/json``).
    """
    r = requests.get(
        f"{BASE_URL}/oauth/login",
        timeout=REQUEST_TIMEOUT,
        # Explicitly don't follow redirects — we want the raw 404.
        allow_redirects=False,
    )
    assert r.status_code == 404, r.text

    ctype = r.headers.get("Content-Type", "").lower()
    assert "text/html" in ctype, (
        f"expected HTML response, got Content-Type={ctype!r}; body: {r.text!r}"
    )

    body = r.text.lower()
    assert "expired" in body, (
        f"expected the friendly-404 body to mention 'expired'; got: {r.text!r}"
    )
    # Sanity-check: the user should be told how to recover.
    assert "connect" in body or "claude" in body, (
        f"expected recovery guidance (Claude/Connect) in body; got: {r.text!r}"
    )
    # And it absolutely must NOT be the old JSON payload.
    assert '"detail"' not in r.text, (
        f"body appears to still be JSON; got: {r.text!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — nonexistent tx → same friendly HTML 404 (enumeration-resistant)
# ---------------------------------------------------------------------------


def test_oauth_login_unknown_tx_returns_html_404():
    """A ``tx`` query param that doesn't correspond to any row must
    return exactly the same response as the missing-param case.

    This is the enumeration-resistance property — a client probing
    ``tx`` values cannot distinguish "never existed" from "expired" from
    "malformed", so they learn nothing about which tx_ids were ever issued.
    """
    fake_tx = f"tx_{secrets.token_hex(16)}"
    r = requests.get(
        f"{BASE_URL}/oauth/login",
        params={"tx": fake_tx},
        timeout=REQUEST_TIMEOUT,
        allow_redirects=False,
    )
    assert r.status_code == 404, r.text

    ctype = r.headers.get("Content-Type", "").lower()
    assert "text/html" in ctype, (
        f"expected HTML response, got Content-Type={ctype!r}"
    )

    body = r.text.lower()
    assert "expired" in body, (
        f"expected 'expired' in the friendly-404 body; got: {r.text!r}"
    )
    assert '"detail"' not in r.text, (
        f"body should not be the old JSON payload; got: {r.text!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — valid tx → 200 + login form (no regression)
# ---------------------------------------------------------------------------


def test_oauth_login_valid_tx_returns_login_form(pending_tx: str):
    """A valid, non-expired ``tx`` must still render the existing login
    form with HTTP 200. Asserting on the ``name="tx"`` hidden input
    guarantees the form shape is intact and the POST handler will
    receive the tx on submit.
    """
    r = requests.get(
        f"{BASE_URL}/oauth/login",
        params={"tx": pending_tx},
        timeout=REQUEST_TIMEOUT,
        allow_redirects=False,
    )
    assert r.status_code == 200, r.text

    ctype = r.headers.get("Content-Type", "").lower()
    assert "text/html" in ctype, (
        f"expected HTML login form, got Content-Type={ctype!r}"
    )

    body = r.text
    # The hidden tx input is how the POST handler learns which
    # pending-authz row to consume on submit — if this regresses, the
    # whole OAuth flow is broken.
    assert f'name="tx"' in body, (
        "login form missing hidden tx input"
    )
    assert pending_tx in body, (
        f"expected tx_id {pending_tx!r} to appear in the rendered form"
    )
    # And there should be a password field — this confirms we got the
    # login form, not the friendly-404 page.
    assert 'name="password"' in body, (
        "expected the rendered page to include the password input"
    )
