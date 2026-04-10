"""Async Postgres connection pool and RLS session context helpers."""

import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncGenerator

import psycopg
from psycopg_pool import AsyncConnectionPool

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:dev@localhost:5432/cortex",
)

_pool: AsyncConnectionPool | None = None

# Role mapping: app role -> Postgres role
ROLE_MAP = {
    "admin": "kb_admin",
    "editor": "kb_editor",
    "commenter": "kb_commenter",
    "viewer": "kb_viewer",
}

# Validation pattern: only allow alphanumeric, underscore, hyphen, period
_SAFE_VALUE = re.compile(r"^[\w\-\.]+$")


def _sanitize(value: str) -> str:
    """Ensure a value is safe for use in SET LOCAL statements."""
    val = str(value)
    if not _SAFE_VALUE.match(val):
        raise ValueError(f"Unsafe session variable value: {val!r}")
    return val


async def init_pool() -> AsyncConnectionPool:
    """Create and open the global connection pool."""
    global _pool
    _pool = AsyncConnectionPool(
        conninfo=DATABASE_URL,
        min_size=2,
        max_size=10,
        open=False,
    )
    await _pool.open()
    return _pool


async def close_pool() -> None:
    """Close the global connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> AsyncConnectionPool:
    """Return the global connection pool. Raises if not initialized."""
    if _pool is None:
        raise RuntimeError("Connection pool not initialized. Call init_pool() first.")
    return _pool


async def set_session_context(conn: psycopg.AsyncConnection, user: Any) -> None:
    """Set Postgres session variables for RLS and switch to the appropriate role.

    Args:
        conn: An async psycopg connection (must be inside a transaction).
        user: An object with id, org_id, role, department, and source attributes.
    """
    user_id = _sanitize(user.id)
    org_id = _sanitize(user.org_id)
    role = _sanitize(user.role)
    department = _sanitize(user.department) if user.department else ""

    # Determine the Postgres role to assume
    if user.source == "agent":
        pg_role = "kb_agent"
    else:
        pg_role = ROLE_MAP.get(role)
        if pg_role is None:
            raise ValueError(f"Unknown role: {role!r}")

    # SET LOCAL scopes these to the current transaction
    await conn.execute(f"SET LOCAL app.user_id = '{user_id}'")
    await conn.execute(f"SET LOCAL app.org_id = '{org_id}'")
    await conn.execute(f"SET LOCAL app.role = '{role}'")
    await conn.execute(f"SET LOCAL app.department = '{department}'")
    await conn.execute(f"SET LOCAL ROLE {pg_role}")


@asynccontextmanager
async def get_db(user: Any) -> AsyncGenerator[psycopg.AsyncConnection, None]:
    """Get a connection from the pool with RLS session context set.

    Usage:
        async with get_db(current_user) as conn:
            result = await conn.execute("SELECT ...")
    """
    pool = get_pool()
    async with pool.connection() as conn:
        # autocommit=False is the default; SET LOCAL requires a transaction
        async with conn.transaction():
            await set_session_context(conn, user)
            yield conn
        # Transaction ends here; role and session vars are automatically reset
