"""OAuth tx-handoff login page (API side of Sprint 0039).

The Render-deployed MCP server no longer auto-approves ``/authorize``. Instead,
when Claude Co-work (or any RFC 8252 client) hits ``/authorize`` on the MCP,
the MCP writes a row to ``oauth_pending_authorizations`` with a generated
``tx_id``, then 302s the browser to *this* module's
``GET /oauth/login?tx=<tx_id>``.

The user logs in with email + password. On success we HMAC-sign
``f"{tx}|{user_id}"`` with the shared ``OAUTH_HANDOFF_SECRET`` and 302 back to
``{MCP_BASE_URL}/oauth/continue?tx=...&user_id=...&sig=...``. The MCP's
``/oauth/continue`` handler verifies the signature, deletes the
pending-authz row, mints an auth code bound to ``user_id``, and completes the
standard OAuth 2.1 redirect back to Claude.

**Pending authz row is NOT deleted by this module.** The MCP's
``/oauth/continue`` handler is the single-writer that consumes the row.
This matters: if the user submits twice (double-click) we want both
submissions to redirect to ``/oauth/continue`` with valid sigs; the MCP
side then no-ops the second one when it finds no row.

See ``.xireactor/specs/0039--2026-04-18--oauth-user-bound-auth.md`` step 5.
"""

from __future__ import annotations

import hashlib
import hmac
import html as _html
import os
import urllib.parse
from typing import Any

import bcrypt
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from psycopg.rows import dict_row

from database import get_pool
from routes.setup import _BASE_STYLE

router = APIRouter(tags=["oauth"])


# ---------------------------------------------------------------------------
# MCP base URL (mirrors api/routes/setup.py::_mcp_url_for_display)
# ---------------------------------------------------------------------------


async def _mcp_base_url(pool) -> str:
    """Resolve the MCP service's public base URL for the redirect hop.

    Kept structurally identical to ``routes/setup.py::_mcp_url_for_display``
    so the two surfaces can never drift. Resolution order:

    1. ``brilliant_settings.mcp_public_url`` column (migration 029).
    2. ``BRILLIANT_MCP_PUBLIC_URL`` env var.
    3. ``http://localhost:8011`` local-dev default.

    Returned URL never has a trailing slash — the caller appends
    ``/oauth/continue`` directly.
    """
    raw: str | None = None
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT mcp_public_url FROM brilliant_settings WHERE id = 1"
            )
            row = await cur.fetchone()
        if row and row[0]:
            raw = row[0]
    except Exception:
        # Column may not exist on an older DB; fall through to env.
        raw = None

    if not raw:
        raw = os.getenv("BRILLIANT_MCP_PUBLIC_URL", "").strip() or None

    if not raw:
        raw = "http://localhost:8011"

    if not (raw.startswith("http://") or raw.startswith("https://")):
        raw = f"https://{raw}"

    return raw.rstrip("/")


# ---------------------------------------------------------------------------
# Handoff signature
# ---------------------------------------------------------------------------


def _handoff_secret() -> str:
    """Read ``OAUTH_HANDOFF_SECRET`` at request time (never at import).

    Resolved lazily so the API can boot in local-dev without the var set
    (e.g. running the test suite that doesn't touch this route). When a
    request lands on ``POST /oauth/login`` without the var, we raise 500
    rather than silently minting an unverifiable signature — that would
    appear to work in dev and then break surprisingly on deploy.
    """
    secret = os.environ.get("OAUTH_HANDOFF_SECRET", "").strip()
    if not secret:
        raise HTTPException(
            status_code=500,
            detail=(
                "OAUTH_HANDOFF_SECRET is not configured; API cannot sign "
                "the OAuth handoff. Set this env var (shared with the "
                "MCP service) and retry."
            ),
        )
    return secret


def _sign_handoff(tx: str, user_id: str) -> str:
    """Compute ``hex(HMAC-SHA256(OAUTH_HANDOFF_SECRET, f"{tx}|{user_id}"))``."""
    secret = _handoff_secret()
    msg = f"{tx}|{user_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Pending-authz lookup
# ---------------------------------------------------------------------------


async def _load_pending_authz(pool, tx: str) -> dict[str, Any] | None:
    """Return the pending-authz row for ``tx`` or ``None`` if missing/expired.

    ``expires_at`` is a unix timestamp (``DOUBLE PRECISION``) — see migration
    030. We compare against Postgres' own ``now()`` so the API and MCP can't
    drift against each other's clocks.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT tx_id, client_id, expires_at
            FROM oauth_pending_authorizations
            WHERE tx_id = %s
              AND expires_at > extract(epoch from now())
            """,
            (tx,),
        )
        cur.row_factory = dict_row
        return await cur.fetchone()


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


_EXPIRED_LINK_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sign-in link expired — Brilliant</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_BASE_STYLE}</style>
</head>
<body>
  <h1>This sign-in link has expired</h1>
  <p class="sub">Your authorization session is no longer valid.</p>
  <div class="info">
    This usually happens when too much time has passed, when the link has
    already been used, or when services were still warming up from a fresh
    deploy. Return to Claude and click <strong>Connect</strong> again to
    start a fresh sign-in.
  </div>
</body>
</html>"""


def _render_login(tx: str, email: str = "", error: str | None = None) -> str:
    """Render the OAuth login form.

    Mirrors the ``/setup`` + ``/auth/login`` visual language via the shared
    ``_BASE_STYLE`` imported from ``routes/setup.py`` so a user flowing
    through setup → Co-work connect never hits an unfamiliar chrome.
    """
    safe_tx = _html.escape(tx, quote=True)
    safe_email = _html.escape(email, quote=True)
    error_html = (
        f'<div class="error" role="alert">{_html.escape(error)}</div>'
        if error
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Authorize Claude — Brilliant</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_BASE_STYLE}</style>
</head>
<body>
  <h1>Authorize Claude</h1>
  <p class="sub">Sign in to allow Claude to access your knowledge base.</p>
  <div class="info">
    Claude is requesting permission to read and write on your behalf. This
    session will be scoped to <strong>your user</strong> — not the
    workspace admin.
  </div>
  {error_html}
  <form method="post" action="/oauth/login" enctype="application/x-www-form-urlencoded">
    <input type="hidden" name="tx" value="{safe_tx}">
    <div>
      <label for="email">Email</label>
      <input id="email" name="email" type="email" value="{safe_email}"
             required autofocus>
    </div>
    <div>
      <label for="password">Password</label>
      <input id="password" name="password" type="password" required>
    </div>
    <div>
      <button type="submit">Sign in &amp; authorize</button>
    </div>
  </form>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def oauth_login_form(request: Request) -> HTMLResponse:
    """Render the OAuth login form.

    Preconditions:

    - ``tx`` query param present.
    - Matching row in ``oauth_pending_authorizations`` with ``expires_at > now()``.

    Any other state → friendly HTML 404. We intentionally do NOT
    distinguish missing vs expired vs malformed ``tx`` — a client that
    hit a broken link can only recover by restarting the OAuth flow
    from Claude, and a narrower error would leak whether a given
    ``tx_id`` was ever issued. The response body is HTML (not JSON)
    because this route is always reached via a browser redirect from
    the MCP's ``/authorize`` hop, so the user needs human-readable
    recovery guidance — see sprint 0042, T-0250.
    """
    tx = (request.query_params.get("tx") or "").strip()
    if not tx:
        return HTMLResponse(_EXPIRED_LINK_HTML, status_code=404)

    pool = get_pool()
    row = await _load_pending_authz(pool, tx)
    if row is None:
        return HTMLResponse(_EXPIRED_LINK_HTML, status_code=404)

    return HTMLResponse(_render_login(tx=tx))


@router.post("/login", response_model=None)
async def oauth_login_submit(
    request: Request,
    tx: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
) -> HTMLResponse | RedirectResponse:
    """Validate credentials and hand off to the MCP's ``/oauth/continue``.

    On failure: re-render the form with a 400 + generic error message
    (never distinguish unknown-email from wrong-password — same
    enumeration-resistance rule as ``/auth/login``).

    On success: compute the HMAC sig and 302 to the MCP. We deliberately
    do NOT delete the pending-authz row here — that is the MCP's job on
    ``/oauth/continue`` so the tx is consumed atomically with auth-code
    minting.
    """
    tx_clean = (tx or "").strip()
    email_clean = (email or "").strip().lower()

    pool = get_pool()

    # Validate tx still exists + hasn't expired before burning a bcrypt cycle.
    # Also re-check after auth succeeds — belt + suspenders against a race
    # where the row expires mid-submit (unlikely but cheap).
    row = await _load_pending_authz(pool, tx_clean)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")

    # Look up the user by email. Mirrors routes/auth.py::_authenticate_and_rotate
    # but does NOT rotate the API key — OAuth sessions don't share the
    # panic-button semantics of /auth/login. We only need to verify the
    # password hash and bind user_id to the tx.
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT id, is_active, password_hash
            FROM users
            WHERE email = %s
            """,
            (email_clean,),
        )
        cur.row_factory = dict_row
        user = await cur.fetchone()

    auth_ok = (
        user is not None
        and user["is_active"]
        and user["password_hash"]
        and bcrypt.checkpw(
            password.encode("utf-8"),
            user["password_hash"].encode("utf-8"),
        )
    )

    if not auth_ok:
        return HTMLResponse(
            _render_login(
                tx=tx_clean,
                email=email_clean,
                error="Invalid email or password",
            ),
            status_code=400,
        )

    user_id = user["id"]
    sig = _sign_handoff(tx_clean, user_id)

    mcp_base = await _mcp_base_url(pool)
    query = urllib.parse.urlencode(
        {"tx": tx_clean, "user_id": user_id, "sig": sig}
    )
    continue_url = f"{mcp_base}/oauth/continue?{query}"

    # 302 (not 303) to match the stock OAuth redirect idiom; FastAPI's
    # ``RedirectResponse`` default is 307 which would re-POST — explicitly
    # downgrade to 302 so the browser issues a GET.
    return RedirectResponse(url=continue_url, status_code=302)
