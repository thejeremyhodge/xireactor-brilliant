"""Tests for the PDF digest pipeline (spec 0034b, T-0183).

Exercises `POST /attachments?digest=true&content_type=application/pdf`
end-to-end against a live API:

* Upload a tiny valid PDF → staging row created + `staging_id` in response.
* Non-PDF content or `digest=false` → no staging row created.
* Corrupt PDF bytes → staging row still created (empty content), no 500.
* Tier 1/2 auto-approval produces an `entry_attachments(role='source')`
  row linking the promoted entry back to the original blob.

Also exercises `api/services/pdf_extract.py` in isolation with in-process
`pypdf` round-trips (no API / DB required) so the extractor's contract
is covered even if the integration tests are skipped.

Prerequisites for the integration path:
  1. `docker compose up -d --build`  (API on :8020 in this worktree)
  2. `pip install -r tests/requirements-dev.txt`
  3. Migration 023 applied (expands submission_category CHECK).

Run:
  pytest tests/test_pdf_digest.py -v
"""

from __future__ import annotations

import base64
import io
import os
import sys
import time
from pathlib import Path

import pytest


# --- Import the pure-python extractor without requiring a running stack -----

_REPO_ROOT = Path(__file__).resolve().parent.parent
_API_DIR = _REPO_ROOT / "api"
if str(_API_DIR) not in sys.path:
    sys.path.insert(0, str(_API_DIR))


try:
    from services.pdf_extract import extract_pdf  # noqa: E402
    # The extractor is tolerant of missing pypdf (returns empty tuples);
    # but the unit tests here assert actual parsing, so we require pypdf
    # on the host Python as well.
    import pypdf  # noqa: F401 — availability check only
    _EXTRACT_AVAILABLE = True
except Exception:  # pragma: no cover
    _EXTRACT_AVAILABLE = False

try:
    import requests  # noqa: E402
    _REQUESTS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _REQUESTS_AVAILABLE = False

try:
    import psycopg  # noqa: E402
    _PSYCOPG_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PSYCOPG_AVAILABLE = False


BASE_URL = os.environ.get("CORTEX_BASE_URL", "http://localhost:8020")
DB_DSN = os.environ.get(
    "CORTEX_DB_DSN",
    "postgresql://postgres:dev@localhost:5452/cortex",
)
ADMIN_KEY = os.environ.get("ADMIN_KEY", "bkai_adm1_testkey_admin")


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------


# Tiny pypdf-generated PDF with /Title = "Fixture Test PDF" — blank page,
# 463 bytes. Kept inline so the test suite doesn't grow binary fixtures.
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


def _api_available() -> bool:
    try:
        return requests.get(f"{BASE_URL}/health", timeout=2.0).status_code == 200
    except Exception:
        return False


_INTEGRATION_GUARDS = [
    pytest.mark.skipif(
        not _REQUESTS_AVAILABLE,
        reason="requests not installed; install tests/requirements-dev.txt",
    ),
    pytest.mark.skipif(
        not _api_available(),
        reason=f"Brilliant API not reachable at {BASE_URL} — start docker compose up -d.",
    ),
    pytest.mark.skipif(
        not _PSYCOPG_AVAILABLE,
        reason="psycopg not installed; cannot verify DB rows",
    ),
]


def _headers() -> dict:
    return {"Authorization": f"Bearer {ADMIN_KEY}"}


def _unique_bytes(base: bytes) -> bytes:
    """Return a copy of `base` with a random-ish suffix so sha256 is unique.

    Appending bytes after `%%EOF` is a no-op for pypdf (EOF marker ends the
    stream), so this preserves PDF validity while breaking dedup across
    tests.
    """
    suffix = f"\n% fixture-{time.time_ns()}-{os.getpid()}\n".encode()
    return base + suffix


# ----------------------------------------------------------------------------
# Pure-python extractor tests (no API / DB needed)
# ----------------------------------------------------------------------------


_extract_skip = pytest.mark.skipif(
    not _EXTRACT_AVAILABLE,
    reason="services.pdf_extract not importable",
)


@_extract_skip
def test_extract_pdf_uses_metadata_title(fixture_pdf_bytes):
    title, text = extract_pdf(fixture_pdf_bytes, filename="irrelevant.pdf")
    assert title == "Fixture Test PDF"
    # Blank page → no extractable text.
    assert text == ""


@_extract_skip
def test_extract_pdf_filename_fallback_on_empty_bytes():
    title, text = extract_pdf(b"", filename="My Doc.pdf")
    assert title == "My Doc"
    assert text == ""


@_extract_skip
def test_extract_pdf_returns_empty_on_corrupt_bytes():
    title, text = extract_pdf(b"not a pdf at all", filename="nope.pdf")
    assert title == ""
    assert text == ""


@_extract_skip
def test_extract_pdf_filename_strips_pdf_extension():
    title, text = extract_pdf(b"", filename="/tmp/path/to/report.PDF")
    assert title == "report"
    assert text == ""


# ----------------------------------------------------------------------------
# Integration tests against a live API + DB
# ----------------------------------------------------------------------------


@pytest.mark.usefixtures("_api_guard")
def test_digest_true_pdf_creates_staging_row(fixture_pdf_bytes):
    """Happy path: PDF + digest=true → staging row with staging_id + auto_approved."""
    pdf = _unique_bytes(fixture_pdf_bytes)
    resp = requests.post(
        f"{BASE_URL}/attachments",
        params={"digest": "true", "content_type": "application/pdf"},
        headers=_headers(),
        files={"file": ("fixture.pdf", pdf, "application/pdf")},
        timeout=10,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "staging_id" in body
    assert body["content_type"] == "application/pdf"
    assert body["size_bytes"] == len(pdf)

    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT submission_category, change_type, proposed_title, status, promoted_entry_id "
                "FROM staging WHERE id = %s",
                (body["staging_id"],),
            )
            row = cur.fetchone()
    assert row is not None
    category, change_type, title, status, promoted_entry_id = row
    assert category == "attachment_digest"
    assert change_type == "create"
    assert title == "Fixture Test PDF"
    # Tier 1/2 auto-approves synchronously.
    assert status == "auto_approved"
    assert promoted_entry_id is not None


@pytest.mark.usefixtures("_api_guard")
def test_auto_approved_digest_creates_entry_attachments_row(fixture_pdf_bytes):
    """After Tier 1/2 auto-approve, entry_attachments(role='source') is populated."""
    pdf = _unique_bytes(fixture_pdf_bytes)
    resp = requests.post(
        f"{BASE_URL}/attachments",
        params={"digest": "true", "content_type": "application/pdf"},
        headers=_headers(),
        files={"file": ("fixture.pdf", pdf, "application/pdf")},
        timeout=10,
    )
    assert resp.status_code == 201
    body = resp.json()
    blob_id = body["blob_id"]
    staging_id = body["staging_id"]

    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT promoted_entry_id FROM staging WHERE id = %s",
                (staging_id,),
            )
            (entry_id,) = cur.fetchone()
            assert entry_id is not None

            cur.execute(
                "SELECT role FROM entry_attachments "
                "WHERE entry_id = %s AND blob_id = %s",
                (entry_id, blob_id),
            )
            row = cur.fetchone()
    assert row is not None, "entry_attachments row missing"
    assert row[0] == "source"


@pytest.mark.usefixtures("_api_guard")
def test_digest_false_does_not_create_staging(fixture_pdf_bytes):
    pdf = _unique_bytes(fixture_pdf_bytes)
    # digest not set → defaults to false
    resp = requests.post(
        f"{BASE_URL}/attachments",
        headers=_headers(),
        files={"file": ("no-digest.pdf", pdf, "application/pdf")},
        timeout=10,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "staging_id" not in body
    # Content-type in response matches the multipart header.
    assert body["content_type"] == "application/pdf"


@pytest.mark.usefixtures("_api_guard")
def test_non_pdf_with_digest_true_does_not_create_staging():
    """digest=true + non-PDF content type → no staging row (silent fallthrough)."""
    payload = b"hello world text\n"
    resp = requests.post(
        f"{BASE_URL}/attachments",
        params={"digest": "true", "content_type": "text/plain"},
        headers=_headers(),
        files={"file": ("hello.txt", payload, "text/plain")},
        timeout=10,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "staging_id" not in body


@pytest.mark.usefixtures("_api_guard")
def test_corrupt_pdf_still_creates_staging_row():
    """Corrupt bytes shouldn't 500 — we stage with empty content instead."""
    corrupt = b"%PDF-1.4\nthis is not really a pdf\n%%EOF\n" + os.urandom(8)
    resp = requests.post(
        f"{BASE_URL}/attachments",
        params={"digest": "true", "content_type": "application/pdf"},
        headers=_headers(),
        files={"file": ("corrupt.pdf", corrupt, "application/pdf")},
        timeout=10,
    )
    # The upload path succeeds; extraction failure is absorbed.
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "staging_id" in body

    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT proposed_content, proposed_title "
                "FROM staging WHERE id = %s",
                (body["staging_id"],),
            )
            content, title = cur.fetchone()
    # Empty extracted text; title falls back to filename ("corrupt").
    assert content == ""
    assert title == "corrupt"


# ----------------------------------------------------------------------------
# Guard fixture — applied per-integration-test via usefixtures
# ----------------------------------------------------------------------------


@pytest.fixture
def _api_guard():
    """Apply the stacked skip guards to each integration test."""
    if not _REQUESTS_AVAILABLE:
        pytest.skip("requests not installed; install tests/requirements-dev.txt")
    if not _PSYCOPG_AVAILABLE:
        pytest.skip("psycopg not installed; cannot verify DB rows")
    if not _api_available():
        pytest.skip(f"Brilliant API not reachable at {BASE_URL}")
