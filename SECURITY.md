# Security Policy

## Supported Versions

xiReactor Brilliant is pre-1.0. Security patches are issued against the latest tagged release on `main` only. Older tags are not backported.

| Version | Supported |
|---------|-----------|
| 0.2.x   | ✅ (latest) |
| < 0.2   | ❌         |

## Reporting a Vulnerability

**Please do not open public GitHub issues for security reports.**

Use [GitHub's private vulnerability reporting](https://github.com/thejeremyhodge/xireactor-brilliant/security/advisories/new) to submit a report. This creates a private advisory visible only to the maintainers.

Alternatively, email the maintainer directly (contact listed on the maintainer's GitHub profile).

### What to include

- A description of the issue and the impact
- Steps to reproduce (minimum viable repro preferred)
- The affected version, tag, or commit SHA
- Any proof-of-concept code or logs you can share safely

### What to expect

- **Acknowledgement**: within 72 hours
- **Initial assessment**: within 7 days
- **Fix / mitigation timeline**: communicated after triage; varies by severity
- **Disclosure**: coordinated via the GitHub advisory. Reporters who want credit will be named in the advisory and release notes. Anonymous reports are also welcome.

## Scope

In scope:

- The `api/` FastAPI service and its auth/permissions surface
- The `mcp/` server (stdio + Streamable HTTP / OAuth 2.1)
- The database layer (`db/migrations/`), especially RLS policies and permission grants
- The `tools/vault_import.py` ingestion path
- Supply-chain concerns in `requirements.txt` / `Dockerfile`s published under this repo

Out of scope:

- Issues in third-party dependencies that are already tracked upstream (please report those to the upstream project; we'll pick up the fix on our next release)
- Social-engineering attacks against maintainers or contributors
- Denial-of-service via unreasonable load against a self-hosted instance (operator responsibility)
- Findings that require physical access or root on the host
- Findings against the demo credentials shipped in `tests/demo_e2e.sh` — these are intentionally public seed keys for local dev

## Hardening Notes for Operators

A few deployment choices materially affect your security posture:

- **Change `ADMIN_PASSWORD`** from the `change-me-before-first-run` placeholder before exposing the API beyond `localhost`
- **Rotate `ADMIN_API_KEY`** after first boot if you let it auto-generate
- **Do not reuse the demo API keys** (`bkai_*_testkey_*`) outside of local dev — they are public
- **Scope `ANTHROPIC_API_KEY`** to the Tier 3 reviewer use case; use a project-scoped key, not an org-wide key
- **Put the API behind a reverse proxy** with TLS and rate-limiting before exposing to the internet; `docker-compose.yml` binds to `0.0.0.0` by default

## Acknowledgements

Reporters who responsibly disclose issues will be credited in the published advisory and the `CHANGELOG.md` entry for the release that ships the fix, unless they request anonymity.
