"""xiReactor Brilliant API — FastAPI application entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from database import init_pool, close_pool, get_pool
from admin_bootstrap import ensure_admin_user
from middleware.request_log import RequestLogMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage connection pool lifecycle and run startup bootstrap."""
    await init_pool()
    await ensure_admin_user(get_pool())
    yield
    await close_pool()


app = FastAPI(
    title="xiReactor Brilliant API",
    description="Knowledge base API with RLS-enforced permissions and governance pipeline",
    version="0.1.0",
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
