"""Tests for the downstream plugin discovery loaders.

Covers both the API-side loader (``api/plugins/__init__.py``) and the
MCP-side loader (``mcp/plugins/__init__.py``). Each is exercised against
an external plugin directory pointed to by its env var, since the
package directories ship empty upstream and using them here would pollute
the source tree.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient


_REPO_ROOT = Path(__file__).resolve().parent.parent
_API_DIR = _REPO_ROOT / "api"
_MCP_DIR = _REPO_ROOT / "mcp"


def _load(module_path: Path, attr_name: str):
    """Load a module by file path and return one of its attributes."""
    spec = importlib.util.spec_from_file_location(
        f"_tested_{module_path.stem}_{attr_name}", str(module_path)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, attr_name)


# ---------------------------------------------------------------------------
# API loader
# ---------------------------------------------------------------------------


@pytest.fixture
def api_load_plugins():
    if str(_API_DIR) not in sys.path:
        sys.path.insert(0, str(_API_DIR))
    # api/plugins/__init__.py — load fresh so env vars are re-read each test.
    return _load(_API_DIR / "plugins" / "__init__.py", "load_plugins")


def test_api_loader_picks_up_external_plugin(tmp_path, monkeypatch, api_load_plugins):
    plugin = tmp_path / "ping_plugin.py"
    plugin.write_text(textwrap.dedent("""
        from fastapi import APIRouter
        _router = APIRouter()

        @_router.get("/plugin-ping")
        def _ping():
            return {"source": "plugin"}

        def register(app):
            app.include_router(_router)
    """))
    monkeypatch.setenv("XIREACTOR_API_PLUGIN_DIR", str(tmp_path))

    app = FastAPI()
    loaded = api_load_plugins(app)
    assert "ping_plugin" in loaded

    body = TestClient(app).get("/plugin-ping").json()
    assert body == {"source": "plugin"}


def test_api_loader_skips_modules_without_register(tmp_path, monkeypatch, api_load_plugins):
    (tmp_path / "no_register.py").write_text("x = 1\n")
    monkeypatch.setenv("XIREACTOR_API_PLUGIN_DIR", str(tmp_path))

    app = FastAPI()
    loaded = api_load_plugins(app)
    assert "no_register" not in loaded


def test_api_loader_swallows_plugin_exceptions(tmp_path, monkeypatch, api_load_plugins):
    (tmp_path / "good.py").write_text(textwrap.dedent("""
        from fastapi import APIRouter
        _r = APIRouter()

        @_r.get("/good")
        def _g():
            return {"ok": True}

        def register(app):
            app.include_router(_r)
    """))
    (tmp_path / "bad.py").write_text(textwrap.dedent("""
        def register(app):
            raise RuntimeError("boom")
    """))
    monkeypatch.setenv("XIREACTOR_API_PLUGIN_DIR", str(tmp_path))

    app = FastAPI()
    loaded = api_load_plugins(app)

    # The good plugin still loaded; the bad one is reported as not-loaded
    # but did NOT raise out of the loader.
    assert "good" in loaded
    assert "bad" not in loaded
    assert TestClient(app).get("/good").json() == {"ok": True}


def test_api_loader_ignores_missing_env_dir(monkeypatch, api_load_plugins, tmp_path):
    monkeypatch.setenv("XIREACTOR_API_PLUGIN_DIR", str(tmp_path / "does-not-exist"))
    app = FastAPI()
    # Should not raise; should return an empty list (no package-resident
    # plugins ship with upstream).
    assert api_load_plugins(app) == []


# ---------------------------------------------------------------------------
# MCP loader
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp_load_plugins():
    return _load(_MCP_DIR / "plugins" / "__init__.py", "load_plugins")


def test_mcp_loader_invokes_register_with_mcp_and_api(tmp_path, monkeypatch, mcp_load_plugins):
    captured: dict = {}
    plugin = tmp_path / "tool_plugin.py"
    plugin.write_text(textwrap.dedent("""
        def register(mcp, api):
            mcp.calls.append(("register", api.tag))
    """))
    monkeypatch.setenv("XIREACTOR_MCP_PLUGIN_DIR", str(tmp_path))

    class _Mcp:
        def __init__(self):
            self.calls: list = []

    class _Api:
        tag = "stub-api"

    mcp, api = _Mcp(), _Api()
    loaded = mcp_load_plugins(mcp, api)

    assert "tool_plugin" in loaded
    assert mcp.calls == [("register", "stub-api")]


def test_mcp_loader_swallows_plugin_exceptions(tmp_path, monkeypatch, mcp_load_plugins):
    (tmp_path / "explodes.py").write_text(textwrap.dedent("""
        def register(mcp, api):
            raise RuntimeError("kaboom")
    """))
    monkeypatch.setenv("XIREACTOR_MCP_PLUGIN_DIR", str(tmp_path))

    # Loader returns cleanly even though the plugin raised.
    assert mcp_load_plugins(object(), object()) == []
