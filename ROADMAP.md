# Roadmap

## Shipped (v0.1)

- Entry CRUD, full-text search, wiki-link traversal via recursive CTEs
- Multi-tenant organizations with row-level security (PostgreSQL RLS)
- 4-tier governance pipeline (auto-approve, notify, AI review, human review)
- Tier 3 AI reviewer via Anthropic API (opt-in via `ANTHROPIC_API_KEY`)
- KB-native escalation — pending reviews surface in `session_init` preamble
- MCP integration for Claude Co-work and Claude Code
- Obsidian vault import pipeline with preview, collision detection, and batch rollback
- Invite-based auth with API keys
- Granular entry-level and path-level permissions

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

- **A wiki** — no WYSIWYG editor, no comments, no page history UI
- **A notes app** — no Obsidian replacement, no PKM features
- **A SaaS product** — no hosted Cortex Cloud, no subscription tiers
- **A general-purpose CMS** — scoped to structured context for AI agents
- **A knowledge graph** — entry links are practical navigation aids, not an ontology layer

If you need those things, Cortex is the wrong tool. If you need a database-backed, API-first context layer that AI agents can safely read and write concurrently, you're in the right place.
