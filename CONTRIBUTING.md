# Contributing to xiReactor Brilliant

## Branching Model

This project uses a two-branch model:
- **`main`** — Tagged releases. Always matches what's documented in the README's Getting Started. If you want to *use* Brilliant, clone `main`.
- **`dev`** — Integration branch for in-progress work. If you want to contribute or follow the latest development, branch from `dev`.

**Contributing a change:**
1. Fork the repo (or create a feature branch if you have write access).
2. Branch from `dev`: `git checkout dev && git pull && git checkout -b feature/your-change`
3. Make your change, commit, push.
4. Open a pull request against `dev` (not `main`).

**Releases and `main`:** Code changes flow `dev` → `main` with a tagged release. Tags follow semver (`v0.2.0`, `v0.3.0`, etc.). Doc-only changes (markdown files, no code paths touched) may land directly on `main` at maintainer discretion — they don't need to wait for a release cut, since they can't affect runtime behavior.

Don't PR against `main` directly unless your change is documentation-only or a critical hotfix.

## Dev Setup

```bash
git clone https://github.com/thejeremyhodge/xireactor-brilliant.git
cd xireactor-brilliant
cp .env.sample .env
# Edit .env: set ADMIN_PASSWORD to something other than the default
docker compose up -d
# Wait for services to be healthy (~15s)
bash tests/demo_e2e.sh
```

If `demo_e2e.sh` passes, your local stack is working.

## Branch Flow

See **Branching Model** above for the `main` / `dev` split. Additional PR mechanics:

- One logical change per PR
- Rebase on `dev` before requesting review
- Squash-merge is fine for single-commit PRs; rebase-merge for multi-commit

## Code Style

- **Python:** Match existing patterns. Type hints where they aid clarity. No new dependencies without discussion in an issue first.
- **SQL migrations:** Sequential numbering (`NNN_description.sql`). Migrations must be idempotent where possible. Document any destructive changes loudly in the PR.
- **Shell scripts:** Use `set -euo pipefail`. Quote variables.

## Tests

- Add coverage to `tests/demo_e2e.sh` for new API endpoints or behavior changes
- For schema changes, update `tests/validate_schema.sql`
- PRs that change behavior without test coverage will be asked to add it

## Commit Messages

Use a type prefix with a short imperative line:

```
feat: add blob attachment support to entries
fix: prevent duplicate entry_versions on concurrent writes
docs: clarify RLS setup in ARCHITECTURE.md
refactor: extract governance tier logic to shared module
test: add staging promotion edge cases to demo_e2e
chore: bump anthropic SDK to 0.40
```

## Pull Requests

- Describe what changed and why
- Link the relevant issue if one exists
- Call out schema/migration changes explicitly — reviewers need to know
- Include steps to test if not obvious from the diff

## Prompt Requests (experimental)

We're trying a second contribution path alongside pull requests: describe *what* you want as a GitHub Issue, and a maintainer (with AI-agent assistance) decides whether to execute it directly. No code, no fork, no merge — just intent.

**How to file:**
- Open an Issue with the label `type:prompt-request`.
- Include: the change you want, *why* (use case / rationale), and acceptance criteria (how to verify it's done).

**What a good prompt request looks like:**
- Concrete scope — one feature, one fix, one refactor.
- Acceptance criteria an agent or human can verify.
- Rationale alongside the ask — "I want X because Y" is more useful than just "X".

**What happens next:**
- The maintainer evaluates the *intent*, not an implementation.
- Executed prompts credit the prompter with a `Prompt-Request-By: @username` trailer in the commit body.
- The maintainer may modify scope, ask for clarification, or decline with a brief rationale.

**When a pull request is a better fit:**
- You've reproduced a bug and have a specific fix in mind.
- You want to write the code yourself and be credited as the author.
- The change requires codebase-specific expertise that's hard to convey as a prompt.
- You want visible commit authorship on your GitHub profile.

Neither path is "better" in the abstract. Code PRs carry empirical verification a prompt can't; prompt requests lower the contribution barrier to zero. Use whichever fits the change you want to propose.

## Issues

File issues at [github.com/thejeremyhodge/xireactor-brilliant/issues](https://github.com/thejeremyhodge/xireactor-brilliant/issues).

- **Bugs:** Include `docker compose logs` output and the steps to reproduce
- **Features:** Describe the use case, not just the solution
- **Questions:** Ask away — no issue is too basic

## Cutting a release

The Sprint 0048 version handshake means four version strings now travel together. Release cuts must bump them in lockstep, otherwise the skill's session-start handshake either falsely refuses on a fresh install (over-bumped `MIN_SKILL_VERSION`) or misses a real incompatibility (under-bumped).

**The four strings:**
- `api/_version.py::API_VERSION` — current API release
- `api/_version.py::LATEST_SKILL_VERSION` — newest skill bundle published
- `api/_version.py::MIN_SKILL_VERSION` — oldest skill bundle that can still talk to this API
- `mcp/_version.py::MCP_VERSION` — MCP service version (kept identical to `API_VERSION`; lives in `mcp/` because the MCP container is built with `dockerContext: ./mcp` and can't import from `api/`)
- `skill/SKILL.md` frontmatter `skill_version` — the bundle's self-reported version

**Procedure:**
1. Pick the new version per [SemVer 2.0](https://semver.org/spec/v2.0.0.html) (patch / minor / major).
2. Bump `API_VERSION` in `api/_version.py`.
3. Bump `LATEST_SKILL_VERSION` in `api/_version.py` to match `API_VERSION` — every release ships a fresh skill bundle.
4. Decide whether to bump `MIN_SKILL_VERSION` (see criteria below). When in doubt, **don't bump** — spurious refusals are worse than a missing warning.
5. Bump `MCP_VERSION` in `mcp/_version.py` to match `API_VERSION`.
6. Bump `skill_version` in `skill/SKILL.md` frontmatter to match `API_VERSION`.
7. Re-zip the skill bundle with explicit paths (macOS zip 3.0 silently drops files when given a directory wildcard — see the relevant feedback memory):
   ```bash
   cd skill && zip -FS brilliant-kb-assistant.zip SKILL.md references/api-reference.md
   ```
8. Add a `## [x.y.z] — YYYY-MM-DD — <headline>` entry to `CHANGELOG.md` summarising what changed.
9. Squash-merge `dev` → `main`, tag `vx.y.z`, and `gh release create vx.y.z` (the GHCR image publish + shields.io release badge both depend on the GitHub Release object, not just the tag).

### When to bump `MIN_SKILL_VERSION`

This is the load-bearing decision in the dance. Bump it **only** when an older skill literally cannot work against the new API:

**Bump it when:**
- An MCP tool was removed or renamed.
- A required argument was added to an MCP tool (older skills will call without it and get a 422).
- A response shape that the skill parses changed (field renamed, removed, or restructured).
- An API route the skill calls was removed or moved.
- The auth header contract changed (e.g. new required header, format change).

**Don't bump it when:**
- New tools or routes were added (older skills simply don't call them).
- Internal refactors with no observable behaviour change.
- Performance improvements.
- Bug fixes that don't change the response shape or status codes.
- Docstring / comment / cosmetic changes.

A bumped `MIN_SKILL_VERSION` immediately turns into hard refusals for every operator running an older skill until they update — treat it as a breaking change and call it out loudly in the CHANGELOG entry.

## For Maintainers

Maintainers of this repository may keep additional local directories alongside the tracked source — planning and operational notes, marketing-site sources, research scratch, and tooling configuration. These live in the working tree but are excluded via `.gitignore` and never pushed. They exist only to keep the maintainer's day-to-day workflow in one place.

Contributors do not need any of this. A normal clone of `main` (or `dev`) gives you everything required to build, test, and contribute — the Dev Setup above is the complete picture. If `bash tests/demo_e2e.sh` passes on a fresh clone, you're ready to open a PR.

If you notice anything referenced in tracked code or docs that appears to live only in a gitignored path, that's a bug — please open an issue. Public-facing functionality should always resolve against files committed to the repo.

## Security

Security vulnerabilities should **not** be filed as public issues. Use [GitHub's private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) or email the maintainer directly.
