"""Integration tests for the tar → blob → server-parse vault import path.

Spec 0040 / T-0237. Exercises the full wet-test-driven replacement for the
deleted ``import_vault_content`` MCP tool:

    1. Build a gzipped tarball of markdown files in-memory.
    2. Upload it to ``POST /attachments`` → capture ``blob_id``.
    3. POST ``{"blob_id": ...}`` to ``POST /import/vault-from-blob``.
    4. Assert the response counts, exclusion filtering, and size-cap 413s.

Test matrix:
  * happy path — 3 files where one is ``.obsidian/workspace.json`` → 2 imported
  * uncompressed cap (zip-bomb guard) — tarball that expands past
    ``MAX_VAULT_UNCOMPRESSED_BYTES`` returns 413 and leaves
    ``import_batches`` count unchanged.
  * compressed cap — a blob larger than ``MAX_VAULT_TARBALL_BYTES`` returns
    413. We ship a tiny ``MAX_VAULT_TARBALL_BYTES`` via a per-test
    ``docker-compose`` override (mirrors the pattern in
    ``tests/test_attachments.py::api_with_small_max_bytes``).

Prerequisites
-------------
  1. ``docker compose up -d --build``   (API on :8010, Postgres on :5442)
  2. ``pip install -r tests/requirements-dev.txt``
  3. Migrations applied through 025 (blobs + attachments).

Run
---
  pytest tests/test_vault_import_from_blob.py -v
"""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import tarfile
import textwrap
import time
import uuid
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent


try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _REQUESTS_AVAILABLE = False

try:
    import psycopg
    _PSYCOPG_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PSYCOPG_AVAILABLE = False


# ----------------------------------------------------------------------------
# Configuration — matches the main worktree defaults (API :8010, DB :5442)
# ----------------------------------------------------------------------------

BASE_URL = os.environ.get("BRILLIANT_BASE_URL", "http://localhost:8010")
DB_DSN = os.environ.get(
    "BRILLIANT_DB_DSN",
    "postgresql://postgres:dev@localhost:5442/brilliant",
)

# Admin key from db/migrations/005_seed.sql — same key used across the suite.
ADMIN_KEY = os.environ.get("ADMIN_KEY", "bkai_adm1_testkey_admin")

COMPOSE_PROJECT_NAME = os.environ.get("COMPOSE_PROJECT_NAME", "")
API_SERVICE = os.environ.get("API_SERVICE", "api")

REQUEST_TIMEOUT = 30.0


# ----------------------------------------------------------------------------
# Skip markers — keep the file silently skipped when the stack is down so it
# slots cleanly into the default `pytest tests/` sweep.
# ----------------------------------------------------------------------------


def _api_available() -> bool:
    if not _REQUESTS_AVAILABLE:
        return False
    try:
        return requests.get(f"{BASE_URL}/health", timeout=2.0).status_code == 200
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(
        not _REQUESTS_AVAILABLE,
        reason="requests not installed; pip install -r tests/requirements-dev.txt",
    ),
    pytest.mark.skipif(
        not _PSYCOPG_AVAILABLE,
        reason="psycopg not installed; pip install -r tests/requirements-dev.txt",
    ),
    pytest.mark.skipif(
        not _api_available(),
        reason=f"Brilliant API not reachable at {BASE_URL} "
        f"(start `docker compose up -d`).",
    ),
]


# ----------------------------------------------------------------------------
# Admin key fixture — provision a fresh bcrypt-hashed API key directly in the
# DB so the test suite doesn't depend on whatever state the seeded
# ``bkai_adm1_testkey_admin`` key is in on a long-lived local stack. Wet-test
# flows have been observed to rotate or invalidate it; this fixture is
# robust against that.
# ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def admin_key() -> str:
    """Insert an ``admin`` API key for ``usr_admin`` + ``org_demo`` and clean
    up on teardown. Returns the plaintext bearer token.

    Falls back to the seeded ``ADMIN_KEY`` if the insert fails (e.g. running
    against a fresh stack where the env-provided key already works)."""
    # Cheap probe: if ADMIN_KEY already works, just use it.
    try:
        probe = requests.get(
            f"{BASE_URL}/entries?limit=1",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=5.0,
        )
        if probe.status_code in (200, 404):  # 404 == route-name drift, still authed
            # 401 is the signal to provision a fresh key.
            if probe.status_code != 401:
                yield ADMIN_KEY
                return
    except Exception:
        pass

    suffix = uuid.uuid4().hex[:8]
    # API auth lookup keys off the first 9 chars of the token.
    key_prefix = f"bkai_{suffix[:4]}"
    token = f"{key_prefix}_testkey_vault_{suffix}"

    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO api_keys (user_id, org_id, key_hash, key_prefix,
                                      key_type, label)
                VALUES ('usr_admin', 'org_demo',
                        crypt(%s, gen_salt('bf')), %s,
                        'interactive', %s)
                """,
                (token, key_prefix, f"test-vault-import-{suffix}"),
            )

    try:
        yield token
    finally:
        with psycopg.connect(DB_DSN, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM api_keys WHERE key_prefix = %s", (key_prefix,)
                )


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _build_tarball(entries: list[tuple[str, bytes]]) -> bytes:
    """Build a gzipped tarball of ``(arcname, content_bytes)`` pairs in-memory.

    Mirrors the shape Co-work Claude produces with ``tar czf vault.tgz .`` —
    flat archive, POSIX names, no metadata other than size + regular file.
    """
    buf = io.BytesIO()
    # Deterministic mtime so successive calls produce the same bytes when the
    # content matches — helps the same-org dedup path stay predictable in
    # case a later test relies on a fresh blob_id.
    now = time.time()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for arcname, content in entries:
            info = tarfile.TarInfo(name=arcname)
            info.size = len(content)
            info.mtime = int(now)
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _unique_tarball(entries: list[tuple[str, bytes]]) -> bytes:
    """Return a tarball with a uuid-tagged sentinel file so sha256 is unique.

    The ``/attachments`` endpoint dedups by sha256 within an org, so if two
    tests upload identical tarball bytes the second call gets the first
    test's ``blob_id`` back. That's fine semantically but defeats per-test
    isolation. Injecting a uuid-tagged suffix guarantees a fresh blob row.
    """
    tag = uuid.uuid4().hex
    salted = list(entries) + [
        (f".brilliant-test-salt-{tag}.txt", f"salt={tag}\n".encode()),
    ]
    return _build_tarball(salted)


def _upload_tarball(data: bytes, *, key: str) -> requests.Response:
    return requests.post(
        f"{BASE_URL}/attachments",
        headers=_auth(key),
        files={"file": ("vault.tgz", data, "application/gzip")},
        timeout=REQUEST_TIMEOUT,
    )


def _import_from_blob(
    blob_id: str,
    *,
    key: str,
    source_vault: str | None = None,
    base_path: str | None = None,
    excludes: list[str] | None = None,
) -> requests.Response:
    body: dict = {"blob_id": blob_id}
    if source_vault is not None:
        body["source_vault"] = source_vault
    if base_path is not None:
        body["base_path"] = base_path
    if excludes is not None:
        body["excludes"] = excludes
    return requests.post(
        f"{BASE_URL}/import/vault-from-blob",
        headers={**_auth(key), "Content-Type": "application/json"},
        json=body,
        timeout=REQUEST_TIMEOUT,
    )


def _count_import_batches() -> int:
    """Admin-owned count of ``import_batches`` rows (bypasses RLS via superuser)."""
    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM import_batches")
            return int(cur.fetchone()[0])


# ----------------------------------------------------------------------------
# Case 1 — happy path: 3-file tarball with .obsidian/ filtered out
# ----------------------------------------------------------------------------


def test_happy_path_filters_obsidian_and_imports_two_files(admin_key):
    """Tarball with ``note.md``, ``sub/other.md``, ``.obsidian/workspace.json``
    should produce exactly 2 imported items (the ``.obsidian/`` file gets
    filtered by ``DEFAULT_EXCLUDES``)."""
    run_tag = uuid.uuid4().hex[:8]
    tar_bytes = _unique_tarball(
        [
            (
                "note.md",
                (
                    f"---\n"
                    f"title: Top Note {run_tag}\n"
                    f"content_type: context\n"
                    f"---\n"
                    f"# Top Note {run_tag}\n\n"
                    f"Body content for top note.\n"
                ).encode(),
            ),
            (
                "sub/other.md",
                (
                    f"# Other Note {run_tag}\n\n"
                    f"Body for the subdir note.\n"
                ).encode(),
            ),
            (
                ".obsidian/workspace.json",
                b'{"main": {"id": "root"}, "left": {}}\n',
            ),
        ]
    )

    # Step 1 — upload as blob
    up = _upload_tarball(tar_bytes, key=admin_key)
    assert up.status_code == 201, up.text
    blob_id = up.json()["blob_id"]
    assert blob_id

    # Step 2 — import from blob, under a unique base_path so we don't collide
    # with previous test runs.
    base = f"test-vault-{run_tag}"
    resp = _import_from_blob(
        blob_id, key=admin_key, source_vault=base, base_path=base
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    # Admin + interactive key → direct-insert path → ``created == 2`` and
    # ``staged == 0``. The ``.obsidian/workspace.json`` must be filtered.
    assert body["batch_id"], "missing batch_id in response"
    assert body["created"] + body["staged"] == 2, (
        f"expected 2 items imported (two .md files, .obsidian/ filtered), "
        f"got created={body['created']} staged={body['staged']} body={body}"
    )

    # DB-level check: confirm no entry references the .obsidian path. We
    # also confirm the two .md paths landed under the test prefix.
    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT logical_path FROM entries "
                "WHERE import_batch_id = %s::uuid",
                (body["batch_id"],),
            )
            paths = {row[0] for row in cur.fetchall()}
            cur.execute(
                "SELECT target_path FROM staging "
                "WHERE import_batch_id = %s::uuid",
                (body["batch_id"],),
            )
            paths.update(row[0] for row in cur.fetchall())

    assert all(".obsidian" not in p for p in paths), (
        f".obsidian files leaked into the batch: {paths}"
    )
    assert any("note" in p for p in paths), (
        f"note.md missing from imported paths: {paths}"
    )
    assert any("sub/other" in p for p in paths), (
        f"sub/other.md missing from imported paths: {paths}"
    )


# ----------------------------------------------------------------------------
# Case 2 — explicit .obsidian filter assertion (no staging/entry row)
# ----------------------------------------------------------------------------


def test_obsidian_file_does_not_produce_staging_or_entry(admin_key):
    """A tarball containing ``.obsidian/workspace.json`` must not create a
    staging row or entry for that file — the default excludes filter it out
    inside ``iter_tarball_md`` before the import pipeline runs."""
    run_tag = uuid.uuid4().hex[:8]
    tar_bytes = _unique_tarball(
        [
            (
                "kept.md",
                f"# Kept Note {run_tag}\n\nBody.\n".encode(),
            ),
            (
                ".obsidian/workspace.json",
                b'{"x": 1}\n',
            ),
            (
                ".obsidian/plugins/foo.json",
                b'{"y": 2}\n',
            ),
        ]
    )

    up = _upload_tarball(tar_bytes, key=admin_key)
    assert up.status_code == 201, up.text
    blob_id = up.json()["blob_id"]

    base = f"test-obsidian-{run_tag}"
    resp = _import_from_blob(
        blob_id, key=admin_key, source_vault=base, base_path=base
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    batch_id = body["batch_id"]

    # Only the single kept.md should be imported; nothing from .obsidian/.
    assert body["created"] + body["staged"] == 1, (
        f"expected exactly 1 imported file, got {body}"
    )

    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT logical_path FROM entries "
                "WHERE import_batch_id = %s::uuid",
                (batch_id,),
            )
            entry_paths = [row[0] for row in cur.fetchall()]
            cur.execute(
                "SELECT target_path FROM staging "
                "WHERE import_batch_id = %s::uuid",
                (batch_id,),
            )
            staging_paths = [row[0] for row in cur.fetchall()]

    all_paths = entry_paths + staging_paths
    assert len(all_paths) == 1, (
        f"expected exactly one DB row for the batch, got {all_paths}"
    )
    for p in all_paths:
        assert ".obsidian" not in p, (
            f"an .obsidian path leaked into DB: {p}"
        )


# ----------------------------------------------------------------------------
# Case 3 — uncompressed cap trips 413 and does not write any rows
# ----------------------------------------------------------------------------
#
# Gzip compresses zero-filled (or highly repetitive) content extremely well,
# so a tarball of a few zero-padded ``.md`` files can stay well under the
# default 25MB compressed cap while expanding past the 200MB uncompressed
# cap mid-iteration. ``iter_tarball_md`` raises ``ValueError`` once cumulative
# member.size crosses ``MAX_VAULT_UNCOMPRESSED_BYTES``; the route handler
# translates that to a 413. No entries / staging rows / import_batches rows
# should be written — the handler bails before ``_execute_import`` runs.


def _build_zero_bomb_tarball(per_file_bytes: int, count: int) -> bytes:
    """Build a tarball of ``count`` zero-padded ``.md`` files, each of size
    ``per_file_bytes``. The gz-compressed output is tiny (a few hundred KB)
    but the uncompressed size is ``count * per_file_bytes``.

    Each file gets a tiny valid-markdown header so the importer wouldn't
    reject them on format grounds — the point of the test is the
    ``iter_tarball_md`` cap, not downstream parsing."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        now = int(time.time())
        header = b"# zero-bomb\n\n"
        pad = b"\x00" * max(per_file_bytes - len(header), 0)
        payload = header + pad
        for i in range(count):
            name = f"bomb_{i}.md"
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            info.mtime = now
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def test_uncompressed_cap_returns_413_and_preserves_batch_count(admin_key):
    """Tarball that expands past ``MAX_VAULT_UNCOMPRESSED_BYTES`` (default
    200MB) must return 413 and must not add any import_batches rows."""
    # 25 files * 10MB = 250MB uncompressed, well past the 200MB cap.
    per_file = 10 * 1024 * 1024  # 10 MiB
    tar_bytes = _build_zero_bomb_tarball(per_file_bytes=per_file, count=25)

    # Sanity: the compressed size must stay under the 25MB compressed cap
    # so we actually reach the uncompressed-cap branch (rather than 413ing
    # on the compressed probe).
    assert len(tar_bytes) < 25_000_000, (
        f"zero-bomb tarball too big compressed ({len(tar_bytes)} bytes); "
        f"compressed cap would trip before the uncompressed one"
    )

    up = _upload_tarball(tar_bytes, key=admin_key)
    assert up.status_code == 201, up.text
    blob_id = up.json()["blob_id"]

    before_count = _count_import_batches()

    resp = _import_from_blob(blob_id, key=admin_key)
    assert resp.status_code == 413, (
        f"expected 413 for uncompressed-cap breach, got {resp.status_code}: "
        f"{resp.text}"
    )
    detail = ""
    try:
        detail = resp.json().get("detail", "")
    except Exception:
        detail = resp.text
    # The walker's error message mentions "max_uncompressed" — the route
    # passes it straight through.
    assert "uncompressed" in detail.lower() or "limit" in detail.lower(), (
        f"413 detail should reference the uncompressed cap: {detail!r}"
    )

    # Rolling out of the endpoint before ``_execute_import`` fires means
    # no batch row should have landed.
    after_count = _count_import_batches()
    assert after_count == before_count, (
        f"uncompressed-cap 413 leaked an import_batches row: "
        f"before={before_count} after={after_count}"
    )


# ----------------------------------------------------------------------------
# Case 4 — compressed cap trips 413 via docker-compose env override
# ----------------------------------------------------------------------------
#
# The route reads ``MAX_VAULT_TARBALL_BYTES`` from ``os.environ`` at every
# request, so to exercise the 413-on-blob-size branch we restart the API
# container with a small cap via a per-test override file. Mirrors the
# pattern in ``tests/test_attachments.py::api_with_small_max_bytes`` so the
# override leaves no footprint on the repo's committed compose state if the
# test crashes mid-run.


_SMALL_MAX_TARBALL = 2048  # 2 KiB — any real tarball we build overruns this.

_TEST_OVERRIDE_FILENAME = "docker-compose.test-vault-413.yml"


def _compose_available() -> bool:
    try:
        r = subprocess.run(
            ["docker", "compose", "version"],
            check=False,
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _wait_for_api(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(f"{BASE_URL}/health", timeout=2.0).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _compose_files() -> list[str]:
    base = _REPO_ROOT / "docker-compose.yml"
    override = _REPO_ROOT / "docker-compose.override.yml"
    files = ["-f", str(base)]
    if override.exists():
        files += ["-f", str(override)]
    return files


def _build_test_override(max_tarball_bytes: int) -> str:
    return (
        "# Auto-written by tests/test_vault_import_from_blob.py.\n"
        "# Safe to delete — regenerated on demand.\n"
        "services:\n"
        "  api:\n"
        "    environment:\n"
        f"      MAX_VAULT_TARBALL_BYTES: \"{max_tarball_bytes}\"\n"
    )


@pytest.fixture
def api_with_small_tarball_cap():
    """Restart the API with ``MAX_VAULT_TARBALL_BYTES=2048`` for the duration
    of one test, then tear the override back out."""
    if not _compose_available():
        pytest.skip("`docker compose` CLI not available; cannot exercise 413 path")

    override_path = _REPO_ROOT / _TEST_OVERRIDE_FILENAME
    override_path.write_text(_build_test_override(_SMALL_MAX_TARBALL))

    env = {**os.environ}
    if COMPOSE_PROJECT_NAME:
        env["COMPOSE_PROJECT_NAME"] = COMPOSE_PROJECT_NAME
    compose_files = _compose_files() + ["-f", str(override_path)]

    def _up(extra_files: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", "compose", *extra_files, "up", "-d", API_SERVICE],
            cwd=str(_REPO_ROOT),
            env=env,
            check=True,
            capture_output=True,
            timeout=60,
        )

    try:
        _up(compose_files)
        if not _wait_for_api(timeout=30):
            pytest.fail("API failed to come back up after override restart")
        yield _SMALL_MAX_TARBALL
    finally:
        try:
            override_path.unlink()
        except FileNotFoundError:
            pass
        try:
            _up(_compose_files())
            _wait_for_api(timeout=30)
        except Exception as e:  # pragma: no cover — teardown best-effort
            sys.stderr.write(
                f"WARNING: failed to restore API after vault-413 test: {e}\n"
            )


def test_compressed_cap_returns_413(api_with_small_tarball_cap, admin_key):
    """With ``MAX_VAULT_TARBALL_BYTES`` clamped small, a routine 3-file
    tarball (well over 2 KiB once gz headers + metadata are counted) must
    be rejected with a 413 before any parsing happens."""
    small_max = api_with_small_tarball_cap
    run_tag = uuid.uuid4().hex[:8]

    # Use random-looking bytes (hex-encoded) so gzip can't squash the payload
    # below the cap. ``os.urandom`` + ``.hex()`` produces uniformly-random
    # ASCII that compresses poorly — a few KiB of it stays a few KiB.
    def _incompressible(n: int) -> bytes:
        return os.urandom(n).hex().encode()

    tar_bytes = _unique_tarball(
        [
            (f"{run_tag}-one.md",   b"# one\n\n"   + _incompressible(2048)),
            (f"{run_tag}-two.md",   b"# two\n\n"   + _incompressible(2048)),
            (f"{run_tag}-three.md", b"# three\n\n" + _incompressible(2048)),
        ]
    )
    assert len(tar_bytes) > small_max, (
        f"fixture tarball ({len(tar_bytes)} bytes) is not larger than the "
        f"configured cap ({small_max}); test would not exercise 413 path"
    )

    up = _upload_tarball(tar_bytes, key=admin_key)
    assert up.status_code == 201, up.text
    blob_id = up.json()["blob_id"]

    before_count = _count_import_batches()

    resp = _import_from_blob(blob_id, key=admin_key)
    assert resp.status_code == 413, (
        f"expected 413 for compressed-cap breach, got {resp.status_code}: "
        f"{resp.text}"
    )
    detail = ""
    try:
        detail = resp.json().get("detail", "")
    except Exception:
        detail = resp.text
    assert "MAX_VAULT_TARBALL_BYTES" in detail or str(small_max) in detail, (
        f"413 detail should reference the tarball cap: {detail!r}"
    )

    # No batch row should have been written — the 413 fires before
    # ``_execute_import`` runs.
    after_count = _count_import_batches()
    assert after_count == before_count, (
        f"compressed-cap 413 leaked an import_batches row: "
        f"before={before_count} after={after_count}"
    )


# ----------------------------------------------------------------------------
# Case 5 — inline-bytes upload (Sprint 0040a / T-0244)
# ----------------------------------------------------------------------------
#
# The MCP tool ``upload_attachment`` gained a ``content_base64`` + ``filename``
# mode so remote Co-work can transmit tarball bytes inline (the remote MCP
# can't read Co-work's sandbox filesystem). Two shapes of coverage:
#
#   (a) + (b) ``test_inline_bytes_upload_chains_to_import_from_blob`` —
#       encodes a tarball as base64, decodes it, uploads via HTTP multipart
#       (the pathway the MCP tool takes after decoding), captures the
#       ``blob_id``, and chains into ``POST /import/vault-from-blob``.
#       Covers the wet-flow end-to-end at the server-contract level.
#
#   (c) ``test_mcp_tool_rejects_invalid_base64`` — invokes the MCP tool
#       function via a subprocess with ``cwd=mcp/`` so the local ``mcp/``
#       namespace does not shadow the installed ``mcp`` SDK. Verifies the
#       400-shape error dict, ``detail`` mentions base64, and supplying
#       neither / both of ``path`` / ``content_base64`` is also rejected.


def test_inline_bytes_upload_chains_to_import_from_blob(admin_key):
    """End-to-end coverage of the inline-bytes pathway (T-0244 ACs a + b).

    Mirrors what ``upload_attachment(content_base64=..., filename=...)``
    does: base64-decode the tarball and POST the bytes to ``/attachments``
    as multipart. The decoded-bytes round-trip must match the original
    tarball (byte-identical), the upload must return a ``blob_id``, and
    that blob_id must chain cleanly into ``/import/vault-from-blob`` with
    a non-empty ``{created, staged}`` count.
    """
    run_tag = uuid.uuid4().hex[:8]
    tar_bytes = _unique_tarball(
        [
            (
                "inline.md",
                (
                    f"---\n"
                    f"title: Inline Note {run_tag}\n"
                    f"content_type: context\n"
                    f"---\n"
                    f"# Inline Note {run_tag}\n\nBody.\n"
                ).encode(),
            ),
            (
                "nested/also.md",
                f"# Nested {run_tag}\n\nNested body.\n".encode(),
            ),
        ]
    )

    # Round-trip through base64 (the MCP tool calls
    # ``base64.b64decode(content_base64, validate=True)`` on the client
    # string before forwarding the bytes to ``/attachments``).
    b64 = base64.b64encode(tar_bytes).decode("ascii")
    decoded = base64.b64decode(b64, validate=True)
    assert decoded == tar_bytes, "base64 round-trip corrupted tarball bytes"

    up = requests.post(
        f"{BASE_URL}/attachments",
        headers=_auth(admin_key),
        files={"file": ("vault.tgz", decoded, "application/gzip")},
        params={"content_type": "application/gzip", "digest": "false"},
        timeout=REQUEST_TIMEOUT,
    )
    assert up.status_code == 201, up.text
    up_body = up.json()
    blob_id = up_body.get("blob_id")
    assert blob_id, f"upload response missing blob_id: {up_body}"
    assert up_body.get("content_type") == "application/gzip"

    base = f"test-inline-{run_tag}"
    resp = _import_from_blob(
        blob_id, key=admin_key, source_vault=base, base_path=base
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["batch_id"], f"import response missing batch_id: {body}"
    assert body["created"] + body["staged"] == 2, (
        f"expected 2 imported files (both .md), got {body}"
    )


# ----------------------------------------------------------------------------
# Case 6 — browser multipart upload (Sprint 0040b / T-0245..T-0248)
# ----------------------------------------------------------------------------
#
# ``POST /import/vault-upload`` is the MCP-bypass path: a user's browser POSTs
# the tarball as multipart form-data with a Bearer token. The endpoint writes
# the bytes to the blob store (same ``services.storage`` pipeline as
# ``/attachments``) and runs ``_execute_import`` inline, returning the
# ``ImportExecuteResponse`` shape plus a ``blob_id``.
#
# These tests hit the HTTP endpoint directly — no MCP subprocess — because the
# endpoint is the unit under test. Size-cap coverage reuses the
# ``api_with_small_tarball_cap`` fixture that the ``/vault-from-blob`` 413
# test defines above.


def _upload_vault_multipart(
    data: bytes,
    *,
    key: str | None,
    source_vault: str | None = None,
    base_path: str | None = None,
    excludes: str | None = None,
) -> requests.Response:
    """POST a tarball to ``/import/vault-upload`` as multipart form-data.

    ``key=None`` deliberately omits the Authorization header so the auth-
    required test can assert 401 on anonymous callers.
    """
    headers: dict = {}
    if key is not None:
        headers.update(_auth(key))
    data_fields: dict = {}
    if source_vault is not None:
        data_fields["source_vault"] = source_vault
    if base_path is not None:
        data_fields["base_path"] = base_path
    if excludes is not None:
        data_fields["excludes"] = excludes
    return requests.post(
        f"{BASE_URL}/import/vault-upload",
        headers=headers,
        files={"file": ("vault.tgz", data, "application/gzip")},
        data=data_fields or None,
        timeout=REQUEST_TIMEOUT,
    )


def test_browser_upload_happy_path(admin_key):
    """Multipart POST of a 2-file tarball → 201 with non-null batch_id and
    ``created + staged == 2`` (admin direct-insert path).

    This is the end-to-end browser pathway: skip the ``/attachments`` pre-
    upload, hand the tarball bytes straight to ``/import/vault-upload``,
    and assert the endpoint internally writes the blob row + runs the
    shared ``_execute_import`` pipeline.
    """
    run_tag = uuid.uuid4().hex[:8]
    tar_bytes = _unique_tarball(
        [
            (
                "browser.md",
                (
                    f"---\n"
                    f"title: Browser Note {run_tag}\n"
                    f"content_type: context\n"
                    f"---\n"
                    f"# Browser Note {run_tag}\n\nBody.\n"
                ).encode(),
            ),
            (
                "sub/browser-nested.md",
                f"# Nested Browser {run_tag}\n\nNested body.\n".encode(),
            ),
        ]
    )

    base = f"test-browser-upload-{run_tag}"
    resp = _upload_vault_multipart(
        tar_bytes,
        key=admin_key,
        source_vault=base,
        base_path=base,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body.get("batch_id"), f"missing batch_id in response: {body}"
    assert body["created"] + body["staged"] == 2, (
        f"expected 2 imported files (both .md), "
        f"got created={body['created']} staged={body['staged']} body={body}"
    )


def test_browser_upload_requires_auth():
    """POST without a Bearer token → 401. Ensures the public surface stays
    closed — no vault can be uploaded anonymously."""
    tar_bytes = _unique_tarball(
        [("anon.md", b"# Anonymous\n\nShould never land.\n")]
    )
    resp = _upload_vault_multipart(tar_bytes, key=None)
    assert resp.status_code == 401, (
        f"expected 401 for anonymous caller, got {resp.status_code}: "
        f"{resp.text}"
    )


def test_browser_upload_enforces_size_cap(api_with_small_tarball_cap, admin_key):
    """With ``MAX_VAULT_TARBALL_BYTES`` clamped to 2 KiB, a routine multi-file
    tarball must be rejected with a 413 **before** any ``import_batches``
    row is written (the cap is enforced inline on the streaming read, so
    no DB work runs)."""
    small_max = api_with_small_tarball_cap
    run_tag = uuid.uuid4().hex[:8]

    # Random-looking payload so gzip can't squash it below the cap.
    def _incompressible(n: int) -> bytes:
        return os.urandom(n).hex().encode()

    tar_bytes = _unique_tarball(
        [
            (f"{run_tag}-a.md", b"# a\n\n" + _incompressible(2048)),
            (f"{run_tag}-b.md", b"# b\n\n" + _incompressible(2048)),
            (f"{run_tag}-c.md", b"# c\n\n" + _incompressible(2048)),
        ]
    )
    assert len(tar_bytes) > small_max, (
        f"fixture tarball ({len(tar_bytes)} bytes) is not larger than the "
        f"configured cap ({small_max}); test would not exercise 413 path"
    )

    before_count = _count_import_batches()

    resp = _upload_vault_multipart(
        tar_bytes,
        key=admin_key,
        source_vault=f"test-browser-413-{run_tag}",
        base_path=f"test-browser-413-{run_tag}",
    )
    assert resp.status_code == 413, (
        f"expected 413 for oversize upload, got {resp.status_code}: "
        f"{resp.text}"
    )
    detail = ""
    try:
        detail = resp.json().get("detail", "")
    except Exception:
        detail = resp.text
    assert "MAX_VAULT_TARBALL_BYTES" in detail or str(small_max) in detail, (
        f"413 detail should reference the tarball cap: {detail!r}"
    )

    after_count = _count_import_batches()
    assert after_count == before_count, (
        f"browser-upload 413 leaked an import_batches row: "
        f"before={before_count} after={after_count}"
    )


# ----------------------------------------------------------------------------
# Subprocess harness for the MCP-tool-level validation test.
# ----------------------------------------------------------------------------
#
# The repo's top-level ``mcp/`` directory is a namespace package that
# shadows the installed ``mcp`` SDK when imported from the repo root. The
# Docker image works around this by running from ``cwd=mcp/``; we do the
# same here via subprocess so the test can invoke the real tool function.


_MCP_DIR = _REPO_ROOT / "mcp"


def _mcp_sdk_installed() -> bool:
    """True iff the ``mcp`` SDK is importable from ``cwd=mcp/``.

    Running from inside ``mcp/`` disambiguates: the local files are then
    the top of ``sys.path`` as individual modules (``tools``, ``client``),
    and ``import mcp`` resolves to the site-packages SDK instead of the
    namespace package at the repo root.
    """
    probe = subprocess.run(
        [sys.executable, "-c", "import mcp.server.fastmcp"],
        cwd=str(_MCP_DIR),
        capture_output=True,
        timeout=10,
    )
    return probe.returncode == 0


_MCP_SDK_SKIP = pytest.mark.skipif(
    not _MCP_DIR.is_dir() or not _mcp_sdk_installed(),
    reason=(
        "`mcp[cli]` SDK not importable from the host; install "
        "`pip install -r mcp/requirements.txt` (or run inside the mcp container) "
        "to exercise the MCP-tool-level validation path."
    ),
)


def _invoke_upload_attachment(**kwargs) -> dict:
    """Run ``upload_attachment(**kwargs)`` via subprocess and return the dict.

    Registers the tool on a throwaway ``FastMCP`` with a stub ``api`` client
    (so validation errors return before any network call) and invokes the
    underlying Python function. Returns the tool's return dict.
    """
    script = textwrap.dedent(
        """
        import asyncio, json, sys
        from mcp.server.fastmcp import FastMCP

        # Stub BrilliantClient — validation paths must return before any
        # outbound call, so `post_multipart` raising is a test failure.
        class _StubClient:
            async def post_multipart(self, *a, **k):
                raise AssertionError(
                    "post_multipart reached; validation should have short-circuited"
                )
            async def get(self, *a, **k): pass
            async def post(self, *a, **k): pass
            async def put(self, *a, **k): pass
            async def delete(self, *a, **k): pass

        sys.path.insert(0, ".")
        import tools as _tools

        srv = FastMCP("t")
        _tools.register_tools(srv, _StubClient())

        kwargs = json.loads(sys.argv[1])

        async def _run():
            manager = getattr(srv, "_tool_manager", None)
            tool = manager.get_tool("upload_attachment") if manager else None
            fn = getattr(tool, "fn", None) if tool else None
            if fn is None:
                # Fallback: FastMCP exposes tools via `list_tools` coroutine.
                registered = await srv.list_tools()
                for t in registered:
                    if t.name == "upload_attachment":
                        fn = t.fn
                        break
            return await fn(**kwargs)

        result = asyncio.run(_run())
        print(json.dumps(result))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, json.dumps(kwargs)],
        cwd=str(_MCP_DIR),
        capture_output=True,
        timeout=15,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"upload_attachment subprocess failed:\n"
            f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
        )
    return json.loads(proc.stdout.strip().splitlines()[-1])


@_MCP_SDK_SKIP
def test_mcp_tool_rejects_invalid_base64():
    """Invalid base64 → 400-shape dict with a detail that mentions base64."""
    result = _invoke_upload_attachment(
        content_base64="not-valid-base64!!!",
        filename="vault.tgz",
        content_type="application/gzip",
    )
    assert result.get("error") is True, result
    assert result.get("status") == 400, result
    detail = result.get("detail", "")
    assert "base64" in detail.lower(), (
        f"detail should mention base64: {detail!r}"
    )


@_MCP_SDK_SKIP
def test_mcp_tool_rejects_both_path_and_base64():
    """Supplying both ``path`` and ``content_base64`` → 400-shape dict."""
    result = _invoke_upload_attachment(
        path="/tmp/anything",
        content_base64=base64.b64encode(b"x").decode("ascii"),
        filename="x.bin",
    )
    assert result.get("error") is True, result
    assert result.get("status") == 400, result
    detail = result.get("detail", "")
    assert "exactly one" in detail.lower() or "both" in detail.lower(), (
        f"detail should explain mutual exclusion: {detail!r}"
    )


@_MCP_SDK_SKIP
def test_mcp_tool_rejects_missing_filename_with_base64():
    """``content_base64`` without ``filename`` → 400-shape dict."""
    result = _invoke_upload_attachment(
        content_base64=base64.b64encode(b"x").decode("ascii"),
    )
    assert result.get("error") is True, result
    assert result.get("status") == 400, result
    detail = result.get("detail", "")
    assert "filename" in detail.lower(), (
        f"detail should mention filename: {detail!r}"
    )
