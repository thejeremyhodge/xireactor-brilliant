# Contributing to xiReactor Cortex

## Dev Setup

```bash
git clone https://github.com/thejeremyhodge/xireactor-cortex.git
cd xireactor-cortex
cp .env.sample .env
# Edit .env: set ADMIN_PASSWORD to something other than the default
docker compose up -d
# Wait for services to be healthy (~15s)
bash tests/demo_e2e.sh
```

If `demo_e2e.sh` passes, your local stack is working.

## Branch Flow

- Branch from `main`
- One logical change per PR
- Rebase on `main` before requesting review
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

File issues at [github.com/thejeremyhodge/xireactor-cortex/issues](https://github.com/thejeremyhodge/xireactor-cortex/issues).

- **Bugs:** Include `docker compose logs` output and the steps to reproduce
- **Features:** Describe the use case, not just the solution
- **Questions:** Ask away — no issue is too basic

## Security

Security vulnerabilities should **not** be filed as public issues. Use [GitHub's private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) or email the maintainer directly.
