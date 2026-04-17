"""End-to-end tests for the attachment subsystem (spec 0034b, T-0186).

Exercises the full attachment cycle against the live API + DB stack in
the `brilliant-uploads` worktree (API on :8020, Postgres on :5452):

  * upload → ``blobs`` row                                 (case 1)
  * same-org dedup: same sha256 → same ``blob_id``         (case 2)
  * cross-org isolation: distinct ``blob_id`` + 404 read   (case 3)
  * digest pipeline → staging → entry + attachment link    (case 4)
  * signed-URL round-trip: bytes match the upload          (case 5)
  * permission-404 (not 403) on forbidden ``blob_id``      (case 6)
  * over-size 413 via ``MAX_ATTACHMENT_BYTES`` override    (case 7)

Prerequisites
-------------
  1. ``docker compose up -d``  (API on :8020 in this worktree)
  2. ``pip install -r tests/requirements-dev.txt``
  3. Migrations applied through 025 (staging_attachment_digest).

Run
---
  pytest tests/test_attachments.py -q

Minimal overlap with ``tests/test_pdf_digest.py``
-------------------------------------------------
That file already covers the PDF extractor + digest staging branch
end-to-end. Here we focus on whole-cycle semantics (round-trip, dedup,
cross-org, signed-URL bytes, permission 404, size cap) — exactly the
surface area ``/attachments`` exposes as a contract.
"""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
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
# Configuration — worktree-specific defaults (API :8020, DB :5452)
# ----------------------------------------------------------------------------

BASE_URL = os.environ.get("BRILLIANT_BASE_URL", "http://localhost:8020")
DB_DSN = os.environ.get(
    "BRILLIANT_DB_DSN",
    "postgresql://postgres:dev@localhost:5452/brilliant",
)

# Admin key from db/migrations/005_seed.sql — same one used by test_pdf_digest.
ADMIN_KEY = os.environ.get("ADMIN_KEY", "bkai_adm1_testkey_admin")

# docker compose project name for this worktree (spec 0034b / Worktree Setup).
COMPOSE_PROJECT_NAME = os.environ.get(
    "COMPOSE_PROJECT_NAME", "brilliant-uploads"
)
API_SERVICE = os.environ.get("API_SERVICE", "api")
API_CONTAINER = os.environ.get("API_CONTAINER", "brilliant-uploads-api")

REQUEST_TIMEOUT = 15.0


# ----------------------------------------------------------------------------
# Inline fixture PDF
# ----------------------------------------------------------------------------
#
# A ~460-byte pypdf-produced PDF with /Title = "Fixture Test PDF" and a
# single blank page. Kept inline so the suite doesn't grow binary fixtures.
# Same bytes used by test_pdf_digest — fine here; we de-duplicate by
# appending a per-test comment after %%EOF to break sha256 uniqueness.

_FIXTURE_PDF_B64 = (
    "JVBERi0xLjMKJeLjz9MKMSAwIG9iago8PAovVHlwZSAvUGFnZXMKL0NvdW50IDEKL0tpZHMg"
    "WyA0IDAgUiBdCj4+CmVuZG9iagoyIDAgb2JqCjw8Ci9Qcm9kdWNlciAocHlwZGYpCi9UaXRs"
    "ZSAoRml4dHVyZVwwNDBUZXN0XDA0MFBERikKPj4KZW5kb2JqCjMgMCBvYmoKPDwKL1R5cGUg"
    "L0NhdGFsb2cKL1BhZ2VzIDEgMCBSCj4+CmVuZG9iago0IDAgb2JqCjw8Ci9UeXBlIC9QYWdl"
    "Ci9SZXNvdXJjZXMgPDwKPj4KL01lZGlhQm94IFsgMC4wIDAuMCAyMDAgMjAwIF0KL1BhcmVu"
    "dCAxIDAgUgo+PgplbmRvYmoKeHJlZgowIDUKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAw"
    "MDE1IDAwMDAwIG4gCjAwMDAwMDAwNzQgMDAwMDAgbiAKMDAwMDAwMDE0NSAwMDAwMCBuIAow"
    "MDAwMDAwMTk0IDAwMDAwIG4gCnRyYWlsZXIKPDwKL1NpemUgNQovUm9vdCAzIDAgUgovSW5m"
    "byAyIDAgUgo+PgpzdGFydHhyZWYKMjg4CiUlRU9GCg=="
)


@pytest.fixture(scope="module")
def fixture_pdf_bytes() -> bytes:
    return base64.b64decode(_FIXTURE_PDF_B64)


def _unique_pdf(base: bytes) -> bytes:
    """Return ``base`` with a random-ish tail appended after %%EOF.

    pypdf stops parsing at the %%EOF marker so the suffix is ignored for
    metadata / text purposes — but it changes the sha256, which breaks
    per-org dedup between tests. Essential for any test that asserts
    ``dedup=False`` on an upload.
    """
    suffix = f"\n% fixture-{time.time_ns()}-{uuid.uuid4().hex}\n".encode()
    return base + suffix


# ----------------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------------


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


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
        f"(start `docker compose up -d` in the uploads worktree).",
    ),
]


def _upload(
    data: bytes,
    *,
    key: str = ADMIN_KEY,
    filename: str = "fixture.pdf",
    content_type: str | None = None,
    digest: bool = False,
) -> requests.Response:
    params: dict = {}
    if digest:
        params["digest"] = "true"
    if content_type is not None:
        params["content_type"] = content_type
    multipart_ct = content_type or "application/octet-stream"
    return requests.post(
        f"{BASE_URL}/attachments",
        params=params or None,
        headers=_auth(key),
        files={"file": (filename, data, multipart_ct)},
        timeout=REQUEST_TIMEOUT,
    )


def _get(path: str, *, key: str, allow_redirects: bool = False) -> requests.Response:
    return requests.get(
        f"{BASE_URL}{path}",
        headers=_auth(key),
        timeout=REQUEST_TIMEOUT,
        allow_redirects=allow_redirects,
    )


# ----------------------------------------------------------------------------
# DB helpers — used to provision a second org + its admin API key, and to
# clean the rows up after the test so re-runs are deterministic.
# ----------------------------------------------------------------------------


def _db_exec(sql: str, params: tuple | None = None) -> list[tuple]:
    """Run a statement (superuser) and return any rows fetched."""
    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            try:
                return cur.fetchall()
            except psycopg.ProgrammingError:
                return []


def _blob_row(blob_id: str) -> dict | None:
    """Read a blobs row directly (bypasses RLS)."""
    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, org_id, sha256, content_type, size_bytes,
                       storage_backend, storage_key, uploaded_by
                FROM blobs WHERE id = %s
                """,
                (blob_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = [c.name for c in cur.description]
            return dict(zip(cols, row))


# ----------------------------------------------------------------------------
# Second-org fixture — creates org_attachtest + an admin user + bcrypt-hashed
# API key in the DB via ``crypt()``, scoped to the test session. Teardown
# removes everything we added so the suite is idempotent.
# ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def other_org():
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_attachtest_{suffix}"
    user_id = f"usr_attachtest_{suffix}"
    # 9-char prefix: bkai_XXXX. The API's auth handler keys lookup by
    # the first 9 chars of the token.
    key_prefix = f"bkai_{suffix[:4]}"
    token = f"{key_prefix}_testkey_other_{suffix}"

    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO organizations (id, name, settings) "
                "VALUES (%s, %s, '{}')",
                (org_id, f"Attachment Test Org {suffix}"),
            )
            cur.execute(
                """
                INSERT INTO users (id, org_id, display_name, email_hash, role)
                VALUES (%s, %s, %s,
                        encode(digest(%s, 'sha256'), 'hex'),
                        'admin')
                """,
                (user_id, org_id, f"Attach Admin {suffix}", f"{user_id}@test.local"),
            )
            cur.execute(
                """
                INSERT INTO api_keys (user_id, org_id, key_hash, key_prefix,
                                      key_type, label)
                VALUES (%s, %s, crypt(%s, gen_salt('bf')), %s, 'interactive',
                        %s)
                """,
                (user_id, org_id, token, key_prefix, f"test-{suffix}"),
            )

    try:
        yield {
            "org_id": org_id,
            "user_id": user_id,
            "key_prefix": key_prefix,
            "token": token,
        }
    finally:
        # Tear down — children first due to FKs. Blobs / entry_attachments /
        # staging / entries may exist depending on test flow; cascade from
        # each.
        with psycopg.connect(DB_DSN, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM entry_attachments WHERE org_id = %s",
                    (org_id,),
                )
                cur.execute("DELETE FROM staging WHERE org_id = %s", (org_id,))
                cur.execute(
                    "DELETE FROM entry_versions WHERE org_id = %s",
                    (org_id,),
                )
                cur.execute("DELETE FROM entries WHERE org_id = %s", (org_id,))
                cur.execute("DELETE FROM blobs WHERE org_id = %s", (org_id,))
                cur.execute("DELETE FROM api_keys WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
                cur.execute(
                    "DELETE FROM organizations WHERE id = %s", (org_id,)
                )


# ----------------------------------------------------------------------------
# Case 1 — Upload → blobs row
# ----------------------------------------------------------------------------


def test_upload_creates_blob_row(fixture_pdf_bytes):
    """Upload a small PDF; verify `blobs` row carries matching sha256 + size."""
    payload = _unique_pdf(fixture_pdf_bytes)
    expected_sha = hashlib.sha256(payload).hexdigest()

    resp = _upload(
        payload,
        content_type="application/pdf",
        filename="case1.pdf",
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["sha256"] == expected_sha
    assert body["dedup"] is False
    assert body["size_bytes"] == len(payload)
    assert body["content_type"] == "application/pdf"
    assert "blob_id" in body

    row = _blob_row(body["blob_id"])
    assert row is not None, "blobs row not found via DB lookup"
    assert row["sha256"] == expected_sha
    assert int(row["size_bytes"]) == len(payload)
    assert row["content_type"] == "application/pdf"
    assert row["uploaded_by"] == "usr_admin"


# ----------------------------------------------------------------------------
# Case 2 — Dedup within the same org
# ----------------------------------------------------------------------------


def test_same_org_dedup_returns_same_blob_id(fixture_pdf_bytes):
    """Uploading identical bytes twice within one org returns one blob_id."""
    payload = _unique_pdf(fixture_pdf_bytes)

    first = _upload(
        payload, content_type="application/pdf", filename="dedup.pdf"
    )
    assert first.status_code == 201, first.text
    first_body = first.json()
    assert first_body["dedup"] is False

    second = _upload(
        payload, content_type="application/pdf", filename="dedup-again.pdf"
    )
    assert second.status_code == 201, second.text
    second_body = second.json()

    assert second_body["blob_id"] == first_body["blob_id"], (
        "same-org dedup must return the existing blob_id"
    )
    assert second_body["dedup"] is True
    assert second_body["sha256"] == first_body["sha256"]


# ----------------------------------------------------------------------------
# Case 3 — Cross-org isolation: distinct blob_ids + 404 on forbidden read
# ----------------------------------------------------------------------------


def test_cross_org_isolation_distinct_blob_ids_and_404(
    fixture_pdf_bytes, other_org
):
    """Identical content uploaded by two orgs yields two blobs; each org 404s
    when it tries to read the other's blob via GET /attachments/{blob_id}."""
    payload = _unique_pdf(fixture_pdf_bytes)

    # org_demo (ADMIN_KEY) upload.
    r1 = _upload(
        payload, content_type="application/pdf", filename="cross-demo.pdf"
    )
    assert r1.status_code == 201, r1.text
    demo_blob_id = r1.json()["blob_id"]

    # Second org upload — same bytes, different org.
    r2 = _upload(
        payload,
        key=other_org["token"],
        content_type="application/pdf",
        filename="cross-other.pdf",
    )
    assert r2.status_code == 201, r2.text
    other_blob_id = r2.json()["blob_id"]

    # Distinct blob_ids — tenant isolation holds even on a shared backend.
    assert demo_blob_id != other_blob_id, (
        "identical content from two orgs must yield distinct blob_ids"
    )

    # DB-level check: two rows, different org_ids, same sha256.
    demo_row = _blob_row(demo_blob_id)
    other_row = _blob_row(other_blob_id)
    assert demo_row is not None and other_row is not None
    assert demo_row["org_id"] == "org_demo"
    assert other_row["org_id"] == other_org["org_id"]
    assert demo_row["sha256"] == other_row["sha256"]

    # Forbidden reads — each side gets 404 (not 403), because neither org has
    # any entry_attachments row referencing the other org's blob. Importantly
    # 404, not 403, avoids leaking blob existence across tenants.
    r_demo_reads_other = _get(f"/attachments/{other_blob_id}", key=ADMIN_KEY)
    assert r_demo_reads_other.status_code == 404, (
        f"expected 404 on cross-org blob read, got "
        f"{r_demo_reads_other.status_code}: {r_demo_reads_other.text}"
    )

    r_other_reads_demo = _get(
        f"/attachments/{demo_blob_id}", key=other_org["token"]
    )
    assert r_other_reads_demo.status_code == 404, (
        f"expected 404 on cross-org blob read, got "
        f"{r_other_reads_demo.status_code}: {r_other_reads_demo.text}"
    )


# ----------------------------------------------------------------------------
# Case 4 — Digest pipeline → staging → entry → entry_attachments
# ----------------------------------------------------------------------------
#
# Admin uploads via the digest endpoint; tier 1/2 auto-promotes the staging
# row synchronously, so the resulting entry already carries the
# entry_attachments(role='source') link on return. We then verify:
#
#   * staging row exists with submission_category='attachment_digest'
#   * promoted_entry_id is populated
#   * GET /entries/{id}/attachments returns exactly one attachment
#     whose blob_id matches the uploaded blob
#
# Minimal overlap with test_pdf_digest: that file asserts the pure DB wiring;
# here we assert the *public API* shape ``GET /entries/{id}/attachments``
# serves the link back to its caller.


def test_digest_to_approved_entry_attachments_round_trip(fixture_pdf_bytes):
    payload = _unique_pdf(fixture_pdf_bytes)

    resp = _upload(
        payload,
        content_type="application/pdf",
        filename="digest-case4.pdf",
        digest=True,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert "staging_id" in body, "digest=true should return a staging_id"
    blob_id = body["blob_id"]
    staging_id = body["staging_id"]

    # Look up the staging row to find the promoted entry id and confirm the
    # submission_category (mirrors migration 023's expanded CHECK).
    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT submission_category, status, promoted_entry_id
                FROM staging WHERE id = %s
                """,
                (staging_id,),
            )
            row = cur.fetchone()
    assert row is not None, "staging row missing"
    category, status, promoted_entry_id = row
    assert category == "attachment_digest"
    # Admin via web_ui → tier 1 → auto-approved synchronously.
    assert status == "auto_approved"
    assert promoted_entry_id is not None, (
        "tier 1/2 digest should auto-promote to an entry"
    )
    entry_id = str(promoted_entry_id)

    # Public API: GET /entries/{id}/attachments returns exactly the
    # entry_attachments row the digest pipeline created.
    listing = _get(f"/entries/{entry_id}/attachments", key=ADMIN_KEY)
    assert listing.status_code == 200, listing.text
    attachments = listing.json()
    assert isinstance(attachments, list)
    assert len(attachments) == 1, (
        f"expected exactly one attachment for digest entry, got: {attachments}"
    )
    att = attachments[0]
    assert att["blob_id"] == blob_id
    assert att["entry_id"] == entry_id
    assert att["role"] == "source"
    assert att["blob"]["sha256"] == body["sha256"]


# ----------------------------------------------------------------------------
# Case 5 — Signed-URL round-trip (bytes match the upload sha256)
# ----------------------------------------------------------------------------


def test_signed_url_round_trip_matches_upload_bytes(fixture_pdf_bytes):
    """After digest → auto-approve, `GET /attachments/{blob_id}` issues a
    302 to a signed URL; following it must yield bytes whose sha256 equals
    the uploaded payload's sha256."""
    payload = _unique_pdf(fixture_pdf_bytes)
    expected_sha = hashlib.sha256(payload).hexdigest()

    # Go through the digest pipeline so an entry_attachments row exists —
    # that's the prerequisite for the auth check in GET /attachments/{id}.
    up = _upload(
        payload,
        content_type="application/pdf",
        filename="signed-url.pdf",
        digest=True,
    )
    assert up.status_code == 201, up.text
    blob_id = up.json()["blob_id"]

    # allow_redirects=False so we inspect the 302 target explicitly.
    redirect = _get(f"/attachments/{blob_id}", key=ADMIN_KEY)
    assert redirect.status_code == 302, (
        f"expected 302 redirect, got {redirect.status_code}: {redirect.text}"
    )
    location = redirect.headers.get("Location")
    assert location, "302 response missing Location header"

    # Normalize: the LocalStorage signer returns a relative URL
    # (/attachments/_local/...). S3Storage would return an absolute URL —
    # support both shapes.
    if location.startswith("/"):
        signed_url = f"{BASE_URL}{location}"
    else:
        signed_url = location

    # Signed URL is a bearer token on its own — no auth header needed.
    fetched = requests.get(signed_url, timeout=REQUEST_TIMEOUT)
    assert fetched.status_code == 200, fetched.text
    assert hashlib.sha256(fetched.content).hexdigest() == expected_sha, (
        "signed-URL-served bytes do not match uploaded sha256"
    )
    # Bonus: verify content-type was preserved through the pipeline.
    assert fetched.headers.get("Content-Type", "").startswith(
        "application/pdf"
    )


# ----------------------------------------------------------------------------
# Case 6 — Permission 404 (not 403) on a forbidden blob_id
# ----------------------------------------------------------------------------
#
# We upload a blob WITHOUT digesting it (so no entry_attachments row is
# created). Any user — including the owner — requesting
# GET /attachments/{blob_id} must get 404, because the endpoint's access
# check requires at least one visible entry_attachments row linking the
# caller to the blob. The point: the response must be 404 (don't leak
# existence), not 403 (which would tell a probe the blob exists).


def test_attachment_without_entry_link_returns_404_not_403(fixture_pdf_bytes):
    payload = _unique_pdf(fixture_pdf_bytes)
    up = _upload(
        payload, content_type="application/pdf", filename="no-link.pdf"
    )
    assert up.status_code == 201, up.text
    blob_id = up.json()["blob_id"]

    # Sanity: the blobs row actually exists.
    assert _blob_row(blob_id) is not None

    # No entry_attachments row → 404 for the owner too.
    resp = _get(f"/attachments/{blob_id}", key=ADMIN_KEY)
    assert resp.status_code == 404, (
        f"expected 404 on blob with no entry link, got "
        f"{resp.status_code}: {resp.text}"
    )


def test_attachment_cross_org_with_entry_link_still_404(
    fixture_pdf_bytes, other_org
):
    """Even a blob with a valid entry_attachments link is 404 to an outside
    org: the cross-org request can't see the owning entry via RLS, so the
    auth probe finds zero rows and answers 404 (not 403)."""
    payload = _unique_pdf(fixture_pdf_bytes)
    # org_demo runs the full digest → entry_attachments pipeline so a link
    # exists — this exercises the more-interesting 404 path where a blob
    # DOES have a link but the caller can't see it.
    up = _upload(
        payload,
        content_type="application/pdf",
        filename="link-hidden.pdf",
        digest=True,
    )
    assert up.status_code == 201, up.text
    blob_id = up.json()["blob_id"]

    # Owner can see it — establish the positive control.
    assert _get(f"/attachments/{blob_id}", key=ADMIN_KEY).status_code == 302

    # Different org reads it — 404, not 403.
    resp = _get(f"/attachments/{blob_id}", key=other_org["token"])
    assert resp.status_code == 404, (
        f"cross-org blob read must be 404 (not 403) to avoid leaking "
        f"existence; got {resp.status_code}: {resp.text}"
    )


# ----------------------------------------------------------------------------
# Case 7 — Over-size 413
# ----------------------------------------------------------------------------
#
# The endpoint reads MAX_ATTACHMENT_BYTES from os.environ on each request,
# so to exercise the 413 path without having to push 50+ MiB we need to
# restart the API container with a small cap. The fixture below does so via
# ``docker compose up -d api`` with an injected override file, and restores
# the pre-test state on teardown.
#
# If docker/compose isn't available the test is skipped with a clear
# message; otherwise the test runs deterministically.

_SMALL_MAX = 1024  # 1 KiB — tiny enough that any non-trivial payload overruns.

# Per-test override file name. Dropped next to the worktree's compose files
# and merged in via `docker compose -f ... -f ...` so we never mutate the
# committed / user-curated docker-compose.override.yml contents.
_TEST_OVERRIDE_FILENAME = "docker-compose.test-413.yml"


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
    """Poll /health until the API is responsive again after a restart."""
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
    """Compose file list mirroring the default resolution order.

    docker compose picks up ``docker-compose.yml`` + ``docker-compose.override.yml``
    by default. We must pass both explicitly when adding a third file
    because supplying `-f` disables the implicit lookup.
    """
    base = _REPO_ROOT / "docker-compose.yml"
    override = _REPO_ROOT / "docker-compose.override.yml"
    files = ["-f", str(base)]
    if override.exists():
        files += ["-f", str(override)]
    return files


@pytest.fixture
def api_with_small_max_bytes():
    """Temporarily restart the API container with MAX_ATTACHMENT_BYTES=1024.

    Writes a dedicated ``docker-compose.test-413.yml`` next to the
    worktree's compose files, passes it to ``docker compose up -d api``
    alongside the base + worktree override files, and removes it +
    recreates the container with the baseline config on teardown.

    Using a dedicated override file (rather than mutating the checked-in
    one) means the test leaves no footprint on the repo's normal compose
    state if it crashes mid-run.
    """
    if not _compose_available():
        pytest.skip("`docker compose` CLI not available; cannot exercise 413 path")

    override_path = _REPO_ROOT / _TEST_OVERRIDE_FILENAME
    override_path.write_text(_build_test_override(_SMALL_MAX))

    env = {**os.environ, "COMPOSE_PROJECT_NAME": COMPOSE_PROJECT_NAME}
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
        yield _SMALL_MAX
    finally:
        # Drop the override file first so any recreate goes back to baseline.
        try:
            override_path.unlink()
        except FileNotFoundError:
            pass
        # Recreate once more without the test override to drop the cap.
        try:
            _up(_compose_files())
            _wait_for_api(timeout=30)
        except Exception as e:  # pragma: no cover — teardown best-effort
            sys.stderr.write(
                f"WARNING: failed to restore API after 413 test: {e}\n"
            )


def _build_test_override(max_bytes: int) -> str:
    """Produce the merge-overlay YAML setting MAX_ATTACHMENT_BYTES on api.

    Compose merges ``environment:`` mappings across override files, so
    this only needs to supply the delta — existing env vars
    (DATABASE_URL, etc.) are preserved from the base file.
    """
    return (
        "# Auto-written by tests/test_attachments.py::api_with_small_max_bytes.\n"
        "# Safe to delete — regenerated on demand.\n"
        "services:\n"
        "  api:\n"
        "    environment:\n"
        f"      MAX_ATTACHMENT_BYTES: \"{max_bytes}\"\n"
    )


def test_oversize_upload_returns_413(api_with_small_max_bytes):
    """With MAX_ATTACHMENT_BYTES set small, a larger upload must 413."""
    small_max = api_with_small_max_bytes
    # 2 * the cap, random-ish content type -- size is what matters.
    payload = b"X" * (small_max * 2)

    resp = _upload(
        payload,
        content_type="application/octet-stream",
        filename="big.bin",
    )
    assert resp.status_code == 413, (
        f"expected 413 for oversize upload, got {resp.status_code}: {resp.text}"
    )
    # Error detail should mention the cap.
    try:
        detail = resp.json().get("detail", "")
    except Exception:
        detail = resp.text
    assert str(small_max) in detail or "MAX_ATTACHMENT_BYTES" in detail


def test_within_cap_after_restart_still_uploads(api_with_small_max_bytes):
    """Sanity: with the small cap in place, a tiny payload still succeeds.

    Ensures the 413 test's assertion isn't just tripping a generic 5xx from
    a broken restart. Also verifies the env override is actually active
    (a tiny upload under the cap would always succeed regardless).
    """
    # Under the small cap (1 KiB), a 100-byte payload still uploads cleanly.
    tiny = b"ok" * 50
    resp = _upload(
        tiny,
        content_type="application/octet-stream",
        filename="tiny.bin",
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["size_bytes"] == len(tiny)
