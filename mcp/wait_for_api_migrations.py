"""Block the MCP's deploy until the API has finished migrating the DB.

Render Blueprint runs each service's deploy lane independently — there is
no native cross-service `depends_on`. Without coordination the MCP can
boot before brilliant-api's `preDeployCommand: python tools/render_migrate.py`
has finished, which produces `role "kb_admin" does not exist` from
`_publish_public_url_to_db()` at module-import time.

This script is run as the MCP's `preDeployCommand`. It polls the shared DB
for the `kb_admin` role, which 004_rls.sql creates. render_migrate.py
applies migrations in numeric order with one transaction per file, so once
kb_admin exists, 004 has committed and every later migration has either
committed or is mid-flight under its own tx — safe to proceed.

Exit codes:
    0  — kb_admin role visible; safe to deploy MCP
    1  — timed out (deploy is blocked)
"""

from __future__ import annotations

import os
import sys
import time

import psycopg


TIMEOUT_SECONDS = 240
POLL_INTERVAL_SECONDS = 3
CONNECT_TIMEOUT_SECONDS = 5


def main() -> int:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        # Compose / local dev paths run this script too only if explicitly
        # wired in. Treat absence as "not on Render, skip the gate."
        print("DATABASE_URL not set — skipping wait", flush=True)
        return 0

    deadline = time.time() + TIMEOUT_SECONDS
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            with psycopg.connect(dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM pg_roles WHERE rolname = 'kb_admin'"
                    )
                    if cur.fetchone():
                        print(
                            f"API migrations ready (kb_admin present, "
                            f"attempt={attempt})",
                            flush=True,
                        )
                        return 0
            print(
                f"Waiting for API migrations… (attempt={attempt}, "
                f"kb_admin not yet created)",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 — DB may be reachable mid-deploy
            print(
                f"Waiting for DB / migrations (attempt={attempt}): {exc}",
                flush=True,
            )
        time.sleep(POLL_INTERVAL_SECONDS)

    print(
        f"Timed out after {TIMEOUT_SECONDS}s waiting for kb_admin role; "
        f"check brilliant-api preDeployCommand logs.",
        file=sys.stderr,
        flush=True,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
