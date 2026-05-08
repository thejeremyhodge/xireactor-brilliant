"""MCP-side version constants — kept in lockstep with ``api/_version.py``.

The MCP service container is built with ``dockerContext: ./mcp`` (see
``render.yaml``), so ``api/_version.py`` is NOT importable inside the
remote MCP container. We duplicate the constants here and rely on the
release-cut procedure (``CONTRIBUTING.md`` "Cutting a release") to keep
both files in sync.

The Sprint 0048 release dance bumps four version strings together:
``api/_version.py``, ``mcp/_version.py``, ``skill/SKILL.md`` frontmatter,
and the ``CHANGELOG.md`` heading. Drift between this file and
``api/_version.py`` is the symptom of a missed step.
"""

MCP_VERSION = "0.8.0"
SKILL_DOWNLOAD_URL_FALLBACK = (
    "https://github.com/thejeremyhodge/xireactor-brilliant/releases/latest"
)


async def build_get_version_payload(api) -> dict:
    """Assemble the ``get_version`` MCP tool response.

    Calls ``GET /version`` on the API via ``BrilliantClient`` and merges
    the response with this MCP service's own ``MCP_VERSION`` plus the API
    URL it dialed. On any failure (HTTP error envelope OR a thrown
    exception) we degrade gracefully: return the MCP-local version with
    ``api_unreachable: true`` so the skill can still surface a useful
    message instead of crashing the session-start handshake.

    Lives on ``mcp/_version.py`` (rather than ``mcp/tools.py``) so the
    test suite can import it without dragging in FastMCP's heavyweight
    ``mcp.server.fastmcp`` import chain — which collides with the local
    ``mcp/`` directory name when both are on sys.path.

    ``api`` is duck-typed (``BrilliantClient``-shaped: ``base_url`` attr +
    awaitable ``get(path, params=None, *, api_key=None, act_as=None)``).
    """
    api_url = getattr(api, "base_url", "") or ""
    unreachable_envelope = {
        "api_version": None,
        "mcp_version": MCP_VERSION,
        "min_skill_version": None,
        "latest_skill_version": None,
        "skill_download_url": SKILL_DOWNLOAD_URL_FALLBACK,
        "api_url": api_url,
        "api_unreachable": True,
    }

    try:
        resp = await api.get("/version")
    except Exception:  # noqa: BLE001 — never crash session-start handshake
        return unreachable_envelope

    if not isinstance(resp, dict) or resp.get("error") is True:
        return unreachable_envelope

    return {
        "api_version": resp.get("api_version"),
        "mcp_version": MCP_VERSION,
        "min_skill_version": resp.get("min_skill_version"),
        "latest_skill_version": resp.get("latest_skill_version"),
        "skill_download_url": resp.get("skill_download_url")
        or SKILL_DOWNLOAD_URL_FALLBACK,
        "api_url": api_url,
        "api_unreachable": False,
    }
