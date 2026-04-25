#!/usr/bin/env python3
"""
Backfill entry_links for import batches affected by the Bug A regex gap.

Re-runs sync_entry_links across every entry in one (or all) import_batches
for a given org. Needed for prod recovery of MOC table-escaped wikilinks
(spec 0046, T-0272.4) that were silently unresolved before the regex fix
in T-0272.1 landed.

Usage:
    # Dry-run a specific batch
    python tools/backfill_entry_links.py \\
        --dsn "$DATABASE_URL" \\
        --org-id "$ORG_ID" \\
        --batch-id 3b1de26f-e923-46a7-8d77-2f8007f141b7 \\
        --dry-run

    # Real run, all batches for org
    python tools/backfill_entry_links.py \\
        --dsn "$DATABASE_URL" \\
        --org-id "$ORG_ID"

Prerequisites:
- T-0272.1 regex fix must be deployed (running against old regex reproduces Bug A).
- DB role must be superuser or kb_admin; script SET LOCAL ROLEs kb_admin.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path

import psycopg

# Make `api.services.links` importable when invoked from repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api.services.links import sync_entry_links  # noqa: E402

logger = logging.getLogger("backfill_entry_links")

# Mirror api/database.py's sanitizer so SET LOCAL interpolation is safe.
_SAFE_VALUE = re.compile(r"^[\w\-\.]+$")


def _sanitize(value: str, *, label: str) -> str:
    val = str(value)
    if not _SAFE_VALUE.match(val):
        raise ValueError(f"Unsafe value for {label}: {val!r}")
    return val


def _truncate(s: str, n: int = 80) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 3] + "..."


async def _list_batches(conn, org_id: str, batch_id: str | None) -> list[dict]:
    """Return batch rows for processing, newest first when scanning all."""
    if batch_id:
        sql = (
            "SELECT id::text, created_by, linked_count, source_vault, created_at "
            "FROM import_batches "
            "WHERE org_id = %s AND id = %s::uuid"
        )
        cur = await conn.execute(sql, (org_id, batch_id))
    else:
        sql = (
            "SELECT id::text, created_by, linked_count, source_vault, created_at "
            "FROM import_batches "
            "WHERE org_id = %s "
            "ORDER BY created_at DESC"
        )
        cur = await conn.execute(sql, (org_id,))

    rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "created_by": r[1],
            "linked_count": r[2],
            "source_vault": r[3],
            "created_at": r[4],
        }
        for r in rows
    ]


async def _count_existing_links(conn, batch_id: str) -> int:
    """Count current entry_links rows whose source entry belongs to batch_id.

    We count rows attributable to the batch via the source entry (rather than
    entry_links.import_batch_id directly) so the metric tracks the total
    outgoing edges currently on the batch's entries, which is what
    import_batches.linked_count semantically represents.
    """
    cur = await conn.execute(
        """
        SELECT COUNT(*)
          FROM entry_links el
          JOIN entries e ON e.id = el.source_entry_id
         WHERE e.import_batch_id = %s::uuid
        """,
        (batch_id,),
    )
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _process_batch(
    conn,
    org_id: str,
    batch: dict,
    dry_run: bool,
) -> dict:
    """Process a single batch; returns a stats dict for reporting.

    Uses one transaction for the batch. In dry-run mode the transaction is
    rolled back at the end so no writes persist.
    """
    batch_id = batch["id"]
    created_by = batch["created_by"]

    # Manually manage the transaction so we can ROLLBACK on dry-run.
    await conn.execute("BEGIN")
    try:
        # Session scoping for RLS + role elevation. SET LOCAL dies with the tx.
        await conn.execute(f"SET LOCAL app.org_id = '{_sanitize(org_id, label='org_id')}'")
        await conn.execute(f"SET LOCAL app.user_id = '{_sanitize(created_by, label='created_by')}'")
        await conn.execute("SET LOCAL app.role = 'admin'")
        await conn.execute("SET LOCAL app.department = ''")
        await conn.execute("SET LOCAL ROLE kb_admin")

        before_linked = await _count_existing_links(conn, batch_id)

        # Pull every published entry in this batch.
        cur = await conn.execute(
            """
            SELECT id::text, content, created_by
              FROM entries
             WHERE import_batch_id = %s::uuid
               AND status = 'published'
             ORDER BY created_at
            """,
            (batch_id,),
        )
        entries = await cur.fetchall()

        total_linked = 0
        unresolved_union: list[str] = []
        unresolved_seen: set[str] = set()

        for row in entries:
            entry_id, content, entry_created_by = row[0], row[1], row[2]
            # Prefer the entry's own created_by for audit provenance; fall
            # back to batch.created_by if for some reason it's null.
            user_id = entry_created_by or created_by
            linked, unresolved = await sync_entry_links(
                conn,
                entry_id,
                content or "",
                org_id,
                user_id,
                source="api",
                import_batch_id=batch_id,
            )
            total_linked += linked
            for t in unresolved:
                key = t.lower()
                if key not in unresolved_seen:
                    unresolved_seen.add(key)
                    unresolved_union.append(t)

        after_linked = await _count_existing_links(conn, batch_id)

        # Keep import_batches.linked_count in sync with the new edge total
        # so /import/batches reports the recovered number. Skip in dry-run.
        if not dry_run:
            await conn.execute(
                "UPDATE import_batches SET linked_count = %s WHERE id = %s::uuid",
                (after_linked, batch_id),
            )
            await conn.execute("COMMIT")
        else:
            await conn.execute("ROLLBACK")

        return {
            "batch_id": batch_id,
            "source_vault": batch["source_vault"],
            "entries": len(entries),
            "before_linked": before_linked,
            "after_linked": after_linked,
            "delta": after_linked - before_linked,
            "total_linked_written": total_linked,
            "unresolved_sample": unresolved_union[:5],
            "unresolved_total": len(unresolved_union),
            "dry_run": dry_run,
        }
    except Exception:
        # Make sure we don't leak an open tx on error.
        try:
            await conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        raise


def _print_report(stats: dict) -> None:
    """Print a single-line summary for a batch."""
    sample = " | ".join(_truncate(t) for t in stats["unresolved_sample"])
    mode = "DRY-RUN" if stats["dry_run"] else "APPLIED"
    logger.info(
        "%s batch=%s vault=%s entries=%d before_linked=%d after_linked=%d "
        "delta=%d unresolved_total=%d unresolved_sample=[%s]",
        mode,
        stats["batch_id"],
        stats["source_vault"],
        stats["entries"],
        stats["before_linked"],
        stats["after_linked"],
        stats["delta"],
        stats["unresolved_total"],
        sample,
    )


async def _run(dsn: str, org_id: str, batch_id: str | None, dry_run: bool) -> int:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        # Initialize session-level app.* GUCs so RLS policies resolve during
        # the pre-transaction _list_batches() SELECT. Render Postgres does
        # not have these GUCs pre-registered at the cluster level, so an
        # unset current_setting('app.org_id') raises UndefinedObject.
        safe_org = _sanitize(org_id, label="org_id")
        await conn.execute(f"SET app.org_id = '{safe_org}'")
        await conn.execute("SET app.user_id = ''")
        await conn.execute("SET app.role = 'admin'")
        await conn.execute("SET app.department = ''")

        batches = await _list_batches(conn, org_id, batch_id)
        if not batches:
            if batch_id:
                logger.error(
                    "No import_batches row found for org_id=%s batch_id=%s",
                    org_id,
                    batch_id,
                )
            else:
                logger.error("No import_batches rows found for org_id=%s", org_id)
            return 1

        logger.info(
            "Processing %d batch(es) for org_id=%s (dry_run=%s)",
            len(batches),
            org_id,
            dry_run,
        )

        totals = {"entries": 0, "delta": 0, "unresolved_total": 0}
        for batch in batches:
            stats = await _process_batch(conn, org_id, batch, dry_run)
            _print_report(stats)
            totals["entries"] += stats["entries"]
            totals["delta"] += stats["delta"]
            totals["unresolved_total"] += stats["unresolved_total"]

        logger.info(
            "TOTAL batches=%d entries=%d delta=%d unresolved_total=%d (dry_run=%s)",
            len(batches),
            totals["entries"],
            totals["delta"],
            totals["unresolved_total"],
            dry_run,
        )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill entry_links across import batches (spec 0046, T-0272.4).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Dry-run a single prod batch:
  python tools/backfill_entry_links.py \\
      --dsn "$DATABASE_URL" --org-id "$ORG_ID" \\
      --batch-id 3b1de26f-e923-46a7-8d77-2f8007f141b7 --dry-run

  # Apply to every batch in the org, newest first:
  python tools/backfill_entry_links.py --org-id "$ORG_ID"
""",
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres DSN (default: $DATABASE_URL).",
    )
    parser.add_argument(
        "--org-id",
        required=True,
        help="Tenant org_id to scope the backfill (required even as superuser).",
    )
    parser.add_argument(
        "--batch-id",
        default=None,
        help="Specific import_batches.id (UUID). If omitted, scan all batches "
        "for the org in created_at DESC order.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count would-be-recovered edges without writing; rolls back each "
        "per-batch transaction.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.dsn:
        print(
            "ERROR: --dsn not provided and DATABASE_URL not set. "
            "Pass --dsn or export DATABASE_URL.",
            file=sys.stderr,
        )
        return 1

    try:
        _sanitize(args.org_id, label="org_id")
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        return asyncio.run(_run(args.dsn, args.org_id, args.batch_id, args.dry_run))
    except psycopg.Error as exc:
        logger.error("Database error: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.error("Interrupted")
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhandled error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
