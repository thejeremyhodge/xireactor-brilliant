"""Staging / governance pipeline endpoints."""

import hashlib
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg.rows import dict_row

from auth import UserContext, get_current_user
from database import get_db
from models import (
    AIReviewResult,
    ProcessResult,
    ReviewAction,
    StagingList,
    StagingResponse,
    StagingSubmit,
)
from services.access_log import log_entry_reads
from services.ai_reviewer import review_staging_item

router = APIRouter(tags=["staging"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assign_governance_tier(
    change_type: str,
    sensitivity: str | None,
    source: str,
    role: str,
) -> int:
    """Determine governance tier for a staging submission.

    Four-tier escalation model:
      Tier 1 — Auto-approve: creates (non-sensitive), appends, links,
               agent updates (non-sensitive), admin/editor web_ui writes.
               No checks needed.
      Tier 2 — Auto-approve with conflict detection: updates and modifications
               on non-sensitive content. Inline checks; clean = auto-approve,
               conflicts escalate to Tier 3.
      Tier 3 — Batch/AI review: high-sensitivity content (system, strategic)
               and Tier 2 escalations.
      Tier 4 — Human-only: deletions, sensitivity changes, governance mods.
    """
    # Tier 4: destructive operations always require human review
    if change_type == "delete":
        return 4

    # Tier 3: high-sensitivity content requires AI/batch review
    if sensitivity in ("system", "strategic"):
        return 3

    # Tier 1: additive operations on non-sensitive content
    if source == "web_ui" and role in ("admin", "editor"):
        return 1
    if change_type == "create" and sensitivity in (None, "shared"):
        return 1
    if change_type == "append":
        return 1
    if change_type == "create_link":
        return 1
    if change_type == "update" and sensitivity in (None, "shared") and source == "agent":
        return 1

    # Tier 2: everything else (updates, modifications on non-sensitive content)
    return 2


def _row_to_response(row: dict) -> StagingResponse:
    """Convert a database row dict to a StagingResponse."""
    return StagingResponse(
        id=str(row["id"]),
        org_id=str(row["org_id"]),
        target_entry_id=str(row["target_entry_id"]) if row.get("target_entry_id") else None,
        target_path=row["target_path"],
        change_type=row["change_type"],
        proposed_title=row.get("proposed_title"),
        proposed_content=row["proposed_content"],
        proposed_meta=row.get("proposed_meta"),
        governance_tier=row["governance_tier"],
        submission_category=row["submission_category"],
        status=row["status"],
        priority=row["priority"],
        submitted_by=str(row["submitted_by"]),
        source=row["source"],
        promoted_entry_id=str(row["promoted_entry_id"]) if row.get("promoted_entry_id") else None,
        created_at=row["created_at"],
    )


async def _validate_content_type(conn, content_type: str) -> None:
    """Validate content_type against the DB registry. Raises 422 on invalid type."""
    cur = await conn.execute(
        "SELECT name, alias_of FROM content_type_registry WHERE name = %s AND is_active = true",
        (content_type,),
    )
    row = await cur.fetchone()
    if row is None:
        cur = await conn.execute(
            "SELECT name FROM content_type_registry WHERE alias_of IS NULL AND is_active = true ORDER BY name"
        )
        valid = [r[0] for r in await cur.fetchall()]
        raise HTTPException(
            status_code=422,
            detail=f"Invalid content_type '{content_type}'. Must be one of: {valid}",
        )
    if row[1] is not None:
        raise HTTPException(
            status_code=422,
            detail=f"'{content_type}' is an alias — use '{row[1]}' instead",
        )


async def _promote_staging_item(conn, staging: dict, approver_id: str) -> dict:
    """Promote a staging item to the entries table. Returns the entry row (id, version).

    Handles create, update, and append change types. Creates version records.
    """
    meta = staging["proposed_meta"] or {}
    content_type = meta.get("content_type", "context")

    # Validate content_type before promoting
    await _validate_content_type(conn, content_type)
    sensitivity = meta.get("sensitivity", "shared")
    tags = meta.get("tags", [])
    # Strip structural fields — only org-specific data belongs in domain_meta
    domain_meta = {k: v for k, v in meta.items()
                   if k not in ("content_type", "sensitivity", "tags")}

    if staging["change_type"] == "create":
        content_hash = hashlib.sha256(staging["proposed_content"].encode()).hexdigest()
        cur = await conn.execute(
            """
            INSERT INTO entries (
                org_id, title, content, content_hash,
                content_type, logical_path, sensitivity,
                tags, domain_meta, source,
                created_by, updated_by
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s
            )
            RETURNING id, version
            """,
            (
                staging["org_id"],
                staging["proposed_title"] or "Untitled",
                staging["proposed_content"],
                content_hash,
                content_type,
                staging["target_path"],
                sensitivity,
                tags,
                json.dumps(domain_meta),
                staging["source"],
                staging["submitted_by"],
                approver_id,
            ),
        )
        cur.row_factory = dict_row
        entry_row = await cur.fetchone()

        await conn.execute(
            """
            INSERT INTO entry_versions (
                entry_id, org_id, version, title, content, content_hash,
                domain_meta, tags, status,
                changed_by, source, change_summary, governance_action
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s
            )
            ON CONFLICT (entry_id, version) DO NOTHING
            """,
            (
                entry_row["id"],
                staging["org_id"],
                entry_row["version"],
                staging["proposed_title"] or "Untitled",
                staging["proposed_content"],
                content_hash,
                json.dumps(domain_meta),
                tags,
                "active",
                approver_id,
                "web_ui",
                f"Created from staging item {staging['id']}",
                "evaluator_approved",
            ),
        )

        # Attachment-digest hook (T-0183): when the staging row came from
        # the PDF digest pipeline, the blob is already persisted; link it
        # to the newly-created entry via entry_attachments(role='source').
        # The caller (submit_staging / process_staging / approve_staging)
        # has already escalated to kb_admin before calling us, so the
        # INSERT bypasses the kb_agent/kb_editor WITH CHECK clause and
        # still carries proper org_id.
        if staging.get("submission_category") == "attachment_digest":
            blob_id = (meta or {}).get("blob_id")
            if blob_id:
                await conn.execute(
                    """
                    INSERT INTO entry_attachments (
                        org_id, entry_id, blob_id, role
                    ) VALUES (%s, %s, %s, 'source')
                    ON CONFLICT (entry_id, blob_id) DO NOTHING
                    """,
                    (staging["org_id"], entry_row["id"], blob_id),
                )

        return entry_row

    elif staging["change_type"] == "update":
        if not staging["target_entry_id"]:
            raise HTTPException(
                status_code=422,
                detail="Cannot promote update without target_entry_id",
            )

        cur = await conn.execute(
            "SELECT version, content, content_hash, title FROM entries WHERE id = %s",
            (staging["target_entry_id"],),
        )
        cur.row_factory = dict_row
        current = await cur.fetchone()
        if current is None:
            raise HTTPException(status_code=404, detail="Target entry not found")
        new_version = current["version"] + 1

        # COALESCE: keep existing content when proposed_content is null/empty
        effective_content = staging["proposed_content"] or current["content"]
        content_hash = hashlib.sha256(effective_content.encode()).hexdigest()

        # Only override structural fields that were EXPLICITLY provided in
        # proposed_meta. Using the defaulted locals (content_type defaults
        # to "context", sensitivity to "shared") would silently rewrite
        # fields on every meta-only update (e.g. a tag-only edit would
        # reset content_type to "context"). See issue #12.
        explicit_content_type = meta.get("content_type") if meta else None
        explicit_sensitivity = meta.get("sensitivity") if meta else None

        await conn.execute(
            """
            UPDATE entries
            SET title = COALESCE(%s, title),
                content = %s,
                content_hash = %s,
                content_type = COALESCE(%s, content_type),
                logical_path = COALESCE(%s, logical_path),
                sensitivity = COALESCE(%s, sensitivity),
                tags = COALESCE(%s, tags),
                domain_meta = COALESCE(%s, domain_meta),
                version = %s,
                updated_by = %s,
                source = %s
            WHERE id = %s
            """,
            (
                staging["proposed_title"],
                effective_content,
                content_hash,
                explicit_content_type,
                staging["target_path"],
                explicit_sensitivity,
                tags if tags else None,
                json.dumps(domain_meta) if domain_meta else None,
                new_version,
                approver_id,
                staging["source"],
                staging["target_entry_id"],
            ),
        )

        await conn.execute(
            """
            INSERT INTO entry_versions (
                entry_id, org_id, version, title, content, content_hash,
                domain_meta, tags, status,
                changed_by, source, change_summary, governance_action
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s
            )
            ON CONFLICT (entry_id, version) DO NOTHING
            """,
            (
                staging["target_entry_id"],
                staging["org_id"],
                new_version,
                staging["proposed_title"] or current["title"],
                effective_content,
                content_hash,
                json.dumps(domain_meta),
                tags,
                "active",
                approver_id,
                "web_ui",
                f"Updated from staging item {staging['id']}",
                "evaluator_approved",
            ),
        )
        return {"id": staging["target_entry_id"], "version": new_version}

    elif staging["change_type"] == "append":
        if not staging["target_entry_id"]:
            raise HTTPException(
                status_code=422,
                detail="Cannot promote append without target_entry_id",
            )

        cur = await conn.execute(
            "SELECT version, content, title FROM entries WHERE id = %s",
            (staging["target_entry_id"],),
        )
        cur.row_factory = dict_row
        current_row = await cur.fetchone()
        if current_row is None:
            raise HTTPException(status_code=404, detail="Target entry not found")

        new_version = current_row["version"] + 1
        new_content = current_row["content"] + "\n\n" + staging["proposed_content"]
        content_hash = hashlib.sha256(new_content.encode()).hexdigest()

        await conn.execute(
            """
            UPDATE entries
            SET content = %s,
                content_hash = %s,
                version = %s,
                updated_by = %s,
                source = %s
            WHERE id = %s
            """,
            (
                new_content,
                content_hash,
                new_version,
                approver_id,
                staging["source"],
                staging["target_entry_id"],
            ),
        )

        await conn.execute(
            """
            INSERT INTO entry_versions (
                entry_id, org_id, version, title, content, content_hash,
                domain_meta, tags, status,
                changed_by, source, change_summary, governance_action
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s
            )
            ON CONFLICT (entry_id, version) DO NOTHING
            """,
            (
                staging["target_entry_id"],
                staging["org_id"],
                new_version,
                staging["proposed_title"] or current_row["title"],
                new_content,
                content_hash,
                json.dumps(domain_meta),
                tags,
                "active",
                approver_id,
                "web_ui",
                f"Appended from staging item {staging['id']}",
                "evaluator_approved",
            ),
        )
        return {"id": staging["target_entry_id"], "version": new_version}

    elif staging["change_type"] == "create_link":
        # Link creation routed through staging for agent keys
        source_entry_id = meta.get("source_entry_id")
        target_entry_id = meta.get("target_entry_id")
        link_type = meta.get("link_type", "relates_to")
        weight = meta.get("weight", 1.0)
        link_metadata = meta.get("metadata", {})

        if not source_entry_id or not target_entry_id:
            raise HTTPException(
                status_code=422,
                detail="create_link requires source_entry_id and target_entry_id in proposed_meta",
            )

        cur = await conn.execute(
            """
            INSERT INTO entry_links (
                org_id, source_entry_id, target_entry_id,
                link_type, weight, metadata,
                created_by, source
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                staging["org_id"],
                source_entry_id,
                target_entry_id,
                link_type,
                weight,
                json.dumps(link_metadata),
                staging["submitted_by"],
                staging["source"],
            ),
        )
        cur.row_factory = dict_row
        link_row = await cur.fetchone()
        return {"id": link_row["id"]}


_SELECT_COLS = """
    id, org_id, target_entry_id, target_path, change_type,
    proposed_title, proposed_content, proposed_meta, content_hash,
    submitted_by, source,
    governance_tier, submission_category,
    status, priority, evaluator_notes, reviewed_at, reviewed_by,
    promoted_entry_id, created_at
"""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=StagingResponse, status_code=201)
async def submit_staging(
    body: StagingSubmit,
    user: UserContext = Depends(get_current_user),
):
    """Submit a proposed change to the governance staging pipeline."""
    # Content is required for create and append
    if body.change_type in ("create", "append") and not body.proposed_content:
        raise HTTPException(
            status_code=422,
            detail=f"proposed_content is required for {body.change_type} changes",
        )

    # For updates: at least one of proposed_content / proposed_title /
    # proposed_meta / content_type must be supplied — otherwise there is
    # nothing to apply (issue #12).
    if body.change_type == "update":
        has_content = body.proposed_content is not None
        has_title = body.proposed_title is not None
        has_meta = bool(body.proposed_meta)
        has_ct = body.content_type is not None
        if not (has_content or has_title or has_meta or has_ct):
            raise HTTPException(
                status_code=422,
                detail=(
                    "update requires at least one of proposed_content, "
                    "proposed_title, proposed_meta, or content_type"
                ),
            )

    # content_type is required for create changes
    if body.change_type == "create" and not body.content_type:
        # Check proposed_meta fallback
        meta_ct = (body.proposed_meta or {}).get("content_type")
        if not meta_ct:
            raise HTTPException(
                status_code=422,
                detail="content_type is required for create changes",
            )

    # Merge explicit content_type into proposed_meta so promotion picks it up
    if body.content_type:
        if body.proposed_meta is None:
            body.proposed_meta = {}
        body.proposed_meta["content_type"] = body.content_type

    # Determine sensitivity from proposed_meta (if provided)
    sensitivity = None
    if body.proposed_meta and "sensitivity" in body.proposed_meta:
        sensitivity = body.proposed_meta["sensitivity"]

    governance_tier = _assign_governance_tier(
        change_type=body.change_type,
        sensitivity=sensitivity,
        source=user.source,
        role=user.role,
    )

    # For metadata-only updates (no proposed_content), leave content_hash
    # NULL rather than hashing an empty string — otherwise every such
    # submission shares sha256("") and would falsely collide in the
    # Tier 2 duplicate-content check below.
    content_hash = (
        hashlib.sha256(body.proposed_content.encode()).hexdigest()
        if body.proposed_content is not None
        else None
    )

    async with get_db(user) as conn:
        # Validate content_type against registry at submission time (not just promotion)
        effective_ct = (body.proposed_meta or {}).get("content_type")
        if effective_ct:
            await _validate_content_type(conn, effective_ct)
        # Tier 1 and 2: attempt auto-approve; Tier 3-4: pending
        initial_status = "auto_approved" if governance_tier in (1, 2) else "pending"

        cur = await conn.execute(
            f"""
            INSERT INTO staging (
                org_id, target_entry_id, target_path, change_type,
                proposed_title, proposed_content, proposed_meta, content_hash,
                submitted_by, source,
                governance_tier, submission_category,
                status, priority
            ) VALUES (
                %(org_id)s, %(target_entry_id)s, %(target_path)s, %(change_type)s,
                %(proposed_title)s, %(proposed_content)s, %(proposed_meta)s, %(content_hash)s,
                %(submitted_by)s, %(source)s,
                %(governance_tier)s, %(submission_category)s,
                %(status)s, %(priority)s
            )
            RETURNING {_SELECT_COLS}
            """,
            {
                "org_id": user.org_id,
                "target_entry_id": body.target_entry_id,
                "target_path": body.target_path,
                "change_type": body.change_type,
                "proposed_title": body.proposed_title,
                "proposed_content": body.proposed_content,
                "proposed_meta": json.dumps(body.proposed_meta) if body.proposed_meta else None,
                "content_hash": content_hash,
                "submitted_by": user.id,
                "source": user.source,
                "governance_tier": governance_tier,
                "submission_category": body.submission_category,
                "status": initial_status,
                "priority": 3,  # default priority
            },
        )
        cur.row_factory = dict_row
        row = await cur.fetchone()

        if governance_tier in (1, 2):
            # --- Conflict detection (Tier 2 inline checks; Tier 1 also gets OCC) ---
            escalation_reasons: list[str] = []

            # Optimistic concurrency check for update/append
            if body.expected_version is not None and body.change_type in ("update", "append") and body.target_entry_id:
                cur2 = await conn.execute(
                    "SELECT version FROM entries WHERE id = %s",
                    (body.target_entry_id,),
                )
                version_row = await cur2.fetchone()
                if version_row and version_row[0] != body.expected_version:
                    if governance_tier == 1:
                        raise HTTPException(
                            status_code=409,
                            detail=f"Stale version: expected {body.expected_version} but entry is at version {version_row[0]}. Re-read and resubmit.",
                        )
                    escalation_reasons.append(
                        f"Version stale: expected {body.expected_version}, entry at {version_row[0]}"
                    )

            # Tier 2 additional conflict checks
            if governance_tier == 2:
                # Check for duplicate content hash in entries — only when
                # we actually have a hash. Metadata-only updates (no
                # proposed_content) skip this check; otherwise every such
                # submission would collide on sha256("").
                if content_hash is not None:
                    cur3 = await conn.execute(
                        "SELECT id FROM entries WHERE content_hash = %s AND org_id = %s LIMIT 1",
                        (content_hash, user.org_id),
                    )
                    dup = await cur3.fetchone()
                    if dup:
                        escalation_reasons.append(f"Duplicate content_hash — matches entry {dup[0]}")

                # Check for other pending items targeting the same entry
                if body.target_entry_id:
                    cur4 = await conn.execute(
                        """
                        SELECT COUNT(*) FROM staging
                        WHERE target_entry_id = %s AND status = 'pending' AND id != %s
                        """,
                        (body.target_entry_id, row["id"]),
                    )
                    conflict_count = (await cur4.fetchone())[0]
                    if conflict_count > 0:
                        escalation_reasons.append(
                            f"Conflict: {conflict_count} other pending item(s) target the same entry"
                        )

            # If Tier 2 has conflicts, escalate to Tier 3
            if escalation_reasons:
                notes = "; ".join(escalation_reasons)
                await conn.execute(
                    """
                    UPDATE staging
                    SET status = 'pending',
                        governance_tier = 3,
                        evaluator_notes = %s
                    WHERE id = %s
                    """,
                    (f"Escalated from Tier 2: {notes}", row["id"]),
                )
                row["status"] = "pending"
                row["governance_tier"] = 3
                return _row_to_response(row)

            # All checks passed — promote synchronously
            # Escalate to admin for promote (agent role can't INSERT into entries)
            await conn.execute("SET LOCAL ROLE kb_admin")

            # Promote to entries synchronously
            entry_row = await _promote_staging_item(conn, row, user.id)

            # Link staging row to promoted entry (skip for create_link — returns link ID, not entry ID)
            if body.change_type != "create_link":
                await conn.execute(
                    "UPDATE staging SET promoted_entry_id = %s WHERE id = %s",
                    (entry_row["id"], row["id"]),
                )
                row["promoted_entry_id"] = entry_row["id"]

            # Audit log — distinguish tier 1 vs tier 2 auto-approvals
            tier_label = f"Tier {governance_tier}"
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
                    user.org_id,
                    user.id,
                    user.role,
                    user.source,
                    f"staging.tier{governance_tier}_auto_approved",
                    "staging",
                    str(row["id"]),
                    body.target_path,
                    f"{tier_label} auto-approved staging item {row['id']} ({body.change_type})",
                ),
            )

        return _row_to_response(row)


@router.get("", response_model=StagingList)
async def list_staging(
    status: str = Query("pending", description="Filter by status"),
    target_path: str | None = Query(None, description="Filter by path prefix"),
    change_type: str | None = Query(None, description="Filter by change type"),
    since: str | None = Query(None, description="Filter by created_at >= ISO datetime"),
    user: UserContext = Depends(get_current_user),
):
    """List staging items. RLS handles visibility (admin sees all, others see own)."""
    conditions = ["status = %s"]
    params: list = [status]

    if target_path:
        conditions.append("target_path LIKE %s")
        params.append(f"{target_path}%")

    if change_type:
        conditions.append("change_type = %s")
        params.append(change_type)

    if since:
        conditions.append("created_at >= %s")
        params.append(since)

    where_clause = " AND ".join(conditions)

    async with get_db(user) as conn:
        # Count
        cur = await conn.execute(
            f"SELECT COUNT(*) FROM staging WHERE {where_clause}",
            params,
        )
        total = (await cur.fetchone())[0]

        # Fetch items
        cur = await conn.execute(
            f"""
            SELECT {_SELECT_COLS}
            FROM staging
            WHERE {where_clause}
            ORDER BY priority ASC, created_at ASC
            """,
            params,
        )
        cur.row_factory = dict_row
        rows = await cur.fetchall()

        # Observability: only log rows that surface a concrete target_entry_id.
        # Staging items without one (e.g. pending creates) have no entry to log.
        target_ids = [
            str(r["target_entry_id"]) for r in rows if r.get("target_entry_id")
        ]
        if target_ids:
            await log_entry_reads(conn, user, target_ids)

        return StagingList(
            items=[_row_to_response(r) for r in rows],
            total=total,
        )


@router.post("/process", response_model=ProcessResult)
async def process_staging(
    user: UserContext = Depends(get_current_user),
):
    """Batch-evaluate all pending staging items with deterministic checks.

    Admin only. Runs type validation, duplicate detection, conflict detection,
    and version staleness checks. Clean items are auto-approved; others are
    flagged or rejected with explanatory notes.
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can run batch processing")

    now = datetime.now(timezone.utc)
    approved = 0
    flagged = 0
    rejected = 0
    details: list[dict] = []

    async with get_db(user) as conn:
        # Fetch pending items in Tiers 1-3 only; Tier 4 requires human review
        cur = await conn.execute(
            f"""
            SELECT {_SELECT_COLS}
            FROM staging
            WHERE status = 'pending' AND governance_tier <= 3
            ORDER BY priority ASC, created_at ASC
            """,
        )
        cur.row_factory = dict_row
        pending_items = await cur.fetchall()

        if not pending_items:
            return ProcessResult(approved=0, flagged=0, rejected=0, details=[])

        # Pre-fetch valid content types for type validation
        cur = await conn.execute(
            "SELECT name FROM content_type_registry WHERE alias_of IS NULL AND is_active = true"
        )
        valid_types = {r[0] for r in await cur.fetchall()}

        # Build set of target_entry_ids with multiple pending items (conflict detection)
        target_ids = [
            str(item["target_entry_id"])
            for item in pending_items
            if item["target_entry_id"]
        ]
        conflicting_targets = {
            tid for tid in target_ids if target_ids.count(tid) > 1
        }

        for item in pending_items:
            item_id = str(item["id"])
            issues: list[str] = []
            action = None  # will be set to "approved", "deferred", or "rejected"

            # --- Check 1: Type validation ---
            meta = item.get("proposed_meta") or {}
            proposed_type = meta.get("content_type")
            if proposed_type and proposed_type not in valid_types:
                issues.append(f"Invalid content_type '{proposed_type}'")

            # --- Check 2: Duplicate detection (content_hash) ---
            # Skip for metadata-only items where content_hash is NULL
            # (no body content to dedupe on).
            if item["content_hash"] is not None:
                cur = await conn.execute(
                    "SELECT id FROM entries WHERE content_hash = %s AND org_id = %s LIMIT 1",
                    (item["content_hash"], item["org_id"]),
                )
                dup = await cur.fetchone()
                if dup:
                    issues.append(f"Duplicate content_hash — matches entry {dup[0]}")
                    action = "deferred"

            # --- Check 3: Conflict detection (same target_entry_id, multiple pending) ---
            if item["target_entry_id"] and str(item["target_entry_id"]) in conflicting_targets:
                issues.append(
                    f"Conflict: multiple pending items target entry {item['target_entry_id']}"
                )
                # Leave as pending per spec — don't reject
                if action is None:
                    action = "conflict"

            # --- Check 4: Version staleness (update/append) ---
            if item["change_type"] in ("update", "append") and item["target_entry_id"]:
                cur = await conn.execute(
                    "SELECT updated_at FROM entries WHERE id = %s",
                    (item["target_entry_id"],),
                )
                entry_row = await cur.fetchone()
                if entry_row is None:
                    issues.append("Target entry no longer exists")
                    action = "rejected"
                elif entry_row[0] > item["created_at"]:
                    issues.append(
                        f"Stale: entry updated at {entry_row[0]} after staging submitted at {item['created_at']}"
                    )
                    action = "rejected"

            # --- Decide outcome ---
            if action == "rejected":
                await conn.execute(
                    """
                    UPDATE staging
                    SET status = 'rejected',
                        evaluator_notes = %s,
                        reviewed_by = %s,
                        reviewed_at = %s
                    WHERE id = %s
                    """,
                    ("; ".join(issues), user.id, now, item["id"]),
                )
                rejected += 1
                details.append({"id": item_id, "action": "rejected", "tier": item["governance_tier"], "notes": issues})

            elif action == "deferred":
                await conn.execute(
                    """
                    UPDATE staging
                    SET status = 'deferred',
                        evaluator_notes = %s,
                        reviewed_by = %s,
                        reviewed_at = %s
                    WHERE id = %s
                    """,
                    ("; ".join(issues), user.id, now, item["id"]),
                )
                flagged += 1
                details.append({"id": item_id, "action": "deferred", "tier": item["governance_tier"], "notes": issues})

            elif action == "conflict":
                # Leave as pending, but add notes
                await conn.execute(
                    """
                    UPDATE staging
                    SET evaluator_notes = COALESCE(evaluator_notes || '; ', '') || %s
                    WHERE id = %s
                    """,
                    ("; ".join(issues), item["id"]),
                )
                flagged += 1
                details.append({"id": item_id, "action": "pending", "tier": item["governance_tier"], "notes": issues})

            elif issues:
                # Has issues but no definitive action — flag as deferred
                await conn.execute(
                    """
                    UPDATE staging
                    SET status = 'deferred',
                        evaluator_notes = %s,
                        reviewed_by = %s,
                        reviewed_at = %s
                    WHERE id = %s
                    """,
                    ("; ".join(issues), user.id, now, item["id"]),
                )
                flagged += 1
                details.append({"id": item_id, "action": "deferred", "tier": item["governance_tier"], "notes": issues})

            else:
                # --- Tier 3 AI review gate ---
                if item["governance_tier"] == 3:
                    ai_result: AIReviewResult = await review_staging_item(conn, item)
                    reasoning_note = f"AI review ({ai_result.action}, confidence={ai_result.confidence:.2f}): {ai_result.reasoning}"

                    if ai_result.action == "reject":
                        await conn.execute(
                            """
                            UPDATE staging
                            SET status = 'rejected',
                                evaluator_notes = COALESCE(evaluator_notes || '; ', '') || %s,
                                reviewed_by = %s,
                                reviewed_at = %s
                            WHERE id = %s
                            """,
                            (reasoning_note, user.id, now, item["id"]),
                        )
                        rejected += 1
                        details.append({"id": item_id, "action": "rejected", "tier": 3, "notes": [reasoning_note]})
                        continue

                    if ai_result.action == "escalate":
                        # Leave as pending for human review; write reasoning
                        await conn.execute(
                            """
                            UPDATE staging
                            SET evaluator_notes = COALESCE(evaluator_notes || '; ', '') || %s
                            WHERE id = %s
                            """,
                            (reasoning_note, item["id"]),
                        )
                        flagged += 1
                        details.append({"id": item_id, "action": "pending", "tier": 3, "notes": [reasoning_note]})
                        continue

                    # ai_result.action == "approve" — fall through to promotion below
                    # Write the AI reasoning even on approve
                    await conn.execute(
                        """
                        UPDATE staging
                        SET evaluator_notes = COALESCE(evaluator_notes || '; ', '') || %s
                        WHERE id = %s
                        """,
                        (reasoning_note, item["id"]),
                    )

                # All checks pass — auto-approve (or AI-approved for Tier 3)
                entry_row = await _promote_staging_item(conn, item, user.id)
                await conn.execute(
                    """
                    UPDATE staging
                    SET status = 'approved',
                        promoted_entry_id = %s,
                        reviewed_by = %s,
                        reviewed_at = %s
                    WHERE id = %s
                    """,
                    (entry_row["id"], user.id, now, item["id"]),
                )
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
                        item["org_id"],
                        user.id,
                        user.role,
                        user.source,
                        "staging.batch_approved",
                        "staging",
                        item_id,
                        item["target_path"],
                        f"Batch-approved staging item {item_id} ({item['change_type']})",
                    ),
                )
                approved += 1
                details.append({"id": item_id, "action": "approved", "tier": item["governance_tier"], "entry_id": str(entry_row["id"])})

    return ProcessResult(approved=approved, flagged=flagged, rejected=rejected, details=details)


@router.post("/{staging_id}/approve", response_model=StagingResponse)
async def approve_staging(
    staging_id: str,
    body: ReviewAction | None = None,
    user: UserContext = Depends(get_current_user),
):
    """Admin approves a staged item, promoting content to entries."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can approve staging items")

    now = datetime.now(timezone.utc)

    async with get_db(user) as conn:
        # Load the staging item
        cur = await conn.execute(
            f"SELECT {_SELECT_COLS} FROM staging WHERE id = %s",
            (staging_id,),
        )
        cur.row_factory = dict_row
        staging = await cur.fetchone()

        if staging is None:
            raise HTTPException(status_code=404, detail="Staging item not found")

        if staging["status"] != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"Staging item is already '{staging['status']}', cannot approve",
            )

        # Promote entry using shared helper
        await _promote_staging_item(conn, staging, user.id)

        # Update staging item status
        cur = await conn.execute(
            f"""
            UPDATE staging
            SET status = 'approved',
                reviewed_by = %s,
                reviewed_at = %s
            WHERE id = %s
            RETURNING {_SELECT_COLS}
            """,
            (user.id, now, staging_id),
        )
        cur.row_factory = dict_row
        updated = await cur.fetchone()

        # Insert audit log entry
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
                staging["org_id"],
                user.id,
                user.role,
                user.source,
                "staging.approved",
                "staging",
                staging_id,
                staging["target_path"],
                f"Approved staging item {staging_id} ({staging['change_type']})",
            ),
        )

        return _row_to_response(updated)


@router.post("/{staging_id}/reject", response_model=StagingResponse)
async def reject_staging(
    staging_id: str,
    body: ReviewAction | None = None,
    user: UserContext = Depends(get_current_user),
):
    """Admin rejects a staged item with optional reason."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can reject staging items")

    now = datetime.now(timezone.utc)
    reason = body.reason if body else None

    async with get_db(user) as conn:
        # Verify it exists and is pending
        cur = await conn.execute(
            "SELECT status FROM staging WHERE id = %s",
            (staging_id,),
        )
        cur.row_factory = dict_row
        check = await cur.fetchone()

        if check is None:
            raise HTTPException(status_code=404, detail="Staging item not found")

        if check["status"] != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"Staging item is already '{check['status']}', cannot reject",
            )

        # Update staging item
        cur = await conn.execute(
            f"""
            UPDATE staging
            SET status = 'rejected',
                evaluator_notes = %s,
                reviewed_by = %s,
                reviewed_at = %s
            WHERE id = %s
            RETURNING {_SELECT_COLS}
            """,
            (reason, user.id, now, staging_id),
        )
        cur.row_factory = dict_row
        updated = await cur.fetchone()

        # Insert audit log entry
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
                updated["org_id"],
                user.id,
                user.role,
                user.source,
                "staging.rejected",
                "staging",
                staging_id,
                updated["target_path"],
                f"Rejected staging item {staging_id}" + (f": {reason}" if reason else ""),
            ),
        )

        return _row_to_response(updated)
