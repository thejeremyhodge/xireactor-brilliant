"""Authentication routes: email+password login.

This module serves two callers from the same ``/auth/login`` path:

- **JSON clients** (frontend, curl, scripts) — ``Content-Type: application/json``
  get back a ``LoginResponse`` JSON body. Shape is unchanged.
- **HTML clients** (browser form posts from ``GET /auth/login``) —
  ``Content-Type: application/x-www-form-urlencoded`` get back an HTML page
  that shows the rotated API key, a copy button, and a client-side
  ``brilliant-credentials.txt`` download button. This is the
  "I lost my key / panic-button" recovery flow wired into Sprint 0037b's
  Render deploy path.

Rotation is the new baseline for ``POST /auth/login``: *every* successful
login revokes all prior unrevoked keys for the user and mints a new one.
That intentionally invalidates sessions on other devices — the flow
doubles as a key-leak panic button. JSON callers get the rotation too;
the frontend simply stores the fresh key it just received.
"""

import html as _html
import json as _json
import os
import secrets

import bcrypt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from psycopg.rows import dict_row

from database import get_pool
from models import LoginRequest, LoginResponse, UserResponse

router = APIRouter(tags=["auth"])


# ---------------------------------------------------------------------------
# HTML rendering helpers
# ---------------------------------------------------------------------------


def _render_login_form(email: str = "", error: str | None = None) -> str:
    """Render the login form HTML.

    Used by ``GET /auth/login`` and by ``POST /auth/login`` re-rendering on
    invalid credentials. Error messages never distinguish between unknown
    email and wrong password — always ``"Invalid email or password"`` — so
    the form can't be used to enumerate valid accounts.
    """
    safe_email = _html.escape(email, quote=True)
    error_html = ""
    if error:
        error_html = (
            '<div class="error" role="alert">'
            f"{_html.escape(error)}"
            "</div>"
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sign in — Brilliant</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 420px; margin: 64px auto; padding: 0 16px; color: #111; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 0.25rem; }}
  p.sub {{ color: #555; margin-top: 0; }}
  form {{ display: flex; flex-direction: column; gap: 12px; margin-top: 24px; }}
  label {{ font-size: 0.9rem; color: #333; }}
  input[type=email], input[type=password] {{
    padding: 10px 12px; font-size: 1rem; border: 1px solid #ccc;
    border-radius: 6px; width: 100%; box-sizing: border-box;
  }}
  button {{ padding: 10px 14px; font-size: 1rem; background: #111; color: #fff;
            border: 0; border-radius: 6px; cursor: pointer; }}
  button:hover {{ background: #333; }}
  .error {{ background: #fdecec; color: #8a1f1f; padding: 10px 12px;
            border-radius: 6px; border: 1px solid #f5b5b5; }}
  .warn {{ background: #fff8e1; border: 1px solid #f0d878; color: #6b5200;
           padding: 10px 12px; border-radius: 6px; font-size: 0.9rem; }}
</style>
</head>
<body>
  <h1>Sign in to Brilliant</h1>
  <p class="sub">Recover or rotate your API key.</p>
  {error_html}
  <div class="warn">
    Signing in <strong>rotates your API key</strong> — all previous keys
    will be invalidated. Use this as a panic button if you think a key leaked.
  </div>
  <form method="post" action="/auth/login" enctype="application/x-www-form-urlencoded">
    <label for="email">Email</label>
    <input id="email" name="email" type="email" value="{safe_email}" required autofocus>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" required>
    <button type="submit">Sign in &amp; rotate key</button>
  </form>
</body>
</html>
"""


def _mcp_url_for_display() -> str:
    """Render the MCP connector URL for display alongside rotated credentials.

    Mirrors ``api/routes/setup.py::_mcp_url_for_display``. ``BRILLIANT_MCP_PUBLIC_URL``
    is wired into the API service via ``fromService`` in ``render.yaml`` as a
    bare hostname (e.g. ``brilliant-mcp.onrender.com``) — we prepend
    ``https://`` here. If it's already a full URL we respect it. On local
    dev the var is unset, so we fall back to the documented local MCP URL.
    """
    raw = os.getenv("BRILLIANT_MCP_PUBLIC_URL", "").strip()
    if not raw:
        return "http://localhost:8011"
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return f"https://{raw}"


def _render_credentials_page(
    email: str, api_key: str, mcp_url: str, login_url: str
) -> str:
    """Render the post-rotation credentials page.

    Mirrors the shape of ``/setup/done`` from T-0214: shows the new API key
    once, provides a copy button, and a client-side Blob download button
    for ``brilliant-credentials.txt``. Duplication with setup is intentional
    for now — keeping this file self-contained beats a premature shared
    template layer.
    """
    safe_email = _html.escape(email, quote=True)
    safe_key = _html.escape(api_key, quote=True)
    safe_mcp = _html.escape(mcp_url, quote=True)
    safe_login_url = _html.escape(login_url, quote=True)
    # The JS embeds the plaintext values via JSON.stringify to avoid any
    # quoting surprises inside the Blob contents.
    js_email = _json.dumps(email)
    js_key = _json.dumps(api_key)
    js_mcp = _json.dumps(mcp_url)
    js_login_url = _json.dumps(login_url)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>New API key — Brilliant</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 560px; margin: 64px auto; padding: 0 16px; color: #111; }}
  h1 {{ font-size: 1.5rem; }}
  .field {{ margin: 16px 0; }}
  .label {{ font-size: 0.85rem; color: #555; text-transform: uppercase;
            letter-spacing: 0.04em; margin-bottom: 4px; }}
  code {{ display: block; background: #f4f4f4; border: 1px solid #ddd;
          padding: 10px 12px; border-radius: 6px; word-break: break-all;
          font-size: 0.95rem; }}
  button {{ padding: 8px 12px; font-size: 0.9rem; background: #111; color: #fff;
            border: 0; border-radius: 6px; cursor: pointer; margin-right: 8px; }}
  button.secondary {{ background: #fff; color: #111; border: 1px solid #111; }}
  .warn {{ background: #fff8e1; border: 1px solid #f0d878; color: #6b5200;
           padding: 10px 12px; border-radius: 6px; font-size: 0.9rem;
           margin-bottom: 24px; }}
</style>
</head>
<body>
  <h1>Your new API key</h1>
  <div class="warn">
    <strong>Save this now.</strong> It will not be shown again. Any older
    keys you had are now invalidated.
  </div>

  <div class="field">
    <div class="label">Admin email</div>
    <code id="email-val">{safe_email}</code>
  </div>

  <div class="field">
    <div class="label">API key</div>
    <code id="key-val">{safe_key}</code>
  </div>

  <div class="field">
    <div class="label">MCP URL</div>
    <code id="mcp-val">{safe_mcp}</code>
  </div>

  <div class="field">
    <div class="label">Login URL</div>
    <code id="login-val">{safe_login_url}</code>
  </div>

  <div class="field">
    <button id="copy-key">Copy API key</button>
    <button id="copy-mcp" class="secondary">Copy MCP URL</button>
    <button id="download" class="secondary">Download brilliant-credentials.txt</button>
  </div>

<script>
  const EMAIL = {js_email};
  const API_KEY = {js_key};
  const MCP_URL = {js_mcp};
  const LOGIN_URL = {js_login_url};

  async function copyTo(btnId, value) {{
    try {{
      await navigator.clipboard.writeText(value);
      const btn = document.getElementById(btnId);
      const prev = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(() => {{ btn.textContent = prev; }}, 1500);
    }} catch (e) {{
      alert("Copy failed — please select the value manually.");
    }}
  }}

  document.getElementById("copy-key").addEventListener("click",
    () => copyTo("copy-key", API_KEY));
  document.getElementById("copy-mcp").addEventListener("click",
    () => copyTo("copy-mcp", MCP_URL));

  document.getElementById("download").addEventListener("click", () => {{
    const body =
      "Admin email: " + EMAIL + "\\n" +
      "API key: " + API_KEY + "\\n" +
      "MCP URL: " + MCP_URL + "\\n" +
      "Login URL: " + LOGIN_URL + "\\n";
    const blob = new Blob([body], {{ type: "text/plain" }});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "brilliant-credentials.txt";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }});
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Core login logic (shared by JSON + HTML paths)
# ---------------------------------------------------------------------------


class _LoginFailure(Exception):
    """Raised by ``_authenticate_and_rotate`` for any invalid-credential case.

    We deliberately collapse *all* failure modes (unknown email, wrong
    password, inactive account, missing password_hash) into a single
    exception with a single message. Callers translate this into the right
    surface — 401 JSON or a re-rendered form with an inline error — without
    ever leaking which specific check failed.
    """


async def _authenticate_and_rotate(
    email_raw: str, password: str
) -> tuple[str, dict]:
    """Verify credentials, rotate keys, return ``(api_key_plaintext, user_row)``.

    Rotation: before minting the new key we revoke every prior unrevoked
    key for the user. That is intentional — this endpoint is the panic
    button for a suspected leak, so "sign in and every other device drops"
    is the feature, not a bug.
    """
    email = email_raw.strip().lower()

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT id, org_id, display_name, email, role, department,
                   is_active, password_hash
            FROM users
            WHERE email = %s
            """,
            (email,),
        )
        cur.row_factory = dict_row
        user = await cur.fetchone()

        if user is None:
            raise _LoginFailure()
        if not user["is_active"]:
            raise _LoginFailure()
        if not user["password_hash"]:
            raise _LoginFailure()
        if not bcrypt.checkpw(
            password.encode("utf-8"),
            user["password_hash"].encode("utf-8"),
        ):
            raise _LoginFailure()

        # Rotation: revoke any currently-active keys for this user *before*
        # we mint the replacement. This invalidates every other live
        # session by design — it is the "something leaked" escape hatch.
        await conn.execute(
            "UPDATE api_keys SET is_revoked = TRUE "
            "WHERE user_id = %s AND is_revoked = FALSE",
            (user["id"],),
        )

        # Mint a fresh key. Format is preserved from the original login
        # handler so the `bkai_xxxx` 9-char prefix still matches the
        # partial unique index on api_keys.key_prefix WHERE NOT is_revoked.
        suffix = secrets.token_hex(12)
        key_prefix = f"bkai_{suffix[:4]}"
        full_key = f"{key_prefix}_{suffix[4:]}"
        key_hash = bcrypt.hashpw(
            full_key.encode("utf-8"), bcrypt.gensalt()
        ).decode("utf-8")

        await conn.execute(
            """
            INSERT INTO api_keys (user_id, org_id, key_hash, key_prefix, key_type, label)
            VALUES (%s, %s, %s, %s, 'interactive', 'Login session key')
            """,
            (user["id"], user["org_id"], key_hash, key_prefix),
        )

        return full_key, dict(user)


def _login_url_from_request(request: Request) -> str:
    """Best-effort absolute login URL, from the request's host and scheme."""
    # ``request.url_for`` would also work, but it can over-escape and pull
    # the scheme from ASGI state which isn't always accurate behind TLS
    # terminators like Render's proxy. The Host header + forwarded proto
    # (picked up by the ASGI server when ``forwarded_allow_ips`` is set)
    # is good enough.
    scheme = request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}/auth/login"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    """Render the HTML login form.

    ``?email=x@y.co`` pre-fills the email field — convenient when the user
    clicks the login URL embedded in their ``brilliant-credentials.txt``.
    """
    email = request.query_params.get("email", "") or ""
    return HTMLResponse(_render_login_form(email=email))


@router.post("/login")
async def login(request: Request):
    """Authenticate, rotate the user's API key, return JSON or HTML.

    Content negotiation is driven entirely by the request's ``Content-Type``:

    - ``application/json`` → returns :class:`LoginResponse` JSON (unchanged
      wire format for existing frontend callers).
    - ``application/x-www-form-urlencoded`` → returns an HTML credentials
      page with copy + download buttons.

    Both paths perform the same rotation (revoke-all-then-issue) so a
    JSON-driven frontend and the browser recovery form stay in sync.
    """
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    if content_type == "application/x-www-form-urlencoded" or content_type == "multipart/form-data":
        form = await request.form()
        email = (form.get("email") or "").strip()
        password = form.get("password") or ""

        try:
            api_key, user = await _authenticate_and_rotate(email, password)
        except _LoginFailure:
            # Re-render the form with an inline error. Keep the entered
            # email so the user doesn't have to retype it, but never echo
            # the password back.
            return HTMLResponse(
                _render_login_form(email=email, error="Invalid email or password"),
                status_code=401,
            )

        login_url = _login_url_from_request(request)
        mcp_url = _mcp_url_for_display()
        return HTMLResponse(
            _render_credentials_page(
                email=user["email"] or email,
                api_key=api_key,
                mcp_url=mcp_url,
                login_url=login_url,
            )
        )

    # Default / JSON path. We also land here for missing Content-Type —
    # matches the pre-existing behaviour where FastAPI parsed the JSON
    # body via the ``LoginRequest`` Pydantic model.
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    try:
        body = LoginRequest(**payload)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid login payload")

    try:
        api_key, user = await _authenticate_and_rotate(body.email, body.password)
    except _LoginFailure:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    return LoginResponse(
        api_key=api_key,
        user=UserResponse(
            id=user["id"],
            org_id=user["org_id"],
            display_name=user["display_name"],
            email=user["email"],
            role=user["role"],
            department=user["department"],
            is_active=user["is_active"],
        ),
    )
