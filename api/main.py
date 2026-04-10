"""xiReactor Cortex API — FastAPI application entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import init_pool, close_pool, get_pool
from admin_bootstrap import ensure_admin_user


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage connection pool lifecycle and run startup bootstrap."""
    await init_pool()
    await ensure_admin_user(get_pool())
    yield
    await close_pool()


app = FastAPI(
    title="xiReactor Cortex API",
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


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


# Register route modules — try/except since they may not exist yet
_route_modules = [
    ("routes.entries", "entries", "/entries"),
    ("routes.links", "links", "/entries"),
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
