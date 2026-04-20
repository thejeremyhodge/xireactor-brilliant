"""Shared Brilliant MCP tool definitions — register on any FastMCP instance.

Sprint 0039 (T-0229): every tool handler pulls the OAuth-bound ``user_id``
off the authenticated ``AccessToken`` (a ``BrilliantAccessToken`` from
``remote_server.py``) and passes it as ``act_as=user_id`` to every
``api.*`` call. The API then acts-as that user while authenticating with
the MCP's service-role key (see ``mcp/client.py``).

Two transports share this module:

* **Remote (Streamable HTTP / OAuth)** — ``get_access_token()`` returns a
  ``BrilliantAccessToken`` with ``user_id`` bound via the /authorize ->
  /oauth/login handoff. If ``user_id`` is ``None`` on a remote-transport
  request we raise ``ToolError`` (surfaces as a tool error response; no
  API call is made, no service-level write happens).
* **Local stdio (Claude Desktop)** — the request has no authenticated
  user. ``get_access_token()`` returns ``None`` and we skip the
  ``act_as`` param. The local user's single-tenant API key is assumed
  to be set via env for the stdio transport.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import mimetypes
import sys
from pathlib import Path

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from client import BrilliantClient

# Locate the shared vault-walking helpers (`tools/vault_parse.py`). Used by
# the `import_vault(path)` MCP tool so we don't duplicate the walker logic.
# Search both the repo-root-relative `tools/` dir (local MCP run from the
# repo) and the co-located `mcp/tools/` dir (if packaged alongside).
#
# The `_VAULT_PARSE_AVAILABLE` flag gates whether the `import_vault(path)`
# tool registers at boot. On remote Render deploys the MCP Dockerfile uses
# `dockerContext: ./mcp`, so the repo-root `tools/` dir isn't shipped —
# the flag stays False and `import_vault` never appears in the Co-work
# tool list (avoids exposing a filesystem-walk tool that has no access to
# the caller's filesystem). On local stdio runs (Claude Desktop / Claude
# Code against a repo checkout) the flag is True and the tool registers.
_MCP_DIR = Path(__file__).resolve().parent
_CANDIDATE_TOOL_DIRS: list[Path] = [
    _MCP_DIR.parent / "tools",  # repo-root layout
    _MCP_DIR / "tools",         # packaged-alongside fallback
]
_VAULT_PARSE_AVAILABLE: bool = False
for _tool_dir in _CANDIDATE_TOOL_DIRS:
    if (_tool_dir / "vault_parse.py").is_file():
        if str(_tool_dir) not in sys.path:
            sys.path.insert(0, str(_tool_dir))
        _VAULT_PARSE_AVAILABLE = True
        break


def _resolve_act_as_user_id() -> str | None:
    """Return the OAuth-bound ``user_id`` for the in-flight tool call, or None.

    Behaviour matrix:

    * Remote (OAuth) transport, token carries ``user_id`` → return the
      UUID string. Tool handler passes ``act_as=<uuid>`` on every
      outbound API call, API acts-as that user under RLS.
    * Remote (OAuth) transport, token with ``user_id is None`` →
      raise ``ToolError``. This is a security invariant: a bearer
      token issued post-sprint must always have a bound user, and
      falling through to the MCP's service identity would let any
      valid-but-unbound token hit the API with service-level scope.
    * Local stdio transport, no authenticated user →
      ``get_access_token()`` returns ``None``. We return ``None`` so
      the tool skips ``X-Act-As-User`` and the API falls back to the
      presenting key's identity. Local stdio is single-user by
      design, so this preserves pre-0039 behaviour.
    """
    access_token = get_access_token()
    if access_token is None:
        # Stdio / no auth context — preserved pre-0039 behaviour.
        return None
    # Remote path. The token should be a ``BrilliantAccessToken`` with
    # ``user_id`` populated; ``getattr`` is defensive against any stock
    # ``AccessToken`` sneaking through (would still raise below).
    user_id = getattr(access_token, "user_id", None)
    if not user_id:
        raise ToolError(
            "Authenticated token is missing a bound user_id. "
            "Re-authenticate via the OAuth login flow."
        )
    return user_id


def register_tools(mcp: FastMCP, api: BrilliantClient) -> None:
    """Register all Brilliant tools on the given FastMCP server instance."""

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
        tags: list[str] | None = None,
        fuzzy: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Search and list knowledge base entries with optional filters.

        Use `q` for full-text search (ranked by relevance). Without `q`, results
        are ordered by last update. Combine filters to narrow results.

        Tag filtering:
          tag   — single tag match (entry must contain this tag).
          tags  — AND-match across multiple tags (entry must contain ALL
                  listed tags). Use this for triangulation, e.g.
                  tags=["client-thryv", "sprint-planning"] to find entries
                  at the intersection. Mutually exclusive with `tag` —
                  passing both returns a 422 error.

        Set `fuzzy=true` to enable a trigram-similarity fallback when the exact
        FTS query returns zero rows (e.g. a user typed "klaude" when they meant
        "claude"). Fuzzy is a pure fallback — the exact/FTS path runs first and
        is returned as-is when it has any hits. Default is False so existing
        behavior is unchanged.

        Content types: context, project, meeting, decision, intelligence, daily,
        resource, department, team, system, onboarding.
        """
        user_id = _resolve_act_as_user_id()
        params: dict = {"limit": limit, "offset": offset}
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
        if tags:
            # httpx serialises a list value as repeated query params
            # (?tags=a&tags=b), which is what the API's
            # `tags: list[str] = Query(None)` expects. No manual
            # querystring build needed.
            params["tags"] = list(tags)
        if fuzzy:
            params["fuzzy"] = "true"
        return await api.get("/entries", params=params, act_as=user_id)

    @mcp.tool()
    async def get_entry(entry_id: str) -> dict:
        """Retrieve a single knowledge base entry by ID.

        Returns the full entry including content, metadata, tags, and version info.
        Returns 404 if the entry doesn't exist or is hidden by your permissions.
        """
        user_id = _resolve_act_as_user_id()
        return await api.get(f"/entries/{entry_id}", act_as=user_id)

    @mcp.tool()
    async def get_index(
        depth: int = 3,
        path: str | None = None,
        content_type: str | None = None,
        tag: str | None = None,
    ) -> dict:
        """Get a permission-filtered, tiered index of the entire knowledge base.

        Depth levels control how much detail is returned:
          L1 — Category counts only (minimal tokens, always safe at any scale)
          L2 — + Document titles, IDs, paths, timestamps
          L3 — + Relationships between entries (recommended default)
          L4 — + Summaries for each entry
          L5 — + Full content of all entries (use sparingly)

        Optional filters narrow the scope:
          path — logical_path prefix (e.g. 'Projects/' for project entries)
          content_type — specific type (e.g. 'decision', 'meeting')
          tag — single tag match. For multi-tag AND filtering use
                ``search_entries(tags=[...])`` — ``get_index`` only supports
                one tag at a time.

        Scale guard: at depth >= 2, if your KB has more than 200 visible
        published entries AND you supply no narrowing filter, the endpoint
        returns 422 with body
        ``{"error": "index_too_large", "total": N, "hint": "..."}``. Start
        from ``session_init.manifest`` (categories, top_paths, tags_top),
        pick a narrowing axis (path, content_type, or tag), and re-call
        ``get_index`` with that filter — or drop to ``search_entries`` for
        ranked results. L1 (counts only) is unconstrained; call
        ``get_index(depth=1)`` any time you just need category totals.
        """
        user_id = _resolve_act_as_user_id()
        params: dict = {"depth": depth}
        if path is not None:
            params["path"] = path
        if content_type is not None:
            params["content_type"] = content_type
        if tag is not None:
            params["tag"] = tag
        return await api.get("/index", params=params, act_as=user_id)

    @mcp.tool()
    async def get_types() -> dict:
        """List all registered content types in the knowledge base.

        Returns canonical types and their aliases. When creating entries,
        use canonical type names (not aliases).
        """
        user_id = _resolve_act_as_user_id()
        return await api.get("/types", act_as=user_id)

    @mcp.tool()
    async def get_neighbors(entry_id: str, depth: int = 1) -> dict:
        """Traverse the knowledge graph from an entry to find connected entries.

        Depth 1 returns direct neighbors. Depth 2-3 uses recursive CTE traversal
        with cycle prevention. Bidirectional links (relates_to, contradicts) are
        traversed both ways; directional links follow outgoing direction only.
        """
        user_id = _resolve_act_as_user_id()
        return await api.get(
            f"/entries/{entry_id}/links",
            params={"depth": depth},
            act_as=user_id,
        )

    @mcp.tool()
    async def session_init() -> dict:
        """Initialize an agent session with a compact density manifest.

        Returns a single `manifest` object budgeted to ~2K tokens regardless
        of KB size. The manifest tells you WHAT exists and WHERE to look —
        it intentionally does NOT carry full entry content, summaries, or
        the relationship graph. Drill down with `get_index(depth=N, path=...)`,
        `search_entries(q=...)`, and `get_entry(id)` once you know what you
        need.

        Manifest fields:
          total_entries   — total published entries visible to you (RLS-filtered)
          last_updated    — ISO timestamp of the most recent entry update
          user            — {id, display_name, role, department, source};
                            check `source` to know whether you have an agent
                            key (writes must go through submit_staging)
          categories      — [{content_type, count}, ...] ordered by count desc
          top_paths       — [{logical_path_prefix, count}, ...] top-level
                            buckets (first path segment), capped at 15 rows.
                            Use these prefixes as `path=` args to get_index.
          system_entries  — [{id, title, logical_path}, ...] — HANDLES ONLY.
                            Fetch full content with get_entry(id) when needed.
                            A fresh org with no rules returns an empty array.
          pending_reviews — {count, items[0..5], review_url}. If count > 0
                            surface this in your standup unconditionally.
          hints           — short list of suggested next tool calls

        The content-type registry is NOT carried here — query via get_types.

        Call this at the start of every conversation to load ambient context
        and check for items requiring your attention.
        """
        user_id = _resolve_act_as_user_id()
        return await api.get("/session-init", act_as=user_id)

    @mcp.tool()
    async def list_tags(limit: int = 500, offset: int = 0) -> dict:
        """List your org's full tag corpus with per-tag usage counts.

        Returns a paginated view of every tag attached to a published entry
        visible to you (RLS-filtered), ordered by usage count descending and
        then tag alphabetically. Complements `session_init.manifest.tags_top`,
        which is capped at 20 — use `list_tags` to reach the long tail.

        Parameters:
          limit   — page size, default 500, max 5000
          offset  — pagination offset, default 0

        Returns:
          {
            "tags":  [{"tag": "<name>", "count": <int>}, ...],
            "total": <int>   # distinct tags visible to you
          }

        Empty-corpus orgs return `{"tags": [], "total": 0}` — not an error.
        Pair with `search_entries(tag="<name>")` to drill into a specific tag.
        """
        user_id = _resolve_act_as_user_id()
        return await api.get(
            "/tags",
            params={"limit": limit, "offset": offset},
            act_as=user_id,
        )

    @mcp.tool()
    async def get_tag_neighbors(tag: str, limit: int = 10) -> dict:
        """List tags that frequently co-occur with ``tag`` on the same entry.

        Use this to triangulate: given a known high-signal tag (e.g. a
        client or project name you spotted in `session_init.manifest.tags_top`
        or `list_tags`), discover which other tags are typically attached
        to the same entries. The agent can then narrow further with
        `search_entries(tags=[tag, neighbor])`.

        Ranking is ``co_count`` descending, then ``jaccard`` descending,
        then ``tag`` ascending for stable ordering. ``co_count`` is the
        raw number of entries carrying both tags; ``jaccard`` normalises
        that by the union size, so a rare-but-always-paired tag can
        outrank a common tag that merely happens to overlap.

        Parameters:
          tag    — target tag to find neighbors for (path-escaped
                   automatically; pass the raw tag string)
          limit  — max neighbors to return, default 10, max 100

        Returns:
          {
            "tag": "<input>",
            "neighbors": [
              {"tag": "<other>", "co_count": <int>, "jaccard": <float>},
              ...
            ]
          }

        Unknown or unused tags return ``{"tag": "<input>", "neighbors": []}``
        with no error — treated as an empty co-occurrence set.
        Complements `list_tags` (full corpus) and
        `search_entries(tags=[...])` (intersect once you know what to
        pair).
        """
        user_id = _resolve_act_as_user_id()
        return await api.get(
            f"/tags/{tag}/co-occurring",
            params={"limit": limit},
            act_as=user_id,
        )

    @mcp.tool()
    async def suggest_tags(content: str, limit: int = 10) -> dict:
        """Suggest tags for free-form content, drawn from your org's existing vocabulary.

        Ranks tags already in use across your published entries by how well
        they match ``content`` (case-insensitive substring, whole-word bonus)
        weighted by each tag's usage frequency. No LLM, no embeddings —
        deterministic and fast. RLS scopes the corpus to your org.

        Returns up to ``limit`` suggestions, each with:
          tag          — the tag string
          score        — relevance score (higher = better match)
          usage_count  — how many published entries currently use this tag

        An empty-corpus org or content with no matching tags returns
        ``{"suggestions": []}`` — not an error. Use this before creating
        an entry to stay consistent with the org's existing taxonomy.
        """
        user_id = _resolve_act_as_user_id()
        return await api.post(
            "/tags/suggest",
            json={"content": content, "limit": limit},
            act_as=user_id,
        )

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
        user_id = _resolve_act_as_user_id()
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
        return await api.post("/entries", json=body, act_as=user_id)

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
        user_id = _resolve_act_as_user_id()
        body: dict = {}
        for field in [
            "title", "content", "summary", "content_type",
            "logical_path", "sensitivity", "department", "tags", "domain_meta",
        ]:
            val = locals()[field]
            if val is not None:
                body[field] = val
        return await api.put(f"/entries/{entry_id}", json=body, act_as=user_id)

    @mcp.tool()
    async def delete_entry(entry_id: str) -> dict:
        """Soft-delete a knowledge base entry (sets status to archived).

        IMPORTANT: Agent keys cannot call this directly — use submit_staging
        instead. Only interactive/API keys may delete entries directly.

        The entry remains in the database but is excluded from search and index.
        """
        user_id = _resolve_act_as_user_id()
        return await api.delete(f"/entries/{entry_id}", act_as=user_id)

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
        user_id = _resolve_act_as_user_id()
        return await api.patch(
            f"/entries/{entry_id}/append",
            json={"content": content, "separator": separator},
            act_as=user_id,
        )

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
        user_id = _resolve_act_as_user_id()
        body: dict = {
            "target_entry_id": target_entry_id,
            "link_type": link_type,
            "weight": weight,
        }
        if metadata is not None:
            body["metadata"] = metadata
        return await api.post(
            f"/entries/{source_entry_id}/links",
            json=body,
            act_as=user_id,
        )

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
        user_id = _resolve_act_as_user_id()
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
        return await api.post("/staging", json=body, act_as=user_id)

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
        user_id = _resolve_act_as_user_id()
        params: dict = {"status": status}
        if target_path is not None:
            params["target_path"] = target_path
        if change_type is not None:
            params["change_type"] = change_type
        if since is not None:
            params["since"] = since
        return await api.get("/staging", params=params, act_as=user_id)

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
        user_id = _resolve_act_as_user_id()
        endpoint = f"/staging/{staging_id}/{action}"
        body: dict = {}
        if reason is not None:
            body["reason"] = reason
        return await api.post(endpoint, json=body if body else None, act_as=user_id)

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
        user_id = _resolve_act_as_user_id()
        return await api.post("/staging/process", act_as=user_id)

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
        # Invite redemption is unauthenticated by design — it is the flow
        # that mints the very first key for a new user. We pass an empty
        # api_key override (so no ``Authorization`` header is trusted) and
        # deliberately skip ``act_as`` since there is no bound user yet.
        return await api.post(
            "/invitations/redeem",
            json={
                "invite_code": invite_code,
                "token": token,
                "email": email,
                "display_name": display_name,
                "password": password,
            },
            api_key="",
        )

    # -------------------------------------------------------------------
    # Import tools
    # -------------------------------------------------------------------

    async def import_vault(
        path: str,
        preview_only: bool = False,
        exclude: list[str] | None = None,
        max_files: int = 500,
        source_vault: str | None = None,
        base_path: str | None = None,
    ) -> dict:
        """Import an Obsidian (or plain markdown) vault in a single call.

        The MCP server process walks the directory at `path`, collects every
        `.md` file (skipping `.obsidian/**` and `.trash/**` by default), reads
        the contents, then POSTs the batch to `/import` (or `/import/preview`
        if `preview_only=True`). The server handles YAML frontmatter parsing,
        wikilink / markdown-link extraction into `entry_links`, collision
        detection, and batch tracking.

        Parameters:
          path           — absolute filesystem path to a vault directory the
                           MCP server process can read directly. For
                           Docker-hosted MCP the path must live on a
                           bind-mounted volume (same caveat as
                           `upload_attachment`).
          preview_only   — when True, routes to `/import/preview` and returns
                           the collision report without writing anything.
          exclude        — additional glob patterns (relative to `path`) to
                           skip; always merged with `.obsidian/**` and
                           `.trash/**`.
          max_files      — safety limit; if the walk turns up more than this
                           many `.md` files, the tool returns an error dict
                           without sending anything to the API.
          source_vault   — provenance identifier stored on the import batch;
                           defaults to the vault directory name.
          base_path      — logical_path prefix applied to every file;
                           defaults to the vault directory name.

        Returns the server response from `/import` (containing `batch_id`,
        counts, and error list) or `/import/preview` (collision report).
        On client-side errors (bad path, nothing to import, exceeds
        max_files) returns `{"error": True, "detail": "..."}` without
        touching the server.
        """
        user_id = _resolve_act_as_user_id()
        try:
            from vault_parse import (  # type: ignore
                build_payloads,
                collect_md_files,
                resolve_exclude_patterns,
            )
        except ImportError as exc:
            return {
                "error": True,
                "detail": (
                    "Shared vault walker not found on MCP server. Expected "
                    "`tools/vault_parse.py` to be importable. "
                    f"({exc})"
                ),
            }

        vault_path = Path(path).expanduser().resolve()
        if not vault_path.is_dir():
            return {
                "error": True,
                "detail": f"Vault path does not exist or is not a directory: {vault_path}",
            }

        vault_name = vault_path.name
        effective_base = base_path if base_path is not None else vault_name
        effective_source = source_vault if source_vault is not None else vault_name

        exclude_patterns = resolve_exclude_patterns(exclude)

        # Walk in a thread so we don't block the event loop on large vaults.
        md_files = await asyncio.to_thread(collect_md_files, vault_path, exclude_patterns)

        if not md_files:
            return {
                "error": False,
                "files_analyzed": 0,
                "detail": "No .md files found under the vault path (after excludes).",
            }

        if len(md_files) > max_files:
            return {
                "error": True,
                "detail": (
                    f"Found {len(md_files)} files, which exceeds max_files={max_files}. "
                    f"Increase max_files or add exclude patterns to reduce the file count."
                ),
            }

        payloads, read_errors = await asyncio.to_thread(build_payloads, vault_path, md_files)

        if not payloads:
            return {
                "error": True,
                "detail": "No readable .md files in vault.",
                "read_errors": read_errors,
            }

        if preview_only:
            response = await api.post(
                "/import/preview",
                json={
                    "files": payloads,
                    "base_path": effective_base,
                },
                act_as=user_id,
            )
        else:
            response = await api.post(
                "/import",
                json={
                    "files": payloads,
                    "base_path": effective_base,
                    "source_vault": effective_source,
                    "collisions": [],
                },
                act_as=user_id,
            )

        # Surface client-side read errors alongside the server response so
        # callers see everything that went wrong in one payload.
        if read_errors and isinstance(response, dict):
            existing = response.get("errors") or []
            response["errors"] = list(existing) + read_errors

        return response

    # ``import_vault`` is a filesystem-walk tool — it only makes sense when
    # the MCP process can see the caller's vault directory directly. On
    # remote Render deploys the `tools/vault_parse.py` walker isn't shipped
    # (the MCP Dockerfile's `dockerContext: ./mcp` excludes repo-root
    # `tools/`), so we gate registration on the walker being importable.
    # Remote Co-work users get only `import_vault_from_blob`; local stdio
    # users (Claude Code / Desktop) keep `import_vault` alongside it.
    if _VAULT_PARSE_AVAILABLE:
        mcp.tool()(import_vault)

    @mcp.tool()
    async def import_vault_from_blob(
        blob_id: str,
        source_vault: str | None = None,
        base_path: str | None = None,
        excludes: list[str] | None = None,
    ) -> dict:
        """Import a vault tarball that has already been uploaded as a blob.

        This is the remote-friendly bulk import path — use it from Claude
        Co-work or any client that can't hand the MCP a local filesystem
        path. The full flow is:

            1. In bash, tar+gzip your vault:
                   tar czf /tmp/vault.tgz -C /path/to/vault .
               Keep the tarball under 25MB compressed (``MAX_VAULT_TARBALL_BYTES``);
               the server rejects larger uploads with 413.
            2. Call ``upload_attachment(path="/tmp/vault.tgz")`` and capture
               the ``blob_id`` from the response.
            3. Call ``import_vault_from_blob(blob_id=<blob_id>)`` — this
               tool. The API fetches the blob under your RLS scope, streams
               it through ``tarfile.extractfile()`` one member at a time,
               filters for ``.md`` files (skipping ``.obsidian/**`` and
               ``.trash/**`` by default), and runs the same frontmatter
               parsing, wikilink extraction, and governance pipeline as
               ``POST /import``. Typical completion: 10-30s for a ~1k-file
               vault. Single blocking call, no polling.
            4. Inspect ``list_staging`` to see what landed in the
               governance queue (or the published entries if your role is
               editor/admin).

        Parameters:
          blob_id       — UUID returned by ``upload_attachment``. Must be
                          visible to you under RLS (same org / scope).
          source_vault  — provenance identifier stored on the import batch.
                          Defaults to ``"cowork-upload"``.
          base_path     — ``logical_path`` prefix applied to every file.
                          Defaults to ``"cowork-upload"``.
          excludes      — additional glob patterns (relative to the vault
                          root inside the tarball) to skip; always merged
                          with the built-in ``.obsidian/**`` and
                          ``.trash/**`` defaults.

        Returns the server response from ``POST /import/vault-from-blob``:
          {
            "batch_id":   "<uuid>",
            "created":    <int>,
            "staged":     <int>,
            "linked":     <int>,
            "errors":     [...],
            ...
          }

        Notes:
          * On remote Render deploys this is the ONLY bulk-import path.
            The legacy ``import_vault(path)`` filesystem-walk tool is
            local-MCP-only (stdio transport with ``tools/vault_parse.py``
            on the server's sys.path) and will not appear in Co-work's
            tool list.
          * Tarballs over 25MB compressed return 413 — split the vault
            or raise ``MAX_VAULT_TARBALL_BYTES`` on the server.
          * Tarballs that decompress past 200MB also return 413 (zip-bomb
            guard).
        """
        user_id = _resolve_act_as_user_id()
        body: dict = {"blob_id": blob_id}
        if source_vault is not None:
            body["source_vault"] = source_vault
        if base_path is not None:
            body["base_path"] = base_path
        if excludes is not None:
            body["excludes"] = excludes
        return await api.post(
            "/import/vault-from-blob",
            json=body,
            act_as=user_id,
        )

    @mcp.tool()
    async def rollback_import(batch_id: str) -> dict:
        """Rollback an entire import batch.

        Archives all entries created by the batch, removes links and pending
        staging items. The batch status changes to 'rolled_back'.

        Use the batch_id returned from ``import_vault`` or
        ``import_vault_from_blob`` to identify which batch to roll back.
        Cannot roll back an already rolled-back batch (returns 409).
        """
        user_id = _resolve_act_as_user_id()
        return await api.delete(f"/import/{batch_id}", act_as=user_id)

    # -------------------------------------------------------------------
    # Attachment tools
    # -------------------------------------------------------------------

    @mcp.tool()
    async def upload_attachment(
        path: str | None = None,
        digest: bool = True,
        content_type: str | None = None,
        content_base64: str | None = None,
        filename: str | None = None,
    ) -> dict:
        """Upload a file to Brilliant and optionally digest it into a staged entry.

        Two mutually-exclusive transmission modes:

        * **Filesystem path** — `upload_attachment(path="/tmp/vault.tgz")`.
          Only works when the MCP server process can read the file
          (local stdio MCP, or Docker-hosted MCP with a bind-mounted
          volume). Remote deploys (Render) cannot see the client's
          filesystem — use `content_base64` instead.
        * **Inline base64 bytes** — `upload_attachment(
          content_base64="H4sIAA...=", filename="vault.tgz",
          content_type="application/gzip")`. The bytes travel in the
          tool-call JSON itself, so this works from any transport
          including remote Co-work. Pair with `filename` so the server
          records a sensible original name.

        Supply exactly one of `path` or `content_base64`. Supplying both
        (or neither) returns a 400-shaped error dict.

        Parameters:
          path            — absolute path to a local file. The MCP server
                            process must be able to read the file
                            directly (`open(path, 'rb')`). Mutually
                            exclusive with `content_base64`.
          digest          — when True and content_type resolves to
                            `application/pdf`, the upload triggers the
                            PDF digest pipeline. Ignored for non-PDF
                            uploads.
          content_type    — explicit MIME override. If None, derived
                            from the filename extension via
                            `mimetypes.guess_type`, falling back to
                            `application/octet-stream`.
          content_base64  — base64-encoded file bytes (standard
                            alphabet, padding optional). Mutually
                            exclusive with `path`.
          filename        — original filename to record on the blob.
                            Required with `content_base64`; ignored
                            with `path` (the path basename is used).

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
        user_id = _resolve_act_as_user_id()

        if path is not None and content_base64 is not None:
            return {
                "error": True,
                "status": 400,
                "detail": (
                    "upload_attachment: pass exactly one of `path` or "
                    "`content_base64`, not both"
                ),
            }
        if path is None and content_base64 is None:
            return {
                "error": True,
                "status": 400,
                "detail": (
                    "upload_attachment: must supply either `path` (local "
                    "file) or `content_base64` + `filename` (inline bytes)"
                ),
            }

        if content_base64 is not None:
            if not filename:
                return {
                    "error": True,
                    "status": 400,
                    "detail": (
                        "upload_attachment: `filename` is required when "
                        "passing `content_base64`"
                    ),
                }
            try:
                data = base64.b64decode(content_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                return {
                    "error": True,
                    "status": 400,
                    "detail": f"upload_attachment: invalid base64 content: {exc}",
                }
            upload_name = filename
        else:
            file_path = Path(path)
            if not file_path.is_file():
                return {
                    "error": True,
                    "status": 400,
                    "detail": f"File not found or not a regular file: {path}",
                }
            try:
                data = file_path.read_bytes()
            except OSError as exc:
                return {
                    "error": True,
                    "status": 400,
                    "detail": f"Could not read {path}: {exc}",
                }
            upload_name = file_path.name

        effective_ct = content_type
        if effective_ct is None:
            guessed, _ = mimetypes.guess_type(upload_name)
            effective_ct = guessed or "application/octet-stream"

        files = {"file": (upload_name, data, effective_ct)}
        params: dict = {"digest": "true" if digest else "false"}
        # The endpoint uses the `content_type` query param as an override
        # applied on top of the multipart part's own content-type. Pass
        # through whatever we resolved so the server's effective type
        # matches what we sent.
        params["content_type"] = effective_ct

        return await api.post_multipart(
            "/attachments",
            files=files,
            params=params,
            act_as=user_id,
        )

    # -------------------------------------------------------------------
    # Analytics tools (admin-only)
    # -------------------------------------------------------------------

    def _coerce_admin_error(result: dict) -> dict:
        """Convert a raw 403 BrilliantClient error dict into the documented shape.

        BrilliantClient returns {"error": True, "status": 403, "detail": ...} on
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
        user_id = _resolve_act_as_user_id()
        if kind == "top-entries":
            params: dict = {"since": since, "limit": limit}
            if actor_type is not None:
                params["actor_type"] = actor_type
            return _coerce_admin_error(
                await api.get("/analytics/top-entries", params=params, act_as=user_id)
            )

        if kind == "top-endpoints":
            params = {"since": since, "limit": limit}
            return _coerce_admin_error(
                await api.get("/analytics/top-endpoints", params=params, act_as=user_id)
            )

        if kind == "session-depth":
            params = {"since": since}
            if actor_id is not None:
                params["actor_id"] = actor_id
            return _coerce_admin_error(
                await api.get("/analytics/session-depth", params=params, act_as=user_id)
            )

        if kind == "summary":
            top_entries_params: dict = {"since": since, "limit": limit}
            if actor_type is not None:
                top_entries_params["actor_type"] = actor_type
            top_endpoints_params: dict = {"since": since, "limit": limit}
            session_depth_params: dict = {"since": since}
            if actor_id is not None:
                session_depth_params["actor_id"] = actor_id

            top_entries, top_endpoints, session_depth = await asyncio.gather(
                api.get("/analytics/top-entries", params=top_entries_params, act_as=user_id),
                api.get("/analytics/top-endpoints", params=top_endpoints_params, act_as=user_id),
                api.get("/analytics/session-depth", params=session_depth_params, act_as=user_id),
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
