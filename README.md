<p align="center">
  <img src=".github/assets/logo.svg" width="140" alt="Brilliant logo" />
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/title-dark.svg">
    <img src=".github/assets/title-light.svg" width="520" alt="xiReactor / Brilliant" />
  </picture>
</p>

<h2 align="center">One shared knowledge base for your whole AI-enabled team</h2>

<p align="center">
  Your people and your agents — reading, writing, and growing the same knowledge base,<br/>
  with governance and permissions built in. Every conversation, decision, and document<br/>
  compounds into leverage your team keeps from day one.
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
  <a href="#deploy-to-render">Deploy to Render</a> •
  <a href="#self-host-with-installsh">Self-host</a> •
  <a href="#connect-claude">Connect Claude</a> •
  <a href="ARCHITECTURE.md">Architecture</a> •
  <a href="CHANGELOG.md">Changelog</a> •
  <a href="ROADMAP.md">Roadmap</a>
</p>

---

## What It Is

Brilliant is a database-backed knowledge base for teams where AI agents and humans share institutional context. Built on PostgreSQL (row-level security, recursive CTEs) with a FastAPI REST API and MCP integration for Claude Co-work and Claude Code. Not a wiki, not a notes app — a structured context layer that gives every agent on your team the same institutional memory.

### Why

Teams using AI assistants lack a shared, governed context layer. Each agent session starts from scratch, re-discovering what others already know. "Context engineering" — giving AI agents the right institutional knowledge at the right time — is the missing infrastructure. Brilliant provides a single source of truth that agents and humans read from and write to, with governance tiers that keep quality high as the team scales.

### Personal-first, team-ready

Brilliant is designed for a single owner to build a core knowledge base first, then invite team members once the foundation is solid. You start alone — importing existing notes, structuring entries, proving the value of centralized context. When the KB is useful enough to share, you add users with scoped permissions. This is not a "set up a team on day one" product; it is a personal tool that grows into a team platform.

---

## Deploy to Render

**Fastest path to a live, team-ready stack — one click, ~3 minutes, no Docker or Postgres know-how required.**

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/thejeremyhodge/xireactor-brilliant)

Render reads `render.yaml` from the repo root and provisions:

- `brilliant-api` — FastAPI service (Starter)
- `brilliant-mcp` — remote MCP server for Claude (Starter)
- `brilliant-db` — managed Postgres (Basic-256mb)
- 1 GB persistent disk attached to the api service at `/data` (attachment blobs)

**Cost:** ~$20–25/mo all-in (two Starter web services + Basic-256mb Postgres + 1 GB disk). Free tier is not supported — Postgres deletes after 30 days, there are no persistent disks for attachments, and idle spin-down makes MCP unusable.

### End-to-end flow

1. **Click the button.** Render prompts for `ADMIN_EMAIL` — the only user-provided input. Password, API key, and OAuth client credentials are all captured after deploy.
2. **Wait ~3 minutes** while Render builds the two Docker images, provisions the database, runs migrations (via `preDeployCommand`), and boots the services.
3. **Visit the api service URL.** The root route redirects to `/setup` because the first-run latch is still false.
4. **Choose a password** on `/setup`. Submit → credentials page renders inline with **six fields**: admin email, API key, OAuth `client_id`, OAuth `client_secret`, MCP connector URL, and a login URL. Copy buttons + a `brilliant-credentials.txt` download save everything in one click. The `/setup` route then 404s forever.
5. **Connect Claude Co-work** via custom connector — paste four fields: a name, the MCP URL, the `client_id`, and the `client_secret`. Claude opens a browser tab to `/oauth/login`; enter the admin email + password. Claude flashes "connected" and all MCP tool calls are now scoped to that user's RLS context.

### Lost your API key?

Visit `https://<your-api-host>/auth/login` and sign in. Signing in **rotates the API key** — all prior keys are invalidated, a fresh one is issued, and the six-field `brilliant-credentials.txt` download is offered. This doubles as a panic button if you suspect a key leaked. Check "Also rotate OAuth client secret" to rotate both atomically.

### Security model (short form)

Three independent gates protect the knowledge base — breaking any one does not grant access:

1. **Pre-registered OAuth client** — `client_id` + `client_secret` from `/setup`. Dynamic Client Registration is disabled; strangers who discover the MCP URL cannot self-register.
2. **User login at `/authorize`** — the MCP redirects to the api's `/oauth/login` page; handoff back to the MCP is HMAC-SHA256 signed with a Render-generated secret.
3. **Per-user RLS via `X-Act-As-User`** — the MCP uses a service-role key; the header is honored only from service keys. Postgres RLS enforces per-user scope on every query. Access logs record the acting user.

Full write-up in [ARCHITECTURE.md](ARCHITECTURE.md#security-model).

---

## Self-host with install.sh

Zero to working API in under 5 minutes on a Mac or Linux box. **Prerequisite:** `curl`. If Docker isn't installed, the installer will install it (pass `--no-install-docker` to opt out).

> **Note on Claude Co-work:** Co-work's custom-connector modal requires an HTTPS URL reachable from Anthropic's cloud. Localhost URLs are rejected. For a local install, connect via Claude Desktop + `mcp-remote` or expose the stack through a public tunnel (ngrok, cloudflared).

### One-liner

Pipe the installer into `bash` — it will detect that it isn't inside a clone and self-clone the latest release tag into `./xireactor-brilliant` before continuing:

```bash
curl -fsSL https://raw.githubusercontent.com/thejeremyhodge/xireactor-brilliant/main/install.sh \
  | bash
```

The installer stands up the stack and opens `/setup` in your browser; complete the form and download `brilliant-credentials.txt`. Pass `--ref <tag|branch>` to pin a version, `--dir <path>` to clone elsewhere, `--help` for all flags.

### Pre-cloned

If you prefer to inspect the repo first:

```bash
git clone https://github.com/thejeremyhodge/xireactor-brilliant.git
cd xireactor-brilliant
./install.sh
```

Use `./install.sh --dry-run` to preview the plan without running it.

### Headless (VPS / CI)

SSH-tunnel path (recommended for VPS operators with a workstation browser):

```bash
./install.sh --headless
# then on your workstation:
ssh -L 8010:localhost:8010 user@your-vps-host
# open http://localhost:8010/setup
```

Fully scripted (CI, no TTY):

```bash
./install.sh --admin-email you@example.com
# Prompts for password twice on a TTY, then auto-fetches brilliant-credentials.txt.

# CI-only escape valve (password visible in ps/shell history):
./install.sh --admin-email you@example.com --admin-password 'CHANGE_ME'
```

If the credentials file write fails or you lose it, recover any time:

```bash
curl -H 'Authorization: Bearer YOUR_ADMIN_API_KEY' \
  http://localhost:8010/credentials > brilliant-credentials.txt
```

### First API call

```bash
curl -H "Authorization: Bearer YOUR_ADMIN_API_KEY" \
  http://localhost:8010/entries
```

`401` → key is wrong. `200` with empty `entries` → seed data did not load (`docker compose logs db`).

### Bulk-import an existing vault

After setup, visit `http://localhost:8010/import/vault` and drop a `.tgz` / `.tar.gz` / `.zip` tarball of an Obsidian (or plain-markdown) vault. Server-side parse runs the same pipeline as the `import_vault` MCP tool, renders `{created, staged, batch_id}` inline with the rollback command. This is the canonical bulk path — it bypasses Claude's per-turn output cap and the Co-work bash sandbox.

<details>
<summary><strong>Manual Docker install (audit each step)</strong></summary>

```bash
git clone https://github.com/thejeremyhodge/xireactor-brilliant.git
cd xireactor-brilliant
cp .env.sample .env
# Edit .env: ADMIN_EMAIL, ADMIN_PASSWORD, POSTGRES_PASSWORD
# Optional: ANTHROPIC_API_KEY for Tier 3 AI reviewer, ADMIN_API_KEY to pin the admin key
docker compose up -d
curl http://localhost:8010/health
```

If `ADMIN_API_KEY` is unset, the auto-generated key is printed once to the API logs:

```bash
docker compose logs api | grep -A 3 "AUTO-GENERATED ADMIN API KEY"
```

Lost the log? Mint a fresh session key via `POST /auth/login` with your admin email + password.

</details>

---

## Connect Claude

Brilliant ships with an MCP server (`mcp/`) and a Claude skill (`skill/`). After the installer finishes, the MCP server is running on `localhost:8011`.

- **Claude Co-work:** use the Deploy-to-Render path above (Co-work requires HTTPS + remote host)
- **Claude Code:** local MCP via `skill/` — see [skill/SKILL.md](skill/SKILL.md)
- **Claude Desktop:** local MCP via `mcp-remote` bridge — see [mcp/README.md](mcp/README.md)

### Demo seed (optional)

```bash
bash tests/demo_e2e.sh
```

Exercises the full flow (health, auth, CRUD, governance, import, search) against `http://localhost:8010`. Seeded demo keys (`bkai_adm1_testkey_admin`, `bkai_edit_testkey_editor`, `bkai_view_testkey_viewer`, `bkai_agnt_testkey_agent`) are fine for local evaluation — rotate before exposing anything.

---

## Features

| Feature | Status |
|---|---|
| Entry CRUD + full-text search | Shipped |
| 4-tier governance pipeline | Shipped |
| Row-level security (multi-tenant) | Shipped, single-org validated |
| MCP integration (Claude Co-work / Code / Desktop) | Shipped |
| OAuth 3-gate authentication (user-bound, per-user RLS) | Shipped |
| Obsidian vault import (browser + MCP) | Shipped |
| File attachments (PDF digest, S3-compatible, content-hash dedup) | Shipped — see [docs/ATTACHMENTS.md](docs/ATTACHMENTS.md) |
| Tier 3 AI reviewer (Anthropic) | Shipped, opt-in via `ANTHROPIC_API_KEY` |
| Observability + MCP usage analytics | Shipped — see [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md) |
| Production Docker compose | Dev-only; bring your own reverse proxy |
| Web frontend | Planned (separate repo) |

Concurrent-writer stress results (20–120 clients, flat ~178 ops/s, 99.8%+ success, zero data corruption) and detailed benchmarks live in [ARCHITECTURE.md](ARCHITECTURE.md#concurrency-results).

---

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
                                          └───────────────────┘
```

- **`api/`** — FastAPI backend (auth, entries, staging/governance, imports, links, permissions, comments, groups)
- **`db/migrations/`** — SQL migrations: core schema → RLS → governance → permissions v2 → attachments → OAuth user-binding
- **`mcp/`** — MCP server wrapping the API (local + remote modes)
- **`skill/`** — Claude skill definition + API reference
- **`tools/vault_import.py`** — CLI helper for Obsidian vault import

Full component boundaries, data flow, and design decisions: [ARCHITECTURE.md](ARCHITECTURE.md).

---

## What's New

See [CHANGELOG.md](CHANGELOG.md) for a full release history. The [releases page](https://github.com/thejeremyhodge/xireactor-brilliant/releases) tags each cut with notes. Recent headline shipments:

- **OAuth 3-gate authentication** — pre-registered `client_id`/`client_secret`, user login at `/oauth/login`, per-user RLS via `X-Act-As-User`. DCR disabled on the MCP.
- **Browser vault upload** — first-class `/import/vault` page for bulk `.tgz` / `.tar.gz` / `.zip` imports, bypassing MCP byte limits.
- **Six-field credentials surface** — `/setup` and `/auth/login` recovery both render all six credentials with copy buttons + `brilliant-credentials.txt` download.
- **Tag triangulation** — `session_init.manifest.tags_top`, `list_tags`, `get_tag_neighbors`, multi-tag AND filtering in `search_entries`.
- **Density manifest** — every Claude session bootstraps on a compact ≤ 2K-token manifest; agents drill in on demand.
- **Render one-click deploy** — `render.yaml` blueprint with auto-provisioned Postgres + persistent disk + HMAC handoff secret.

---

## Production notes

`docker-compose.yml` ships with dev defaults. For production: put the API behind a reverse proxy with TLS (nginx, Caddy, Traefik), use strong `POSTGRES_PASSWORD` / `ADMIN_PASSWORD`, back up the `pgdata` volume, rotate `ADMIN_API_KEY` after setup, and revoke the seeded demo keys (`bkai_*_testkey_*`). A polished `docker-compose.prod.yml` is planned.

## License

Apache 2.0 — see [LICENSE](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).
