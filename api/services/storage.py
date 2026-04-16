"""Blob storage abstraction for file attachments (spec 0034b, T-0181).

Exports a `Storage` protocol with two concrete implementations:

* `LocalStorage` — writes blobs to a local filesystem tree
  (`{root}/{org_id}/{sha[:2]}/{sha}`) and issues HMAC-signed URLs that
  the API itself serves via `GET /attachments/_local/{key}?exp=...&sig=...`.
* `S3Storage` — uses `boto3` with a configurable endpoint URL
  (works with AWS, Cloudflare R2, and MinIO). Lazy-imports boto3 inside
  `__init__` so importing this module doesn't require boto3 to be present.

Backend selection is via the `STORAGE_BACKEND` environment variable
(`local` default, `s3` alternative). Factory `get_storage()` returns
the appropriate instance.

Also exports `verify_local_signed_url(key, exp, sig) -> bool` which the
local-serving handler (wired in T-0184) calls to validate signed URLs.
"""

from __future__ import annotations

import asyncio
import hmac
import hashlib
import os
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Optional, Protocol


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class Storage(Protocol):
    """Abstract blob-storage backend.

    Implementations must be safe to call across async handlers — either
    natively async or using `run_in_executor` under the hood for blocking
    I/O (the local/S3 impls here use the latter pattern).
    """

    async def put(
        self, org_id: str, sha256: str, content_type: str, data: bytes
    ) -> str:
        """Persist `data` and return an opaque `storage_key` string.

        The storage_key is what we write into `blobs.storage_key` — it
        must be sufficient for `get`/`delete`/`signed_url` to locate the
        object without further context.
        """
        ...

    async def get(self, storage_key: str) -> bytes:
        """Return the raw bytes for a previously-stored blob."""
        ...

    async def delete(self, storage_key: str) -> None:
        """Remove the blob identified by `storage_key`. Idempotent."""
        ...

    def signed_url(self, storage_key: str, ttl_seconds: int = 300) -> str:
        """Return a time-limited URL the caller can hand to an HTTP client."""
        ...


# ---------------------------------------------------------------------------
# Local filesystem backend
# ---------------------------------------------------------------------------


_DEFAULT_LOCAL_ROOT = "/data/uploads"
_SIGNING_KEY_FILENAME = ".signing_key"


def _load_or_create_signing_key(root: Path) -> bytes:
    """Return the HMAC signing key for local signed URLs.

    Precedence:

    1. `LOCAL_STORAGE_SIGNING_KEY` env var (hex-decoded if possible,
       otherwise used as raw UTF-8 bytes).
    2. A persisted key file at `{root}/.signing_key`, created on first
       call with 32 bytes from `secrets.token_bytes`.

    Persisting to disk means all workers in the same process group share
    the same key and signed URLs survive container restarts as long as
    the storage volume is mounted.
    """
    env_key = os.environ.get("LOCAL_STORAGE_SIGNING_KEY")
    if env_key:
        try:
            return bytes.fromhex(env_key)
        except ValueError:
            return env_key.encode("utf-8")

    root.mkdir(parents=True, exist_ok=True)
    key_path = root / _SIGNING_KEY_FILENAME
    if key_path.exists():
        data = key_path.read_bytes()
        if data:
            return data
    key = secrets.token_bytes(32)
    # Write atomically-ish: write to a temp file then rename.
    tmp = key_path.with_suffix(".tmp")
    tmp.write_bytes(key)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, key_path)
    return key


def _sign(key: bytes, payload: str) -> str:
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


class LocalStorage:
    """Filesystem-backed `Storage` implementation.

    Layout: `{root}/{org_id}/{sha[:2]}/{sha}`.

    `storage_key` format: `{org_id}/{sha[:2]}/{sha}` (relative to `root`,
    always forward-slash separated so it round-trips through URLs and
    across OSes).
    """

    def __init__(
        self,
        root: Optional[str] = None,
        signing_key: Optional[bytes] = None,
        url_prefix: str = "/attachments/_local",
    ) -> None:
        self.root = Path(root or os.environ.get("LOCAL_STORAGE_ROOT") or _DEFAULT_LOCAL_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)
        self._signing_key = signing_key or _load_or_create_signing_key(self.root)
        self._url_prefix = url_prefix.rstrip("/")

    # -- internal helpers -----------------------------------------------

    def _path_for(self, storage_key: str) -> Path:
        # Storage keys are trusted (we generated them) but defensively
        # reject absolute or `..`-containing segments — these should
        # never happen via `put`, but guard against misuse of `get`.
        parts = [p for p in storage_key.split("/") if p]
        if any(p in ("..", ".") or p.startswith("/") for p in parts):
            raise ValueError(f"Invalid storage_key: {storage_key!r}")
        return self.root.joinpath(*parts)

    # -- protocol methods -----------------------------------------------

    async def put(
        self, org_id: str, sha256: str, content_type: str, data: bytes
    ) -> str:
        # content_type is accepted for parity with S3Storage (which uses
        # it as Content-Type metadata); LocalStorage doesn't persist it
        # because the blobs row is the source of truth.
        del content_type
        if not sha256 or len(sha256) < 4:
            raise ValueError("sha256 must be the full hex digest")
        key = f"{org_id}/{sha256[:2]}/{sha256}"

        def _write() -> None:
            path = self._path_for(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            os.replace(tmp, path)

        await asyncio.get_event_loop().run_in_executor(None, _write)
        return key

    async def get(self, storage_key: str) -> bytes:
        path = self._path_for(storage_key)

        def _read() -> bytes:
            return path.read_bytes()

        return await asyncio.get_event_loop().run_in_executor(None, _read)

    async def delete(self, storage_key: str) -> None:
        path = self._path_for(storage_key)

        def _delete() -> None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

        await asyncio.get_event_loop().run_in_executor(None, _delete)

    def signed_url(self, storage_key: str, ttl_seconds: int = 300) -> str:
        exp = int(time.time()) + int(ttl_seconds)
        sig = _sign(self._signing_key, f"{storage_key}:{exp}")
        quoted = urllib.parse.quote(storage_key, safe="/")
        return f"{self._url_prefix}/{quoted}?exp={exp}&sig={sig}"

    # -- verification ---------------------------------------------------

    def verify(self, storage_key: str, exp: int | str, sig: str) -> bool:
        """Check a local signed URL's expiry and HMAC signature."""
        try:
            exp_int = int(exp)
        except (TypeError, ValueError):
            return False
        if exp_int < int(time.time()):
            return False
        expected = _sign(self._signing_key, f"{storage_key}:{exp_int}")
        return hmac.compare_digest(expected, sig or "")


# Module-level verifier for import from route handlers. A fresh
# `LocalStorage` instance is cheap and uses the same signing key
# (either env-provided or persisted on disk), so verification from a
# request handler doesn't need to reach into the active instance.
def verify_local_signed_url(storage_key: str, exp: int | str, sig: str) -> bool:
    """Verify a signed URL produced by `LocalStorage.signed_url`.

    Returns False if expired, tampered, or malformed. Intended to be
    called from the `/attachments/_local/{key}` handler wired up by
    T-0184.
    """
    return LocalStorage().verify(storage_key, exp, sig)


# ---------------------------------------------------------------------------
# S3-compatible backend
# ---------------------------------------------------------------------------


class S3Storage:
    """S3-compatible `Storage` implementation (AWS / R2 / MinIO).

    Configuration (all via environment variables):

    * `S3_BUCKET` — required.
    * `S3_ENDPOINT_URL` — optional; set for R2/MinIO, unset for AWS.
    * `S3_ACCESS_KEY`, `S3_SECRET_KEY` — optional; fall back to boto3's
      default credential chain (IAM roles, shared config, etc.).
    * `S3_REGION` — optional; defaults to `auto` (friendly for R2).

    `boto3` is imported lazily inside `__init__` so callers that select
    a non-S3 backend don't need the package installed.
    """

    def __init__(
        self,
        bucket: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        region: Optional[str] = None,
    ) -> None:
        import boto3  # noqa: WPS433 — lazy on purpose

        self.bucket = bucket or os.environ.get("S3_BUCKET")
        if not self.bucket:
            raise RuntimeError(
                "S3Storage requires S3_BUCKET env var (or bucket= kwarg)"
            )
        endpoint = endpoint_url or os.environ.get("S3_ENDPOINT_URL") or None
        ak = access_key or os.environ.get("S3_ACCESS_KEY") or None
        sk = secret_key or os.environ.get("S3_SECRET_KEY") or None
        reg = region or os.environ.get("S3_REGION") or "auto"

        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=ak,
            aws_secret_access_key=sk,
            region_name=reg,
        )

    @staticmethod
    def _key_for(org_id: str, sha256: str) -> str:
        return f"{org_id}/{sha256[:2]}/{sha256}"

    async def put(
        self, org_id: str, sha256: str, content_type: str, data: bytes
    ) -> str:
        key = self._key_for(org_id, sha256)

        def _put() -> None:
            self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type or "application/octet-stream",
            )

        await asyncio.get_event_loop().run_in_executor(None, _put)
        return key

    async def get(self, storage_key: str) -> bytes:
        def _get() -> bytes:
            resp = self._client.get_object(Bucket=self.bucket, Key=storage_key)
            return resp["Body"].read()

        return await asyncio.get_event_loop().run_in_executor(None, _get)

    async def delete(self, storage_key: str) -> None:
        def _delete() -> None:
            self._client.delete_object(Bucket=self.bucket, Key=storage_key)

        await asyncio.get_event_loop().run_in_executor(None, _delete)

    def signed_url(self, storage_key: str, ttl_seconds: int = 300) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": storage_key},
            ExpiresIn=int(ttl_seconds),
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


_STORAGE_SINGLETON: Optional[Storage] = None


def get_storage() -> Storage:
    """Return the configured `Storage` instance (cached per-process).

    Selected by the `STORAGE_BACKEND` env var (`local` | `s3`, default
    `local`). The instance is created on first call and reused on
    subsequent calls so HMAC signing keys stay consistent within a
    process.
    """
    global _STORAGE_SINGLETON
    if _STORAGE_SINGLETON is not None:
        return _STORAGE_SINGLETON
    backend = (os.environ.get("STORAGE_BACKEND") or "local").strip().lower()
    if backend == "s3":
        _STORAGE_SINGLETON = S3Storage()
    elif backend == "local":
        _STORAGE_SINGLETON = LocalStorage()
    else:
        raise RuntimeError(
            f"Unknown STORAGE_BACKEND={backend!r}; expected 'local' or 's3'"
        )
    return _STORAGE_SINGLETON


def _reset_storage_singleton_for_tests() -> None:
    """Test-only: clear the cached singleton so env changes take effect."""
    global _STORAGE_SINGLETON
    _STORAGE_SINGLETON = None


__all__ = [
    "Storage",
    "LocalStorage",
    "S3Storage",
    "get_storage",
    "verify_local_signed_url",
]
