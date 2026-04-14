# xiReactor Brilliant

**v0.1 pre-release** — Shipped for public evaluation. APIs may change before v1.0.

> Context engineering infrastructure for institutional-grade teams.
> A multi-tenant knowledge base with API-first architecture, AI agent
> access via MCP, and tiered governance.

## What It Is

xiReactor Brilliant is a database-backed knowledge base for teams where AI agents and humans share institutional context. Built on PostgreSQL (row-level security, recursive CTEs) with a FastAPI REST API and MCP integration for Claude Co-work and Claude Code. Not a wiki, not a notes app — a structured context layer that gives every agent on your team the same institutional memory.

## Why

Teams using AI assistants lack a shared, governed context layer. Each agent session starts from scratch, re-discovering what others already know. "Context engineering" — giving AI agents the right institutional knowledge at the right time — is the missing infrastructure. Brilliant provides a single source of truth that agents and humans read from and write to, with governance tiers that keep quality high as the team scales.

## Personal-First Usage

Brilliant is designed for a single owner to build a core knowledge base first, then invite team members once the foundation is solid. You start alone — importing existing notes, structuring entries, proving the value of centralized context. When the KB is useful enough to share, you add users with scoped permissions. This is not a "set up a team on day one" product; it is a personal tool that grows into a team platform.

## Getting Started

Zero to first API call in under 10 minutes.

**Prerequisites:** Docker + Docker Compose. That's it.

### 1. Clone and configure

```bash
git clone https://github.com/thejeremyhodge/xireactor-brilliant.git
cd xireactor-brilliant
cp .env.sample .env
```

Open `.env` in your editor. At minimum, set:

- `ADMIN_EMAIL` — your email (used to log in via the API)
- `ADMIN_PASSWORD` — a strong password (replaces the `change-me-before-first-run` placeholder)
- `POSTGRES_PASSWORD` — change from the `dev` default if you care about isolation

Optional:

- `ANTHROPIC_API_KEY` — enables the Tier 3 AI reviewer for escalated staging items. If left blank, Tier 3 items fall through to human review (the default); everything else works fine.
- `ADMIN_API_KEY` — if set, the admin bootstraps with this exact key. If unset, one is auto-generated on first startup and printed to the API logs (see step 3).

### 2. Start the stack

```bash
docker compose up -d
```

Verify the API is up:

```bash
curl http://localhost:8010/health
# {"status":"ok"}
```

Postgres listens on `localhost:5442` if you want to inspect the database directly.

### 3. Get your API key

You have two paths depending on whether you want to run the smoke test or go straight to your own admin key.

**Option A — Run the demo (fastest path to a working request)**

The repo ships with seeded demo users and their API keys hardcoded in the end-to-end test. One command exercises the full flow — health check, auth, CRUD, governance, import, search — and leaves you with keys you can reuse:

```bash
bash tests/demo_e2e.sh
```

The demo uses these seeded keys directly:

- Admin: `bkai_adm1_testkey_admin`
- Editor: `bkai_edit_testkey_editor`
- Viewer: `bkai_view_testkey_viewer`
- Agent: `bkai_agnt_testkey_agent`

These are fine for local evaluation. Rotate them before exposing the API to anything you care about.

**Option B — Log in as the admin you configured**

On first startup, the API reads `ADMIN_EMAIL` / `ADMIN_PASSWORD` from `.env` and creates the admin user. If you did not set `ADMIN_API_KEY`, an auto-generated key is printed once to the API logs with a distinctive banner:

```bash
docker compose logs api | grep -A 3 "AUTO-GENERATED ADMIN API KEY"
```

If you missed the log (or restarted with an already-bootstrapped DB), mint a fresh session key by logging in:

```bash
curl -X POST http://localhost:8010/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"YOUR_ADMIN_PASSWORD"}'
```

Expected response:

```json
{
  "api_key": "bkai_abcd_ef0123456789abcdef012345",
  "user": {
    "id": "usr_xr_admin",
    "org_id": "org_demo",
    "display_name": "xiReactor Admin",
    "email": "you@example.com",
    "role": "admin",
    "department": "leadership",
    "is_active": true
  }
}
```

Use the `api_key` value as your Bearer token. (Note: login issues a fresh API key each time; older session keys remain valid until you revoke them.)

### 4. Your first API call

```bash
curl http://localhost:8010/entries \
  -H "Authorization: Bearer YOUR_API_KEY"
```

Expected response shape:

```json
{
  "total": 12,
  "entries": [
    { "id": "...", "title": "...", "logical_path": "...", "content_type": "...", "sensitivity": "..." }
  ]
}
```

If you get `401`, the key is wrong. If you get `200` with an empty list, the seed data did not load — check `docker compose logs db`.

### 5. Connect Claude (optional)

Brilliant ships with an MCP server (`mcp/`) and a Claude skill (`skill/`) for Claude Co-work and Claude Code. The MCP server is already running on `localhost:8011` after `docker compose up -d`. See `skill/SKILL.md` for the skill definition and `mcp/README.md` for remote-deployment notes.

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
| Concurrent writer stress test | 20–120 clients, ~178 ops/sec, 99.8%+ success, 0 data corruption (see below) |
| Production Docker compose | Dev only — bring your own reverse proxy |
| Web frontend | Planned (separate repo) |

### Concurrency Results

Scaling sweep on local Docker (MacBook), 60 seconds per tier:

| Metric | 20 clients | 40 clients | 80 clients | 120 clients |
|---|---|---|---|---|
| Throughput | 178 ops/s | 180 ops/s | 177 ops/s | 177 ops/s |
| Success rate | 100.0% | 99.9% | 99.9% | 99.8% |
| Read P95 | 124ms | 238ms | 471ms | 728ms |
| Update P95 | 249ms | 463ms | 921ms | 1462ms |
| Collision rate | 0.2% | 1.3% | 2.4% | 3.3% |
| Data integrity | 10/10 | 10/10 | 10/10 | 10/10 |
| 5xx errors | 0 | 0 | 0 | 0 |

Throughput stays flat as clients scale. Latency rises linearly (expected — single local Postgres). Zero data corruption at any tier. Production numbers on dedicated hardware would differ.

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
                                          │   21 migrations   │
                                          └───────────────────┘
```

**Key components:**
- **`api/`** — FastAPI backend with auth, entries, staging/governance, imports, links, permissions, comments, groups
- **`db/migrations/`** — 21 SQL migrations (core schema through entry-link backfill)
- **`mcp/`** — MCP server wrapping the API for Claude Co-work (local + remote modes)
- **`skill/`** — Claude Co-work skill definition + API reference
- **`tools/vault_import.py`** — CLI helper for Obsidian vault import

## Production Notes

This ships `docker-compose.yml` with dev defaults. For production:

- Put the API behind a reverse proxy with TLS (nginx, Caddy, or Traefik)
- Set strong `POSTGRES_PASSWORD` and `ADMIN_PASSWORD` — don't use the `.env.sample` defaults
- Back up the `pgdata` volume on a schedule
- Rotate `ADMIN_API_KEY` after initial setup, and revoke the seeded demo keys (`bkai_*_testkey_*`) before going live
- A polished `docker-compose.prod.yml` is planned for v1.1

## License

Apache 2.0 — see [LICENSE](LICENSE)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md)
