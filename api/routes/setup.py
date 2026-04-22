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

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from admin_bootstrap import FirstRunAlreadyClaimed, create_admin_via_post
from auth import UserContext, get_current_user
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


async def _services_ready(pool) -> bool:
    """Return True iff both ``api_public_url`` and ``mcp_public_url`` are set.

    The API and MCP services each publish their own
    ``RENDER_EXTERNAL_URL`` into ``brilliant_settings`` at boot (migrations
    029 and 032). On a cold Render deploy, one or both columns may still be
    NULL at the instant the operator lands on ``/setup`` — if we don't warn
    them, they'll paste a not-yet-resolvable URL into Claude and hit 502s.

    Fail-closed semantics: any exception (missing singleton row, column not
    yet migrated, DB unreachable) returns ``False`` so we show the banner
    rather than silently suppress it. The banner is additive — it never
    replaces the one-shot API key display.
    """
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT api_public_url, mcp_public_url "
                "FROM brilliant_settings WHERE id = 1"
            )
            row = await cur.fetchone()
    except Exception:
        return False

    if not row:
        return False
    return bool(row[0]) and bool(row[1])


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
            port = os.getenv("BRILLIANT_MCP_PORT", "").strip() or "8011"
            base = f"http://localhost:{port}"
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


# ---------------------------------------------------------------------------
# Brand assets — inlined SVGs
# ---------------------------------------------------------------------------
#
# Inlined rather than served from /static/brand/* because (a) FastAPI has no
# static mount configured today and (b) the SVGs are tiny (~2KB total). Keep
# these in sync with .github/assets/logo.svg and .github/assets/title-light.svg
# when the README brand mark is updated.

_BRAND_LOGO_SVG = """<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Brilliant logo" class="brand-logo">
  <defs>
    <linearGradient id="gemEdge" gradientUnits="userSpaceOnUse" x1="50" y1="13" x2="50" y2="93">
      <stop offset="0" stop-color="#8aa0ff"/>
      <stop offset="1" stop-color="#4558c9"/>
    </linearGradient>
  </defs>
  <polygon points="28,13 72,13 85,34 50,93 15,34" fill="none" stroke="#000000" stroke-width="5" stroke-linejoin="round"/>
  <polygon points="28,13 72,13 85,34 50,93 15,34" fill="url(#gemEdge)" stroke="#ffffff" stroke-width="2.4" stroke-linejoin="round"/>
  <g>
    <polygon points="30,17 20,34 40,34" fill="#ffffff"/>
    <polygon points="30,17 50,17 40,34" fill="url(#gemEdge)"/>
    <polygon points="50,17 40,34 60,34" fill="#ffffff"/>
    <polygon points="50,17 70,17 60,34" fill="url(#gemEdge)"/>
    <polygon points="70,17 60,34 80,34" fill="#ffffff"/>
    <polygon points="20,34 40,34 30,51" fill="url(#gemEdge)"/>
    <polygon points="40,34 30,51 50,51" fill="#ffffff"/>
    <polygon points="40,34 60,34 50,51" fill="url(#gemEdge)"/>
    <polygon points="60,34 50,51 70,51" fill="#ffffff"/>
    <polygon points="60,34 80,34 70,51" fill="url(#gemEdge)"/>
    <polygon points="30,51 50,51 40,68" fill="url(#gemEdge)"/>
    <polygon points="50,51 40,68 60,68" fill="#ffffff"/>
    <polygon points="50,51 70,51 60,68" fill="url(#gemEdge)"/>
    <polygon points="40,68 60,68 50,85" fill="url(#gemEdge)"/>
  </g>
</svg>"""

_BRAND_TITLE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 520 72" role="img" aria-label="xiReactor / Brilliant" class="brand-title">
  <text x="260" y="54" text-anchor="middle" font-family="system-ui, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif" font-size="56" font-weight="700" letter-spacing="-1.2">
    <tspan fill="#4558c9">xi</tspan><tspan fill="#1f2328">Reactor / </tspan><tspan fill="#4558c9">Brilliant</tspan>
  </text>
</svg>"""

_BRAND_TAGLINE = "One shared knowledge base for your whole AI-enabled team"


def _render_brand_header() -> str:
    """Render the shared brand header: logo + title SVG + tagline.

    Used by both ``/setup`` (pre-claim form) and the credentials page
    (post-claim) so the first-run operator sees the same mark that appears
    in the README. Purely presentational — no secrets or form fields.
    """
    return f"""<header class="brand-header" role="banner">
    <div class="brand-mark">{_BRAND_LOGO_SVG}{_BRAND_TITLE_SVG}</div>
    <p class="brand-tagline">{_html.escape(_BRAND_TAGLINE)}</p>
  </header>"""


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
  a.button { display: inline-block; padding: 10px 14px; font-size: 1rem;
             background: #111; color: #fff; border: 0; border-radius: 6px;
             cursor: pointer; margin-right: 8px; text-decoration: none; }
  a.button:hover { background: #333; }
  a.button.secondary { background: #fff; color: #111; border: 1px solid #111; }
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

  /* Brand header — logo + title SVG + tagline, matches README brand mark. */
  .brand-header { text-align: center; margin-bottom: 32px; }
  .brand-mark { display: flex; align-items: center; justify-content: center;
                gap: 14px; margin-bottom: 8px; }
  .brand-logo { width: 56px; height: 56px; flex: 0 0 auto; }
  .brand-title { height: 42px; width: auto; flex: 0 1 auto; max-width: 360px; }
  .brand-tagline { color: #555; font-size: 0.95rem; margin: 0; }

  /* Pulsing blue CTA for the credentials-download button. The CTA pulses
   * until the operator clicks it (or explicitly checks the "I've saved"
   * box) — at which point JS removes the .pulse class and the
   * beforeunload guard is cleared. */
  button.pulse { background: #2f5ad8; color: #fff;
                 animation: brilliant-pulse 1.8s ease-in-out infinite;
                 box-shadow: 0 0 0 0 rgba(47, 90, 216, 0.7); }
  button.pulse:hover { background: #1f48c2; }
  @keyframes brilliant-pulse {
    0%   { box-shadow: 0 0 0 0 rgba(47, 90, 216, 0.55); }
    70%  { box-shadow: 0 0 0 14px rgba(47, 90, 216, 0); }
    100% { box-shadow: 0 0 0 0 rgba(47, 90, 216, 0); }
  }

  /* Saved-credentials acknowledgement checkbox (second way to clear the
   * beforeunload guard, for operators who copy-paste instead of
   * downloading). */
  .saved-ack { margin-top: 16px; font-size: 0.9rem; color: #333; }
  .saved-ack input[type=checkbox] { margin-right: 6px; vertical-align: middle; }
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
  {_render_brand_header()}
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


_WARMING_BANNER = """<div class="warn" role="status">
    <strong>Your Brilliant services are still warming up.</strong>
    Your API key below is permanent and safe to save now — copy it before
    doing anything else. One or both Brilliant services haven't finished
    their first boot yet (this usually takes 30&ndash;60 seconds after a
    fresh deploy). Wait for this banner to disappear before connecting
    Claude, then refresh this page to re-check.
  </div>"""


def _render_done_page(
    email: str,
    api_key: str,
    client_id: str,
    client_secret: str,
    mcp_url: str,
    login_url: str,
    services_ready: bool = True,
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

    When ``services_ready`` is False (``api_public_url`` or
    ``mcp_public_url`` still NULL in ``brilliant_settings``), a
    ``<div class="warn">`` "warming up" banner is prepended above the
    one-shot secrets block. The banner is purely informational — no
    ``meta http-equiv="refresh"`` is ever injected, since an auto-refresh
    would obscure the plaintext API key that is only rendered in this
    response. See spec 0042 / T-0251.

    Polish applied in spec 0043 / T-0255:

    - Brand header (logo + title SVG + tagline) matches the README brand
      mark, inlined to keep the page dependency-free.
    - Download button carries a ``pulse`` class with a CSS keyframe
      animation so the CTA is unmissable; JS clears the class on first
      click and writes ``localStorage.brilliant_creds_downloaded = "1"``.
    - ``beforeunload`` listener triggers the browser's "leave site?"
      prompt until the operator clicks download OR checks the "I've saved
      my credentials" checkbox. Both paths call ``clearGuard()``.
    - Secondary Import Obsidian vault button carries the plaintext
      ``api_key`` as a URL fragment (``#api_key=…``) — fragments stay on
      the client, so ``/import/vault`` can autofill the vault-upload
      page's localStorage + input without the key ever hitting the
      server. The destination page clears the fragment via
      ``history.replaceState`` on load.
    """
    safe_email = _html.escape(email, quote=True)
    safe_key = _html.escape(api_key, quote=True)
    safe_client_id = _html.escape(client_id, quote=True)
    safe_client_secret = _html.escape(client_secret, quote=True)
    safe_mcp = _html.escape(mcp_url, quote=True)
    safe_login = _html.escape(login_url, quote=True)

    # Fragment-carrying Import Vault link: the api_key rides in the URL
    # fragment so it never hits the server (fragments are client-side
    # only). /import/vault reads window.location.hash, stuffs the key
    # into localStorage, then replaces history state to strip the
    # fragment from the visible URL so a copy-paste / back-nav doesn't
    # expose the secret.
    safe_import_vault_href = f"/import/vault#api_key={_html.escape(api_key, quote=True)}"

    # JSON-encode for safe JS embedding — sidesteps any quoting surprises
    # inside the Blob contents.
    js_email = _json.dumps(email)
    js_key = _json.dumps(api_key)
    js_client_id = _json.dumps(client_id)
    js_client_secret = _json.dumps(client_secret)
    js_mcp = _json.dumps(mcp_url)
    js_login = _json.dumps(login_url)

    warming_banner = "" if services_ready else _WARMING_BANNER

    brand_header = _render_brand_header()

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>You're live — Brilliant</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_BASE_STYLE}</style>
</head>
<body>
  {brand_header}
  <h1>You're live</h1>
  <p class="sub">Your admin account is created. Save these credentials now.</p>

  {warming_banner}
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
    <button type="button" id="download" class="pulse">Download brilliant-credentials.txt</button>
    <a id="import-vault-btn" class="button secondary"
       href="{safe_import_vault_href}" target="_blank" rel="noopener">Import Obsidian vault</a>
  </div>

  <div class="saved-ack">
    <label>
      <input type="checkbox" id="saved-ack-checkbox">
      I've saved my credentials
    </label>
  </div>

<script>
  const EMAIL = {js_email};
  const API_KEY = {js_key};
  const CLIENT_ID = {js_client_id};
  const CLIENT_SECRET = {js_client_secret};
  const MCP_URL = {js_mcp};
  const LOGIN_URL = {js_login};

  // ----- beforeunload guard -----
  // Browser confirms navigation away as long as `guardActive` is true. It
  // flips to false on either (a) a download-click, or (b) an explicit
  // check of the "I've saved my credentials" checkbox. Either action
  // also writes `brilliant_creds_downloaded=1` so a future page refresh
  // doesn't re-arm the guard. The API key is rendered on this exact
  // response body; a refresh re-GETs /setup/done which 404s post-claim —
  // which is why the guard is worth the UX friction.
  let guardActive = true;

  function clearGuard() {{
    guardActive = false;
    const btn = document.getElementById("download");
    if (btn) {{
      btn.classList.remove("pulse");
    }}
    try {{
      window.localStorage.setItem("brilliant_creds_downloaded", "1");
    }} catch (e) {{ /* localStorage disabled — ignore */ }}
  }}

  window.addEventListener("beforeunload", (ev) => {{
    if (!guardActive) return;
    ev.preventDefault();
    // Most modern browsers ignore the returnValue string and show their
    // own generic "Leave site?" dialog — but setting it is the spec'd
    // way to trigger the prompt. Keep the message short.
    ev.returnValue = "You haven't saved your credentials yet.";
    return ev.returnValue;
  }});

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
    // Stop pulsing + disarm the beforeunload guard on first click. We
    // intentionally clear the guard on download-start (not download-
    // finish) because the browser fires the click event synchronously
    // before the Blob save dialog resolves.
    clearGuard();
  }});

  document.getElementById("saved-ack-checkbox").addEventListener("change", (ev) => {{
    if (ev.currentTarget.checked) {{
      clearGuard();
    }}
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
    services_ready = await _services_ready(pool)

    return HTMLResponse(
        _render_done_page(
            email=email_clean,
            api_key=api_key_plaintext,
            client_id=client_id,
            client_secret=client_secret,
            mcp_url=mcp_url,
            login_url=login_url,
            services_ready=services_ready,
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


# ---------------------------------------------------------------------------
# /credentials — Sprint 0043 T-0254 (Issue #45 Option B)
# ---------------------------------------------------------------------------
#
# Recovery route. Operators who lose ``brilliant-credentials.txt`` can
# re-fetch the six-field payload with their admin API key. The payload
# shape matches the installer file byte-for-byte so a user can
# ``curl ... > brilliant-credentials.txt`` the response and have a
# drop-in replacement.
#
# Auth: reuses :func:`auth.get_current_user` (Bearer token in
# ``Authorization: Bearer <api_key>``). Admin-role gated — non-admin
# callers get 403. No Bearer → 401 (enforced by
# :func:`auth._extract_bearer_token`).
#
# Fields sourced:
#   * admin_email         — ``users.email`` for the authenticated admin.
#   * admin_api_key       — the Bearer token the caller presented (we
#                            verified it already; the DB stores only the
#                            bcrypt hash, so there is no other way to
#                            echo it back).
#   * oauth_client_id     — first row of ``oauth_clients`` (there is
#                            exactly one, minted by admin_bootstrap).
#   * oauth_client_secret — matching row's plaintext secret.
#   * mcp_url             — :func:`_mcp_url_for_display` (DB +
#                            ``BRILLIANT_MCP_PUBLIC_URL`` env fallback).
#   * login_url           — DB ``api_public_url`` →
#                            ``API_BASE_URL`` env →
#                            request host, each suffixed with
#                            ``/auth/login``.
#
# Content negotiation: default JSON; HTML if ``Accept: text/html`` is
# the top-ranked preference. HTML body is a minimal ``<pre>`` + copy
# button (no brand chrome needed — this is an ops recovery surface,
# not the one-shot ceremony).


def _require_admin(user: UserContext) -> None:
    """Raise 403 if the caller is not an admin.

    Mirrors the private helper used across other admin-gated routes
    (``api/routes/users.py``, ``api/routes/analytics.py``, etc.). Kept
    module-local to avoid a cross-module import for a two-line check.
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")


async def _fetch_oauth_client_creds(pool) -> tuple[str, str]:
    """Return ``(client_id, client_secret)`` for the installed OAuth client.

    There is exactly one pre-registered row minted by
    :func:`admin_bootstrap._create_admin_and_flip_latch`. If multiple
    rows exist (manual DB edits, re-registered client), we return the
    most recently created one — which matches what Claude Co-work would
    be configured against.

    Raises HTTPException(500) if the table is empty (bootstrap
    incomplete — should be impossible behind admin auth, but fail-loud
    beats returning an opaque null).
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT client_id, client_secret
            FROM oauth_clients
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
        row = await cur.fetchone()

    if row is None:
        raise HTTPException(
            status_code=500,
            detail="OAuth client not found; admin_bootstrap incomplete",
        )
    return str(row[0]), str(row[1])


async def _fetch_admin_email(pool, user_id: str) -> str:
    """Return the authenticated admin's email.

    The :class:`UserContext` returned by :func:`auth.get_current_user`
    doesn't carry ``email`` (it never needed to — auth only needs
    role + org + id). We fetch it here via a single SELECT so the
    recovery payload can echo it as ``admin_email=...``.

    Returns an empty string when the email is NULL (legacy API-key-only
    admins from pre-013_user_auth.sql deploys). The six-field contract
    still requires the key to be present, so the caller just emits an
    empty value rather than a null.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT email FROM users WHERE id = %s",
            (user_id,),
        )
        row = await cur.fetchone()

    if row is None or row[0] is None:
        return ""
    return str(row[0])


async def _login_url_for_credentials(request: Request, pool) -> str:
    """Render the ``/auth/login`` URL with the same resolution order
    used at install time.

    Priority:

    1. ``brilliant_settings.api_public_url`` (migration 032) — set by
       the API service on boot from ``RENDER_EXTERNAL_URL``. Authoritative
       on Render deploys; NULL on a fresh local dev DB.
    2. ``API_BASE_URL`` env var — local dev override /
       docker-compose plumbing.
    3. The inbound request's scheme + host — last-resort for local
       curls where neither of the above is set.
    """
    base: str | None = None
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT api_public_url FROM brilliant_settings WHERE id = 1"
            )
            row = await cur.fetchone()
        if row and row[0]:
            base = str(row[0])
    except Exception:
        # Column may not exist yet (migration 032 pending); fall through.
        base = None

    if not base:
        raw = os.getenv("API_BASE_URL", "").strip()
        if raw:
            if raw.startswith("http://") or raw.startswith("https://"):
                base = raw
            else:
                base = f"https://{raw}"

    if not base:
        scheme = request.url.scheme
        host = request.headers.get("host") or request.url.netloc
        base = f"{scheme}://{host}"

    return f"{base.rstrip('/')}/auth/login"


def _render_credentials_html(payload: dict) -> str:
    """Render the minimal HTML view of the six-field payload.

    One-way ops surface: a ``<pre>`` block with the exact
    ``key=value`` lines the installer wrote to
    ``brilliant-credentials.txt``, plus a single copy-to-clipboard
    button. Intentionally unstyled brand chrome — this page is for
    operators recovering a lost file, not the one-shot ceremony.
    """
    lines = "\n".join(f"{k}={payload[k]}" for k in _CREDENTIAL_FIELD_ORDER)
    safe_lines = _html.escape(lines)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Brilliant credentials</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 680px; margin: 48px auto; padding: 0 16px; color: #111;
         line-height: 1.5; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.25rem; }}
  p.sub {{ color: #555; margin-top: 0; }}
  pre {{ background: #f4f4f4; border: 1px solid #ddd; border-radius: 6px;
         padding: 16px; overflow-x: auto; font-size: 0.95rem;
         white-space: pre-wrap; word-break: break-all; }}
  button {{ padding: 10px 14px; font-size: 1rem; background: #2f5ad8;
           color: #fff; border: 0; border-radius: 6px; cursor: pointer;
           margin-top: 12px; }}
  button:hover {{ background: #1f48c2; }}
  .note {{ color: #555; font-size: 0.85rem; margin-top: 16px; }}
</style>
</head>
<body>
  <h1>Brilliant credentials</h1>
  <p class="sub">Save this file as <code>brilliant-credentials.txt</code>.</p>
  <pre id="creds">{safe_lines}</pre>
  <button type="button" id="copy-btn">Copy all</button>
  <p class="note">
    This page only shows the admin API key you authenticated with.
    Rotate it via <code>/auth/login</code> if you suspect it's compromised.
  </p>
<script>
  document.getElementById("copy-btn").addEventListener("click", async (ev) => {{
    const text = document.getElementById("creds").textContent;
    try {{
      await navigator.clipboard.writeText(text);
      const btn = ev.currentTarget;
      const prev = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(() => {{ btn.textContent = prev; }}, 1500);
    }} catch (e) {{
      alert("Copy failed — select the text manually.");
    }}
  }});
</script>
</body>
</html>
"""


# Canonical field order for the six-field credential contract. Matches
# :func:`admin_bootstrap.ensure_admin_user`'s machine-parseable block
# and install.sh's ``brilliant-credentials.txt`` writer. Any change here
# must also update T-0253's installer code in lockstep.
_CREDENTIAL_FIELD_ORDER = (
    "admin_email",
    "admin_api_key",
    "oauth_client_id",
    "oauth_client_secret",
    "mcp_url",
    "login_url",
)


def _prefers_html(request: Request) -> bool:
    """Return True when the client's ``Accept`` header tops HTML.

    We intentionally keep this simple — a strict RFC 7231 quality-value
    parser would be overkill for a recovery surface. If ``text/html``
    appears anywhere in ``Accept`` AND ``application/json`` does not, we
    render HTML; otherwise JSON (the default for ``curl`` and scripted
    callers that omit ``Accept`` entirely).
    """
    accept = (request.headers.get("accept") or "").lower()
    if not accept or accept == "*/*":
        return False
    if "application/json" in accept:
        return False
    return "text/html" in accept


@router.get("/credentials")
async def credentials_recovery(
    request: Request,
    user: UserContext = Depends(get_current_user),
):
    """Admin-gated recovery surface for the six-field credential payload.

    Re-emits the exact same ``key=value`` fields that the installer
    wrote to ``brilliant-credentials.txt``, so an operator who lost
    that file can restore it with:

    .. code-block:: sh

        curl -H "Authorization: Bearer $ADMIN_KEY" \\
             http://localhost:8010/credentials \\
             > brilliant-credentials.txt

    Auth: :func:`auth.get_current_user` verifies the Bearer token
    (401 on missing / invalid). :func:`_require_admin` enforces
    ``role=admin`` (403 for non-admin users).

    Content negotiation: default JSON; HTML ``<pre>`` block when the
    client's ``Accept`` header requests ``text/html`` explicitly.

    admin_api_key note: the DB stores only the bcrypt hash of the key.
    We echo the plaintext Bearer the caller already presented, because
    the auth middleware has bcrypt-verified it against the stored hash.
    That means this route is safe to call from a machine that has the
    key but lost the creds file; it is NOT a key-recovery surface for
    operators who've lost the key entirely (those must rotate via
    ``/auth/login``).
    """
    _require_admin(user)

    pool = get_pool()

    # Extract the Bearer token to echo back as admin_api_key — this is
    # the only way to put the plaintext on the wire, since the DB only
    # has the bcrypt hash. Auth already verified it against the hash,
    # so it's safe to round-trip.
    auth_header = request.headers.get("Authorization") or ""
    parts = auth_header.split(" ", 1)
    admin_api_key = parts[1].strip() if len(parts) == 2 else ""

    admin_email = await _fetch_admin_email(pool, user.id)
    client_id, client_secret = await _fetch_oauth_client_creds(pool)
    mcp_url = await _mcp_url_for_display(pool)
    login_url = await _login_url_for_credentials(request, pool)

    payload = {
        "admin_email": admin_email,
        "admin_api_key": admin_api_key,
        "oauth_client_id": client_id,
        "oauth_client_secret": client_secret,
        "mcp_url": mcp_url,
        "login_url": login_url,
    }

    if _prefers_html(request):
        return HTMLResponse(_render_credentials_html(payload))

    return JSONResponse(payload)
