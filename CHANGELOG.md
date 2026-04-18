# Changelog

All notable changes to **xiReactor Brilliant** are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

> **Note:** Release notes with richer context (migration steps, breaking-change rationale,
> contributor credits) live on the [GitHub Releases page](https://github.com/thejeremyhodge/xireactor-brilliant/releases).
> This file is a terse, machine-friendly index that mirrors those releases.

## [Unreleased]

### Added
- _nothing yet_

### Changed
- _nothing yet_

### Fixed
- _nothing yet_

## [0.3.1] — 2026-04-18 — Installer self-clone

### Added
- `install.sh` now self-clones the repo when invoked from outside a brilliant
  checkout — the README's `curl … | bash` one-liner works from any directory
  (issue [#29](https://github.com/thejeremyhodge/xireactor-brilliant/issues/29))
- `--ref <tag|branch|sha>` flag — pick the git ref to clone; default is the
  latest release tag (via GitHub releases API), with `main` as fallback
- `--dir <path>` flag — override the clone target (default `./xireactor-brilliant`)
- New CI job `smoke-self-clone` in `installer-smoke.yml` exercises the
  zero-clone path end-to-end

### Fixed
- README's one-liner install example no longer contradicts the installer's
  actual behavior; added a separate "Manual install (pre-cloned)" section
  documenting the in-place path

## [0.3.0] — 2026-04-17 — Installer, attachments, observability, Brilliant rename

> **Breaking:** container names, database name, OAuth scope, and `CORTEX_*` environment
> variables were all renamed from `cortex` → `brilliant`. Existing installs upgrade via
> `./install.sh --migrate-from-cortex`, which runs `ALTER DATABASE cortex RENAME TO brilliant`
> on the existing `pgdata` volume and rebuilds containers under the new names — no data
> copy. OAuth-connected clients (Claude Co-work) must re-authorize because the scope
> changed from `cortex` to `brilliant`. Old `ghcr.io/thejeremyhodge/cortex-{api,mcp}` image
> tags are frozen; new pushes go to `ghcr.io/thejeremyhodge/brilliant-{api,mcp}`.

### Added
- One-shot installer (`install.sh`) — Docker detection/install, strong random
  `.env` generation, admin bootstrap, health-check polling, and an eight-phase plan
  with `--dry-run`, `--no-install-docker`, and `--key-out` flags (spec 0034a)
- `install.sh --migrate-from-cortex` — in-place upgrade path from a v0.2.x Cortex
  install (rename database, rebuild containers, idempotent; exit codes 76–80)
- File attachments — PDF digest pipeline, S3-compatible or local signed-URL storage,
  content-hash dedup, `POST /attachments`, `GET /attachments/{id}`, MCP
  `upload_attachment` tool (spec 0034b, issue #17, migrations 022 + 025)
- Observability — async request-log middleware, per-entry read tracking, admin
  `/analytics/*` rollup endpoints, MCP `get_usage_stats` tool
  (spec 0034c, issue #15, migration 023)
- `CHANGELOG.md` now covers the full v0.3.0 surface; see
  [docs/ATTACHMENTS.md](docs/ATTACHMENTS.md) and [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md)
  for the richer user-facing docs

### Changed
- **Cortex → Brilliant code-level rename** (spec 0035): `cortex-{db,api,mcp}` container
  names → `brilliant-*`; default `POSTGRES_DB=cortex` → `brilliant`; FastMCP server
  name + client class (`CortexClient` → `BrilliantClient`); `CortexOAuthProvider` →
  `BrilliantOAuthProvider`; OAuth scope `cortex` → `brilliant`; GHCR image names
  `cortex-{api,mcp}` → `brilliant-{api,mcp}`; environment variables
  `CORTEX_BASE_URL` / `CORTEX_API_KEY` / `CORTEX_DB_DSN` / `CORTEX_TEST_ORG_ID` →
  `BRILLIANT_*`
- `session_init` / `SKILL.md` — corrected drift: `system_entries` carries
  user-authored `System/*` entries only (rules, conventions); the content-type
  registry lives in its own table and is fetched via `get_types`
- README "Getting Started" rewritten around the one-shot installer with manual
  `docker compose` path preserved as a collapsible section

### Fixed
- `sync_entry_links` now extracts markdown-style `[label](slug)` references in
  addition to `[[wiki-links]]` so graph traversal stays consistent across authoring
  styles (issue #16, migration 024 — staging content nullable)
- `submit_staging` accepts proposed-meta-only updates (tags, sensitivity, etc.)
  without requiring a content change; 422 no-op guard prevents empty submissions
  (issue #12)

[Full notes](https://github.com/thejeremyhodge/xireactor-brilliant/releases/tag/v0.3.0)

## [0.2.2] — 2026-04-16

### Added
- `CODE_OF_CONDUCT.md` — Contributor Covenant v2.1
- `SECURITY.md` — vulnerability disclosure policy
- `CHANGELOG.md` — Keep-a-Changelog-compatible release history

### Changed
- `CONTRIBUTING.md` — added "For Maintainers" note and formalized doc-only → `main` merge path (bypasses `dev` release gate)
- `.gitignore` — extended to cover private maintainer surfaces

[Full notes](https://github.com/thejeremyhodge/xireactor-brilliant/releases/tag/v0.2.2)

## [0.2.1] — 2026-04-14

### Fixed
- `tests/demo_e2e.sh` API contract drift
- README tweaks

[Full notes](https://github.com/thejeremyhodge/xireactor-brilliant/releases/tag/v0.2.1)

## [0.2.0] — 2026-04-14 — Brilliant rebrand + 4 days of upstream work

### Added
- Brilliant rebrand across README, docs, and package metadata
- Write-path `entry_links` sync — POST / PUT on entries repopulates the `entry_links`
  table from `[[wiki-link]]` references so traversal and render stay in lockstep (spec 0030)
- Permissions v2 — unified polymorphic `permissions` table with user + group principals,
  superseding the legacy entry/path permission tables (spec 0026)
- Comments subsystem — threaded comments with resolve/escalate workflow, author-kind
  tracking, and audit-log integration (API-only surface) (spec 0026)
- Content type registry — canonical types with alias support, server-side validation at
  submission time
- Render-time wiki-link resolution — GET `/entries/{id}` rewrites `[[slug]]` →
  `[Title](/kb/{id})` markdown, with frontmatter strip for imported vault content (spec 0028)
- Skill bundle — Claude Co-work skill with inbox/outbox workflow and KB-aware session
  bootstrap (spec 0029)
- 4-tier governance pipeline on staging writes
- Documented `main` / `dev` branching model in `CONTRIBUTING.md`
- Getting Started section rebuilt in README

[Full notes](https://github.com/thejeremyhodge/xireactor-brilliant/releases/tag/v0.2.0)

## [0.1.0] — Initial public release

First public drop of xiReactor Brilliant. Core entry CRUD, full-text search, wiki-link
traversal via recursive CTEs, multi-tenant organizations with row-level security,
MCP integration for Claude Co-work and Claude Code (stdio + Streamable HTTP / OAuth 2.1),
and Obsidian vault import with preview, collision detection, and batch rollback.

[Unreleased]: https://github.com/thejeremyhodge/xireactor-brilliant/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/thejeremyhodge/xireactor-brilliant/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/thejeremyhodge/xireactor-brilliant/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/thejeremyhodge/xireactor-brilliant/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/thejeremyhodge/xireactor-brilliant/releases/tag/v0.2.0
[0.1.0]: https://github.com/thejeremyhodge/xireactor-brilliant/commit/6dcc794
