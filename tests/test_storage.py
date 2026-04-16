"""Unit tests for the blob storage abstraction (spec 0034b, T-0181).

Covers `api/services/storage.py`:

* Round-trip a 1 MiB payload through `LocalStorage` (put → get → delete)
  and verify the SHA-256 matches end to end.
* Verify that signed URLs produced by `LocalStorage.signed_url` are
  accepted by `verify_local_signed_url` within the TTL window.
* Verify that expired signatures are rejected.
* Verify that tampered signatures / keys / expirations are rejected.
* Verify that `get_storage()` honors `STORAGE_BACKEND=local`.
* Skip the S3 path unless `S3_BUCKET` is set (no network required).

Run:
    pytest tests/test_storage.py -q

The tests write to an isolated temp directory, so they don't pollute
`/data/uploads` and don't require the docker stack to be up.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import sys
import time
from pathlib import Path

import pytest


# Make `api/` importable so `api.services.storage` resolves when running
# pytest from the repo root (and mirror how the api container imports).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_API_DIR = _REPO_ROOT / "api"
if str(_API_DIR) not in sys.path:
    sys.path.insert(0, str(_API_DIR))


@pytest.fixture
def storage_env(tmp_path, monkeypatch):
    """Isolate storage env vars and caches to a per-test tmp directory."""
    # Fresh root + deterministic signing key for reproducibility.
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(root))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_SIGNING_KEY", "00" * 32)

    # Reload the module so the cached singleton (if any) is cleared and
    # env vars are re-read at construction time.
    if "services.storage" in sys.modules:
        storage_mod = importlib.reload(sys.modules["services.storage"])
    else:
        storage_mod = importlib.import_module("services.storage")
    storage_mod._reset_storage_singleton_for_tests()
    yield storage_mod, root
    storage_mod._reset_storage_singleton_for_tests()


# ---------------------------------------------------------------------------
# LocalStorage round-trip
# ---------------------------------------------------------------------------


def test_local_storage_round_trip_1mib(storage_env):
    """1 MiB payload survives put → get and SHA-256 is preserved."""
    storage_mod, root = storage_env
    storage = storage_mod.LocalStorage()

    payload = os.urandom(1024 * 1024)  # 1 MiB
    sha = hashlib.sha256(payload).hexdigest()
    org_id = "00000000-0000-0000-0000-000000000001"

    import asyncio

    async def _run():
        key = await storage.put(org_id, sha, "application/octet-stream", payload)
        assert key == f"{org_id}/{sha[:2]}/{sha}"
        # File on disk at the expected path.
        disk_path = root / org_id / sha[:2] / sha
        assert disk_path.exists()
        assert disk_path.stat().st_size == len(payload)

        got = await storage.get(key)
        assert got == payload
        assert hashlib.sha256(got).hexdigest() == sha

        await storage.delete(key)
        assert not disk_path.exists()

        # Delete is idempotent.
        await storage.delete(key)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Signed-URL validity
# ---------------------------------------------------------------------------


def test_signed_url_valid_within_ttl(storage_env):
    storage_mod, _root = storage_env
    storage = storage_mod.LocalStorage()

    key = "org/ab/abc123"
    url = storage.signed_url(key, ttl_seconds=60)

    # URL embeds the key and the exp/sig query string.
    assert "/attachments/_local/" in url
    assert "exp=" in url and "sig=" in url

    exp = int(_query_param(url, "exp"))
    sig = _query_param(url, "sig")

    assert storage_mod.verify_local_signed_url(key, exp, sig) is True


def test_signed_url_expired(storage_env):
    """An expired exp should be rejected."""
    storage_mod, _root = storage_env
    storage = storage_mod.LocalStorage()

    key = "org/cd/def456"
    # Manually construct a URL with an exp in the past using the same
    # signing path the class uses. We use the class's private helpers
    # via the public signed_url + overriding time is simpler:
    past_exp = int(time.time()) - 10
    sig_past = _sign_like(storage, key, past_exp)

    assert storage_mod.verify_local_signed_url(key, past_exp, sig_past) is False


def test_signed_url_tampered_signature(storage_env):
    """Any bit-flip in the signature must be rejected."""
    storage_mod, _root = storage_env
    storage = storage_mod.LocalStorage()

    key = "org/ef/ghi789"
    url = storage.signed_url(key, ttl_seconds=60)
    exp = int(_query_param(url, "exp"))
    sig = _query_param(url, "sig")

    # Flip one hex character in the signature.
    bad_char = "0" if sig[0] != "0" else "1"
    tampered = bad_char + sig[1:]
    assert tampered != sig

    assert storage_mod.verify_local_signed_url(key, exp, tampered) is False


def test_signed_url_tampered_key(storage_env):
    """Signature bound to a different key must not verify under another key."""
    storage_mod, _root = storage_env
    storage = storage_mod.LocalStorage()

    key_a = "org/aa/aaa"
    key_b = "org/bb/bbb"
    url = storage.signed_url(key_a, ttl_seconds=60)
    exp = int(_query_param(url, "exp"))
    sig = _query_param(url, "sig")

    assert storage_mod.verify_local_signed_url(key_b, exp, sig) is False


def test_signed_url_tampered_exp(storage_env):
    """Extending the exp invalidates the signature."""
    storage_mod, _root = storage_env
    storage = storage_mod.LocalStorage()

    key = "org/zz/zzz"
    url = storage.signed_url(key, ttl_seconds=60)
    exp = int(_query_param(url, "exp"))
    sig = _query_param(url, "sig")

    assert storage_mod.verify_local_signed_url(key, exp + 3600, sig) is False


def test_signed_url_malformed_exp(storage_env):
    storage_mod, _root = storage_env
    storage = storage_mod.LocalStorage()

    key = "org/mm/mmm"
    url = storage.signed_url(key, ttl_seconds=60)
    sig = _query_param(url, "sig")

    assert storage_mod.verify_local_signed_url(key, "not-a-number", sig) is False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_get_storage_local_default(storage_env):
    storage_mod, _root = storage_env
    instance = storage_mod.get_storage()
    assert isinstance(instance, storage_mod.LocalStorage)

    # Second call returns the same singleton.
    assert storage_mod.get_storage() is instance


def test_get_storage_rejects_unknown_backend(storage_env, monkeypatch):
    storage_mod, _root = storage_env
    storage_mod._reset_storage_singleton_for_tests()
    monkeypatch.setenv("STORAGE_BACKEND", "nope")
    with pytest.raises(RuntimeError):
        storage_mod.get_storage()


def test_module_imports_without_boto3(storage_env):
    """Importing the module must not require boto3 at module load time."""
    storage_mod, _root = storage_env
    # Simply re-import and assert the relevant names exist; the fixture
    # already performed the reload. If boto3 were imported at module
    # scope, an environment without it would fail before this assertion.
    for name in ("Storage", "LocalStorage", "S3Storage", "get_storage"):
        assert hasattr(storage_mod, name), f"missing export: {name}"


# ---------------------------------------------------------------------------
# S3 path — skipped unless S3_BUCKET is set
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("S3_BUCKET"),
    reason="S3_BUCKET not set — skipping S3 round-trip (no network in CI)",
)
def test_s3_storage_round_trip(storage_env):  # pragma: no cover — opt-in only
    import asyncio

    storage_mod, _root = storage_env
    storage = storage_mod.S3Storage()

    payload = os.urandom(1024)
    sha = hashlib.sha256(payload).hexdigest()
    org_id = "00000000-0000-0000-0000-000000000099"

    async def _run():
        key = await storage.put(org_id, sha, "application/octet-stream", payload)
        got = await storage.get(key)
        assert got == payload
        await storage.delete(key)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _query_param(url: str, name: str) -> str:
    from urllib.parse import urlparse, parse_qs

    qs = parse_qs(urlparse(url).query)
    vals = qs.get(name)
    if not vals:
        raise AssertionError(f"missing query param {name!r} in {url}")
    return vals[0]


def _sign_like(storage, key: str, exp: int) -> str:
    """Sign a payload using the storage instance's HMAC key.

    Accesses `_signing_key` directly so tests can construct adversarial
    URLs (expired, tampered) without duplicating the sign logic.
    """
    import hmac as _hmac
    import hashlib as _hashlib

    return _hmac.new(
        storage._signing_key,
        f"{key}:{exp}".encode("utf-8"),
        _hashlib.sha256,
    ).hexdigest()
