"""Centralized audit_log emission helper (T-0138, spec 0026).

The `audit_log` table is admin-only (RLS POLICY audit_log_insert grants INSERT to
`kb_admin` only — see db/migrations/004_rls.sql:343). API handlers run under the
caller's mapped Postgres role (kb_editor / kb_commenter / kb_viewer / kb_agent),
so they cannot directly INSERT into audit_log.

This helper temporarily elevates to `kb_admin` for the INSERT inside a SAVEPOINT,
then restores the caller's role. Using `SET LOCAL` inside the savepoint means:

  - the role change is rolled back if the savepoint rolls back, and
  - pool reuse stays safe because `SET LOCAL` is scoped to the current transaction
    (see lesson `feedback_set_local_role.md` — never use plain `SET ROLE`).

Failure policy
--------------
Audit writes MUST NOT abort the parent transaction. We wrap the INSERT in a
savepoint and swallow exceptions with a logged warning. The parent business
write (comment, permission grant, group mutation) is the source of truth; a
missing audit row degrades observability but is preferable to losing the
business write itself.
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any

import psycopg

logger = logging.getLogger(__name__)


# Whitelisted action verbs. Keeps callers honest and prevents typo drift.
VALID_ACTIONS = {
    # comments
    "comment_create",
    "comment_resolve",
    "comment_dismiss",
    "comment_escalate",
    # permissions
    "grant",
    "revoke",
    # groups
    "group_create",
    "group_delete",
    "group_member_add",
    "group_member_remove",
    # imports
    "import_rollback",
}


async def record(
    conn: psycopg.AsyncConnection,
    *,
    actor_id: str,
    actor_role: str,
    source: str,
    org_id: str,
    action: str,
    target_table: str,
    target_id: str | None = None,
    target_path: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit a single audit_log row.

    Runs inside a SAVEPOINT under the `kb_admin` role so:
      * the audit INSERT is permitted by RLS even when the calling handler is
        running as a non-admin role (kb_editor, kb_commenter, etc.), and
      * any failure (e.g. RLS edge case, transient error) does NOT abort the
        outer transaction — we rollback the savepoint and log a warning.

    `metadata` is folded into `change_summary` as a JSON blob — the audit_log
    schema (003_governance.sql) does not have a dedicated metadata column, so
    we serialize structured context into the existing TEXT column.
    """
    if action not in VALID_ACTIONS:
        # Programming error — fail loud in dev/CI but don't break prod requests.
        logger.warning("audit.record called with unknown action %r", action)

    # Build change_summary — concatenate any caller-supplied metadata as JSON.
    if metadata:
        try:
            summary = json.dumps(metadata, default=str, sort_keys=True)
        except (TypeError, ValueError):
            summary = repr(metadata)
    else:
        summary = None

    # Unique savepoint name so nested calls don't collide.
    sp_name = f"audit_{secrets.token_hex(6)}"

    try:
        await conn.execute(f"SAVEPOINT {sp_name}")
        try:
            # Elevate to admin for the INSERT. SET LOCAL inside the savepoint
            # is rolled back if we ROLLBACK TO; on RELEASE the SET LOCAL
            # persists inside the outer transaction, so we must restore the
            # caller's role explicitly before RELEASE.
            await conn.execute("SET LOCAL ROLE kb_admin")
            await conn.execute(
                """
                INSERT INTO audit_log (
                    org_id, actor_id, actor_role, source,
                    action, target_table, target_id, target_path,
                    change_summary
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s
                )
                """,
                (
                    org_id,
                    actor_id,
                    actor_role,
                    source,
                    action,
                    target_table,
                    target_id,
                    target_path,
                    summary,
                ),
            )
        finally:
            # Restore caller's role before releasing the savepoint so the outer
            # transaction continues under the original role. Map app role ->
            # pg role identically to database.set_session_context().
            pg_role = _app_role_to_pg_role(actor_role, source)
            try:
                await conn.execute(f"SET LOCAL ROLE {pg_role}")
            except Exception:  # pragma: no cover — defensive
                logger.exception("audit.record: failed to restore role to %s", pg_role)

        await conn.execute(f"RELEASE SAVEPOINT {sp_name}")
    except Exception:
        # Roll back just the savepoint and continue. The parent transaction
        # remains valid and the business write is preserved.
        logger.exception(
            "audit.record failed for action=%s target=%s/%s — continuing",
            action,
            target_table,
            target_id,
        )
        try:
            await conn.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
            await conn.execute(f"RELEASE SAVEPOINT {sp_name}")
        except Exception:  # pragma: no cover — defensive
            logger.exception("audit.record: failed to rollback savepoint %s", sp_name)


def _app_role_to_pg_role(role: str, source: str) -> str:
    """Mirror of database.ROLE_MAP + the agent-source override.

    Kept local to avoid an import cycle with api.database.
    """
    if source == "agent":
        return "kb_agent"
    return {
        "admin": "kb_admin",
        "editor": "kb_editor",
        "commenter": "kb_commenter",
        "viewer": "kb_viewer",
    }.get(role, "kb_viewer")


async def record_for_user(
    conn: psycopg.AsyncConnection,
    user: Any,
    *,
    action: str,
    target_table: str,
    target_id: str | None = None,
    target_path: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Convenience wrapper that pulls actor_id/role/source/org_id from a UserContext."""
    await record(
        conn,
        actor_id=user.id,
        actor_role=user.role,
        source=user.source,
        org_id=user.org_id,
        action=action,
        target_table=target_table,
        target_id=target_id,
        target_path=target_path,
        metadata=metadata,
    )
