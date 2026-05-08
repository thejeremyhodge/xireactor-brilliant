"""Single source of truth for Brilliant API + skill compatibility versions.

Three constants drive the Sprint 0048 version handshake:

* ``API_VERSION`` — current API release. Bumped on every release cut.
* ``MIN_SKILL_VERSION`` — oldest skill bundle that can talk to this API
  without breaking. Bump only when the MCP tool surface or response
  shapes change in a way an older skill can't handle. Spurious refusals
  are worse than a missing warning, so default to NOT bumping.
* ``LATEST_SKILL_VERSION`` — newest skill bundle published. Always
  matches ``API_VERSION`` (every release ships a fresh skill bundle).

The MCP service imports these too — single repo, single source of truth.
See ``mcp/tools.py::get_version`` for the consumer side, and
``CONTRIBUTING.md`` "Cutting a release" for the bump procedure.
"""

API_VERSION = "0.8.0"
MIN_SKILL_VERSION = "0.7.0"
LATEST_SKILL_VERSION = "0.8.0"
SKILL_DOWNLOAD_URL = (
    "https://github.com/thejeremyhodge/xireactor-brilliant/releases/latest"
)
