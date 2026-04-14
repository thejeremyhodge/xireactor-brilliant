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

**Releases:** Maintainers periodically merge `dev` → `main` and cut a tagged release. Tags follow semver (`v0.2.0`, `v0.3.0`, etc.).

Don't PR against `main` directly unless it's a documentation-only fix or a critical hotfix.

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

## Issues

File issues at [github.com/thejeremyhodge/xireactor-brilliant/issues](https://github.com/thejeremyhodge/xireactor-brilliant/issues).

- **Bugs:** Include `docker compose logs` output and the steps to reproduce
- **Features:** Describe the use case, not just the solution
- **Questions:** Ask away — no issue is too basic

## Security

Security vulnerabilities should **not** be filed as public issues. Use [GitHub's private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) or email the maintainer directly.
