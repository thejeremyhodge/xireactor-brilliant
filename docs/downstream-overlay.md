# Downstream private overlays

xireactor-brilliant is Apache-2.0 licensed and explicitly supports private
forks that add proprietary routes, MCP tools, and deployment configuration
without modifying core code. This page describes the convention upstream
follows so that downstream forks can stay conflict-free across upstream
releases.

## Plugin discovery (recommended)

Both the FastAPI app and the MCP server auto-load plugins on startup. A
downstream fork that uses plugins **never edits any upstream file**.

### API plugins

- **Location:** drop a `.py` file into `api/plugins/` (the package directory,
  empty upstream), **or** point the `XIREACTOR_API_PLUGIN_DIR` env var at a
  directory holding `.py` files.
- **Contract:** the module exposes a top-level `register(app)` callable.
  Typical body:

  ```python
  from fastapi import APIRouter
  _router = APIRouter()

  @_router.get("/my-endpoint")
  def _handler(): ...

  def register(app):
      app.include_router(_router, prefix="/private")
  ```

- Modules whose name starts with `_` are skipped. Loading is fail-soft — a
  plugin that raises is logged and skipped without affecting others.

### MCP plugins

- **Location:** `mcp/plugins/` package or `XIREACTOR_MCP_PLUGIN_DIR`.
- **Contract:** module exposes `register(mcp, api)`. The `mcp` arg is the
  `FastMCP` instance and `api` is the `BrilliantClient`. Register tools via
  the same patterns used in [mcp/tools.py](../mcp/tools.py) (e.g.
  `mcp.tool(...)` decorators).

## Reserved directory: `private/`

Any directory named `private/` at any depth is reserved for downstream forks.
Upstream's `.gitignore` ignores `**/private/`, so downstream forks can place
proprietary source files under these paths without risk of accidentally
publishing them if they ever work in a checkout of the public repo.

A typical downstream layout that combines the plugin contract with the
reserved-directory convention:

| Path | Purpose |
|---|---|
| `api/routes/private/` | Source files for proprietary routers. Copied or bind-mounted into `api/plugins/` at deploy time, **or** referenced via `XIREACTOR_API_PLUGIN_DIR`. |
| `mcp/tools_private/` | Source files for proprietary MCP tools. Same pattern via `XIREACTOR_MCP_PLUGIN_DIR`. |
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
