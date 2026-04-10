# Architecture

## Overview

xiReactor Cortex is a Postgres-backed institutional knowledge base with a FastAPI REST layer, a four-tier governance pipeline for write control, row-level security for multi-tenant isolation, and an MCP integration layer that exposes the full tool surface to Claude Co-work and Claude Code. All agent writes are routed through a staging table; promotion to the live KB is gated by governance tier (auto-approve, conflict detection, AI review, or human approval).

## Components

- **api/** -- FastAPI application. Bearer-token auth via API keys (`api/auth.py`), entry CRUD, full-text and semantic search, staging/governance pipeline (`api/routes/staging.py`), session bootstrap (`api/routes/session.py`), vault import with batch rollback, invite-based onboarding.
- **db/** -- 16 sequential migrations. Core schema, typed wiki-links, append-only versioning, governance staging, RLS policies, OAuth token store, content type registry, granular ACLs (entry-level and path-level), import batch tracking. Recursive CTE traversal for graph queries -- no AGE/graph extension dependency.
- **mcp/** -- MCP server for Claude integration. `server.py` (stdio transport for Claude Code/Desktop), `remote_server.py` (Streamable HTTP + OAuth 2.1 for Claude Co-work), `tools.py` (shared tool definitions registered on either server), `client.py` (HTTP client to the REST API).
- **skill/** -- Claude Co-work skill bundle. `SKILL.md` instructions and `references/` directory with API reference docs. Bootstraps Co-work sessions with KB-aware context.
- **tools/** -- CLI helpers. `vault_import.py` for bulk Obsidian vault ingestion.

## Data Model

Key tables from `db/migrations/`:

| Table | Purpose |
|---|---|
| `organizations` | Tenant container. All data is scoped by `org_id`. |
| `users` | Google Workspace role model (admin, editor, commenter, viewer). Includes `trust_weight` and department. |
| `api_keys` | Per-user Bearer tokens. Three types: `interactive`, `agent`, `api_integration`. bcrypt-hashed, prefix-indexed. |
| `entries` | Core KB content. Markdown body, logical path, content type, sensitivity level, tsvector full-text search, pgvector embedding (1536d), two-layer metadata (tags + domain_meta JSONB). |
| `entry_versions` | Append-only version history. Immutable -- no UPDATE or DELETE policies. |
| `entry_links` | Typed directed edges between entries. Six link types: `relates_to`, `supersedes`, `contradicts`, `depends_on`, `part_of`, `tagged_with`. |
| `staging` | Governance induction queue. All agent writes land here before promotion. Tracks tier, status, evaluator decisions. |
| `audit_log` | Append-only mutation log. Written by API server (admin role), not directly by users. |
| `content_type_registry` | Canonical content types with alias support. Server-side validation at submission time. |
| `entry_permissions` | Per-entry ACL grants (entry_id, user_id, role). Additive -- widens access beyond org-wide role. |
| `path_permissions` | Pattern-based ACL grants (path prefix, user_id, role). Enables subtree-level access control. |
| `project_assignments` | User-to-project access grants. Used in RLS subqueries for project-scoped visibility. |
| `import_batches` | Tracks vault imports as rollback-able units. FK added to entries, staging, and entry_links for batch traceability. |

## Row-Level Security

Every table with user data has RLS enabled and forced (even for table owners). Tenant isolation is enforced by matching `org_id = current_setting('app.org_id')` in every policy. The API layer sets four session variables at connection time via `SET LOCAL` -- `app.user_id`, `app.org_id`, `app.role`, `app.department` -- then switches to the appropriate Postgres role (`kb_admin`, `kb_editor`, `kb_commenter`, `kb_viewer`, `kb_agent`) with `SET LOCAL ROLE`. Using `SET LOCAL` (not `SET`) is critical in pooled connections: it scopes the role switch to the current transaction and prevents poisoning reused connections. See `api/database.py` and `db/migrations/004_rls.sql`.

## Governance Pipeline (4 Tiers)

All writes from agent-type API keys are routed through the `staging` table. Governance tier is assigned at submission time by `_assign_governance_tier()` in `api/routes/staging.py` based on change type, content sensitivity, source, and user role.

| Tier | Trigger | Resolution |
|---|---|---|
| **Tier 1** | Creates (non-sensitive), appends, links, tags, admin/editor web_ui writes | Auto-approve. Committed synchronously; response includes `promoted_entry_id`. |
| **Tier 2** | Updates and modifications on non-sensitive content | Auto-approve with inline conflict detection (staleness, duplicate, content hash checks). Clean items auto-approve; conflicts escalate to Tier 3. |
| **Tier 3** | High-sensitivity content (`system`, `strategic`), Tier 2 escalations | AI reviewer evaluates. Stays `pending` until batch processing or manual review. |
| **Tier 4** | Deletions, sensitivity changes, governance rule modifications | Human-only. Requires explicit approve/reject via `review_staging` endpoint. |

See `db/migrations/012_governance_4tier.sql` for the constraint definition and `api/routes/staging.py` for the full tier assignment and processing logic.

## AI Reviewer (Tier 3)

Implemented in `api/services/ai_reviewer.py`. When `process_staging` encounters a Tier 3 item, it calls `review_staging_item()` which:

1. Fetches 3-5 related entries by logical path prefix and tag overlap for context.
2. Sends the proposed change + related context to the Anthropic API (claude-sonnet-4-6, 1024 max tokens) with a system prompt that includes the four-tier governance rules verbatim.
3. Parses a structured JSON response: `{action, reasoning, confidence}`.
4. Enforces a confidence floor: any result with `confidence < 0.7` is overridden to `escalate` regardless of the stated action.
5. Fails safe on all error paths -- missing `ANTHROPIC_API_KEY`, API errors, malformed responses, and parse failures all return `escalate`.

The reviewer never auto-approves on ambiguity. Invalid actions and low-confidence results always escalate to Tier 4 (human review).

## KB-Native Escalation

Pending Tier 3+ staging items surface in the `session_init` response (`api/routes/session.py`). The `/session-init` endpoint queries staging for items where `status = 'pending' AND governance_tier >= 3`, returning up to 5 item previews with target path, change type, submitter, and age in hours. This means every agent session start includes a governance check -- no SMTP, Slack, or webhook infrastructure required. The KB delivers its own governance signals through the session bootstrap preamble.

## Auth Model

Authentication uses Bearer tokens (API keys) via `api/auth.py`. The flow:

1. Extract Bearer token from `Authorization` header.
2. Look up `api_keys` by `key_prefix` (first 9 chars, e.g. `bkai_XXXX`).
3. bcrypt-verify the full token against `key_hash`.
4. Join to `users` for role, department, org_id.
5. Map `key_type` to `source`: `interactive` -> `web_ui`, `agent` -> `agent`, `api_integration` -> `api`.
6. Return `UserContext` (id, org_id, display_name, role, department, source, key_type).

Agent keys are write-restricted: they cannot INSERT/UPDATE/DELETE on `entries` directly. All agent writes go through staging. Interactive and API integration keys can write directly (still subject to RLS).

## MCP Integration

Two MCP server modes serve different clients:

- **`mcp/server.py`** -- stdio transport for Claude Code and Claude Desktop. Minimal setup: creates a `FastMCP` instance, registers tools, runs via stdio.
- **`mcp/remote_server.py`** -- Streamable HTTP transport with OAuth 2.1 for Claude Co-work. Implements Dynamic Client Registration (DCR) so Co-work can self-register, then standard `authorization_code` + PKCE flow. OAuth state persists in PostgreSQL. CORS configured for `claude.ai` origins.

Both servers register the same 18 tools from `mcp/tools.py`:

**Read:** `search_entries`, `get_entry`, `get_index`, `get_types`, `get_neighbors`, `session_init`
**Write:** `create_entry`, `update_entry`, `delete_entry`, `append_entry`, `create_link`
**Governance:** `submit_staging`, `list_staging`, `review_staging`, `process_staging`
**Onboarding:** `redeem_invite`
**Import:** `import_vault`, `rollback_import`

Tools are thin wrappers over `CortexClient` (`mcp/client.py`), which makes HTTP calls to the FastAPI REST layer. The MCP layer adds no business logic -- it translates MCP tool calls to REST requests.

## Multi-Tenancy

Row-level multi-tenancy via `org_id` on every data table. A single deployment hosts multiple organizations with strict isolation enforced at the Postgres level through RLS policies (not application-level filtering). Every query passes through `org_id = current_setting('app.org_id')` predicates. Granular permissions layer on top: Google Workspace-style org roles (admin/editor/commenter/viewer) + optional per-entry and per-path ACL grants that can only widen access, never restrict it beyond the sensitivity ceiling. See `db/migrations/004_rls.sql` and `db/migrations/011_entry_permissions.sql`.
