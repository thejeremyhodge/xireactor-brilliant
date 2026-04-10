"""Bootstrap admin user from environment variables at API startup.

Reads ADMIN_EMAIL and ADMIN_PASSWORD (required), plus optional ADMIN_API_KEY.
If ADMIN_API_KEY is not set, a key is auto-generated and printed once to logs.

Idempotent: skips creation if the admin user already exists (ON CONFLICT DO NOTHING).
Fail-closed: if required env vars are unset, logs a warning and skips bootstrap
(no default credentials are ever used).
"""

import hashlib
import logging
import os
import secrets

import bcrypt

logger = logging.getLogger("cortex.admin_bootstrap")


def _generate_api_key() -> str:
    """Generate a random API key in the standard bkai_ format."""
    suffix = secrets.token_hex(12)
    prefix = f"bkai_{suffix[:4]}"
    return f"{prefix}_{suffix[4:]}"


async def ensure_admin_user(pool) -> None:
    """Create the admin user and API key if they do not already exist.

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

    # Auto-generate API key if not provided
    key_was_generated = False
    if not admin_api_key:
        admin_api_key = _generate_api_key()
        key_was_generated = True

    # Hash password with bcrypt
    password_hash = bcrypt.hashpw(
        admin_password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")

    # Hash email for the email_hash column (SHA-256)
    email_hash = hashlib.sha256(admin_email.lower().encode("utf-8")).hexdigest()

    # Hash the API key with bcrypt for storage
    api_key_hash = bcrypt.hashpw(
        admin_api_key.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")

    # Key prefix: first 9 chars (e.g. "bkai_abcd")
    key_prefix = admin_api_key[:9]

    user_id = "usr_xr_admin"
    org_id = "org_demo"

    async with pool.connection() as conn:
        async with conn.transaction():
            # Upsert admin user — do nothing if already exists
            result = await conn.execute(
                """
                INSERT INTO users (
                    id, org_id, display_name, email, email_hash,
                    role, department, trust_weight, password_hash, is_active
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    user_id,
                    org_id,
                    "xiReactor Admin",
                    admin_email.lower(),
                    email_hash,
                    "admin",
                    "leadership",
                    0.95,
                    password_hash,
                ),
            )

            user_created = result.rowcount > 0

            if user_created:
                # Insert API key only when the user was just created
                await conn.execute(
                    """
                    INSERT INTO api_keys (
                        user_id, org_id, key_hash, key_prefix, key_type, label
                    )
                    VALUES (%s, %s, %s, %s, 'interactive', 'Admin bootstrap key')
                    """,
                    (user_id, org_id, api_key_hash, key_prefix),
                )

                logger.info("Admin user created: %s (%s)", user_id, admin_email)

                if key_was_generated:
                    logger.warning(
                        "========================================\n"
                        "  AUTO-GENERATED ADMIN API KEY\n"
                        "  STORE THIS NOW — it will not be shown again:\n"
                        "  %s\n"
                        "========================================",
                        admin_api_key,
                    )
                else:
                    logger.info("Admin API key stored (provided via ADMIN_API_KEY env var).")
            else:
                logger.info("Admin user %s already exists — skipping bootstrap.", user_id)
