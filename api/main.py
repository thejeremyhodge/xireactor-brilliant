"""xiReactor Brilliant API — FastAPI application entrypoint."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg import errors as pg_errors

from database import init_pool, close_pool, get_pool
from admin_bootstrap import ensure_admin_user
from middleware.request_log import RequestLogMiddleware

logger = logging.getLogger(__name__)


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


@app.exception_handler(pg_errors.InsufficientPrivilege)
async def _rls_insufficient_privilege_handler(
    request: Request, exc: pg_errors.InsufficientPrivilege
) -> JSONResponse:
    # RLS denied the operation. Surface as 403 instead of leaking 500.
    # Log server-side so a Postgres-role misconfig (the rare non-authz cause)
    # remains visible in the logs even though the wire response stays 403.
    logger.info(
        "RLS denied %s %s for client; returning 403", request.method, request.url.path
    )
    return JSONResponse(
        status_code=403,
        content={"detail": "Your role does not permit this operation."},
    )


@app.exception_handler(pg_errors.CheckViolation)
async def _rls_check_violation_handler(
    request: Request, exc: pg_errors.CheckViolation
) -> JSONResponse:
    # RLS WITH CHECK predicate failed (typically writing past a sensitivity
    # ceiling or to a resource the row-level policy disallows).
    logger.info(
        "RLS WITH CHECK denied %s %s; returning 403",
        request.method,
        request.url.path,
    )
    return JSONResponse(
        status_code=403,
        content={"detail": "This operation is not permitted by policy."},
    )


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


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
    ("routes.analytics", "router", "/analytics"),
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
