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

Sprint 0039 extension — same transaction additionally mints:

  * one OAuth client row (``oauth_clients``): ``client_id`` + ``client_secret``
    pre-registered for Claude Co-work's known redirect URI. Displayed on
    ``/setup/done`` and ``/auth/login`` so the operator can paste all four
    fields (URL + client_id + client_secret + workspace name) into Claude's
    custom-connector wizard. Replaces the pre-0039 DCR auto-mint.
  * one service-role API key (``api_keys`` row, ``key_type='service'``) used
    by the MCP server as its outbound Authorization: Bearer. Every MCP tool
    call sends this key plus an ``X-Act-As-User: <user_id>`` header; the API
    auth middleware (``api/auth.py``) only honors the act-as header when the
    presenting key is a service key. See spec 0039.

All five writes (user, interactive key, service key, OAuth client, latch flip)
share one transaction — any failure rolls the whole ceremony back.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time

import bcrypt

logger = logging.getLogger("brilliant.admin_bootstrap")

# Claude Co-work's fixed OAuth redirect URI. Pre-registered here so the
# custom-connector flow can validate it against `oauth_clients.client_info`
# without a DCR hop. If Anthropic ever changes this, the operator rotates
# the stored client_info via `UPDATE oauth_clients`.
_CLAUDE_COWORK_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"


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


DEFAULT_ORG_NAME = "My Workspace"


def _build_cowork_client_info_json(
    client_id: str,
    client_secret: str,
    issued_at: int,
) -> str:
    """Build the JSON payload stored in ``oauth_clients.client_info``.

    The MCP's :class:`PgOAuthStore.get_client` feeds this JSON into
    ``OAuthClientInformationFull.model_validate_json`` (see
    ``mcp/oauth_store.py``). The shape must therefore match that pydantic
    model — at minimum ``redirect_uris`` (non-empty list), the grant/response
    defaults, plus the four ``client_id``/``client_secret``/``issued_at``
    fields. We keep the API service free of an MCP-SDK dependency by
    hand-rolling the dict — the schema is stable in MCP SDK ≥1.0.
    """
    return json.dumps(
        {
            "redirect_uris": [_CLAUDE_COWORK_REDIRECT_URI],
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": "Brilliant workspace (pre-registered)",
            "client_id": client_id,
            "client_secret": client_secret,
            "client_id_issued_at": issued_at,
        }
    )


async def _create_admin_and_flip_latch(
    pool,
    email: str,
    password: str,
    api_key: str | None = None,
    org_name: str = DEFAULT_ORG_NAME,
) -> tuple[str, str, str, str, str]:
    """Create the admin user + API key + OAuth client + service key, and flip
    the first-run latch — all in one transaction.

    Single transaction:
      1. ``SELECT first_run_complete FROM brilliant_settings WHERE id = 1 FOR UPDATE``
         — if TRUE, raise :class:`FirstRunAlreadyClaimed`.
      2. ``INSERT`` the ``org_demo`` organization row (``ON CONFLICT DO UPDATE``
         — overwrites the legacy "Demo Organization" label with the
         operator-supplied name so production installs don't display
         "Demo Organization" in the UI).
      3. ``INSERT`` the admin user (hardcoded ``id='usr_xr_admin'``,
         ``org_id='org_demo'``).
      4. ``INSERT`` the interactive API key row (bcrypt hash + 9-char prefix).
      5. ``INSERT`` the service API key row (``key_type='service'``) owned
         by the same admin user. The MCP reads this as its outbound
         ``Authorization: Bearer`` and relies on ``X-Act-As-User`` for
         per-user RLS context.
      6. ``INSERT`` the pre-registered OAuth client (``oauth_clients``) —
         ``client_id`` + ``client_secret`` that Claude Co-work's custom
         connector wizard consumes. Pre-registration closes the DCR hole.
      7. ``UPDATE brilliant_settings SET first_run_complete = TRUE``.

    Args:
        pool: An open AsyncConnectionPool.
        email: Admin email (case-insensitive; stored lowercase, SHA-256 hashed).
        password: Admin password (bcrypt hashed before storage).
        api_key: Optional plaintext admin API key. If None, one is generated.
        org_name: Display name for the organization (user-facing). Defaults
            to ``"My Workspace"`` for the env-driven path when
            ``ADMIN_ORG_NAME`` is not set.

    Returns:
        ``(admin_api_key, service_api_key, client_id, client_secret, user_id)``
        — caller is responsible for surfacing the plaintext admin key,
        service key, and client_secret to the operator exactly once. The
        env-driven path only needs to log the admin key (it reads the
        service key + client creds back from Render's env; they're in the
        DB for recovery via ``/auth/login``).

    Raises:
        FirstRunAlreadyClaimed: the latch is already TRUE.
    """
    if api_key is None:
        api_key = _generate_api_key()

    # Service-role API key — the MCP service's outbound Authorization.
    # Generated fresh on every bootstrap so it has no relationship to the
    # admin's interactive key.
    service_api_key = _generate_api_key()

    # Pre-registered OAuth client credentials — Claude Co-work pastes these
    # into its custom-connector wizard.
    client_id = f"brilliant_{secrets.token_hex(16)}"
    client_secret = secrets.token_hex(32)
    client_id_issued_at = int(time.time())
    client_info_json = _build_cowork_client_info_json(
        client_id, client_secret, client_id_issued_at
    )

    # Hash password with bcrypt (unchanged from v0.3.1)
    password_hash = bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")

    # Hash email for the email_hash column (SHA-256, unchanged from v0.3.1)
    email_hash = hashlib.sha256(email.lower().encode("utf-8")).hexdigest()

    # Hash the admin API key with bcrypt for storage (unchanged from v0.3.1)
    api_key_hash = bcrypt.hashpw(
        api_key.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")
    # Key prefix: first 9 chars (e.g. "bkai_abcd") — unchanged from v0.3.1
    key_prefix = api_key[:9]

    # Hash the service API key the same way.
    service_key_hash = bcrypt.hashpw(
        service_api_key.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")
    service_key_prefix = service_api_key[:9]

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

            # Ensure the `org_demo` organization exists with the
            # operator-supplied display name before the admin user
            # INSERT below (users.org_id FK → organizations.id).
            # Locally, 005_seed.sql pre-creates the row with the name
            # "Demo Organization"; we overwrite it here with the
            # user-chosen name so production installs don't display the
            # legacy seed label. On Render (where 005_seed is skipped)
            # this is the sole creator of the org row.
            await conn.execute(
                """
                INSERT INTO organizations (id, name, settings)
                VALUES (
                    'org_demo',
                    %s,
                    '{"governance_tier_default": 2, "max_entries": 10000}'
                )
                ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name
                """,
                (org_name,),
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

            # Insert interactive admin API key row.
            await conn.execute(
                """
                INSERT INTO api_keys (
                    user_id, org_id, key_hash, key_prefix, key_type, label
                )
                VALUES (%s, %s, %s, %s, 'interactive', 'Admin bootstrap key')
                """,
                (user_id, org_id, api_key_hash, key_prefix),
            )

            # Insert service-role API key row. `key_type='service'` is
            # allowed by migration 031 (T-0224, shipping in the same
            # sprint). The MCP reads its plaintext from
            # `BRILLIANT_SERVICE_API_KEY` in Render env — it's logged once
            # here so the operator can paste it into Render if needed.
            await conn.execute(
                """
                INSERT INTO api_keys (
                    user_id, org_id, key_hash, key_prefix, key_type, label
                )
                VALUES (%s, %s, %s, %s, 'service', 'MCP service bootstrap key')
                """,
                (user_id, org_id, service_key_hash, service_key_prefix),
            )

            # Insert the pre-registered OAuth client. The JSON shape mirrors
            # `PgOAuthStore.save_client` (mcp/oauth_store.py) so the MCP
            # can `get_client()` it without a DCR round-trip.
            await conn.execute(
                """
                INSERT INTO oauth_clients (
                    client_id, client_secret, client_id_issued_at, client_info
                )
                VALUES (%s, %s, %s, %s::jsonb)
                """,
                (
                    client_id,
                    client_secret,
                    client_id_issued_at,
                    client_info_json,
                ),
            )

            # Flip the latch — commits with the INSERTs as one atomic unit.
            await conn.execute(
                "UPDATE brilliant_settings "
                "SET first_run_complete = TRUE, updated_at = now() "
                "WHERE id = 1"
            )

    return api_key, service_api_key, client_id, client_secret, user_id


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
    admin_org_name = os.getenv("ADMIN_ORG_NAME", "").strip() or DEFAULT_ORG_NAME

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
        (
            api_key_plaintext,
            service_api_key,
            client_id,
            client_secret,
            user_id,
        ) = await _create_admin_and_flip_latch(
            pool,
            admin_email,
            admin_password,
            api_key=admin_api_key or None,
            org_name=admin_org_name,
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

    # Sprint 0039 — also surface the service key + OAuth client creds once.
    # On the Render path these are read back from env / DB; on the local
    # env-driven path (install.sh, docker-compose) the operator captures
    # them from these log lines if they want to wire a local MCP.
    logger.warning(
        "========================================\n"
        "  MCP SERVICE API KEY (key_type=service)\n"
        "  Set BRILLIANT_SERVICE_API_KEY on the MCP service:\n"
        "  %s\n"
        "========================================",
        service_api_key,
    )
    logger.warning(
        "========================================\n"
        "  OAUTH CLIENT (pre-registered for Claude Co-work)\n"
        "  client_id:     %s\n"
        "  client_secret: %s\n"
        "========================================",
        client_id,
        client_secret,
    )


async def create_admin_via_post(
    pool, email: str, password: str, org_name: str = DEFAULT_ORG_NAME
) -> tuple[str, str, str, str, str]:
    """POST-driven bootstrap — called from `/setup` (T-0214) on Render deploys.

    Mints a fresh admin API key, a service API key, and a pre-registered
    OAuth client in the same transaction. The caller renders all of these
    to the operator exactly once on ``/setup/done``.

    Args:
        pool: An open AsyncConnectionPool.
        email: Admin email from the `/setup` form.
        password: Admin password from the `/setup` form.
        org_name: Organization display name from the `/setup` form
            (user-facing label shown throughout the UI).

    Returns:
        ``(admin_api_key, service_api_key, client_id, client_secret, user_id)``
        — plaintext credentials; the caller is responsible for displaying
        them to the operator exactly once (``/setup`` POST response body).

    Raises:
        FirstRunAlreadyClaimed: the latch is already TRUE — `/setup` should
            map this to a 404.
    """
    return await _create_admin_and_flip_latch(
        pool, email, password, api_key=None, org_name=org_name
    )
