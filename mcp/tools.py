"""Shared Brilliant MCP tool definitions — register on any FastMCP instance."""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from client import CortexClient


def register_tools(mcp: FastMCP, api: CortexClient) -> None:
    """Register all 18 Brilliant tools on the given FastMCP server instance."""

    # -------------------------------------------------------------------
    # Read tools
    # -------------------------------------------------------------------

    @mcp.tool()
    async def search_entries(
        q: str | None = None,
        content_type: str | None = None,
        logical_path: str | None = None,
        department: str | None = None,
        tag: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Search and list knowledge base entries with optional filters.

        Use `q` for full-text search (ranked by relevance). Without `q`, results
        are ordered by last update. Combine filters to narrow results.

        Content types: context, project, meeting, decision, intelligence, daily,
        resource, department, team, system, onboarding.
        """
        params = {"limit": limit, "offset": offset}
        if q:
            params["q"] = q
        if content_type:
            params["content_type"] = content_type
        if logical_path:
            params["logical_path"] = logical_path
        if department:
            params["department"] = department
        if tag:
            params["tag"] = tag
        return await api.get("/entries", params=params)

    @mcp.tool()
    async def get_entry(entry_id: str) -> dict:
        """Retrieve a single knowledge base entry by ID.

        Returns the full entry including content, metadata, tags, and version info.
        Returns 404 if the entry doesn't exist or is hidden by your permissions.
        """
        return await api.get(f"/entries/{entry_id}")

    @mcp.tool()
    async def get_index(
        depth: int = 3,
        path: str | None = None,
        content_type: str | None = None,
    ) -> dict:
        """Get a permission-filtered, tiered index of the entire knowledge base.

        Depth levels control how much detail is returned:
          L1 — Category counts only (minimal tokens)
          L2 — + Document titles, IDs, paths, timestamps
          L3 — + Relationships between entries (recommended default)
          L4 — + Summaries for each entry
          L5 — + Full content of all entries (use sparingly)

        Optional filters narrow the scope:
          path — logical_path prefix (e.g. 'Projects/' for project entries)
          content_type — specific type (e.g. 'decision', 'meeting')
        """
        params: dict = {"depth": depth}
        if path is not None:
            params["path"] = path
        if content_type is not None:
            params["content_type"] = content_type
        return await api.get("/index", params=params)

    @mcp.tool()
    async def get_types() -> dict:
        """List all registered content types in the knowledge base.

        Returns canonical types and their aliases. When creating entries,
        use canonical type names (not aliases).
        """
        return await api.get("/types")

    @mcp.tool()
    async def get_neighbors(entry_id: str, depth: int = 1) -> dict:
        """Traverse the knowledge graph from an entry to find connected entries.

        Depth 1 returns direct neighbors. Depth 2-3 uses recursive CTE traversal
        with cycle prevention. Bidirectional links (relates_to, contradicts) are
        traversed both ways; directional links follow outgoing direction only.
        """
        return await api.get(f"/entries/{entry_id}/links", params={"depth": depth})

    @mcp.tool()
    async def session_init() -> dict:
        """Initialize an agent session with a pre-assembled context bundle.

        Returns a dynamically-scoped index (depth chosen by KB size),
        all system entries, metadata (total entries, last_updated,
        user role summary), and pending_reviews.

        The pending_reviews section surfaces Tier 3+ governance items
        awaiting human review, scoped to the caller's organization:
          count      — total pending items (up to 20)
          items      — preview of top 5 items with id, target_path,
                       change_type, governance_tier, submitted_by, age_hours
          review_url — API path to list all pending high-tier items

        Call this at the start of every conversation to load ambient
        context and check for items requiring your attention.
        """
        return await api.get("/session-init")

    # -------------------------------------------------------------------
    # Write tools
    # -------------------------------------------------------------------

    @mcp.tool()
    async def create_entry(
        title: str,
        content: str,
        content_type: str,
        logical_path: str,
        sensitivity: str = "shared",
        summary: str | None = None,
        department: str | None = None,
        tags: list[str] | None = None,
        domain_meta: dict | None = None,
        project_id: str | None = None,
    ) -> dict:
        """Create a new knowledge base entry.

        IMPORTANT: Agent keys cannot call this directly — use submit_staging
        instead. Only interactive/API keys may create entries directly.

        The `source` field is auto-set from your API key type (web_ui, agent, api).

        Content types: context, project, meeting, decision, intelligence, daily,
        resource, department, team, system, onboarding.

        Sensitivity levels: system, strategic, operational, private, project,
        meeting, shared.
        """
        body: dict = {
            "title": title,
            "content": content,
            "content_type": content_type,
            "logical_path": logical_path,
            "sensitivity": sensitivity,
        }
        if summary is not None:
            body["summary"] = summary
        if department is not None:
            body["department"] = department
        if tags is not None:
            body["tags"] = tags
        if domain_meta is not None:
            body["domain_meta"] = domain_meta
        if project_id is not None:
            body["project_id"] = project_id
        return await api.post("/entries", json=body)

    @mcp.tool()
    async def update_entry(
        entry_id: str,
        title: str | None = None,
        content: str | None = None,
        summary: str | None = None,
        content_type: str | None = None,
        logical_path: str | None = None,
        sensitivity: str | None = None,
        department: str | None = None,
        tags: list[str] | None = None,
        domain_meta: dict | None = None,
    ) -> dict:
        """Update an existing knowledge base entry (partial update).

        IMPORTANT: Agent keys cannot call this directly — use submit_staging
        instead. Only interactive/API keys may update entries directly.

        Only include fields you want to change. Automatically creates a version
        snapshot before applying changes and bumps the version number.
        """
        body: dict = {}
        for field in [
            "title", "content", "summary", "content_type",
            "logical_path", "sensitivity", "department", "tags", "domain_meta",
        ]:
            val = locals()[field]
            if val is not None:
                body[field] = val
        return await api.put(f"/entries/{entry_id}", json=body)

    @mcp.tool()
    async def delete_entry(entry_id: str) -> dict:
        """Soft-delete a knowledge base entry (sets status to archived).

        IMPORTANT: Agent keys cannot call this directly — use submit_staging
        instead. Only interactive/API keys may delete entries directly.

        The entry remains in the database but is excluded from search and index.
        """
        return await api.delete(f"/entries/{entry_id}")

    @mcp.tool()
    async def append_entry(
        entry_id: str,
        content: str,
        separator: str = "\n\n",
    ) -> dict:
        """Append content to an existing knowledge base entry.

        IMPORTANT: Agent keys cannot call this directly — use submit_staging
        with change_type='append' instead. Only interactive/API keys may
        append directly.

        Atomically concatenates new content after a separator (default: double
        newline). Creates a version snapshot before applying.
        """
        return await api.patch(f"/entries/{entry_id}/append", json={
            "content": content,
            "separator": separator,
        })

    @mcp.tool()
    async def create_link(
        source_entry_id: str,
        target_entry_id: str,
        link_type: str,
        weight: float = 1.0,
        metadata: dict | None = None,
    ) -> dict:
        """Create a typed link between two knowledge base entries.

        IMPORTANT: Agent keys cannot call this directly — use submit_staging
        with change_type='create_link' and proposed_meta containing
        source_entry_id, target_entry_id, link_type, weight, and metadata.

        Link types:
          relates_to   — bidirectional general relationship
          supersedes   — source replaces target
          contradicts  — bidirectional conflict
          depends_on   — source requires target
          part_of      — source is a component of target
          tagged_with  — source is categorized by target
        """
        body: dict = {
            "target_entry_id": target_entry_id,
            "link_type": link_type,
            "weight": weight,
        }
        if metadata is not None:
            body["metadata"] = metadata
        return await api.post(f"/entries/{source_entry_id}/links", json=body)

    # -------------------------------------------------------------------
    # Governance tools
    # -------------------------------------------------------------------

    @mcp.tool()
    async def submit_staging(
        target_path: str,
        proposed_content: str | None = None,
        change_type: str = "create",
        proposed_title: str | None = None,
        content_type: str | None = None,
        target_entry_id: str | None = None,
        proposed_meta: dict | None = None,
        submission_category: str = "user_direct",
        expected_version: int | None = None,
    ) -> dict:
        """Submit a proposed change to the governance pipeline.

        All writes from agent-type API keys are routed through staging. Governance
        tier is auto-assigned based on sensitivity and change type:

          Tier 1 — Auto-approve: creates (non-sensitive), appends, links, tags,
                   admin/editor web_ui writes. Committed synchronously; response
                   includes `status: "auto_approved"` and `promoted_entry_id`.
          Tier 2 — Auto-approve with conflict detection: updates run inline
                   staleness/duplicate/conflict checks. Clean items auto-approve
                   like Tier 1. Items with conflicts escalate to Tier 3.
          Tier 3 — Batch/AI review: high-sensitivity content and Tier 2 escalations.
                   Stays `pending` until processed by `process_staging` or manual review.
          Tier 4 — Human-only: deletions, sensitivity changes, governance rule mods.
                   Only resolvable via manual approve/reject.

        Change types: create, update, append, delete, create_link.

        content_type: Required for create. One of: context, project, meeting,
        decision, intelligence, daily, resource, department, team, system,
        onboarding, session. Validated against the type registry at submission
        time — invalid types return 422.

        For create_link: pass source_entry_id, target_entry_id, link_type,
        weight, and metadata in proposed_meta. Use proposed_content for a
        human-readable description of the link (e.g. "Links X to Y").

        For update/append: pass `expected_version` to enable optimistic concurrency.
        Returns 409 if the entry has been modified since you last read it.
        """
        body: dict = {
            "target_path": target_path,
            "change_type": change_type,
            "submission_category": submission_category,
        }
        if proposed_content is not None:
            body["proposed_content"] = proposed_content
        if proposed_title is not None:
            body["proposed_title"] = proposed_title
        if content_type is not None:
            body["content_type"] = content_type
        if target_entry_id is not None:
            body["target_entry_id"] = target_entry_id
        if proposed_meta is not None:
            body["proposed_meta"] = proposed_meta
        if expected_version is not None:
            body["expected_version"] = expected_version
        return await api.post("/staging", json=body)

    @mcp.tool()
    async def list_staging(
        status: str = "pending",
        target_path: str | None = None,
        change_type: str | None = None,
        since: str | None = None,
    ) -> dict:
        """List items in the governance staging pipeline.

        Admins see all items; non-admins see only their own submissions.

        Filters:
          status — pending, approved, rejected (default: pending)
          target_path — filter by path prefix (e.g. 'Projects/')
          change_type — create, update, append, delete
          since — ISO datetime, returns items created on or after this time
        """
        params: dict = {"status": status}
        if target_path is not None:
            params["target_path"] = target_path
        if change_type is not None:
            params["change_type"] = change_type
        if since is not None:
            params["since"] = since
        return await api.get("/staging", params=params)

    @mcp.tool()
    async def review_staging(
        staging_id: str,
        action: str,
        reason: str | None = None,
    ) -> dict:
        """Approve or reject a pending staging item (admin only).

        Primarily used for Tier 4 items (deletions, governance-sensitive changes)
        that require human judgment. Can also be used for Tier 3 items that
        batch processing left unresolved.

        Set action to 'approve' or 'reject'. On approval, the proposed change is
        applied to the knowledge base. A reason is recorded in the audit log.
        """
        endpoint = f"/staging/{staging_id}/{action}"
        body: dict = {}
        if reason is not None:
            body["reason"] = reason
        return await api.post(endpoint, json=body if body else None)

    @mcp.tool()
    async def process_staging() -> dict:
        """Batch-evaluate pending staging items in Tiers 1-3 (admin only).

        Tier 4 items (deletions, governance-sensitive) are skipped — they require
        human review via `review_staging`.

        Runs deterministic checks on each eligible pending item:
          - Type validation (content_type exists in registry)
          - Duplicate detection (content_hash matches existing entry)
          - Conflict detection (multiple pending items target same entry)
          - Version staleness (entry modified after staging submission)

        Clean items are auto-approved and promoted to entries. Duplicates are
        deferred. Stale items are rejected. Conflicts are noted but left pending.

        Returns counts (approved, flagged, rejected) and per-item details
        including governance tier for each processed item.
        """
        return await api.post("/staging/process")

    # -------------------------------------------------------------------
    # Onboarding tools
    # -------------------------------------------------------------------

    @mcp.tool()
    async def redeem_invite(
        invite_code: str,
        token: str,
        email: str,
        display_name: str,
        password: str,
    ) -> dict:
        """Redeem an invite code to join an organization (no authentication required).

        Use this when onboarding a new user. The invite code (CTX-XXXX-XXXX format)
        and token are provided by an admin. On success, returns a new API key that
        should be stored for future authentication. The password is used for
        email+password login via the frontend.

        This is a single-use action — failed attempts invalidate the invite.
        """
        return await api.post("/invitations/redeem", json={
            "invite_code": invite_code,
            "token": token,
            "email": email,
            "display_name": display_name,
            "password": password,
        }, api_key="")

    # -------------------------------------------------------------------
    # Import tools
    # -------------------------------------------------------------------

    @mcp.tool()
    async def import_vault(
        files: list[dict],
        base_path: str = "",
        source_vault: str = "vault",
        preview_only: bool = False,
    ) -> dict:
        """Import markdown files into the knowledge base from a vault.

        Supports preview mode (dry-run) to check for collisions before committing.
        In execute mode, creates a tracked batch that can be rolled back.

        Each file dict must have 'filename' (str) and 'content' (str) keys.

        Set preview_only=True to analyze without importing. Returns collision
        report, type mappings, and projected counts.

        Set preview_only=False (default) to execute the import with batch tracking.
        Returns batch_id for potential rollback via rollback_import.
        """
        if preview_only:
            return await api.post("/import/preview", json={
                "files": files,
                "base_path": base_path,
            })
        else:
            return await api.post("/import", json={
                "files": files,
                "base_path": base_path,
                "source_vault": source_vault,
                "collisions": [],
            })

    @mcp.tool()
    async def rollback_import(batch_id: str) -> dict:
        """Rollback an entire import batch.

        Archives all entries created by the batch, removes links and pending
        staging items. The batch status changes to 'rolled_back'.

        Use the batch_id returned from import_vault to identify which batch
        to roll back. Cannot roll back an already rolled-back batch (returns 409).
        """
        return await api.delete(f"/import/{batch_id}")

    # -------------------------------------------------------------------
    # Attachment tools
    # -------------------------------------------------------------------

    @mcp.tool()
    async def upload_attachment(
        path: str,
        digest: bool = True,
        content_type: str | None = None,
    ) -> dict:
        """Upload a local file to Brilliant and optionally digest it into a staged entry.

        Streams the file at `path` to `POST /attachments` as multipart and
        returns the JSON response. When `digest=True` and the effective
        content type is `application/pdf`, the server extracts text via
        pypdf and creates a staged entry (visible via `list_staging`) that,
        on approval, links back to the stored blob via `entry_attachments`.

        Parameters:
          path          — absolute path to a local file. The MCP server
                          process must be able to read the file directly
                          (`open(path, 'rb')`), which for Docker-hosted
                          MCP means the file must live on a bind-mounted
                          volume. Relative paths are resolved against the
                          MCP server's working directory.
          digest        — when True and content_type resolves to
                          `application/pdf`, the upload triggers the PDF
                          digest pipeline. Ignored for non-PDF uploads.
          content_type  — explicit MIME override. If None, the type is
                          derived from the file extension via
                          `mimetypes.guess_type`, falling back to
                          `application/octet-stream`.

        Returns the server's JSON verbatim. For successful uploads:
          {
            "blob_id": "<uuid>",
            "sha256": "<hex>",
            "dedup": <bool>,
            "size_bytes": <int>,
            "content_type": "<mime>",
            "staging_id": "<uuid>"   # only when digest=True and PDF
          }

        Uploading identical bytes twice within the same org returns
        `dedup: true` with the original blob_id.
        """
        file_path = Path(path)
        if not file_path.is_file():
            return {
                "error": True,
                "status": 400,
                "detail": f"File not found or not a regular file: {path}",
            }

        effective_ct = content_type
        if effective_ct is None:
            guessed, _ = mimetypes.guess_type(file_path.name)
            effective_ct = guessed or "application/octet-stream"

        try:
            data = file_path.read_bytes()
        except OSError as exc:
            return {
                "error": True,
                "status": 400,
                "detail": f"Could not read {path}: {exc}",
            }

        files = {"file": (file_path.name, data, effective_ct)}
        params: dict = {"digest": "true" if digest else "false"}
        # The endpoint uses the `content_type` query param as an override
        # applied on top of the multipart part's own content-type. Pass
        # through whatever we resolved so the server's effective type
        # matches what we sent.
        params["content_type"] = effective_ct

        return await api.post_multipart("/attachments", files=files, params=params)

    # -------------------------------------------------------------------
    # Analytics tools (admin-only)
    # -------------------------------------------------------------------

    def _coerce_admin_error(result: dict) -> dict:
        """Convert a raw 403 CortexClient error dict into the documented shape.

        CortexClient returns {"error": True, "status": 403, "detail": ...} on
        HTTP 4xx/5xx. For admin-only endpoints we surface a friendlier
        {"error": "admin-only", "detail": ...} so non-admin callers never see
        a raw exception or a generic error envelope.
        """
        if isinstance(result, dict) and result.get("error") is True and result.get("status") == 403:
            return {"error": "admin-only", "detail": result.get("detail")}
        return result

    @mcp.tool()
    async def get_usage_stats(
        kind: str = "top-entries",
        since: str = "24h",
        actor_type: str | None = None,
        actor_id: str | None = None,
        limit: int = 20,
    ) -> dict:
        """Usage analytics rollups for admins.

        Wraps the /analytics/* endpoints and returns consolidated JSON for the
        calling admin's org. Non-admin callers receive
        {"error": "admin-only", "detail": ...} — never a raised exception and
        never a raw 500.

        kind:
          - "top-entries"     — most-read entries in the window
          - "top-endpoints"   — most-hit endpoints with latency stats
          - "session-depth"   — session breakdown for a specific actor_id
          - "summary"         — returns all three at once (single response)

        since: "1h" | "24h" | "7d" | "30d" (default "24h")

        actor_type: optional filter for top-entries — "user" | "agent" | "api".
        actor_id: required for "session-depth" (and "summary" when you want
          per-actor session data — omit to skip).
        limit: page size for top-entries and top-endpoints (default 20).

        Admin-only — non-admin callers get {"error": "admin-only", ...}.
        """
        if kind == "top-entries":
            params: dict = {"since": since, "limit": limit}
            if actor_type is not None:
                params["actor_type"] = actor_type
            return _coerce_admin_error(await api.get("/analytics/top-entries", params=params))

        if kind == "top-endpoints":
            params = {"since": since, "limit": limit}
            return _coerce_admin_error(await api.get("/analytics/top-endpoints", params=params))

        if kind == "session-depth":
            params = {"since": since}
            if actor_id is not None:
                params["actor_id"] = actor_id
            return _coerce_admin_error(await api.get("/analytics/session-depth", params=params))

        if kind == "summary":
            top_entries_params: dict = {"since": since, "limit": limit}
            if actor_type is not None:
                top_entries_params["actor_type"] = actor_type
            top_endpoints_params: dict = {"since": since, "limit": limit}
            session_depth_params: dict = {"since": since}
            if actor_id is not None:
                session_depth_params["actor_id"] = actor_id

            top_entries, top_endpoints, session_depth = await asyncio.gather(
                api.get("/analytics/top-entries", params=top_entries_params),
                api.get("/analytics/top-endpoints", params=top_endpoints_params),
                api.get("/analytics/session-depth", params=session_depth_params),
            )

            # If any sub-call returned 403 (admin-only), surface the same
            # admin-only envelope so the whole summary call fails clearly
            # rather than returning a half-populated dict.
            for sub in (top_entries, top_endpoints, session_depth):
                if (
                    isinstance(sub, dict)
                    and sub.get("error") is True
                    and sub.get("status") == 403
                ):
                    return {"error": "admin-only", "detail": sub.get("detail")}

            return {
                "top_entries": top_entries,
                "top_endpoints": top_endpoints,
                "session_depth": session_depth,
            }

        return {
            "error": "invalid-kind",
            "detail": f"unknown kind {kind!r} — expected one of: "
                      "top-entries, top-endpoints, session-depth, summary",
        }
