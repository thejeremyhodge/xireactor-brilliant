# Roadmap

## Shipped (v0.1)

- Entry CRUD, full-text search, wiki-link traversal via recursive CTEs
- Multi-tenant organizations with row-level security (PostgreSQL RLS)
- 4-tier governance pipeline (auto-approve, notify, AI review, human review)
- Tier 3 AI reviewer via Anthropic API (opt-in via `ANTHROPIC_API_KEY`)
- KB-native escalation — pending reviews surface in `session_init` preamble
- MCP integration for Claude Co-work and Claude Code (18 tools, stdio + Streamable HTTP/OAuth 2.1)
- Obsidian vault import pipeline with preview, collision detection, and batch rollback
- Invite-based auth with API keys
- Permissions v2 — unified polymorphic `permissions` table (user + group principals), superseding the legacy entry/path permission tables (spec 0026)
- Comments subsystem — threaded comments with resolve/escalate workflow, author-kind tracking, and audit log integration (API-only; not exposed via MCP) (spec 0026)
- Content type registry — canonical types with alias support, server-side validation at submission time
- Render-time wiki-link resolution — GET `/entries/{id}` rewrites `[[slug]]` → `[Title](/kb/{id})` markdown links, with frontmatter strip for imported vault content (spec 0028)
- Write-path `entry_links` sync — POST / PUT on entries repopulates the `entry_links` table from `[[wiki-link]]` references so traversal and render stay in lockstep (spec 0030)
- Skill bundle — Claude Co-work skill with inbox/outbox workflow and KB-aware session bootstrap (spec 0029)
- Favicon + public branding polish for connector card rendering

## In Progress

- Web frontend (separate repo, React + Vite)
- Production-hardened `docker-compose.prod.yml` with reverse proxy example

## Ideas (not committed)

- Alternative LLM providers for Tier 3 reviewer (OpenAI, Bedrock, local models)
- Graph visualization of entry relationships
- CRDT-backed concurrent editing on overlapping paths
- Blob/attachment support for non-text entries

## Explicit Anti-Goals

This project is NOT trying to be:

- **A wiki** — no WYSIWYG editor, no page history UI (comments exist, but as a governance/review primitive, not as a discussion surface)
- **A notes app** — no Obsidian replacement, no PKM features
- **A SaaS product** — no hosted Brilliant Cloud, no subscription tiers
- **A general-purpose CMS** — scoped to structured context for AI agents
- **A knowledge graph** — entry links are practical navigation aids, not an ontology layer

If you need those things, Brilliant is the wrong tool. If you need a database-backed, API-first context layer that AI agents can safely read and write concurrently, you're in the right place.
