<p align="center">
  <img src=".github/assets/logo.svg" width="140" alt="Brilliant logo" />
</p>

<h1 align="center">xiReactor Brilliant</h1>

<p align="center">
  <strong>Shared context that compounds.</strong>
</p>

<p align="center">
  <a href="https://github.com/thejeremyhodge/xireactor-brilliant/releases"><img src="https://img.shields.io/github/v/release/thejeremyhodge/xireactor-brilliant?style=flat&color=4558c9&label=release" alt="Latest release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/thejeremyhodge/xireactor-brilliant?style=flat" alt="License"></a>
  <a href="https://github.com/thejeremyhodge/xireactor-brilliant/actions/workflows/installer-smoke.yml"><img src="https://img.shields.io/github/actions/workflow/status/thejeremyhodge/xireactor-brilliant/installer-smoke.yml?branch=main&style=flat&label=installer%20smoke" alt="Installer smoke test"></a>
  <a href="https://github.com/thejeremyhodge/xireactor-brilliant/commits/main"><img src="https://img.shields.io/github/last-commit/thejeremyhodge/xireactor-brilliant?style=flat" alt="Last commit"></a>
  <a href="https://github.com/thejeremyhodge/xireactor-brilliant/stargazers"><img src="https://img.shields.io/github/stars/thejeremyhodge/xireactor-brilliant?style=flat&color=yellow" alt="Stars"></a>
</p>

<p align="center">
  <a href="#what-it-is">What It Is</a> •
  <a href="#getting-started">Get Started</a> •
  <a href="ARCHITECTURE.md">Architecture</a> •
  <a href="mcp/README.md">MCP</a> •
  <a href="ROADMAP.md">Roadmap</a> •
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

---

> **v0.4.0 pre-release** — Shipped for public evaluation. APIs may change before v1.0.
>
> Context engineering infrastructure for institutional-grade teams.
> A multi-tenant knowledge base with API-first architecture, AI agent
> access via MCP, and tiered governance.

## New in v0.4.0

- **`session_init` density manifest** — every Claude session now bootstraps on a compact ≤ 2K-token manifest instead of a ~46K-token index dump. Agents drill into paths/entries on demand via `get_index` / `get_entry` / `get_neighbors`. **Breaking** for anything that read `system_entries[].content` directly — see the CHANGELOG for the migration pattern.
- **`import_vault(path)` MCP tool** — one MCP call imports an Obsidian (or plain markdown) vault by filesystem path. The server parses YAML frontmatter into entry fields (tags, sensitivity, content_type, department) and extracts `[[wikilinks]]` / markdown links into `entry_links`.
- **Fuzzy search fallback** — `search_entries(q=..., fuzzy=true)` retries via `pg_trgm` word-similarity when the FTS path returns zero rows, so `"klaude"` still surfaces `"claude"` entries. Off by default; existing search behavior unchanged.
- **`suggest_tags(content)` MCP tool** — deterministic, RLS-scoped ranking over the org's existing tag vocabulary. Use it when writing a new entry to reuse tags people are already searching by.

## What It Is

xiReactor Brilliant is a database-backed knowledge base for teams where AI agents and humans share institutional context. Built on PostgreSQL (row-level security, recursive CTEs) with a FastAPI REST API and MCP integration for Claude Co-work and Claude Code. Not a wiki, not a notes app — a structured context layer that gives every agent on your team the same institutional memory.

## Why

Teams using AI assistants lack a shared, governed context layer. Each agent session starts from scratch, re-discovering what others already know. "Context engineering" — giving AI agents the right institutional knowledge at the right time — is the missing infrastructure. Brilliant provides a single source of truth that agents and humans read from and write to, with governance tiers that keep quality high as the team scales.

## Personal-First Usage

Brilliant is designed for a single owner to build a core knowledge base first, then invite team members once the foundation is solid. You start alone — importing existing notes, structuring entries, proving the value of centralized context. When the KB is useful enough to share, you add users with scoped permissions. This is not a "set up a team on day one" product; it is a personal tool that grows into a team platform.

## Getting Started

Zero to working API in under 5 minutes.

**Prerequisites:** a Mac or Linux box with `curl`. If Docker isn't installed, the installer will install it (pass `--no-install-docker` to opt out).

### One-liner install

Pipe the installer into `bash` from any directory — it will detect that it isn't inside a clone and self-clone the latest release tag into `./xireactor-brilliant` before continuing:

```bash
curl -fsSL https://raw.githubusercontent.com/thejeremyhodge/xireactor-brilliant/main/install.sh \
  | bash -s -- --admin-email you@example.com
```

The installer prints a phased plan, stands the stack up, and finishes with a summary banner containing your admin API key. The same key is written to `./brilliant-credentials.txt` (mode 600, inside the cloned directory).

To pin a specific version or track `main`, pass `--ref`:

```bash
curl -fsSL https://raw.githubusercontent.com/thejeremyhodge/xireactor-brilliant/main/install.sh \
  | bash -s -- --admin-email you@example.com --ref v0.3.0
```

To pick a different clone target, pass `--dir /path/to/target`.

### Manual install (pre-cloned)

If you prefer to inspect the repo before running anything, clone it first. When the installer is invoked from inside a clone it runs in place and never re-clones:

```bash
git clone https://github.com/thejeremyhodge/xireactor-brilliant.git
cd xireactor-brilliant
./install.sh --admin-email you@example.com
```

Dry-run the plan first if you want to see what it will do:

```bash
./install.sh --dry-run --admin-email you@example.com
```

All flags:

```bash
./install.sh --help
```

### First API call

```bash
curl -H "Authorization: Bearer $(cat ./brilliant-credentials.txt)" \
  http://localhost:8010/entries
```

Expected response shape:

```json
{
  "entries": [
    { "id": "...", "title": "...", "logical_path": "...", "content_type": "...", "sensitivity": "..." }
  ],
  "total": 12,
  "limit": 50,
  "offset": 0
}
```

If you get `401`, the key is wrong. If you get `200` with an empty list, the seed data did not load — check `docker compose logs db`.

### Demo seed (optional)

The repo ships with seeded demo users and their API keys hardcoded in the end-to-end test. One command exercises the full flow — health check, auth, CRUD, governance, import, search — and leaves you with keys you can reuse:

```bash
bash tests/demo_e2e.sh
```

The script targets `http://localhost:8010` by default. Override with `BASE_URL` if you run the stack on a different host or port.

The demo uses these seeded keys directly:

- Admin: `bkai_adm1_testkey_admin`
- Editor: `bkai_edit_testkey_editor`
- Viewer: `bkai_view_testkey_viewer`
- Agent: `bkai_agnt_testkey_agent`

These are fine for local evaluation. Rotate them before exposing the API to anything you care about.

### Connect Claude (optional)

Brilliant ships with an MCP server (`mcp/`) and a Claude skill (`skill/`) for Claude Co-work and Claude Code. The MCP server is already running on `localhost:8011` after the installer finishes. See `skill/SKILL.md` for the skill definition and `mcp/README.md` for remote-deployment notes.

<details>
<summary><strong>Manual install</strong> (without the one-liner)</summary>

Prefer the one-liner unless you want to audit each step or are running on a host where automated Docker install isn't safe.

**1. Clone and configure**

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
- `ADMIN_API_KEY` — if set, the admin bootstraps with this exact key. If unset, one is auto-generated on first startup and printed to the API logs.

**2. Start the stack**

```bash
docker compose up -d
curl http://localhost:8010/health
# {"status":"ok"}
```

Postgres listens on `localhost:5442` if you want to inspect the database directly.

**3. Get your admin API key**

If you did not set `ADMIN_API_KEY`, an auto-generated key is printed once to the API logs with a distinctive banner:

```bash
docker compose logs api | grep -A 3 "AUTO-GENERATED ADMIN API KEY"
```

If you missed the log (or restarted with an already-bootstrapped DB), mint a fresh session key by logging in:

```bash
curl -X POST http://localhost:8010/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"YOUR_ADMIN_PASSWORD"}'
```

Use the returned `api_key` value as your Bearer token. (Login issues a fresh API key each time; older session keys remain valid until you revoke them.)

</details>

## Features

| Feature | Status |
|---|---|
| Entry CRUD + full-text search | Shipped |
| 4-tier governance pipeline | Shipped |
| Row-level security (multi-tenant) | Shipped, single-org validated |
| MCP integration (Claude Co-work / Claude Code) | Shipped |
| Obsidian vault import | Shipped |
| File attachments (PDF digest, S3-compatible storage, dedup by content hash) | Shipped — see [docs/ATTACHMENTS.md](docs/ATTACHMENTS.md) |
| Tier 3 AI reviewer (Anthropic) | Shipped, opt-in via ANTHROPIC_API_KEY |
| KB-native review escalation | Shipped |
| Observability + MCP usage analytics | Shipped in v0.3.0 — see [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md) |
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
                                          │   25 migrations   │
                                          └───────────────────┘
```

**Key components:**
- **`api/`** — FastAPI backend with auth, entries, staging/governance, imports, links, permissions, comments, groups
- **`db/migrations/`** — 25 SQL migrations (core schema through attachment digest + access-log analytics)
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
