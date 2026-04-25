"""Shared pytest fixtures for the integration suite.

Consolidates the duplicated skipif + BASE_URL/DB_DSN/ADMIN_KEY wiring that
currently lives at the top of every test_*.py file. Individual test files
can still override by reading the env vars directly.
"""

from __future__ import annotations

import os

import pytest
import requests

BASE_URL = os.environ.get("CORTEX_BASE_URL", "http://localhost:8010")
DB_DSN = os.environ.get(
    "CORTEX_DB_DSN",
    "postgresql://postgres:dev@localhost:5442/cortex",
)
REQUEST_TIMEOUT = 10.0

# Seeded demo keys — present in repo by design (see demo_e2e.sh).
# The API rejects these prefixes in non-dev environments.
ADMIN_KEY = "bkai_adm1_testkey_admin"
EDITOR_KEY = "bkai_edit_testkey_editor"
VIEWER_KEY = "bkai_view_testkey_viewer"
AGENT_KEY = "bkai_agnt_testkey_agent"

try:
    import psycopg  # noqa: F401

    _PSYCOPG_AVAILABLE = True
except ImportError:
    _PSYCOPG_AVAILABLE = False


def _api_available() -> bool:
    try:
        return requests.get(f"{BASE_URL}/health", timeout=2.0).status_code == 200
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    """Auto-skip integration tests when the API isn't reachable."""
    if _api_available():
        return
    skip_reason = pytest.mark.skip(
        reason=(
            f"xiReactor API not reachable at {BASE_URL}. "
            "Start the stack with `docker compose up -d` before running integration tests."
        )
    )
    for item in items:
        item.add_marker(skip_reason)


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def db_dsn() -> str:
    return DB_DSN


@pytest.fixture(scope="session")
def admin_headers() -> dict:
    return {
        "Authorization": f"Bearer {ADMIN_KEY}",
        "Content-Type": "application/json",
    }


@pytest.fixture(scope="session")
def editor_headers() -> dict:
    return {
        "Authorization": f"Bearer {EDITOR_KEY}",
        "Content-Type": "application/json",
    }


@pytest.fixture(scope="session")
def viewer_headers() -> dict:
    return {
        "Authorization": f"Bearer {VIEWER_KEY}",
        "Content-Type": "application/json",
    }


def headers_for(api_key: str) -> dict:
    """Convenience helper for tests that need arbitrary key headers."""
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


@pytest.fixture(scope="session")
def psycopg_available() -> bool:
    return _PSYCOPG_AVAILABLE
