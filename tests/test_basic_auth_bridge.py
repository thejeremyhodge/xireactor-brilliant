"""Unit tests for ``_BasicAuthTokenBodyBridge`` ASGI middleware (T-0263).

The middleware lives in ``mcp/remote_server.py`` and intercepts
``POST /token`` requests carrying ``Authorization: Basic`` credentials.
It decodes the header and injects ``client_id`` / ``client_secret`` into
the urlencoded form body before FastMCP's two POST-body validators
(``ClientAuthenticator``, ``AuthorizationCodeRequest``) see the request.
RFC 6749 §2.3.1 clients (e.g. ``mcp-remote``) put credentials in the
header and MAY omit them from the body — without this bridge FastMCP
401s them. See ST-0208 for the incident that motivated the fix.

The scenarios below exercise the middleware with a minimal ASGI harness
(fake scope / receive / send, in-process downstream recorder). No DB,
no network, no FastMCP.

Run modes:
  - pytest (host, with mcp[cli] SDK installed):
      pytest tests/test_basic_auth_bridge.py -v
  - Standalone (inside the mcp container where pytest is absent):
      docker cp tests/test_basic_auth_bridge.py <mcp-ct>:/tmp/
      docker exec <mcp-ct> python3 /tmp/test_basic_auth_bridge.py

Blocks commit of the ST-0208 fix (middleware + ``uvicorn.run(create_app())``
entry flip in mcp/remote_server.py).
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import sys
import traceback
from pathlib import Path
from urllib.parse import parse_qsl

# Make the bridge importable. When running on the host the module lives
# at ``<repo>/mcp/remote_server.py``; inside the container the file is
# at ``/app/remote_server.py`` and cwd is already ``/app``. Either way
# we add the directory to sys.path so ``from remote_server import …``
# resolves, and Python's `mcp` package (the SDK) keeps resolving from
# site-packages because we do not add the repo root.
_THIS = Path(__file__).resolve()
_CANDIDATES = [
    _THIS.parent.parent / "mcp",  # host-layout
    Path("/app"),                  # in-container layout
]
for _cand in _CANDIDATES:
    if _cand.is_dir() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

try:
    from remote_server import _BasicAuthTokenBodyBridge  # noqa: E402
    _BRIDGE_AVAILABLE = True
    _IMPORT_ERROR: str | None = None
except Exception as exc:  # noqa: BLE001 — surface any import failure to the skip reason
    _BRIDGE_AVAILABLE = False
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


try:
    import pytest  # type: ignore[import-not-found]

    pytestmark = pytest.mark.skipif(
        not _BRIDGE_AVAILABLE,
        reason=(
            "mcp/remote_server.py not importable — requires mcp[cli] SDK "
            f"(pip install -r mcp/requirements.txt). Import error: {_IMPORT_ERROR}"
        ),
    )
except ImportError:
    pytest = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# ASGI harness
# ---------------------------------------------------------------------------


class _AppRecorder:
    """Downstream ASGI app that records the scope + fully-drained body."""

    def __init__(self) -> None:
        self.scope: dict | None = None
        self.body: bytes = b""
        self.called: bool = False

    async def __call__(self, scope, receive, send) -> None:
        self.called = True
        self.scope = scope
        chunks: list[bytes] = []
        more = True
        while more:
            msg = await receive()
            chunks.append(msg.get("body", b""))
            more = msg.get("more_body", False)
        self.body = b"".join(chunks)


def _build_receive(body_chunks: list[bytes]):
    """Build an async ``receive`` that emits the given chunks then stops."""
    pending = list(enumerate(body_chunks))

    async def receive():
        if not pending:
            return {"type": "http.request", "body": b"", "more_body": False}
        idx, chunk = pending.pop(0)
        more_body = idx < len(body_chunks) - 1
        return {"type": "http.request", "body": chunk, "more_body": more_body}

    return receive


async def _noop_send(_msg) -> None:
    return None


def _basic(raw: bytes) -> bytes:
    """Build a well-formed ``Authorization: Basic <b64>`` value."""
    return b"Basic " + base64.b64encode(raw)


def _make_scope(
    *,
    method: str = "POST",
    path: str = "/token",
    authorization: bytes | None = None,
    content_type: bytes | None = b"application/x-www-form-urlencoded",
    body_len: int | None = None,
) -> dict:
    if authorization is None:
        authorization = _basic(b"client-xyz:secret-abc")
    headers: list[tuple[bytes, bytes]] = []
    if authorization is not False:  # explicit False means "omit"
        headers.append((b"authorization", authorization))
    if content_type is not None:
        headers.append((b"content-type", content_type))
    if body_len is not None:
        headers.append((b"content-length", str(body_len).encode("latin-1")))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers,
    }


def _run_bridge(scope: dict, body_chunks: list[bytes]) -> _AppRecorder:
    recorder = _AppRecorder()
    bridge = _BasicAuthTokenBodyBridge(recorder)
    asyncio.run(bridge(scope, _build_receive(body_chunks), _noop_send))
    return recorder


def _header(recorder: _AppRecorder, name: bytes) -> bytes | None:
    assert recorder.scope is not None
    for k, v in recorder.scope["headers"]:
        if k == name:
            return v
    return None


def _form(body: bytes) -> dict[str, str]:
    return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))


# ---------------------------------------------------------------------------
# Happy path — the scenarios the bridge was written for
# ---------------------------------------------------------------------------


def test_basic_header_empty_body_injects_credentials():
    """POST /token + Basic + empty form body → body gains client_id+secret."""
    scope = _make_scope(body_len=0)
    rec = _run_bridge(scope, [b""])

    assert rec.called
    assert _form(rec.body) == {
        "client_id": "client-xyz",
        "client_secret": "secret-abc",
    }
    # Content-Length must be rewritten to match the mutated body.
    assert _header(rec, b"content-length") == str(len(rec.body)).encode("latin-1")


def test_basic_header_preserves_existing_grant_fields():
    """Existing grant_type/code fields survive; credentials appended."""
    body = b"grant_type=authorization_code&code=abc123&redirect_uri=http%3A%2F%2Flocal"
    scope = _make_scope(body_len=len(body))
    rec = _run_bridge(scope, [body])

    form = _form(rec.body)
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "abc123"
    assert form["redirect_uri"] == "http://local"
    assert form["client_id"] == "client-xyz"
    assert form["client_secret"] == "secret-abc"


def test_body_with_existing_client_id_not_overwritten():
    """Body's client_id wins over Basic's; missing secret still injected."""
    body = b"client_id=from-body&grant_type=authorization_code"
    scope = _make_scope(body_len=len(body))
    rec = _run_bridge(scope, [body])

    form = _form(rec.body)
    assert form["client_id"] == "from-body"
    assert form["client_secret"] == "secret-abc"


def test_body_with_both_credentials_present_is_unchanged():
    """Nothing to inject → body bytes pass through exactly."""
    body = b"client_id=x&client_secret=y&grant_type=refresh_token"
    scope = _make_scope(body_len=len(body))
    rec = _run_bridge(scope, [body])

    assert rec.body == body
    # Headers untouched too — original content-length preserved.
    assert _header(rec, b"content-length") == str(len(body)).encode("latin-1")


def test_multi_chunk_body_is_drained_and_replayed_as_one():
    """Streaming body chunks → bridge concatenates, then replays single chunk."""
    chunks = [b"grant_type=", b"authorization_code", b"&code=xyz"]
    scope = _make_scope(body_len=sum(len(c) for c in chunks))
    rec = _run_bridge(scope, chunks)

    form = _form(rec.body)
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "xyz"
    assert form["client_id"] == "client-xyz"
    assert form["client_secret"] == "secret-abc"


def test_urlencoded_basic_credentials_are_percent_decoded():
    """RFC 7617 — Basic-auth userinfo may be percent-encoded."""
    scope = _make_scope(
        authorization=_basic(b"client%3Aid:secret%3Aval"),
        body_len=0,
    )
    rec = _run_bridge(scope, [b""])

    form = _form(rec.body)
    assert form["client_id"] == "client:id"
    assert form["client_secret"] == "secret:val"


# ---------------------------------------------------------------------------
# Pass-through — these requests must not be mutated
# ---------------------------------------------------------------------------


def test_non_post_method_passes_through():
    body = b"client_id=x"
    scope = _make_scope(method="GET", body_len=len(body))
    rec = _run_bridge(scope, [body])
    assert rec.body == body


def test_non_token_path_passes_through():
    body = b"client_id=x"
    scope = _make_scope(path="/register", body_len=len(body))
    rec = _run_bridge(scope, [body])
    assert rec.body == body


def test_no_authorization_header_passes_through():
    body = b"client_id=x&client_secret=y"
    scope = _make_scope(authorization=False, body_len=len(body))  # type: ignore[arg-type]
    rec = _run_bridge(scope, [body])
    assert rec.body == body


def test_bearer_authorization_passes_through():
    body = b"grant_type=refresh_token"
    scope = _make_scope(
        authorization=b"Bearer abc.def.ghi",
        body_len=len(body),
    )
    rec = _run_bridge(scope, [body])
    assert rec.body == body


def test_json_content_type_passes_through():
    """Bridge only handles urlencoded forms — JSON bodies untouched."""
    body = b'{"client_id":"x"}'
    scope = _make_scope(
        content_type=b"application/json",
        body_len=len(body),
    )
    rec = _run_bridge(scope, [body])
    assert rec.body == body


def test_malformed_basic_no_colon_passes_through():
    """Decoded userinfo missing ':' separator → bridge gives up silently."""
    scope = _make_scope(
        authorization=_basic(b"no-colon-here"),
        body_len=0,
    )
    rec = _run_bridge(scope, [b""])
    assert rec.body == b""
    assert "client_id" not in _form(rec.body)


# ---------------------------------------------------------------------------
# Standalone runner — invoked when pytest is not available (e.g. inside
# the mcp container). Mirrors the minimal runner in mcp/test_remote.py.
# ---------------------------------------------------------------------------


def _main() -> int:
    if not _BRIDGE_AVAILABLE:
        print(f"SKIP: {_IMPORT_ERROR}")
        return 0

    mod = sys.modules[__name__]
    tests = sorted(
        (name, fn)
        for name, fn in inspect.getmembers(mod, inspect.isfunction)
        if name.startswith("test_")
    )
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception:
            print(f"  FAIL  {name}")
            traceback.print_exc()
            failed += 1
        else:
            print(f"  PASS  {name}")
            passed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
