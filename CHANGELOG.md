# Changelog

All notable changes to **xiReactor Brilliant** are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

> **Note:** Release notes with richer context (migration steps, breaking-change rationale,
> contributor credits) live on the [GitHub Releases page](https://github.com/thejeremyhodge/xireactor-brilliant/releases).
> This file is a terse, machine-friendly index that mirrors those releases.

## [Unreleased]

## [0.9.0] — 2026-05-07 — Epistemic axis + heat-axis closeout

### Added
- **`[feature]` Sprint 0050 — epistemic axis (closes ADR [#69](https://github.com/thejeremyhodge/xireactor-brilliant/issues/69))** — realizes the deferred 3-axis closeout: a new `axis=epistemic` lane on `GET /lod` at LOD0 (corpus histogram of `claim_type × verification_status`), LOD2 (per-community histogram), and LOD4 (per-node chip `{claim_type, source_confidence, verification_status, conflict_with}`). Schema additions on `entries` via additive migration `033_epistemic_axis.sql`: `claim_type` (event | observation | claim | rule), `source_confidence` (verified | reported | inferred | rumor), `verification_status` (verified | pending | disputed | superseded), and `conflict_with uuid[]` — all `NOT NULL` with the ADR #69 backfill defaults (`observation` / `reported` / `pending` / empty array). New partial covering index `entries_epistemic_histogram_idx (claim_type, verification_status)` backs the LOD0/LOD2 aggregates. Histograms are aggregate-on-read — no precompute, same discipline as Sprint 0049 communities. `axis=epistemic&level=1|6` returns `400` with the documented message ("epistemic axis is defined at LOD0/LOD2/LOD4 only").
- **`[feature]` Heat-axis branches at LOD2 + LOD4 (closes [#73](https://github.com/thejeremyhodge/xireactor-brilliant/issues/73))** — fixes the silent fall-through where `axis=heat` at non-corpus levels returned the structural silhouette. `_node_lod4` and the community LOD1/LOD2 paths now branch on `axis` and aggregate `entry_access_log` (migration 023) over the appropriate scope. RLS on `entry_access_log` means non-admin act-as readers see "all-cold" — documented in `skill/references/api-reference.md`, with an `rls_filtered: true` hint when the heat count is zero on a node with `degree>0`.
- **`[feature]` Manifest v2 epistemic block** — `GET /session?manifest_version=2` gains a top-level `epistemic` block alongside `structural`, `heat`, and `motifs`. Body is the LOD0 corpus epistemic histogram. v1 path is byte-identical for v0.7.0 clients (regression test from Sprint 0049 still passes unmodified); v2 stays under the ~3K token budget on the 1K-entry fixture.
- **`[feature]` Staging write-path accepts epistemic fields** — `POST /staging/submit` and `POST /staging/review` accept optional `claim_type` and `source_confidence`; defaults inferred from `content_type` via a small inline lookup table when omitted. Reviewer overrides on approval write through to the underlying entry. Existing payloads without epistemic fields still pass unchanged.
- **`[feature]` Reviewer agent writes `verification_status` + opportunistic `conflict_with`** — on staging review, `api/services/ai_reviewer.py` sets `verification_status` to `verified` (auto-approve), `pending` (needs-human), or `disputed` (contradicts existing). When a contradiction is cheaply detectable (<1s budget), `conflict_with` is populated with the conflicting entry id (capped at 5 ids per entry). No background sweep job — opportunistic-only this sprint.
- **`[feature]` Skill v0.9.0 — epistemic-narrow-before-claim pattern** — SKILL.md teaches agents to fetch `axis=epistemic&level=0` before adding new claims; if `verification_status=disputed` is non-zero on a related community, descend to `axis=epistemic&level=4` on the conflict-target before submitting. `api-reference.md` adds heat examples at LOD2/LOD4 and one epistemic example per advertised level. Skill bundle `brilliant-kb-assistant.zip` re-zipped with `references/` intact (per `feedback_skill_rezip_macos_zip` — explicit paths or `-FSr`).
- **`[docs]` `docs/OBSERVABILITY.md`** — new "Epistemic histogram" subsection with a runnable SQL block for ops dashboards; notes that `entries_epistemic_histogram_idx` backs the query.

### Changed
- **`MIN_SKILL_VERSION` held at `0.7.0`** — epistemic axis is additive (LOD0/2/4 still serve `axis=structural|heat` unchanged; new staging fields are optional). v0.7.0 skills continue to work against the v0.9.0 API and receive the "newer skill available" banner via `LATEST_SKILL_VERSION`. `API_VERSION` / `MCP_VERSION` / `LATEST_SKILL_VERSION` bumped to `0.9.0`.

## [0.8.0] — 2026-05-07 — Multi-LOD + 3-axis + tag-triangulation

### Added
- **`[feature]` Sprint 0049 — multi-LOD endpoints** — new `GET /lod?axis=&scope=&level=` uniform endpoint serves LOD0 (corpus silhouette: structural + heat), LOD1/LOD2 (community-by-tag or community-by-path), LOD4 (node silhouette), and LOD6 (markdown section outline). New `api/services/lod.py`, `api/services/motifs.py` (tag-triangulation registry), `api/services/section_outline.py` (pure markdown heading parser, memoized for >10KB entries). `GET /session?manifest_version=2` returns the v0.4.x manifest plus `structural`, `heat`, and `motifs` blocks; v1 default is byte-identical to v0.7.0. Communities are dynamic tag-clusters (no precompute, no Louvain). Three axes framed in research, only structural + heat + tag-triangulation shipped this sprint — epistemic deferred.
- **`[feature]` MCP `get_lod` tool** — wraps `GET /lod` with `(axis, scope, level)` signature. `api-reference.md` documents one example per LOD level.
- **`[feature]` Skill v0.8.0 — LOD0-first session-start flow** — SKILL.md teaches agents to fetch the corpus map (`get_lod` structural + heat) before any `search_entries`, narrow scope deterministically using tag-triangulation motifs, and only descend (LOD2/4/6) once a target is identified. Skill bundle re-zipped. Triggers v0.7.0 handshake "newer skill available" banner for v0.7.0 clients; `MIN_SKILL_VERSION` stays at `0.7.0` since v0.7.0 skills work fine against the v0.8.0 API (v2 manifest is opt-in). (T-0280..T-0287; spec `.xireactor/specs/0049--2026-05-07--multi-lod-3-axis.md`)
- **`[feature]` Measurement scaffold** — `/lod` calls log to `request_log` so we can measure the get_lod-vs-search_entries ratio per session over the next 2-3 weeks of dogfood data.

## [0.7.0] — 2026-05-05 — Skill ↔ API version handshake

### Added
- **`[feature]` Sprint 0048 — version handshake** — new `GET /version` endpoint exposes `api_version`, `min_skill_version`, `latest_skill_version`, and `skill_download_url` (no auth, `Cache-Control: no-cache`) so the skill can determine compatibility on every session start. New MCP `get_version` tool merges the API response with the MCP's own version + dialed API URL and degrades gracefully (`api_unreachable: true`) if the API is mid-deploy. SKILL.md gains a `skill_version` frontmatter field and a "Session Start: Version Check" section with a three-outcome decision tree (silent / upgrade banner / hard refusal). Companion safety net for the `autoDeploy: false` posture shipped in v0.6.1: operators now get an actionable upgrade signal even though deploys no longer auto-roll. `CONTRIBUTING.md` documents the release-cut version-bump dance, including the load-bearing decision of when to bump `MIN_SKILL_VERSION`. (T-0276, T-0277, T-0278, T-0279; spec `.xireactor/specs/0048--2026-05-01--version-handshake.md`)


### Added
- **`[feature][docs]` Sprint 0040b — browser vault upload (MCP-bypass onboarding path)** — first-class `/import/vault` HTML page + `POST /import/vault-upload` multipart endpoint that lets authenticated users push a vault tarball straight from their browser to the API, bypassing Claude's ~32K-token per-turn output cap, the Co-work bash sandbox outbound allowlist, and the MCP protocol entirely. Reuses the Sprint 0040 `_execute_import` + `iter_tarball_md` pipeline unchanged; renders `{created, staged, batch_id}` counts inline with the rollback command on success. Cross-linked from `/setup` so first-run flows naturally into vault seeding. SKILL.md rewritten to direct remote-MCP agents at the browser page instead of attempting base64-over-MCP for real vaults — the 0040a inline-bytes path stays as a small-vault / local-stdio fallback. README quickstart points first-time users at `https://<your-host>/import/vault`. (T-0245, T-0246, T-0247, T-0248, T-0249; spec `.xireactor/specs/0040b--2026-04-19--browser-vault-upload.md`)

### Changed
- **`[chore]` Skill rename — `knowledge-base` → `brilliant-kb-assistant`.** The Claude skill bundle (`skill/SKILL.md` frontmatter + `skill/brilliant-kb-assistant.zip`) is renamed under a new `brilliant-kb-*` namespace that future skills (`brilliant-kb-onboarding`, `brilliant-kb-manage`, `brilliant-kb-users`, …) will share. **Migration:** Claude Co-work operators with the old skill installed must re-upload the renamed bundle — skill IDs do not auto-migrate, and the legacy `knowledge-base` registration will remain until removed manually. Local Claude Code users consuming the bundle via repo path are unaffected.

### Fixed
- _nothing yet_

## [0.5.1] — 2026-04-20 — Installer + Render unified on `/setup` web ceremony

> **Breaking change:** `--key-out` removed from `install.sh`. The default install path no longer writes a credentials file to disk — the `/setup` web ceremony is the single source of truth, and `brilliant-credentials.txt` is delivered as a browser download from the response page. CI / scripted installs use the new headless install path (`--admin-email` + `--admin-password`), which env-bootstraps the admin and auto-writes `./brilliant-credentials.txt` after health-check.

### Added
- Unified installer + Render on the `/setup` web ceremony (closes [#46](https://github.com/thejeremyhodge/xireactor-brilliant/issues/46) and [#47](https://github.com/thejeremyhodge/xireactor-brilliant/issues/47)). The installer no longer env-bootstraps admin on the default path; the operator drives the same browser-based ceremony Render users see.
- Browser auto-open on default install (macOS `open` / Linux `xdg-open` with URL-print fallback when neither binary is available). Failures never block the installer.
- New `--headless` / `--no-browser` flag for VPS / CI / SSH-tunnel users — skips the browser auto-open and prints the `/setup` URL prominently with an SSH-tunnel hint (`ssh -L 8010:localhost:8010 user@host`).
- New interactive password prompt when `--admin-email` is passed without `--admin-password` on a TTY — `read -s` with double-entry confirm; the password never appears in `ps` output or shell history.
- Headless install with `--admin-email` + `--admin-password` auto-writes `./brilliant-credentials.txt` (mode 600, six fields) after health-check by curling `GET /credentials` with the minted admin key — no manual recovery `curl` required.

### Changed
- **BREAKING:** `--key-out` removed from `install.sh`; `KEY_OUT` / `DEFAULT_KEY_OUT` / `write_key_out` / `extract_credentials_block` / `absolutize_path` / `phase_verify` / `rand_password` deleted entirely. No installer-written credentials file on the default path.
- `--admin-email` is no longer required on the default install path; the installer runs flag-free and points the operator at `/setup`. `--admin-email` / `--admin-password` now describe the headless install path explicitly in `--help`.
- `--admin-password` on argv emits a one-line stderr warning ("password visible in `ps`/shell history — prefer interactive entry") but continues — CI-friendly escape valve.
- `--admin-email` implies `--headless` (no browser opens when admin is being claimed via env bootstrap).
- `phase_summary` banner reshaped into three branches: default (browser auto-open + `/setup` CTA), headless-no-admin (URL + SSH-tunnel hint), headless-with-admin (auto-written credentials file + `/credentials` re-fetch hint).
- `.env.sample` — `ADMIN_EMAIL` / `ADMIN_PASSWORD` commented out with a note that they're now the headless install escape hatch.
- README install section rewritten around the browser-auto-open + `/setup` flow; new "Headless install (VPS / CI / scripted)" subsection documents both the `--headless` SSH-tunnel and `--admin-email` / `--admin-password` scripted variants.
- All Sprint 0043 polish (T-0253..T-0259) carried forward unchanged except the six-field credentials file composition: now either a browser deliverable (default path) or an installer-side `/credentials` fetch (headless-with-admin path).

### Fixed
- Installer banner pointing at `/setup` while the env-driven admin bootstrap had already claimed the latch (404 on click) — eliminated by removing the env-bootstrap path from the default install. Both lanes now share one ceremony.
- Auto-generated admin password buried in `.env` and nowhere else — eliminated by removing the auto-generated password entirely. The operator either chooses the password in the `/setup` form (default) or supplies it interactively / on argv (headless).

## [0.5.0] — 2026-04-18 — OAuth user-bound authentication

> **Breaking change:** Dynamic Client Registration on the MCP server is disabled. Every existing Claude Co-work connector must be re-provisioned with the `client_id` + `client_secret` shown on `/setup/done` (or recovered via `/auth/login`). Operators self-hosting before this release should treat `/setup` as a one-time reset surface after upgrading.

### Added
- Three-gate OAuth 2.1 authorization-code flow on the MCP server (gate 1: pre-registered `client_id`/`client_secret`; gate 2: user login at api-hosted `/oauth/login`; gate 3: per-user RLS via `X-Act-As-User`). Replaces the previous DCR auto-approve path where anyone with the public MCP URL could mint admin access.
- Migration 030 — `oauth_pending_authorizations` table for the MCP↔api `/authorize` tx handoff; `user_id` columns on `oauth_access_tokens` + `oauth_auth_codes`.
- Migration 031 — `api_keys.key_type` CHECK extended to accept `'service'`. Service keys authenticate MCP→api calls and may present `X-Act-As-User: <user_id>` to act as the authenticated user; non-service keys presenting the header → 403.
- Migration 032 — `brilliant_settings.api_public_url`; api service publishes `$RENDER_EXTERNAL_URL` to this column at startup so the MCP can construct a browser-resolvable redirect URL for the `/oauth/login` handoff.
- `/oauth/login` route on the api — HTML email + password form; successful submit HMAC-SHA256-signs `{tx}|{user_id}` and 302s to the MCP's `/oauth/continue`.
- `/oauth/continue` custom route on the MCP — verifies the HMAC signature with `hmac.compare_digest`, mints an authorization code bound to the authenticated `user_id`, deletes the pending-authz row atomically.
- OAuth client + service api_key are minted in the same transaction as the admin user during `/setup`, so a single atomic bootstrap produces: 1 user, 1 interactive key, 1 service key, 1 OAuth client. Env-driven `install.sh` bootstrap path preserved.
- `render.yaml` now generates + shares `OAUTH_HANDOFF_SECRET` and `BRILLIANT_SERVICE_API_KEY` across the api + mcp services via `fromService.envVarKey`.
- README "Security model" subsection under Deploy-to-Render describing the three-gate defense in depth.

### Changed
- **BREAKING:** DCR disabled. `POST /register` on the MCP returns 404. Discovery (`/.well-known/oauth-authorization-server`) still advertises `authorize` + `token` endpoints; only `registration_endpoint` is omitted, per RFC 8414.
- `/setup/done` displays **six** fields (was four): admin email, API key, OAuth `client_id`, OAuth `client_secret`, MCP connector URL (with `/mcp` suffix — prior bug fix rolled in here), login URL. `brilliant-credentials.txt` download includes all six.
- `/auth/login` password-login recovery page shows the same six-field layout after successful auth. New "Also rotate OAuth client secret" checkbox (default off) lets operators rotate the `client_secret` alongside the API key in a single atomic transaction; default behavior preserves the live Claude connector.
- MCP outbound API calls now bear `BRILLIANT_SERVICE_API_KEY` + `X-Act-As-User: <user_id>`. `BRILLIANT_API_KEY` removed from `mcp/` entirely. Tool handlers with a `user_id`-missing token raise at the MCP layer rather than silently falling through to service-level access.
- MCP URL display on `/setup/done` + `/auth/login` is idempotent against DB values with or without a `/mcp` suffix.

### Fixed
- `/setup/done` previously omitted the `/mcp` path suffix on the displayed MCP URL; copy-paste into Claude failed to connect. Fixed in `_mcp_url_for_display`.

## [0.4.1] — 2026-04-19 — Tag triangulation read-surface

### Added
- `manifest.tags_top` in `session_init` — up to 20 tags by published-entry count, ordered count desc then tag asc; gives agents the tag shape of the KB at session start without fetching entries. Empty-KB returns an empty list.
- `GET /tags` + MCP `list_tags(limit=500, offset=0)` — paginated full tag corpus with usage counts, `{tags: [{tag, count}], total}`. Default limit 500, max 5000. RLS-scoped.
- Multi-tag AND filtering on `search_entries` — `GET /entries?tags=a&tags=b` returns only entries containing ALL listed tags (GIN-backed `tags @> ARRAY[...]::text[]`). The MCP tool accepts `tags: list[str]`. Singular `?tag=X` is untouched; supplying both simultaneously returns 422.
- `GET /tags/{tag}/co-occurring` + MCP `get_tag_neighbors(tag, limit=10)` — tags frequently seen on the same entries as the target, ranked by co-count then Jaccard similarity (`co_count / (A_total + B_total - co_count)`). Unknown tag returns 200 with empty neighbors.
- Response models: `TagWithCount`, `TagListResponse`, `TagCoOccurrence`, `TagCoOccurrenceResponse`.

### Changed
- **Soft-breaking:** `get_index` applies a scale guard at `depth >= 2`. If the caller's visible published-entry count exceeds 200 AND no narrowing filter (`path`, `content_type`, or new `tag=`) is supplied, the endpoint returns **422** with body `{"error": "index_too_large", "total": N, "hint": "narrow with path=, content_type=, tag=, or use search_entries"}`. L1 (`depth=1`, counts only) remains unconstrained — always safe. Existing callers that browse large KBs at `depth >= 2` without a filter must now narrow, but the 422 body carries a hint string pointing to the recovery options.
- `get_index` accepts a new `tag: str | None` query parameter (single tag); multi-tag AND callers should use `search_entries(tags=[...])`.
- `skill/knowledge-base.zip` re-zipped — SKILL.md now carries a dedicated "Triangulation (tag-driven narrowing)" workflow section and a "Narrowing at scale" subsection that documents the 422 guard.

## [0.4.0] — 2026-04-18 — Tag triangulation + vault import

### Added
- `suggest_tags(content)` MCP tool + `POST /tags/suggest` endpoint — deterministic, RLS-scoped ranking over the caller's existing tag vocabulary; no LLM calls
  (issue [#8](https://github.com/thejeremyhodge/xireactor-brilliant/issues/8))
- `fuzzy=true` flag on `GET /entries` and `search_entries` MCP tool — trigram-similarity fallback that only engages when the exact/FTS path returns zero rows; default behavior unchanged
  (issue [#26](https://github.com/thejeremyhodge/xireactor-brilliant/issues/26))
- Migration 026 enables `pg_trgm` and creates GIN trigram indexes on `entries.title` / `entries.content`
- `import_vault(path)` MCP tool walks an Obsidian vault, sends files to `/import`, and the server-side import path now parses YAML frontmatter into entry fields (`title`, `tags`, `sensitivity`, `content_type`, `department`, `summary`; unknown keys → `domain_meta`) and extracts `[[wikilinks]]` + markdown links into `entry_links`
  (issue [#31](https://github.com/thejeremyhodge/xireactor-brilliant/issues/31), bundles [#24](https://github.com/thejeremyhodge/xireactor-brilliant/issues/24) + [#25](https://github.com/thejeremyhodge/xireactor-brilliant/issues/25))
- `tools/vault_parse.py` — reusable walker + payload builder shared by the `import_vault` MCP tool and the `tools/vault_import.py` CLI

### Changed
- **BREAKING:** `session_init` / `GET /session-init` response shape reworked to a compact ≤ 2K-token density manifest. The old payload inlined full `entries`, `relationships`, `summaries`, and full `content` for every `system_entries` row (~46K tokens on the seeded demo KB). The new shape returns counts + handles only: `manifest.total_entries`, `manifest.last_updated`, `manifest.user`, `manifest.categories` (`[{content_type, count}]`), `manifest.top_paths` (`[{logical_path_prefix, count}]`, capped ~15), `manifest.system_entries` (`[{id, title, logical_path}]` — `content` dropped), `manifest.pending_reviews` (unchanged), and `manifest.hints` (suggested drill-down calls).
  (issue [#7](https://github.com/thejeremyhodge/xireactor-brilliant/issues/7))

  **Migration for agents:** switch from reading `system_entries[].content` to calling `get_entry(id)` on the ids returned in the manifest. For the relationship graph and deep summaries, call `get_index(depth=3, path=...)` or `get_neighbors(id, depth=2)` on demand. The skill bundle (`skill/knowledge-base.zip`) is re-zipped in this release; Claude Co-work operators maintaining a separate skill artifact need to re-zip their copy as well.

- **BREAKING (MCP surface):** `import_vault(files=...)` removed; the new signature is `import_vault(path, preview_only=False, exclude=None, max_files=500, source_vault=None, base_path=None)`. For Docker-hosted MCP, `path` must be on a bind-mounted volume the MCP container can read.

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

[Unreleased]: https://github.com/thejeremyhodge/xireactor-brilliant/compare/v0.5.1...HEAD
[0.5.1]: https://github.com/thejeremyhodge/xireactor-brilliant/compare/v0.5.0...v0.5.1
[0.3.0]: https://github.com/thejeremyhodge/xireactor-brilliant/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/thejeremyhodge/xireactor-brilliant/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/thejeremyhodge/xireactor-brilliant/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/thejeremyhodge/xireactor-brilliant/releases/tag/v0.2.0
[0.1.0]: https://github.com/thejeremyhodge/xireactor-brilliant/commit/6dcc794
