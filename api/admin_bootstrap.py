"""Bootstrap admin user from environment variables at API startup
or from a one-time POST to `/setup`.

Two entry points share one transactional helper:

- `ensure_admin_user(pool)` — startup / env-driven path (install.sh, Docker,
  VPS flows). Reads `ADMIN_EMAIL` / `ADMIN_PASSWORD` / optional `ADMIN_API_KEY`
  from the environment. If required vars are unset, logs a warning and skips
  bootstrap (fail-closed — no default credentials are ever used).

- `create_admin_via_post(pool, email, password)` — POST-driven path used by
  the `/setup` route on Render-style deploys. Does NOT read env vars. Returns
  the plaintext API key so the route can render it once and offer it for
  download. Raises `FirstRunAlreadyClaimed` when the latch is TRUE.

Both paths are guarded by the singleton `brilliant_settings.first_run_complete`
latch (migration 027). Exactly one admin-create-and-flip can succeed; every
subsequent call is a no-op (env path) or raises (POST path).
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets

import bcrypt

logger = logging.getLogger("brilliant.admin_bootstrap")


class FirstRunAlreadyClaimed(Exception):
    """Raised when admin bootstrap is attempted after `first_run_complete=TRUE`.

    The env-driven `ensure_admin_user` path catches this and logs a skip message.
    The POST-driven `create_admin_via_post` path propagates it so `/setup` can
    map it to a 404 response.
    """


def _generate_api_key() -> str:
    """Generate a random API key in the standard bkai_ format."""
    suffix = secrets.token_hex(12)
    prefix = f"bkai_{suffix[:4]}"
    return f"{prefix}_{suffix[4:]}"


async def _create_admin_and_flip_latch(
    pool, email: str, password: str, api_key: str | None = None
) -> tuple[str, str]:
    """Create the admin user + API key and flip the first-run latch atomically.

    Single transaction:
      1. `SELECT first_run_complete FROM brilliant_settings WHERE id = 1 FOR UPDATE`
         — if TRUE, raise `FirstRunAlreadyClaimed`.
      2. `INSERT` the admin user (hardcoded `id='usr_xr_admin'`, `org_id='org_demo'`).
      3. `INSERT` the API key row (bcrypt hash + 9-char prefix).
      4. `UPDATE brilliant_settings SET first_run_complete = TRUE, updated_at = now()`.

    Args:
        pool: An open AsyncConnectionPool.
        email: Admin email (case-insensitive; stored lowercase, SHA-256 hashed).
        password: Admin password (bcrypt hashed before storage).
        api_key: Optional plaintext API key. If None, one is generated here.

    Returns:
        `(api_key_plaintext, user_id)` — caller is responsible for surfacing
        the plaintext key to the operator exactly once.

    Raises:
        FirstRunAlreadyClaimed: the latch is already TRUE.
    """
    if api_key is None:
        api_key = _generate_api_key()

    # Hash password with bcrypt (unchanged from v0.3.1)
    password_hash = bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")

    # Hash email for the email_hash column (SHA-256, unchanged from v0.3.1)
    email_hash = hashlib.sha256(email.lower().encode("utf-8")).hexdigest()

    # Hash the API key with bcrypt for storage (unchanged from v0.3.1)
    api_key_hash = bcrypt.hashpw(
        api_key.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")

    # Key prefix: first 9 chars (e.g. "bkai_abcd") — unchanged from v0.3.1
    key_prefix = api_key[:9]

    user_id = "usr_xr_admin"
    org_id = "org_demo"

    async with pool.connection() as conn:
        async with conn.transaction():
            # Latch check — FOR UPDATE serializes concurrent bootstrap attempts.
            cur = await conn.execute(
                "SELECT first_run_complete FROM brilliant_settings "
                "WHERE id = 1 FOR UPDATE"
            )
            row = await cur.fetchone()

            if row is None:
                # brilliant_settings singleton missing — migration 027 not applied.
                raise RuntimeError(
                    "brilliant_settings singleton row missing; "
                    "ensure migration 027 has been applied"
                )

            if row[0] is True:
                raise FirstRunAlreadyClaimed(
                    "first_run_complete=TRUE; admin bootstrap is closed"
                )

            # Insert admin user (exact same values as v0.3.1 env-driven path).
            await conn.execute(
                """
                INSERT INTO users (
                    id, org_id, display_name, email, email_hash,
                    role, department, trust_weight, password_hash, is_active
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                """,
                (
                    user_id,
                    org_id,
                    "xiReactor Admin",
                    email.lower(),
                    email_hash,
                    "admin",
                    "leadership",
                    0.95,
                    password_hash,
                ),
            )

            # Insert API key row.
            await conn.execute(
                """
                INSERT INTO api_keys (
                    user_id, org_id, key_hash, key_prefix, key_type, label
                )
                VALUES (%s, %s, %s, %s, 'interactive', 'Admin bootstrap key')
                """,
                (user_id, org_id, api_key_hash, key_prefix),
            )

            # Flip the latch — commits with the INSERTs as one atomic unit.
            await conn.execute(
                "UPDATE brilliant_settings "
                "SET first_run_complete = TRUE, updated_at = now() "
                "WHERE id = 1"
            )

    return api_key, user_id


async def ensure_admin_user(pool) -> None:
    """Env-driven bootstrap — called from `main.py` lifespan on every startup.

    No-op unless `ADMIN_EMAIL` + `ADMIN_PASSWORD` are set. Respects the
    `brilliant_settings.first_run_complete` latch: subsequent boots log a
    skip message rather than attempting a second INSERT.

    Args:
        pool: An open AsyncConnectionPool.
    """
    admin_email = os.getenv("ADMIN_EMAIL", "").strip()
    admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
    admin_api_key = os.getenv("ADMIN_API_KEY", "").strip()

    # Fail closed: require both email and password
    if not admin_email or not admin_password:
        logger.warning(
            "ADMIN_EMAIL and/or ADMIN_PASSWORD not set. "
            "Skipping admin bootstrap — no default credentials will be used."
        )
        return

    # Cheap latch pre-check so restart boots don't log anything alarming.
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT first_run_complete FROM brilliant_settings WHERE id = 1"
        )
        row = await cur.fetchone()

    if row is not None and row[0] is True:
        logger.info("Admin user already claimed — skipping bootstrap.")
        return

    key_was_generated = not admin_api_key

    try:
        api_key_plaintext, user_id = await _create_admin_and_flip_latch(
            pool,
            admin_email,
            admin_password,
            api_key=admin_api_key or None,
        )
    except FirstRunAlreadyClaimed:
        # Race: another process / worker flipped the latch between our
        # pre-check and our FOR UPDATE. Treat as successful idempotency.
        logger.info("Admin user already claimed — skipping bootstrap.")
        return

    logger.info("Admin user created: %s (%s)", user_id, admin_email)

    if key_was_generated:
        logger.warning(
            "========================================\n"
            "  AUTO-GENERATED ADMIN API KEY\n"
            "  STORE THIS NOW — it will not be shown again:\n"
            "  %s\n"
            "========================================",
            api_key_plaintext,
        )
    else:
        logger.info("Admin API key stored (provided via ADMIN_API_KEY env var).")


async def create_admin_via_post(
    pool, email: str, password: str
) -> tuple[str, str]:
    """POST-driven bootstrap — called from `/setup` (T-0214) on Render deploys.

    Mints a fresh API key (caller has no opportunity to supply one) and
    returns it in plaintext so the route can render it to the operator
    exactly once.

    Args:
        pool: An open AsyncConnectionPool.
        email: Admin email from the `/setup` form.
        password: Admin password from the `/setup` form.

    Returns:
        `(api_key_plaintext, user_id)`.

    Raises:
        FirstRunAlreadyClaimed: the latch is already TRUE — `/setup` should
            map this to a 404.
    """
    return await _create_admin_and_flip_latch(pool, email, password, api_key=None)
