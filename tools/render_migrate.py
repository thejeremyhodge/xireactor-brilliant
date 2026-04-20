#!/usr/bin/env python3
"""Render-first idempotent migrations runner.

What this tool does
-------------------
Applies every `db/migrations/*.sql` file (in filename order) that has not yet
been recorded in the `schema_migrations` tracking table. Each migration runs
inside its own transaction; on failure the exception bubbles and the tool
exits non-zero.

Why it exists
-------------
Render (and any other "attach to a fresh Postgres" deploy target) cannot use
the `/docker-entrypoint-initdb.d` bootstrap path that local `docker compose`
relies on — Render's managed Postgres has no init-hooks. This script runs as
Render's `preDeployCommand` and guarantees the schema is up to date on every
deploy.

Local dev is unaffected: `docker compose up` continues to mount migrations
into `/docker-entrypoint-initdb.d` and this tool never needs to run there.

Idempotency
-----------
- Safe to re-run. On a freshly-migrated DB the second invocation is a no-op
  and prints "Migrations up to date".
- On a DB that was previously bootstrapped via initdb (schema present but no
  `schema_migrations` row tracking it), the tool detects that state via a
  `to_regclass('public.users')` heuristic and pre-populates the tracking
  table with every migration whose number is <= 026 — treating them as
  already applied. Only 027+ (the migrations added after the initdb
  snapshot) are then run against the existing schema.
- On a truly fresh Render Postgres, `to_regclass('public.users')` returns
  NULL, the heuristic falls through, and every migration 001..NNN applies
  in order.

Usage
-----
    DATABASE_URL=postgresql://... python tools/render_migrate.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import psycopg


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"

# Migrations whose number is <= this value are assumed to be part of the
# initdb snapshot when we detect a pre-bootstrapped DB (public.users exists
# but schema_migrations is empty). Keep this bumped to the highest-numbered
# migration that was part of the baseline before render_migrate.py started
# tracking. The first "new" migration this tool is expected to apply on a
# bootstrapped DB is 027.
INITDB_SNAPSHOT_MAX_NUMBER = 26

# Migrations that exist for local demo/dogfood only and must NOT run on a
# production Render deploy. Retained for historical Render DBs where
# 005_seed.sql was previously skipped and recorded in schema_migrations:
# the file has since moved to db/seed/demo.sql (opt-in via
# `install.sh --seed-demo`) but we keep the name in SKIP_ON_RENDER so a
# future cross-reference to the schema_migrations row still resolves.
# Render v0.5.1+ installs will simply never see the file in MIGRATIONS_DIR.
#
# Historical reason (still true for any DB pre-v0.5.1):
#   - 005_seed.sql — inserts demo users with publicly-known plaintext API
#     keys and demo entries. Would be a security hole on a real deploy,
#     and also fails under FORCE ROW LEVEL SECURITY because Render's
#     auto-generated DB user is not a superuser.
# Listed files are still recorded in schema_migrations so the tool treats
# them as "applied" and doesn't retry them every deploy.
SKIP_ON_RENDER = {"005_seed.sql"}

_NUM_RE = re.compile(r"^(\d+)_")


def _migration_number(filename: str) -> int:
    """Extract the leading integer prefix from a migration filename.

    Returns -1 if the filename does not start with `\\d+_`.
    """
    m = _NUM_RE.match(filename)
    return int(m.group(1)) if m else -1


def main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print(
            "ERROR: DATABASE_URL is not set. "
            "Set it to the target Postgres DSN and re-run.",
            file=sys.stderr,
        )
        return 1

    if not MIGRATIONS_DIR.is_dir():
        print(
            f"ERROR: migrations directory not found at {MIGRATIONS_DIR}",
            file=sys.stderr,
        )
        return 1

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migration_files:
        print(f"ERROR: no *.sql files found in {MIGRATIONS_DIR}", file=sys.stderr)
        return 1

    # Phase 1: ensure tracking table exists (autocommit, so the DDL commits
    # even if the per-migration loop aborts).
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    filename   TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )

            # Detect pre-bootstrapped DB (initdb path): public.users exists
            # but schema_migrations is empty. If so, record every migration
            # <= INITDB_SNAPSHOT_MAX_NUMBER as already-applied WITHOUT
            # re-running its SQL. Only newer migrations get executed below.
            cur.execute("SELECT COUNT(*) FROM schema_migrations")
            tracking_count = cur.fetchone()[0]

            if tracking_count == 0:
                cur.execute("SELECT to_regclass('public.users')")
                users_oid = cur.fetchone()[0]
                if users_oid is not None:
                    print(
                        "Detected pre-bootstrapped DB (public.users exists, "
                        "schema_migrations empty). Marking migrations "
                        f"<= {INITDB_SNAPSHOT_MAX_NUMBER:03d} as already applied.",
                    )
                    prepop = [
                        p.name
                        for p in migration_files
                        if 0 <= _migration_number(p.name) <= INITDB_SNAPSHOT_MAX_NUMBER
                    ]
                    for name in prepop:
                        cur.execute(
                            "INSERT INTO schema_migrations (filename) "
                            "VALUES (%s) ON CONFLICT DO NOTHING",
                            (name,),
                        )
                    print(f"Pre-populated {len(prepop)} snapshot row(s).")

            cur.execute("SELECT filename FROM schema_migrations")
            applied = {row[0] for row in cur.fetchall()}

    # Phase 2: apply any file not yet recorded. One transaction per file so a
    # failure rolls back cleanly and leaves the tracking table consistent.
    applied_this_run = 0
    skipped = 0
    with psycopg.connect(dsn) as conn:
        for path in migration_files:
            name = path.name
            if name in applied:
                skipped += 1
                continue

            # Record-but-don't-run for demo-only migrations (see SKIP_ON_RENDER).
            if name in SKIP_ON_RENDER:
                try:
                    with conn.transaction():
                        with conn.cursor() as cur:
                            cur.execute(
                                "INSERT INTO schema_migrations (filename) "
                                "VALUES (%s) ON CONFLICT DO NOTHING",
                                (name,),
                            )
                except Exception as exc:  # noqa: BLE001
                    print(f"FAILED recording skip for {name}: {exc}", file=sys.stderr)
                    return 2
                print(f"Skipped {name} (demo-only, not applied on Render)")
                applied_this_run += 1
                continue

            sql = path.read_text(encoding="utf-8")
            try:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(sql)
                        cur.execute(
                            "INSERT INTO schema_migrations (filename) VALUES (%s)",
                            (name,),
                        )
            except Exception as exc:  # noqa: BLE001 — surface then exit
                print(f"FAILED applying {name}: {exc}", file=sys.stderr)
                return 2

            print(f"Applied {name}")
            applied_this_run += 1

    total = len(migration_files)
    if applied_this_run == 0:
        print(
            f"Migrations up to date ({applied_this_run} applied, "
            f"{skipped} skipped, {total} total)."
        )
    else:
        print(
            f"Migrations: {applied_this_run} applied, "
            f"{skipped} skipped, {total} total."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
