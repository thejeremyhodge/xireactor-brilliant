# xiReactor Cortex

**v0.1 pre-release** — Shipped for public evaluation. APIs may change before v1.0.

> Context engineering infrastructure for institutional-grade teams.
> A multi-tenant knowledge base with API-first architecture, AI agent
> access via MCP, and tiered governance.

## What It Is

xiReactor Cortex is a database-backed knowledge base for teams where AI agents and humans share institutional context. Built on PostgreSQL (row-level security, recursive CTEs) with a FastAPI REST API and MCP integration for Claude Co-work and Claude Code. Not a wiki, not a notes app — a structured context layer that gives every agent on your team the same institutional memory.

## Why

Teams using AI assistants lack a shared, governed context layer. Each agent session starts from scratch, re-discovering what others already know. "Context engineering" — giving AI agents the right institutional knowledge at the right time — is the missing infrastructure. Cortex provides a single source of truth that agents and humans read from and write to, with governance tiers that keep quality high as the team scales.

## Quick Start

```bash
git clone https://github.com/thejeremyhodge/xireactor-cortex.git
cd xireactor-cortex
cp .env.sample .env
# Edit .env: set ADMIN_PASSWORD
docker compose up -d
bash tests/demo_e2e.sh
```

## Personal-First Usage

Cortex is designed for a single owner to build a core knowledge base first, then invite team members once the foundation is solid. You start alone — importing existing notes, structuring entries, proving the value of centralized context. When the KB is useful enough to share, you add users with scoped permissions. This is not a "set up a team on day one" product; it is a personal tool that grows into a team platform.

## Features

| Feature | Status |
|---|---|
| Entry CRUD + full-text search | Shipped |
| 4-tier governance pipeline | Shipped |
| Row-level security (multi-tenant) | Shipped, single-org validated |
| MCP integration (Claude Co-work / Claude Code) | Shipped |
| Obsidian vault import | Shipped |
| Tier 3 AI reviewer (Anthropic) | Shipped, opt-in via ANTHROPIC_API_KEY |
| KB-native review escalation | Shipped |
| Production Docker compose | Dev only — bring your own reverse proxy |
| Web frontend | Planned (separate repo) |

## Architecture

```
┌─────────────────┐   ┌──────────────┐   ┌───────────────────┐
│  Claude Co-work  │──>│  MCP Server  │──>│                   │
│  (Skill/Anchor)  │   │  (tools.py)  │   │   FastAPI (api/)  │
└─────────────────┘   └──────────────┘   │                   │
                                          │  Routes:          │
┌─────────────────┐                       │  - entries        │
│  Frontend (GUI)  │─────────────────────>│  - staging        │
│  (separate repo) │                      │  - auth/users     │
└─────────────────┘                       │  - imports        │
                                          │  - links/index    │
                                          └────────┬──────────┘
                                                   │
                                          ┌────────v──────────┐
                                          │   PostgreSQL      │
                                          │   Row-Level       │
                                          │   Security (RLS)  │
                                          │   16 migrations   │
                                          └───────────────────┘
```

**Key components:**
- **`api/`** — FastAPI backend with auth, entries, staging/governance, imports, links, permissions
- **`db/migrations/`** — 16 SQL migrations (core schema through admin user bootstrap)
- **`mcp/`** — MCP server wrapping the API for Claude Co-work (local + remote modes)
- **`skill/`** — Claude Co-work skill definition + API reference
- **`tools/`** — CLI helpers (vault_import.py)

## Production Notes

This ships `docker-compose.yml` with dev defaults. For production:

- Put the API behind a reverse proxy with TLS (nginx, Caddy, or Traefik)
- Set strong `POSTGRES_PASSWORD` and `ADMIN_PASSWORD` — don't use the `.env.sample` defaults
- Back up the `pgdata` volume on a schedule
- Rotate `ADMIN_API_KEY` after initial setup
- A polished `docker-compose.prod.yml` is planned for v1.1

## License

Apache 2.0 — see [LICENSE](LICENSE)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md)
