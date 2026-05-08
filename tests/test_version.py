"""Tests for ``GET /version`` and the MCP ``get_version`` tool (Sprint 0048).

Endpoint tests use FastAPI's ``TestClient`` against the bare router so the
suite doesn't require a live DB or admin bootstrap. The MCP tool tests
exercise the helper directly with a stub ``BrilliantClient`` to cover the
happy path and the API-unreachable graceful-fail path.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


_REPO_ROOT = Path(__file__).resolve().parent.parent
_API_DIR = _REPO_ROOT / "api"
_MCP_DIR = _REPO_ROOT / "mcp"

# Both api/ and mcp/ contain a `_version.py`. To avoid the sys.path
# collision, import the api version module explicitly under a distinct
# name (importlib + a spec keyed on the file path) BEFORE adding mcp/ to
# sys.path. Then add mcp/ so `import tools` resolves to mcp/tools.py and
# its sibling `from _version import …` picks up mcp/_version.py.
import importlib.util as _ilu  # noqa: E402

_api_version_spec = _ilu.spec_from_file_location(
    "api_version_constants", str(_API_DIR / "_version.py")
)
api_version_constants = _ilu.module_from_spec(_api_version_spec)
_api_version_spec.loader.exec_module(api_version_constants)
API_VERSION = api_version_constants.API_VERSION
MIN_SKILL_VERSION = api_version_constants.MIN_SKILL_VERSION
LATEST_SKILL_VERSION = api_version_constants.LATEST_SKILL_VERSION
SKILL_DOWNLOAD_URL = api_version_constants.SKILL_DOWNLOAD_URL

# Add api/ for the routes.version import. mcp/tools.py is intentionally
# NOT imported in the test suite (its `from mcp.server.fastmcp import …`
# chain collides with the local `mcp/` directory once sys.path includes
# repo root). The MCP-side helper we want to test lives in
# mcp/_version.py and is loaded directly via importlib.spec — same trick
# as api/_version.py above.
if str(_API_DIR) not in sys.path:
    sys.path.insert(0, str(_API_DIR))

from routes.version import router as version_router  # noqa: E402

_mcp_version_spec = _ilu.spec_from_file_location(
    "mcp_version_module", str(_MCP_DIR / "_version.py")
)
mcp_version_module = _ilu.module_from_spec(_mcp_version_spec)
_mcp_version_spec.loader.exec_module(mcp_version_module)
build_get_version_payload = mcp_version_module.build_get_version_payload
MCP_VERSION = mcp_version_module.MCP_VERSION


# ---------------------------------------------------------------------------
# Endpoint tests — bare-router TestClient avoids DB/lifespan deps
# ---------------------------------------------------------------------------


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(version_router)
    return TestClient(app)


def test_version_endpoint_returns_four_field_shape():
    resp = _client().get("/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "api_version": API_VERSION,
        "min_skill_version": MIN_SKILL_VERSION,
        "latest_skill_version": LATEST_SKILL_VERSION,
        "skill_download_url": SKILL_DOWNLOAD_URL,
    }


def test_version_endpoint_sets_no_cache_header():
    resp = _client().get("/version")
    assert resp.headers.get("cache-control") == "no-cache"


def test_version_endpoint_requires_no_auth():
    # No Authorization header at all — must still return 200.
    resp = _client().get("/version")
    assert resp.status_code == 200
    # And an explicit bogus header doesn't change the outcome.
    resp_bogus = _client().get("/version", headers={"Authorization": "Bearer nope"})
    assert resp_bogus.status_code == 200


def test_version_endpoint_fields_sourced_from_constants():
    body = _client().get("/version").json()
    assert body["api_version"] == API_VERSION
    assert body["min_skill_version"] == MIN_SKILL_VERSION
    assert body["latest_skill_version"] == LATEST_SKILL_VERSION
    assert body["skill_download_url"] == SKILL_DOWNLOAD_URL


# ---------------------------------------------------------------------------
# MCP get_version tests — stub client for happy + unreachable paths
# ---------------------------------------------------------------------------


class _StubClient:
    """Stand-in for ``BrilliantClient`` used by ``get_version_payload``."""

    def __init__(self, response: dict, base_url: str = "https://api.example.test"):
        self._response = response
        self.base_url = base_url

    async def get(self, path, params=None, *, api_key=None, act_as=None):  # noqa: D401
        assert path == "/version"
        return self._response


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_mcp_get_version_happy_path():
    api_resp = {
        "api_version": "0.8.0",
        "min_skill_version": "0.8.0",
        "latest_skill_version": "0.8.0",
        "skill_download_url": SKILL_DOWNLOAD_URL,
    }
    payload = _run(build_get_version_payload(_StubClient(api_resp)))

    assert payload["api_version"] == "0.8.0"
    assert payload["mcp_version"] == MCP_VERSION
    assert payload["min_skill_version"] == "0.8.0"
    assert payload["latest_skill_version"] == "0.8.0"
    assert payload["skill_download_url"] == SKILL_DOWNLOAD_URL
    assert payload["api_url"] == "https://api.example.test"
    assert payload["api_unreachable"] is False


def test_mcp_get_version_api_error_envelope_returns_unreachable():
    """When BrilliantClient surfaces a 4xx/5xx as an error envelope, treat
    the API as unreachable so the skill can still warn the user without
    crashing."""
    err_envelope = {"error": True, "status": 500, "detail": "boom"}
    payload = _run(build_get_version_payload(_StubClient(err_envelope)))

    assert payload["api_unreachable"] is True
    assert payload["api_version"] is None
    assert payload["mcp_version"] == MCP_VERSION
    assert payload["min_skill_version"] is None
    assert payload["latest_skill_version"] is None
    # Local fallback: the skill download URL is known to the MCP itself.
    assert payload["skill_download_url"] == SKILL_DOWNLOAD_URL
    assert payload["api_url"] == "https://api.example.test"


def test_mcp_get_version_api_exception_returns_unreachable():
    """Network-level exception (httpx ConnectError, timeout, etc.) is
    swallowed into the unreachable envelope rather than propagating."""
    class _ExplodingClient:
        base_url = "https://api.example.test"

        async def get(self, *_a, **_kw):
            raise RuntimeError("connection refused")

    payload = _run(build_get_version_payload(_ExplodingClient()))

    assert payload["api_unreachable"] is True
    assert payload["mcp_version"] == MCP_VERSION
    assert payload["api_url"] == "https://api.example.test"
