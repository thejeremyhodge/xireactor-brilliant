"""Unit tests for the "services warming up" banner on ``/setup`` (T-0251).

Spec 0042. Sibling tasks T-0250 (friendly 404 on ``/oauth/login``) and
T-0252 (upload spinner) have their own tests.

The /setup POST handler renders a one-shot credentials page containing
the plaintext API key. On a fresh Render deploy, the MCP service may not
have finished publishing ``mcp_public_url`` to ``brilliant_settings`` by
the time the operator submits the form — at that moment, a
``<div class="warn">`` "warming up" banner must appear so the operator
doesn't paste a not-yet-resolvable URL into Claude. The banner is
additive: the API key still renders in the same response.

These tests exercise the rendering logic directly (``_render_done_page``)
plus the DB-reading helper (``_services_ready``) with a minimal async
pool stub. A full wire-through-the-POST-handler test would flip the
``first_run_complete`` latch, poisoning the rest of the suite — and the
unit tests here cover the same invariant without that side effect.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


# ``api/`` is a flat package — mirror the import path used by the running
# service (see ``api/routes/oauth.py`` which does ``from routes.setup …``).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_API_DIR = _REPO_ROOT / "api"
if str(_API_DIR) not in sys.path:
    sys.path.insert(0, str(_API_DIR))

from routes.setup import _render_done_page, _services_ready  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures — minimal async pool / connection stubs
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    async def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row):
        self._row = row

    async def execute(self, *_args, **_kwargs):
        return _FakeCursor(self._row)


class _FakeConnCtx:
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return _FakeConn(self._row)

    async def __aexit__(self, *_exc):
        return False


class _FakePool:
    """Async context-manager pool stub returning a fixed settings row."""

    def __init__(self, row):
        self._row = row

    def connection(self):
        return _FakeConnCtx(self._row)


class _BrokenPool:
    """Pool stub that raises on connection() — covers the fail-closed path."""

    def connection(self):
        raise RuntimeError("db unreachable")


# ---------------------------------------------------------------------------
# _render_done_page — banner presence / absence
# ---------------------------------------------------------------------------


def _call_render(services_ready: bool) -> str:
    return _render_done_page(
        email="admin@example.com",
        api_key="bkai_adm1_plaintext_demo",
        client_id="client-demo",
        client_secret="secret-demo",
        mcp_url="https://example.test/mcp",
        login_url="https://example.test/auth/login",
        services_ready=services_ready,
    )


def test_done_page_includes_warming_banner_when_services_not_ready():
    body = _call_render(services_ready=False)

    # Banner visible and classed correctly.
    assert 'class="warn" role="status"' in body
    assert "warming up" in body.lower()

    # Copy MUST NOT imply the user loses the key by refreshing — the key is
    # already rendered and safe to copy before any refresh.
    assert "lose the api key if you refresh" not in body.lower()
    assert "you will lose" not in body.lower()

    # Still renders the plaintext API key — banner is additive, not a
    # replacement for the one-shot credentials panel.
    assert "bkai_adm1_plaintext_demo" in body


def test_done_page_omits_warming_banner_when_services_ready():
    body = _call_render(services_ready=True)

    # The static "this is the only time you'll see these secrets" warn block
    # always renders — the banner we care about is the "warming up" one.
    assert "warming up" not in body.lower()
    assert 'role="status"' not in body

    # Plaintext key still present.
    assert "bkai_adm1_plaintext_demo" in body


def test_done_page_never_auto_refreshes():
    """The credentials page shows the API key exactly once. An auto-refresh
    would re-GET /setup/done which 404s once the latch is flipped, burying
    the key. Assert neither code path emits meta-refresh or JS reload.
    """
    for ready in (True, False):
        body = _call_render(services_ready=ready)
        assert "http-equiv=\"refresh\"" not in body.lower()
        assert "http-equiv='refresh'" not in body.lower()
        assert "location.reload" not in body.lower()
        assert "window.location =" not in body.lower()


# ---------------------------------------------------------------------------
# _services_ready — DB-reading helper
# ---------------------------------------------------------------------------


def _run(coro):
    # Fresh loop per call — ``asyncio.run`` avoids the deprecation warning
    # from ``get_event_loop()`` under Python 3.12+.
    return asyncio.run(coro)


def test_services_ready_true_when_both_urls_populated():
    pool = _FakePool(row=("https://api.example.test", "https://mcp.example.test"))
    assert _run(_services_ready(pool)) is True


def test_services_ready_false_when_mcp_url_null():
    pool = _FakePool(row=("https://api.example.test", None))
    assert _run(_services_ready(pool)) is False


def test_services_ready_false_when_api_url_null():
    pool = _FakePool(row=(None, "https://mcp.example.test"))
    assert _run(_services_ready(pool)) is False


def test_services_ready_false_when_both_null():
    pool = _FakePool(row=(None, None))
    assert _run(_services_ready(pool)) is False


def test_services_ready_false_when_singleton_row_missing():
    pool = _FakePool(row=None)
    assert _run(_services_ready(pool)) is False


def test_services_ready_false_on_db_error():
    # Fail-closed: any exception → show the banner (treat as "not ready")
    # rather than silently suppress it.
    assert _run(_services_ready(_BrokenPool())) is False
