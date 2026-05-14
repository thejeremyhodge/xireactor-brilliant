# Downstream private overlays

xireactor-brilliant is Apache-2.0 licensed and explicitly supports private
forks that add proprietary routes, MCP tools, and deployment configuration
without modifying core code. This page describes the convention upstream
follows so that downstream forks can stay conflict-free across upstream
releases.

## Reserved directory: `private/`

Any directory named `private/` at any depth is reserved for downstream forks.
Upstream's `.gitignore` ignores `**/private/`, so downstream forks can place
proprietary files under these paths without risk of accidentally publishing
them if they ever work in a checkout of the public repo.

The conventional layout downstream forks should use:

| Path | Purpose |
|---|---|
| `api/routes/private/` | Additional FastAPI routers. Mount via [api/main.py](../api/main.py)'s `_route_modules` list — one entry per router. |
| `mcp/tools_private/` | Additional MCP tools. Expose `register_private_tools(mcp, api)` and call it after `register_tools(...)` in [mcp/server.py](../mcp/server.py) and [mcp/remote_server.py](../mcp/remote_server.py). |
| `deploy/private/` | Proprietary `render.yaml`, `docker-compose.override.yml`, branded templates, env samples. |
| `db/migrations/private/` | Proprietary migrations. Use `9xx_*.sql` numbering to avoid collisions with upstream's sequential migrations (currently up to `021_*`). Run as a separate pass after upstream migrations. |
| `tests/private/` | Pytest tree mirroring proprietary code. |

## Recommended fork workflow

A downstream fork keeps two long-lived branches:

- `upstream` — mirror of public `main`; never hand-edited.
- `main` — deployment branch; contains `upstream` plus proprietary commits.

```sh
git remote add upstream https://github.com/thejeremyhodge/xireactor-brilliant.git
git fetch upstream
git checkout upstream && git merge --ff-only upstream/main
git checkout main && git merge upstream
```

In the steady state this should produce zero merge conflicts. If a conflict
appears in an upstream file, that file is a candidate for a generic
extension point upstream (an issue / PR is welcome).

## Apache-2.0 obligations for downstream forks

Apache-2.0 imposes no source-disclosure requirement: private forks can remain
private, even if binaries are distributed to third parties. Two practical
recommendations:

- Add a `NOTICE` file attributing upstream xireactor-brilliant (satisfies
  §4(c) cleanly).
- Header proprietary files with your own copyright so provenance is
  unambiguous on inspection.
