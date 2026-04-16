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

[Unreleased]: https://github.com/thejeremyhodge/xireactor-brilliant/compare/v0.2.2...HEAD
[0.2.2]: https://github.com/thejeremyhodge/xireactor-brilliant/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/thejeremyhodge/xireactor-brilliant/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/thejeremyhodge/xireactor-brilliant/releases/tag/v0.2.0
[0.1.0]: https://github.com/thejeremyhodge/xireactor-brilliant/commit/6dcc794
