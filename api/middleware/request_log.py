"""Fire-and-forget request logging middleware.

For every request (except /health and /static/*), records:
    endpoint (path template), method, status, response_bytes, approx_tokens,
    duration_ms, org_id, actor_id

Inserts into request_log via asyncio.create_task so the client response is
never blocked on the DB write. All exceptions in the background task are
swallowed — a logging failure must never surface as a client-visible error.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Route
from starlette.types import ASGIApp

from database import get_pool

logger = logging.getLogger(__name__)

# Safe identifier pattern for SET LOCAL values (matches database._SAFE_VALUE)
_SAFE_VALUE = re.compile(r"^[\w\-\.]+$")

_MAX_ENDPOINT_LEN = 256


def _resolve_endpoint(request: Request) -> str:
    """Return the matched route's path template, or raw path as fallback.

    When FastAPI/Starlette has matched a route, `request.scope["route"]` is
    a `starlette.routing.Route` with `.path` = "/entries/{entry_id}". This
    is preferable to the raw URL so IDs don't explode cardinality.
    """
    route = request.scope.get("route")
    if isinstance(route, Route):
        path = route.path
    else:
        path = request.url.path
    if len(path) > _MAX_ENDPOINT_LEN:
        path = path[:_MAX_ENDPOINT_LEN]
    return path


def _should_skip(path: str) -> bool:
    """Return True if this path should not be logged."""
    if path == "/health":
        return True
    if path.startswith("/static"):
        return True
    return False


async def _log_request(
    *,
    org_id: str | None,
    actor_id: str | None,
    endpoint: str,
    method: str,
    status: int,
    response_bytes: int | None,
    approx_tokens: int | None,
    duration_ms: int,
) -> None:
    """Insert one row into request_log under a kb_admin-scoped connection.

    The request may be unauthenticated (no org_id). The RLS INSERT policy
    on request_log accepts org_id IS NULL or org_id = current_setting('app.org_id').
    When an org_id is present we scope app.org_id so the policy's second
    branch also accepts authenticated inserts.

    All exceptions are swallowed — a logging failure must never surface.
    """
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL ROLE kb_admin")
                if org_id is not None and _SAFE_VALUE.match(str(org_id)):
                    await conn.execute(
                        f"SET LOCAL app.org_id = '{org_id}'"
                    )
                await conn.execute(
                    """
                    INSERT INTO request_log
                        (org_id, actor_id, endpoint, method, status,
                         response_bytes, approx_tokens, duration_ms)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        org_id,
                        actor_id,
                        endpoint,
                        method,
                        status,
                        response_bytes,
                        approx_tokens,
                        duration_ms,
                    ),
                )
    except Exception as exc:  # noqa: BLE001 — intentional catch-all
        logger.warning("request_log insert failed: %s", exc)


class RequestLogMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that logs every request to the request_log table.

    Installs above CORS so that the timing includes the full stack including
    authentication. /health and /static/* are skipped.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Any):
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = int((time.perf_counter() - start) * 1000)

        endpoint = _resolve_endpoint(request)
        if _should_skip(endpoint):
            return response

        # Response size — content-length header if present.
        content_length = response.headers.get("content-length")
        try:
            response_bytes = int(content_length) if content_length else None
        except (TypeError, ValueError):
            response_bytes = None
        approx_tokens = response_bytes // 4 if response_bytes else None

        # Pull auth context stashed by get_current_user. Missing => unauthenticated.
        org_id = getattr(request.state, "user_org_id", None)
        actor_id = getattr(request.state, "user_id", None)

        # Fire-and-forget: do NOT await.
        try:
            asyncio.create_task(
                _log_request(
                    org_id=org_id,
                    actor_id=actor_id,
                    endpoint=endpoint,
                    method=request.method,
                    status=response.status_code,
                    response_bytes=response_bytes,
                    approx_tokens=approx_tokens,
                    duration_ms=duration_ms,
                )
            )
        except Exception as exc:  # noqa: BLE001 — never surface logging failures
            logger.warning("request_log task scheduling failed: %s", exc)

        return response
