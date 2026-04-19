"""First-run admin setup routes.

Three routes form the one-time admin claim ceremony shown on fresh Render
deploys (see Sprint 0037b):

- ``GET /setup``       — renders an email + password form
- ``POST /setup``      — creates the admin user + api_key, flips the
                          ``brilliant_settings.first_run_complete`` latch,
                          and INLINE-renders the credentials page as its
                          response body (no redirect — the plaintext key is
                          only ever present in the POST response)
- ``GET /setup/done``  — only useful when the latch is still FALSE, in which
                          case it nudges the operator back to the form

**Latch invariant:** every handler calls :func:`_require_first_run_open`
before doing anything else. Once the latch flips to TRUE (inside the same
transaction that creates the admin row in :mod:`api.admin_bootstrap`),
every subsequent hit to any route in this module returns 404. That is the
security boundary — no second admin can ever be claimed through this
surface.

**Credentials shown exactly once:** the plaintext API key returned by
``create_admin_via_post`` is embedded directly in the HTML response body
of the POST handler. The browser never re-fetches it; there is no
intermediate storage; the operator either copies it, downloads the
client-side-generated ``brilliant-credentials.txt`` blob, or loses it and
must recover via ``/auth/login``.
"""

from __future__ import annotations

import html as _html
import json as _json
import os

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from admin_bootstrap import FirstRunAlreadyClaimed, create_admin_via_post
from database import get_pool

router = APIRouter(tags=["setup"])


# ---------------------------------------------------------------------------
# Latch check
# ---------------------------------------------------------------------------


async def _require_first_run_open(pool) -> None:
    """Raise HTTPException(404) if the first-run latch is already claimed.

    Uses a raw pooled connection (no RLS context). The latch is readable by
    every PG role per migration 027, so no role-switch is needed.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT first_run_complete FROM brilliant_settings WHERE id = 1"
        )
        row = await cur.fetchone()

    if row is None:
        # Migration 027 not applied — fail closed rather than silently
        # expose /setup.
        raise HTTPException(
            status_code=500,
            detail="brilliant_settings singleton missing; migration 027 pending",
        )

    if row[0] is True:
        raise HTTPException(status_code=404, detail="Not found")


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def _login_url_from_request(request: Request) -> str:
    """Construct an absolute ``/auth/login`` URL from the inbound request.

    Matches the helper in ``routes/auth.py`` so the credentials page points
    callers back at the same login surface it mirrors.
    """
    scheme = request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}/auth/login"


async def _mcp_url_for_display(pool) -> str:
    """Render the MCP connector URL for display on ``/setup/done``.

    Resolution order:

    1. ``brilliant_settings.mcp_public_url`` — populated by the MCP service
       at boot with its own ``RENDER_EXTERNAL_URL`` (see
       ``mcp/remote_server.py::_publish_public_url_to_db``). This is the
       authoritative source on Render, where ``fromService.property: host``
       only exposes the internal service-discovery name.
    2. ``BRILLIANT_MCP_PUBLIC_URL`` env var — explicit override or
       fallback when the DB column is NULL (local dev, or a fresh Render
       deploy before the MCP service has completed its first boot). We
       prepend ``https://`` if the operator provided a bare hostname.
    3. ``http://localhost:8011`` — last-resort local dev default.

    Always returns a URL ending in ``/mcp`` — Claude Co-work's custom
    connector wizard requires the full MCP endpoint, not the bare host.
    Any trailing slash on the base URL is stripped before appending so we
    don't emit a double slash like ``https://foo.com//mcp``.
    """
    base: str | None = None
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT mcp_public_url FROM brilliant_settings WHERE id = 1"
            )
            row = await cur.fetchone()
        if row and row[0]:
            base = row[0]
    except Exception:
        # Column may not exist yet (migration 029 pending); fall through
        # to the env-var path rather than 500ing the /setup/done render.
        pass

    if not base:
        raw = os.getenv("BRILLIANT_MCP_PUBLIC_URL", "").strip()
        if not raw:
            base = "http://localhost:8011"
        elif raw.startswith("http://") or raw.startswith("https://"):
            base = raw
        else:
            base = f"https://{raw}"

    # Normalize: strip trailing slashes, then append /mcp. If the operator
    # (or DB column) already included /mcp for some reason, don't double it.
    base = base.rstrip("/")
    if base.endswith("/mcp"):
        return base
    return f"{base}/mcp"


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


_BASE_STYLE = """
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 560px; margin: 64px auto; padding: 0 16px; color: #111;
         line-height: 1.5; }
  h1 { font-size: 1.6rem; margin-bottom: 0.25rem; }
  p.sub { color: #555; margin-top: 0; }
  form { display: flex; flex-direction: column; gap: 14px; margin-top: 24px; }
  label { font-size: 0.9rem; color: #333; }
  input[type=email], input[type=password], input[type=text] {
    padding: 10px 12px; font-size: 1rem; border: 1px solid #ccc;
    border-radius: 6px; width: 100%; box-sizing: border-box;
  }
  button { padding: 10px 14px; font-size: 1rem; background: #111; color: #fff;
           border: 0; border-radius: 6px; cursor: pointer; margin-right: 8px; }
  button:hover { background: #333; }
  button.secondary { background: #fff; color: #111; border: 1px solid #111; }
  .error { background: #fdecec; color: #8a1f1f; padding: 10px 12px;
           border-radius: 6px; border: 1px solid #f5b5b5; }
  .warn { background: #fff8e1; border: 1px solid #f0d878; color: #6b5200;
          padding: 10px 12px; border-radius: 6px; font-size: 0.9rem;
          margin-bottom: 20px; }
  .info { background: #f1f5ff; border: 1px solid #c5d3f2; color: #22346b;
          padding: 10px 12px; border-radius: 6px; font-size: 0.9rem; }
  .field { margin: 16px 0; }
  .field-label { font-size: 0.8rem; color: #555; text-transform: uppercase;
                 letter-spacing: 0.04em; margin-bottom: 4px; }
  code { display: block; background: #f4f4f4; border: 1px solid #ddd;
         padding: 10px 12px; border-radius: 6px; word-break: break-all;
         font-size: 0.95rem; }
  .actions { margin-top: 24px; }
"""


def _render_setup_form(
    email: str = "",
    org_name: str = "",
    error: str | None = None,
) -> str:
    """Render the ``/setup`` form.

    On validation failure we re-render with the submitted email + org_name
    pre-filled (but never the password — passwords are never echoed back).
    """
    safe_email = _html.escape(email, quote=True)
    safe_org = _html.escape(org_name, quote=True)
    error_html = (
        f'<div class="error" role="alert">{_html.escape(error)}</div>'
        if error
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Welcome to Brilliant — Setup</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_BASE_STYLE}</style>
</head>
<body>
  <h1>Welcome to Brilliant</h1>
  <p class="sub">Let's finish setting up your knowledge base.</p>
  <div class="info">
    This is a <strong>one-time setup</strong>. Once you submit, this page
    is sealed and can never be used again.
  </div>
  {error_html}
  <form method="post" action="/setup" enctype="application/x-www-form-urlencoded">
    <div>
      <label for="org_name">Workspace name</label>
      <input id="org_name" name="org_name" type="text" value="{safe_org}"
             placeholder="e.g. Acme Corp" maxlength="100" required autofocus>
    </div>
    <div>
      <label for="email">Admin email</label>
      <input id="email" name="email" type="email" value="{safe_email}" required>
    </div>
    <div>
      <label for="password">Choose a password</label>
      <input id="password" name="password" type="password" minlength="8" required>
    </div>
    <div>
      <label for="password_confirm">Confirm password</label>
      <input id="password_confirm" name="password_confirm" type="password" minlength="8" required>
    </div>
    <div>
      <button type="submit">Create my admin account</button>
    </div>
  </form>
</body>
</html>
"""


def _render_done_page(
    email: str,
    api_key: str,
    client_id: str,
    client_secret: str,
    mcp_url: str,
    login_url: str,
) -> str:
    """Render the post-setup credentials page.

    Shows all six user-facing fields exactly once:

    - ``email``          — admin email
    - ``api_key``        — admin interactive API key (plaintext)
    - ``client_id``      — OAuth client id for Claude Co-work
    - ``client_secret``  — OAuth client secret for Claude Co-work
    - ``mcp_url``        — MCP endpoint (already ``/mcp``-suffixed)
    - ``login_url``      — password-recovery URL

    Note the deliberate omission of ``service_api_key`` — that's an
    MCP-internal credential consumed by the MCP service's outbound
    Authorization header. It's read back from env or the DB by ops tooling
    and is never part of the operator-visible ceremony.

    Client-side JS offers a clipboard-copy button per secret and a
    Blob-based ``brilliant-credentials.txt`` download listing all six.
    """
    safe_email = _html.escape(email, quote=True)
    safe_key = _html.escape(api_key, quote=True)
    safe_client_id = _html.escape(client_id, quote=True)
    safe_client_secret = _html.escape(client_secret, quote=True)
    safe_mcp = _html.escape(mcp_url, quote=True)
    safe_login = _html.escape(login_url, quote=True)

    # JSON-encode for safe JS embedding — sidesteps any quoting surprises
    # inside the Blob contents.
    js_email = _json.dumps(email)
    js_key = _json.dumps(api_key)
    js_client_id = _json.dumps(client_id)
    js_client_secret = _json.dumps(client_secret)
    js_mcp = _json.dumps(mcp_url)
    js_login = _json.dumps(login_url)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>You're live — Brilliant</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_BASE_STYLE}</style>
</head>
<body>
  <h1>You're live</h1>
  <p class="sub">Your admin account is created. Save these credentials now.</p>

  <div class="warn">
    <strong>This is the only time you'll see these secrets.</strong>
    Download the <code style="display:inline;padding:1px 5px;">.txt</code>
    file or copy them now. If you lose the API key, sign in with your
    email + password to rotate.
  </div>

  <div class="field">
    <div class="field-label">Admin email</div>
    <code id="email-val">{safe_email}</code>
  </div>

  <div class="field">
    <div class="field-label">API key</div>
    <code id="key-val" data-copy>{safe_key}</code>
    <div class="actions">
      <button type="button" id="copy-key">Copy API key</button>
    </div>
  </div>

  <div class="field">
    <div class="field-label">MCP connector URL (for Claude)</div>
    <code id="mcp-val" data-copy>{safe_mcp}</code>
    <div class="actions">
      <button type="button" id="copy-mcp" class="secondary">Copy MCP URL</button>
    </div>
  </div>

  <div class="field">
    <div class="field-label">OAuth client ID</div>
    <code id="client-id-val" data-copy>{safe_client_id}</code>
    <div class="actions">
      <button type="button" id="copy-client-id" class="secondary">Copy client ID</button>
    </div>
  </div>

  <div class="field">
    <div class="field-label">OAuth client secret</div>
    <code id="client-secret-val" data-copy>{safe_client_secret}</code>
    <div class="actions">
      <button type="button" id="copy-client-secret" class="secondary">Copy client secret</button>
    </div>
  </div>

  <div class="field">
    <div class="field-label">Login URL (password recovery)</div>
    <code id="login-val">{safe_login}</code>
  </div>

  <div class="actions">
    <button type="button" id="download">Download brilliant-credentials.txt</button>
  </div>

  <div class="field">
    <div class="field-label">Optional next step</div>
    <div class="info">
      Already have an Obsidian vault? <a href="/import/vault">Import it now</a>
      to seed your knowledge base.
    </div>
  </div>

<script>
  const EMAIL = {js_email};
  const API_KEY = {js_key};
  const CLIENT_ID = {js_client_id};
  const CLIENT_SECRET = {js_client_secret};
  const MCP_URL = {js_mcp};
  const LOGIN_URL = {js_login};

  async function copyText(text, btn) {{
    try {{
      await navigator.clipboard.writeText(text);
      const prev = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(() => {{ btn.textContent = prev; }}, 1500);
    }} catch (e) {{
      alert("Copy failed — please select the value manually.");
    }}
  }}

  document.getElementById("copy-key").addEventListener("click", (ev) => {{
    copyText(API_KEY, ev.currentTarget);
  }});

  document.getElementById("copy-mcp").addEventListener("click", (ev) => {{
    copyText(MCP_URL, ev.currentTarget);
  }});

  document.getElementById("copy-client-id").addEventListener("click", (ev) => {{
    copyText(CLIENT_ID, ev.currentTarget);
  }});

  document.getElementById("copy-client-secret").addEventListener("click", (ev) => {{
    copyText(CLIENT_SECRET, ev.currentTarget);
  }});

  document.getElementById("download").addEventListener("click", () => {{
    const body =
      "Admin email: " + EMAIL + "\\n" +
      "API key: " + API_KEY + "\\n" +
      "client_id: " + CLIENT_ID + "\\n" +
      "client_secret: " + CLIENT_SECRET + "\\n" +
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


def _render_done_nudge() -> str:
    """Render a tiny ``/setup/done`` nudge shown only when latch is FALSE.

    In practice this page is nearly unreachable: POST ``/setup`` inlines
    the credentials HTML directly in its response (no redirect to
    ``/setup/done``), and a successful POST also flips the latch — so
    subsequent GETs of ``/setup/done`` will 404 via
    :func:`_require_first_run_open`. This exists for the edge case where a
    user manually navigates to ``/setup/done`` before submitting the form.
    """
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Setup not complete — Brilliant</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_BASE_STYLE}</style>
</head>
<body>
  <h1>Setup not complete</h1>
  <p class="sub">Please submit the setup form first.</p>
  <div class="actions">
    <a href="/setup"><button type="button">Go to setup</button></a>
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/setup", response_class=HTMLResponse)
async def setup_form() -> HTMLResponse:
    """Render the admin setup form.

    404s once the latch is set — this URL is single-use.
    """
    pool = get_pool()
    await _require_first_run_open(pool)

    prefilled_email = os.getenv("ADMIN_EMAIL", "").strip()
    return HTMLResponse(_render_setup_form(email=prefilled_email))


@router.post("/setup")
async def setup_submit(
    request: Request,
    org_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
) -> HTMLResponse:
    """Create the admin user, flip the latch, inline the credentials page.

    The plaintext API key only ever exists in:
      1. The transaction that created the key row (RAM), and
      2. The HTML body of this response.

    Validation failures return a 400 with the form re-rendered — the latch
    is NOT flipped on validation failure, so the operator can retry.
    ``FirstRunAlreadyClaimed`` — raised when another request beat us to
    the latch — surfaces as a 404, matching the sealed-after-claim
    invariant.
    """
    pool = get_pool()
    await _require_first_run_open(pool)

    email_clean = (email or "").strip()
    org_name_clean = (org_name or "").strip()

    # Validate — stay before the bootstrap call so a failed validation
    # never touches the DB or the latch.
    error: str | None = None
    if not org_name_clean:
        error = "Workspace name is required."
    elif len(org_name_clean) > 100:
        error = "Workspace name must be 100 characters or fewer."
    elif not email_clean:
        error = "Email is required."
    elif "@" not in email_clean:
        error = "Please enter a valid email address."
    elif len(password) < 8:
        error = "Password must be at least 8 characters."
    elif password != password_confirm:
        error = "Passwords do not match."

    if error:
        return HTMLResponse(
            _render_setup_form(
                email=email_clean, org_name=org_name_clean, error=error
            ),
            status_code=400,
        )

    try:
        (
            api_key_plaintext,
            _service_api_key,
            client_id,
            client_secret,
            _user_id,
        ) = await create_admin_via_post(
            pool, email_clean, password, org_name=org_name_clean
        )
    except FirstRunAlreadyClaimed:
        # Another request raced us to the latch — treat as sealed.
        raise HTTPException(status_code=404, detail="Not found")

    # `_service_api_key` is intentionally NOT displayed: it's an
    # MCP-internal credential (the MCP service's outbound Bearer token for
    # API → act-as-user calls). Ops tooling reads it from env or DB; the
    # operator-facing ceremony only shows the four user-configurable fields
    # plus the derived MCP + login URLs. See spec 0039.

    mcp_url = await _mcp_url_for_display(pool)
    login_url = _login_url_from_request(request)

    return HTMLResponse(
        _render_done_page(
            email=email_clean,
            api_key=api_key_plaintext,
            client_id=client_id,
            client_secret=client_secret,
            mcp_url=mcp_url,
            login_url=login_url,
        )
    )


@router.get("/setup/done", response_class=HTMLResponse)
async def setup_done() -> HTMLResponse:
    """Post-setup page nudge.

    This route is effectively unreachable post-claim: a successful
    ``POST /setup`` flips the latch AND inlines the credentials HTML in
    the same response (no redirect here). Therefore:

    - Latch FALSE (pre-claim): render a small "submit the form first"
      nudge with a link to ``/setup``.
    - Latch TRUE (post-claim): 404 via :func:`_require_first_run_open`.
    """
    pool = get_pool()
    await _require_first_run_open(pool)
    return HTMLResponse(_render_done_nudge())
