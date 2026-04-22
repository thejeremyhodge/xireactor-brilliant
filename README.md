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
  <a href="#getting-started">Get Started</a> •
  <a href="ARCHITECTURE.md">Architecture</a> •
  <a href="mcp/README.md">MCP</a> •
  <a href="ROADMAP.md">Roadmap</a> •
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

---

> **v0.5.0 pre-release** — Shipped for public evaluation. APIs may change before v1.0.
>
> Context engineering infrastructure for institutional-grade teams.
> A multi-tenant knowledge base with API-first architecture, AI agent
> access via MCP, and tiered governance.

## New in v0.5.0

- **OAuth user-bound authentication** — three-gate security model on the remote MCP server. Gate 1: pre-registered `client_id`/`client_secret` (Dynamic Client Registration is **disabled**; strangers who discover the public MCP URL cannot self-register). Gate 2: user login at the api-hosted `/oauth/login` page. Gate 3: per-user RLS — the MCP authenticates with a `key_type='service'` api key and carries `X-Act-As-User: <user_id>` on every tool call; non-service keys presenting that header → 403. The access log records the acting user, not the service identity. **Breaking:** every existing Claude Co-work connector must be re-provisioned with the `client_id` + `client_secret` shown on `/setup` (or recovered via `/auth/login`). See the **Security model** subsection under Deploy-to-Render for the full defense-in-depth write-up.
- **Browser vault upload at `/import/vault`** — first-class HTML page + `POST /import/vault-upload` multipart endpoint for bulk-importing an Obsidian (or plain-markdown) vault straight from the browser. Accepts `.tgz` / `.tar.gz` / `.zip` tarballs (magic-byte dispatch), bypasses Claude's per-turn output cap, the Co-work bash sandbox allowlist, and the MCP protocol entirely. Renders `{created, staged, batch_id}` counts inline with the rollback command on success. The `/setup` credentials page cross-links to it as an optional next step.
- **Six-field credentials surface** — `/setup/done` and `/auth/login` recovery pages both render six fields (admin email, API key, OAuth `client_id`, OAuth `client_secret`, MCP connector URL with `/mcp` suffix, login URL) with copy buttons and a `brilliant-credentials.txt` download. Login rotates the API key; an opt-in "Also rotate OAuth client secret" checkbox rotates both atomically in one transaction.
- **Deploy-window UX polish** — friendly HTML 404 on `GET /oauth/login`, a "warming up" banner on `/setup` while the api/mcp public URLs publish, and a CSS spinner on `/import/vault` uploads. Covers the Render container-swap window so first-run users never see raw 502 / `{"detail":"Not found"}`.

## New in v0.4.1

- **Tag triangulation read-surface** — `session_init.manifest.tags_top` exposes the top 20 tags by entry count, so agents see the tag shape of the KB at session start. `list_tags` paginates the full corpus; `get_tag_neighbors(tag)` returns co-occurring tags (ranked by co-count + Jaccard). `search_entries(tags=[...])` accepts multi-tag AND filtering.
- **`get_index` scale guard** — at `depth >= 2`, the endpoint now returns **422** with a hint when the caller's visible entry count exceeds 200 and no narrowing filter is supplied. Soft-breaking for existing unfiltered drill calls; the hint body points at `path=`, `content_type=`, `tag=`, or `search_entries` as recovery paths. L1 (depth=1) stays unconstrained.

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
  | bash
```

The installer stands up the stack and opens `/setup` in your browser; complete the form and download `brilliant-credentials.txt` from the response page. No credentials file is written by the installer on this default path — the browser download is the single source of truth.

To pin a specific version or track `main`, pass `--ref`:

```bash
curl -fsSL https://raw.githubusercontent.com/thejeremyhodge/xireactor-brilliant/main/install.sh \
  | bash -s -- --ref v0.5.1
```

To pick a different clone target, pass `--dir /path/to/target`.

### Manual install (pre-cloned)

If you prefer to inspect the repo before running anything, clone it first. When the installer is invoked from inside a clone it runs in place and never re-clones:

```bash
git clone https://github.com/thejeremyhodge/xireactor-brilliant.git
cd xireactor-brilliant
./install.sh
```

Dry-run the plan first if you want to see what it will do:

```bash
./install.sh --dry-run
```

All flags:

```bash
./install.sh --help
```

### Headless install (VPS / CI / scripted)

When the installer is running on a host without a desktop browser — a VPS, a CI runner, or any "remote shell" environment — you have two paths.

**SSH-tunnel variant (recommended for VPS operators with a workstation browser):**

```bash
./install.sh --headless
```

The installer skips the browser auto-open, stands up the stack, and prints the `/setup` URL prominently along with an SSH-tunnel hint. Forward the API port from your workstation:

```bash
ssh -L 8010:localhost:8010 user@your-vps-host
```

…then open `http://localhost:8010/setup` in your workstation browser and complete the same ceremony as the default install. Download `brilliant-credentials.txt` from the response page.

**Scripted variant (CI / fully unattended):**

```bash
# Recommended: interactive password prompt (TTY only).
./install.sh --admin-email you@example.com
# Installer prompts for password twice (read -s, no echo).
```

When `--admin-email` is passed without `--admin-password`, the installer prompts interactively on a TTY (double-entry confirm). After health-check, the installer auto-curls `GET /credentials` with the minted admin key and writes `./brilliant-credentials.txt` (mode 600, six fields) — no manual recovery curl required, no browser opens.

For fully unattended CI runs where no TTY is available, pass the password on argv (CI-only escape valve):

```bash
./install.sh --admin-email you@example.com --admin-password 'CHANGE_ME'
```

The installer emits a one-line stderr warning that `--admin-password` is visible in `ps`/shell history; prefer the interactive form whenever a TTY is available. The credentials file is written exactly the same way.

If the `./brilliant-credentials.txt` write ever fails (or if you lose the file), re-fetch it any time with:

```bash
curl -H 'Authorization: Bearer YOUR_ADMIN_API_KEY' \
  http://localhost:8010/credentials > brilliant-credentials.txt
```

### First API call

After completing `/setup` in your browser, copy the API key from the downloaded `brilliant-credentials.txt` (default location: `~/Downloads/brilliant-credentials.txt`) and call the API:

```bash
# After /setup, copy the API key from brilliant-credentials.txt:
curl -H "Authorization: Bearer YOUR_ADMIN_API_KEY" \
  http://localhost:8010/entries
```

If you have the file at a known path, you can extract the key inline:

```bash
curl -H "Authorization: Bearer $(grep '^admin_api_key=' ~/Downloads/brilliant-credentials.txt | cut -d= -f2)" \
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

### Bulk-import an existing vault (optional)

After setup, bulk-import an existing Obsidian (or plain-markdown) vault at `https://<your-host>/import/vault` (locally: `http://localhost:8010/import/vault`). The page accepts a `.tgz` / `.tar.gz` tarball, runs the same server-side parse pipeline as the MCP `import_vault` tool, and renders the batch counts + rollback command inline on success. This is the canonical bulk-import path for non-trivial vaults — it bypasses Claude's per-turn output cap and the Co-work bash sandbox, both of which block MCP-protocol byte streaming for real-world vault sizes. The `/setup` credentials page links to it directly.

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

## Deploy to Render

Zero-ops alternative to `install.sh` — one click provisions the whole stack on Render, no Docker or Postgres know-how required.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/thejeremyhodge/xireactor-brilliant)

Render reads `render.yaml` from the repo root and provisions:

- `brilliant-api` — FastAPI service (Starter)
- `brilliant-mcp` — remote MCP server for Claude (Starter)
- `brilliant-db` — managed Postgres (Basic-256mb)
- 1 GB persistent disk attached to the api service at `/data` (attachment blobs)

**Cost:** ~$20–25/mo all-in (two Starter web services + Basic-256mb Postgres + 1 GB disk).

### Why Starter, not Free

Free tier works for a throwaway demo, but not for a KB you care about:

- **Postgres is deleted after 30 days.** Your entries, users, and governance history go with it.
- **No persistent disks.** Attachments have nowhere to live — the PDF digest pipeline breaks.
- **15-minute idle spin-down + ~60 s cold start.** Every MCP call from Claude eats a minute of latency, which makes the agent UX unusable.

Sources: [render.com/pricing](https://render.com/pricing), [render.com/docs/free](https://render.com/docs/free), [render.com/docs/disks](https://render.com/docs/disks).

### End-to-end flow

1. **Click the button.** Render prompts for `ADMIN_EMAIL` — the only user-provided input. Password, API key, and OAuth client credentials are all captured after deploy.
2. **Wait ~3 minutes** while Render builds the two Docker images, provisions the database, runs migrations (via `preDeployCommand`), and boots the services.
3. **Visit the api service URL.** The root route redirects to `/setup` because the `first_run_complete` latch is still false.
4. **Choose a password** on `/setup`. Submit → credentials page renders inline with **six fields**: admin email, API key, OAuth `client_id`, OAuth `client_secret`, MCP connector URL, and a login URL. Copy buttons + a `brilliant-credentials.txt` download button save everything in one click. The `/setup` route then 404s forever.
5. **Connect Claude Co-work** via custom connector — paste **four fields**: a name, the MCP URL, the `client_id`, and the `client_secret`. Hit connect. Claude opens a browser tab to the api's `/oauth/login` page; enter the admin email + password you chose at `/setup`. Claude flashes "connected" and all MCP tool calls are now scoped to that user's RLS context.

### Lost your API key?

Visit `https://<your-api-host>/auth/login` and sign in with the email and password you chose at `/setup`. Signing in **rotates the API key** — all prior keys are invalidated, a fresh one is issued, and the six-field `brilliant-credentials.txt` download is offered. This doubles as a panic button if you suspect a key leaked. If you suspect the OAuth `client_secret` leaked, check the "Also rotate OAuth client secret" box before submitting — both secrets rotate atomically in one transaction. (Rotating `client_secret` invalidates every Claude connector provisioned against the old secret — re-paste the new one.)

### Security model

Three independent gates protect the knowledge base. Breaking any one of them does not grant access:

1. **Pre-registered OAuth client** — the `client_id` + `client_secret` from `/setup` are required in the Claude Co-work custom connector. Dynamic Client Registration (DCR) is **disabled** on the MCP server; a stranger who discovers the public MCP URL cannot self-register a client. The secret is rotatable from `/auth/login`.
2. **User login at `/authorize`** — the MCP's OAuth `authorize` endpoint does not auto-mint an access token. It redirects the user's browser to the api service's `/oauth/login` page, which requires email + password. The handoff back to the MCP is HMAC-SHA256 signed with a shared `OAUTH_HANDOFF_SECRET` that Render generates at deploy time and never exposes to operators.
3. **Per-user RLS via `X-Act-As-User`** — the MCP authenticates to the api with a `key_type='service'` API key, but every tool call carries an `X-Act-As-User: <user_id>` header bound to the authenticated session. The api's auth middleware honors the header **only** from service keys; a non-service key presenting it returns 403. Downstream Postgres RLS (`SET LOCAL ROLE` + `app.user_id`) enforces per-user scope on every query.

Consequence: three distinct credentials — the `client_secret` (rotatable), the user password, and the service API key (deploy-time rotatable via Render) — plus database-level RLS — gate every MCP tool call. The access log records the acting user, not the service identity.

### Escape valves

Render is one option, not the only one. For larger deployments or different constraints, self-host with `install.sh` (local Docker) or bring your own Postgres / S3-compatible object storage — see [ARCHITECTURE.md](ARCHITECTURE.md) for the component boundaries.

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
                                          │   32 migrations   │
                                          └───────────────────┘
```

**Key components:**
- **`api/`** — FastAPI backend with auth, entries, staging/governance, imports, links, permissions, comments, groups
- **`db/migrations/`** — 32 SQL migrations (core schema through OAuth user-binding + api-service public URL publish)
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
