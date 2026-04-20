#!/usr/bin/env python3
"""Remove demo seed data from a Brilliant Postgres instance.

What this tool does
-------------------
Deletes every row planted by ``db/seed/demo.sql``:

* Entries carrying the ``demo:seed`` tag (FK-cascades cover
  ``entry_links`` + ``entry_versions``).
* Staging / audit / project_assignments / api_keys rows owned by the
  seed users (``usr_admin``, ``usr_editor``, ``usr_commenter``,
  ``usr_viewer``).
* The four demo user rows themselves.

Real (non-demo) entries and the admin-bootstrap'd user are untouched:
the bootstrap path creates a user with a UUID id (``crypto.randbytes``
based) and the real admin's entries do NOT carry the ``demo:seed`` tag.
The ``org_demo`` organization row is intentionally preserved because
admin_bootstrap reuses it for the real admin.

Why the sentinel is a tag, not a column
---------------------------------------
``entries.source`` is CHECK-constrained to ``('web_ui', 'agent', 'api')``
by ``db/migrations/001_core.sql``; we cannot add ``'demo'`` without a
schema change. The ``tags`` column is ``TEXT[]`` with no CHECK, so a
``'demo:seed'`` marker drops in cleanly and is trivially ``ANY()``-able.

Idempotency
-----------
Safe to re-run. A clean DB (no demo rows) exits 0 after reporting
``0`` deletions across every category. Re-running after a partial
failure completes the remaining deletions; tuples already gone produce
``0`` row counts and are not errors.

Usage
-----
    DATABASE_URL=postgresql://... python tools/remove_demo_data.py --yes

    # Interactive (prompts for confirmation):
    DATABASE_URL=postgresql://... python tools/remove_demo_data.py

Exit codes
----------
    0  — completed (possibly a no-op on a clean DB)
    1  — DATABASE_URL missing or connection failed
    2  — aborted by user at interactive confirmation prompt
    3  — SQL error during deletion
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg


# Demo user IDs are stable across seed runs — see db/seed/demo.sql.
# The admin-bootstrap flow (api/admin_bootstrap.py) creates a user with
# a UUID id, so these literal ids never collide with real users.
DEMO_USER_IDS = ("usr_admin", "usr_editor", "usr_commenter", "usr_viewer")

# Tag sentinel on every seeded entry row — see db/seed/demo.sql.
DEMO_ENTRY_TAG = "demo:seed"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove Brilliant demo seed data (idempotent).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Skip the interactive confirmation prompt. Required for "
            "non-interactive cleanups (CI, scripts, one-shot wipes)."
        ),
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Postgres DSN (overrides DATABASE_URL env var).",
    )
    return parser.parse_args(argv)


def _confirm_interactive() -> bool:
    """Prompt on stdin; require an explicit 'yes' to proceed."""
    print(
        "About to delete demo seed rows (entries tagged 'demo:seed' "
        "plus users usr_admin/usr_editor/usr_commenter/usr_viewer and "
        "all rows owned by them). Real entries and the admin-bootstrap "
        "user are unaffected.",
        file=sys.stderr,
    )
    try:
        answer = input("Type 'yes' to continue: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted (no tty).", file=sys.stderr)
        return False
    return answer == "yes"


def _count_demo_rows(cur: psycopg.Cursor) -> dict[str, int]:
    """Snapshot current counts for reporting before + after deletion."""
    counts: dict[str, int] = {}

    cur.execute(
        "SELECT count(*) FROM entries WHERE %s = ANY(tags)",
        (DEMO_ENTRY_TAG,),
    )
    counts["entries"] = cur.fetchone()[0]

    cur.execute(
        "SELECT count(*) FROM users WHERE id = ANY(%s)",
        (list(DEMO_USER_IDS),),
    )
    counts["users"] = cur.fetchone()[0]

    cur.execute(
        "SELECT count(*) FROM api_keys WHERE user_id = ANY(%s)",
        (list(DEMO_USER_IDS),),
    )
    counts["api_keys"] = cur.fetchone()[0]

    cur.execute(
        "SELECT count(*) FROM staging WHERE submitted_by = ANY(%s)",
        (list(DEMO_USER_IDS),),
    )
    counts["staging"] = cur.fetchone()[0]

    cur.execute(
        "SELECT count(*) FROM audit_log WHERE actor_id = ANY(%s)",
        (list(DEMO_USER_IDS),),
    )
    counts["audit_log"] = cur.fetchone()[0]

    cur.execute(
        "SELECT count(*) FROM project_assignments WHERE user_id = ANY(%s)",
        (list(DEMO_USER_IDS),),
    )
    counts["project_assignments"] = cur.fetchone()[0]

    return counts


def _delete_demo_rows(cur: psycopg.Cursor) -> dict[str, int]:
    """Delete demo rows in FK-dependency order. Returns deleted rowcounts."""
    deleted: dict[str, int] = {}

    # 1) Entries (ON DELETE CASCADE covers entry_links + entry_versions).
    cur.execute(
        "DELETE FROM entries WHERE %s = ANY(tags)",
        (DEMO_ENTRY_TAG,),
    )
    deleted["entries"] = cur.rowcount

    # 2) Project assignments referencing demo users (no cascade from users).
    cur.execute(
        "DELETE FROM project_assignments WHERE user_id = ANY(%s) "
        "OR assigned_by = ANY(%s)",
        (list(DEMO_USER_IDS), list(DEMO_USER_IDS)),
    )
    deleted["project_assignments"] = cur.rowcount

    # 3) Staging rows submitted or reviewed by demo users.
    cur.execute(
        "DELETE FROM staging WHERE submitted_by = ANY(%s) "
        "OR reviewed_by = ANY(%s)",
        (list(DEMO_USER_IDS), list(DEMO_USER_IDS)),
    )
    deleted["staging"] = cur.rowcount

    # 4) Audit-log rows actor'd by demo users.
    cur.execute(
        "DELETE FROM audit_log WHERE actor_id = ANY(%s)",
        (list(DEMO_USER_IDS),),
    )
    deleted["audit_log"] = cur.rowcount

    # 5) API keys for demo users.
    cur.execute(
        "DELETE FROM api_keys WHERE user_id = ANY(%s)",
        (list(DEMO_USER_IDS),),
    )
    deleted["api_keys"] = cur.rowcount

    # 6) Demo user rows.
    cur.execute(
        "DELETE FROM users WHERE id = ANY(%s)",
        (list(DEMO_USER_IDS),),
    )
    deleted["users"] = cur.rowcount

    # Intentionally NOT deleting `organizations` row `org_demo` — admin_bootstrap
    # reuses it for the real admin. If no real admin has been bootstrapped and
    # the caller wants a truly clean DB, they can drop+recreate the schema.

    return deleted


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    dsn = args.database_url or os.environ.get("DATABASE_URL")
    if not dsn:
        print(
            "ERROR: DATABASE_URL is not set and --database-url was not passed.",
            file=sys.stderr,
        )
        return 1

    if not args.yes:
        if not _confirm_interactive():
            return 2

    try:
        conn = psycopg.connect(dsn)
    except psycopg.Error as exc:
        print(f"ERROR: could not connect to Postgres: {exc}", file=sys.stderr)
        return 1

    try:
        with conn:
            with conn.cursor() as cur:
                before = _count_demo_rows(cur)
                if sum(before.values()) == 0:
                    print(
                        "No demo rows found — nothing to delete. "
                        "(Idempotent no-op; safe to re-run.)"
                    )
                    return 0

                print("Demo rows found:")
                for table, n in before.items():
                    print(f"  {table:>22}: {n}")

                deleted = _delete_demo_rows(cur)

                print("\nDeleted:")
                for table, n in deleted.items():
                    print(f"  {table:>22}: {n}")
    except psycopg.Error as exc:
        print(f"ERROR: SQL failure during delete: {exc}", file=sys.stderr)
        return 3
    finally:
        conn.close()

    print("\nDone. Demo rows removed. Real entries and the admin-bootstrap "
          "user are unaffected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
