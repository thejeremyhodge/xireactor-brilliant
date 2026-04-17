"""PDF text extraction for the attachment digest pipeline (spec 0034b, T-0183).

Single entry point: `extract_pdf(data, filename) -> (title, text)`.

Title resolution priority:
  1. `/Info /Title` metadata embedded in the PDF
  2. First non-empty line of page 1's extracted text
  3. The supplied `filename` (with `.pdf` extension stripped) as a fallback

Text is the concatenation of `page.extract_text()` across all pages,
joined by a blank line. Whitespace is trimmed at the edges.

Error handling is intentionally permissive: any `pypdf` failure (corrupt
bytes, encrypted PDFs, truncated streams) causes the function to return
`("", "")` rather than raising. Callers get to decide whether an empty
digest is still worth staging (the T-0183 route does stage it — a
staging row with empty content is preferable to a 500 that masks the
underlying attachment).
"""

from __future__ import annotations

import io
import logging
import os

try:  # pypdf is declared in api/requirements.txt; keep the import tolerant
    from pypdf import PdfReader
except ImportError:  # pragma: no cover — only hit in environments without pypdf
    PdfReader = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)


def _title_from_metadata(reader: "PdfReader") -> str:
    """Return the /Info /Title metadata string if present and non-empty."""
    try:
        meta = reader.metadata
    except Exception:  # noqa: BLE001 — any metadata fetch failure is non-fatal
        return ""
    if meta is None:
        return ""
    # pypdf exposes `title` on the DocumentInformation object, plus the
    # raw `/Title` key on the dict-like proxy. Try both — some PDFs stash
    # the title under the dict key without populating the accessor.
    candidate = ""
    try:
        candidate = (getattr(meta, "title", None) or "").strip()
    except Exception:  # noqa: BLE001
        candidate = ""
    if candidate:
        return candidate
    try:
        raw = meta.get("/Title", "") if hasattr(meta, "get") else ""
        if raw:
            return str(raw).strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _title_from_first_page(pages_text: list[str]) -> str:
    """Return the first non-empty stripped line from the first page, if any."""
    if not pages_text:
        return ""
    first = pages_text[0] or ""
    for line in first.splitlines():
        stripped = line.strip()
        if stripped:
            # Cap absurdly long "first lines" — some scans have
            # single-line pages. 200 chars is plenty for a title.
            return stripped[:200]
    return ""


def _title_from_filename(filename: str | None) -> str:
    """Strip directory, then a trailing `.pdf` extension (case-insensitive)."""
    if not filename:
        return ""
    base = os.path.basename(filename).strip()
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    return base.strip()


def extract_pdf(data: bytes, filename: str | None = None) -> tuple[str, str]:
    """Extract `(title, text)` from PDF `data`.

    On any pypdf error, returns `("", "")`. The caller decides whether to
    stage an empty digest, surface an error to the user, or retry with a
    different pipeline (e.g. OCR for scanned PDFs).
    """
    if PdfReader is None:
        logger.warning("pypdf not installed; extract_pdf returning empty tuple")
        return ("", "")

    if not data:
        return ("", _title_from_filename(filename))

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 — corrupt bytes, truncated PDFs, etc.
        logger.info("extract_pdf: PdfReader failed: %s", exc)
        return ("", "")

    pages_text: list[str] = []
    try:
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception as exc:  # noqa: BLE001 — bad page stream
                logger.info("extract_pdf: page.extract_text failed: %s", exc)
                text = ""
            pages_text.append(text)
    except Exception as exc:  # noqa: BLE001 — reader.pages iteration itself failed
        logger.info("extract_pdf: pages iteration failed: %s", exc)
        return ("", "")

    full_text = "\n\n".join(pt for pt in pages_text).strip()

    title = _title_from_metadata(reader)
    if not title:
        title = _title_from_first_page(pages_text)
    if not title:
        title = _title_from_filename(filename)

    return (title, full_text)


__all__ = ["extract_pdf"]
