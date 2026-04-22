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

Spec 0043 / T-0255 extends these tests with brand-header + pulsing-CTA
+ beforeunload + import-vault-fragment assertions.
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

from routes.setup import (  # noqa: E402
    _render_brand_header,
    _render_done_page,
    _render_setup_form,
    _services_ready,
)


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


# ---------------------------------------------------------------------------
# Spec 0043 / T-0255 — brand header + pulsing CTA + beforeunload guard +
# import-vault fragment link
# ---------------------------------------------------------------------------


def test_brand_header_renders_logo_title_and_tagline():
    """The shared brand header renders the Brilliant mark + tagline.

    The tagline copy must match the README hero ("One shared knowledge
    base for your whole AI-enabled team") so first-run operators see
    identical messaging across the landing page and the installer path.
    """
    header = _render_brand_header()

    assert 'class="brand-header"' in header
    # Logo SVG (gem-shape polygon + "Brilliant logo" aria label)
    assert "Brilliant logo" in header
    # Title SVG (xiReactor / Brilliant wordmark)
    assert "xiReactor / Brilliant" in header
    # Tagline must match the README hero copy verbatim.
    assert (
        "One shared knowledge base for your whole AI-enabled team"
        in header
    )


def test_setup_form_includes_brand_header():
    """The pre-claim /setup form also surfaces the brand mark."""
    body = _render_setup_form()
    assert 'class="brand-header"' in body
    assert "Brilliant logo" in body
    assert "One shared knowledge base for your whole AI-enabled team" in body


def test_done_page_includes_brand_header_and_pulsing_cta():
    """Credentials page must render brand + pulsing download CTA."""
    body = _call_render(services_ready=True)

    # Brand header assertions — same expectations as the shared helper.
    assert 'class="brand-header"' in body
    assert "Brilliant logo" in body
    assert "xiReactor / Brilliant" in body
    assert "One shared knowledge base for your whole AI-enabled team" in body

    # Download CTA has the pulse class + matching keyframes + JS to
    # clear the pulse on first click.
    assert 'id="download"' in body
    assert 'class="pulse"' in body
    assert "@keyframes brilliant-pulse" in body
    # Pulse clears via classList.remove on first click / ack.
    assert 'classList.remove("pulse")' in body


def test_done_page_beforeunload_guard_wired():
    """beforeunload listener + its two clearing paths must be present."""
    body = _call_render(services_ready=True)

    # The listener itself.
    assert "beforeunload" in body
    assert "ev.preventDefault()" in body
    # Two legitimate ways out: download click + saved-ack checkbox.
    assert 'id="saved-ack-checkbox"' in body
    assert "I've saved my credentials" in body
    assert "clearGuard()" in body
    # Dismiss sentinel persists to localStorage so the next page visit
    # doesn't re-arm the pulse.
    assert "brilliant_creds_downloaded" in body


def test_done_page_import_vault_fragment_link():
    """The Import Obsidian vault button carries #api_key=<key> + target=_blank.

    The URL fragment is the handoff channel between /setup and
    /import/vault — it never hits the server because fragments stay
    client-side. The link must also open in a new tab so the operator's
    current tab keeps showing the one-shot credentials.
    """
    body = _render_done_page(
        email="admin@example.com",
        api_key="bkai_plain_xyz_1234567890",
        client_id="client-demo",
        client_secret="secret-demo",
        mcp_url="https://example.test/mcp",
        login_url="https://example.test/auth/login",
        services_ready=True,
    )

    # Fragment-carrying link present with the exact plaintext key.
    assert "/import/vault#api_key=bkai_plain_xyz_1234567890" in body
    # Opens in a new tab with rel=noopener to protect the credentials tab.
    assert 'target="_blank"' in body
    assert 'rel="noopener"' in body
    # Label text cues the operator to what they're about to do.
    assert "Import Obsidian vault" in body


def test_done_page_html_escapes_api_key_in_fragment_href():
    """API keys are alphanumeric in practice, but we still pass them
    through html.escape to guard against a surprise URL-unfriendly
    character ever reaching the href attribute. Confirm the helper is
    in the chain by asserting the rendered href uses the escaped form.
    """
    # "<" in a key would explode the attribute if we didn't escape; this
    # is a belt-and-braces check that the html.escape call is wired in.
    body = _render_done_page(
        email="admin@example.com",
        api_key='abc"def<xyz',
        client_id="cid",
        client_secret="csecret",
        mcp_url="https://example.test/mcp",
        login_url="https://example.test/auth/login",
        services_ready=True,
    )
    # Escaped attribute-safe form of the key must appear in the href.
    assert "abc&quot;def&lt;xyz" in body
    # The raw-unescaped combination must never appear inside an href="…".
    assert 'href="/import/vault#api_key=abc"def<xyz"' not in body
