"""xiReactor Brilliant API — FastAPI application entrypoint."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from database import init_pool, close_pool, get_pool
from admin_bootstrap import ensure_admin_user
from middleware.request_log import RequestLogMiddleware

logger = logging.getLogger("brilliant.api")


async def _publish_public_url_to_db(pool) -> None:
    """Publish this API's public URL to ``brilliant_settings.api_public_url``.

    Precise mirror of ``mcp/remote_server.py::_publish_public_url_to_db`` —
    Render's ``fromService.property: host`` returns the internal service-
    discovery name (e.g. ``brilliant-api``), not the public FQDN, so the
    MCP service can't construct the API's browser-visible URL from that
    alone. This function writes our authoritative ``RENDER_EXTERNAL_URL``
    into a shared DB column that the MCP reads when 302-redirecting the
    user to ``/oauth/login`` at the start of the OAuth handoff flow.

    Failure-tolerant by design: a missing ``api_public_url`` column
    (migration 032 not yet applied) or any DB hiccup must not prevent the
    API from serving traffic. We log and continue.

    Idempotent: the UPDATE re-writes the same value on every boot, and the
    row is a singleton keyed by ``id = 1``.

    Local-dev path: when ``$RENDER_EXTERNAL_URL`` is unset or empty we
    warn-log and skip. The MCP's ``_resolve_api_public_url`` falls through
    to ``BRILLIANT_BASE_URL`` → ``http://localhost:8010`` in that case.
    """
    render_external_url = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    if not render_external_url:
        logger.warning(
            "RENDER_EXTERNAL_URL not set; skipping api_public_url publish "
            "(local-dev path — MCP will fall back to BRILLIANT_BASE_URL)."
        )
        return

    try:
        async with pool.connection() as conn:
            async with conn.transaction():
                # SET LOCAL ROLE to kb_admin so the UPDATE is authorised
                # under the grant from migration 027. Works locally
                # (superuser) and on Render (connection user is a member
                # of kb_admin via migration 028). SET LOCAL scopes to
                # this transaction only, avoiding pool-connection role
                # poisoning (see feedback memory: feedback_set_local_role).
                await conn.execute("SET LOCAL ROLE kb_admin")
                await conn.execute(
                    "UPDATE brilliant_settings "
                    "SET api_public_url = %s, updated_at = now() "
                    "WHERE id = 1",
                    (render_external_url,),
                )
        logger.info(
            "Published API public URL to brilliant_settings: %s",
            render_external_url,
        )
    except Exception as exc:  # noqa: BLE001 — never block API boot on this
        logger.warning(
            "Could not publish API public URL to brilliant_settings "
            "(DB unreachable or migration 032 not yet applied): %s",
            exc,
        )


def _log_ready_banner() -> None:
    """Emit an unmistakable "ready" marker for operators watching Render logs.

    A fresh Render deploy can take ~3-4 min from "deploy started" to "edge
    routing live". Without an explicit marker, a non-technical operator
    watching the log stream has no signal for when it's safe to click the
    setup link. This banner is the signal: when this line scrolls past,
    open the URL.
    """
    public_url = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    setup_url = f"{public_url}/setup" if public_url else "http://localhost:8010/setup"
    bar = "=" * 64
    logger.info(bar)
    logger.info("  YOUR BRILLIANT SYSTEM IS COMPLETELY READY NOW")
    logger.info("  Open: %s", setup_url)
    logger.info(bar)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage connection pool lifecycle and run startup bootstrap."""
    await init_pool()
    await _publish_public_url_to_db(get_pool())
    await ensure_admin_user(get_pool())
    _log_ready_banner()
    yield
    await close_pool()


app = FastAPI(
    title="xiReactor Brilliant API",
    description="Knowledge base API with RLS-enforced permissions and governance pipeline",
    version="0.4.0",
    lifespan=lifespan,
    redirect_slashes=False,
)

# CORS — allow all origins in dev mode
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request logger — added AFTER CORS so it becomes the outermost middleware
# (Starlette prepends on add_middleware + reverses on build, so last-added
# wraps everything). This lets us time the full stack including CORS + auth.
app.add_middleware(RequestLogMiddleware)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


@app.head("/")
async def root_head():
    """Lightweight HEAD / for Render's edge health probe.

    Render's port-detection probe hits HEAD / repeatedly during the
    "service is live → port detected" handoff. Without this handler,
    FastAPI returns 405 Method Not Allowed and Render's edge can take
    minutes to finalize routing (5min gap observed on 2026-04-28 deploy).
    Mirrors the fix already applied to the MCP service (commit 5772ea3).
    """
    return Response(status_code=200)


@app.get("/")
async def root():
    """Root dispatcher — redirect to /setup on fresh deploys, else health JSON.

    Inspects the ``brilliant_settings.first_run_complete`` latch (migration
    027). When FALSE, this is a fresh deploy that hasn't claimed its admin
    user yet — redirect the browser to ``/setup`` so the Render
    click-deploy flow lands the operator on the setup form automatically.
    When TRUE, the admin has already been claimed and the root returns a
    small health-style JSON body (``/health`` remains the canonical probe
    endpoint for Render; this root stays JSON for backwards-compat).

    Uses a raw pool connection (no RLS context) — the latch is readable by
    every PG role per migration 027.
    """
    pool = get_pool()
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT first_run_complete FROM brilliant_settings WHERE id = 1"
            )
            row = await cur.fetchone()
    except Exception:
        # DB unavailable or migration 027 not applied yet — fall through to
        # the default JSON response so health-style probes don't get 500s.
        row = None

    if row is not None and row[0] is False:
        return RedirectResponse(url="/setup", status_code=307)

    return {"status": "ok", "docs": "/docs"}


# Register route modules — try/except since they may not exist yet
_route_modules = [
    ("routes.entries", "entries", "/entries"),
    ("routes.links", "links", "/entries"),
    ("routes.graph", "router", "/graph"),
    ("routes.index", "index", "/index"),
    ("routes.staging", "staging", "/staging"),
    ("routes.import_files", "router", "/import"),
    ("routes.types", "types", "/types"),
    ("routes.tags", "router", "/tags"),
    ("routes.session", "session", "/session-init"),
    ("routes.invitations", "invitations", "/invitations"),
    ("routes.permissions", "entry_perms_router", "/entries"),
    ("routes.permissions", "path_perms_router", "/paths"),
    ("routes.auth", "router", "/auth"),
    ("routes.users", "members_router", "/org"),
    ("routes.users", "users_router", "/users"),
    ("routes.groups", "groups_router", "/groups"),
    ("routes.comments", "entries_comments_router", "/entries"),
    ("routes.comments", "comments_router", "/comments"),
    ("routes.attachments", "router", "/attachments"),
    ("routes.analytics", "router", "/analytics"),
    # Setup ceremony — empty prefix keeps /setup at the root.
    ("routes.setup", "router", ""),
    # OAuth tx-handoff login page (Sprint 0039). Prefix yields /oauth/login.
    ("routes.oauth", "router", "/oauth"),
]

for module_path, attr_name, prefix in _route_modules:
    try:
        import importlib

        mod = importlib.import_module(module_path)
        router = getattr(mod, attr_name, None) or getattr(mod, "router", None)
        if router:
            app.include_router(router, prefix=prefix)
    except ImportError:
        pass  # Route module not yet implemented
