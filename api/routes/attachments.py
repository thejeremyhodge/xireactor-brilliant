"""Attachment upload endpoint (spec 0034b, T-0182 / T-0183 / T-0184).

`POST /attachments` accepts a multipart file upload, content-addresses it
by sha256, dedupes per-org against the `blobs` table, and persists bytes
through the configured `Storage` backend (local FS or S3-compatible).

Auth: any authenticated user. Blob inserts run under the caller's
Postgres role via `get_db(user)`, so RLS (migration 022) enforces
`org_id = app.org_id AND uploaded_by = app.user_id`.

Dedup semantics:
  - Same bytes, same org → single `blobs` row; second call returns
    `dedup: true` with the original `blob_id`.
  - Same bytes, different orgs → two distinct `blobs` rows
    (tenant isolation holds even with a shared backend).

When `digest=true` AND the effective content-type is `application/pdf`,
T-0183 wires an extraction pass via `services.pdf_extract.extract_pdf`:
a staging row with `submission_category='attachment_digest'` is created,
the blob_id is stashed in `proposed_meta`, and for Tier 1/2 submissions
the row is auto-promoted synchronously so the resulting entry already
carries an `entry_attachments(role='source')` link on return.
"""

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import re

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from psycopg.rows import dict_row

from auth import UserContext, get_current_user
from database import get_db, get_pool
from routes.staging import _assign_governance_tier, _promote_staging_item
from services.pdf_extract import extract_pdf
from services.storage import get_storage, verify_local_signed_url


router = APIRouter(tags=["attachments"])


# 50 MiB default cap; override via MAX_ATTACHMENT_BYTES env.
_DEFAULT_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
_READ_CHUNK = 65536


def _max_attachment_bytes() -> int:
    """Read the per-request size cap from env at call time.

    Read on each request (rather than at import) so tests can tweak the
    cap via monkeypatching the environment without re-importing the
    module.
    """
    raw = os.environ.get("MAX_ATTACHMENT_BYTES")
    if not raw:
        return _DEFAULT_MAX_ATTACHMENT_BYTES
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_ATTACHMENT_BYTES
    return value if value > 0 else _DEFAULT_MAX_ATTACHMENT_BYTES


@router.post("", status_code=201)
async def upload_attachment(
    file: UploadFile = File(...),
    digest: bool = Query(False, description="If true, run PDF digest pipeline (T-0183)"),
    content_type: str | None = Query(
        None,
        description="Override the multipart content-type (e.g. when uploading from a tool that sends octet-stream).",
    ),
    user: UserContext = Depends(get_current_user),
):
    """Upload a file, dedupe by sha256 within org, return blob metadata.

    Response shape:
        {
            "blob_id": "<uuid>",
            "sha256": "<hex>",
            "dedup": <bool>,
            "size_bytes": <int>,
            "content_type": "<mime>",
            "staging_id": "<uuid>"    # only when digest=True and PDF
        }
    """
    max_bytes = _max_attachment_bytes()

    # Stream into memory, hashing as we go and counting bytes manually.
    # UploadFile.size isn't populated until the file is fully read, so
    # enforcement has to happen inline on the running byte counter.
    hasher = hashlib.sha256()
    buf = io.BytesIO()
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Attachment exceeds MAX_ATTACHMENT_BYTES ({max_bytes} bytes)",
            )
        hasher.update(chunk)
        buf.write(chunk)

    sha256_hex = hasher.hexdigest()
    data = buf.getvalue()

    # Effective content type: explicit query override > multipart header
    # > application/octet-stream fallback.
    effective_ct = (
        content_type
        or file.content_type
        or "application/octet-stream"
    )

    blob_id: str
    dedup: bool
    resp_size_bytes: int
    resp_content_type: str

    async with get_db(user) as conn:
        # Dedup probe: has this org already uploaded these exact bytes?
        cur = await conn.execute(
            """
            SELECT id, storage_backend, storage_key, content_type, size_bytes
            FROM blobs
            WHERE org_id = %s AND sha256 = %s
            """,
            (user.org_id, sha256_hex),
        )
        cur.row_factory = dict_row
        existing = await cur.fetchone()

        if existing is not None:
            blob_id = str(existing["id"])
            dedup = True
            resp_size_bytes = int(existing["size_bytes"])
            resp_content_type = existing["content_type"]
        else:
            # New blob: persist bytes to the backend first, then record the
            # pointer row. If the INSERT fails (RLS, unique race), the bytes
            # are still on-disk but orphaned — acceptable for v1; a sweep
            # job can reconcile later.
            storage = get_storage()
            storage_key = await storage.put(
                user.org_id, sha256_hex, effective_ct, data
            )
            storage_backend = (
                os.environ.get("STORAGE_BACKEND") or "local"
            ).strip().lower()

            # Race-safe insert: if a concurrent request won the race, fall
            # back to the existing row so both callers see consistent IDs.
            cur = await conn.execute(
                """
                INSERT INTO blobs (
                    org_id, sha256, content_type, size_bytes,
                    storage_backend, storage_key, uploaded_by
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (org_id, sha256) DO NOTHING
                RETURNING id, content_type, size_bytes
                """,
                (
                    user.org_id,
                    sha256_hex,
                    effective_ct,
                    total,
                    storage_backend,
                    storage_key,
                    user.id,
                ),
            )
            cur.row_factory = dict_row
            inserted = await cur.fetchone()

            if inserted is None:
                # Lost the race — another writer inserted between our probe
                # and our INSERT. Re-read and return that row, flagging as
                # dedup so the caller's idempotency expectation holds.
                cur = await conn.execute(
                    """
                    SELECT id, content_type, size_bytes
                    FROM blobs
                    WHERE org_id = %s AND sha256 = %s
                    """,
                    (user.org_id, sha256_hex),
                )
                cur.row_factory = dict_row
                inserted = await cur.fetchone()
                if inserted is None:
                    # Shouldn't happen — ON CONFLICT fired but SELECT empty.
                    raise HTTPException(
                        status_code=500,
                        detail="Blob persisted but row not visible; retry.",
                    )
                blob_id = str(inserted["id"])
                dedup = True
                resp_size_bytes = int(inserted["size_bytes"])
                resp_content_type = inserted["content_type"]
            else:
                blob_id = str(inserted["id"])
                dedup = False
                resp_size_bytes = int(inserted["size_bytes"])
                resp_content_type = inserted["content_type"]

        response: dict = {
            "blob_id": blob_id,
            "sha256": sha256_hex,
            "dedup": dedup,
            "size_bytes": resp_size_bytes,
            "content_type": resp_content_type,
        }

        # --- PDF digest branch (T-0183) -----------------------------------
        # When the caller asks to digest a PDF, extract text via pypdf and
        # drop a staging row with submission_category='attachment_digest'.
        # The staging row carries the blob_id in proposed_meta; the normal
        # promotion path (routes/staging.py::_promote_staging_item) reads
        # that meta after entry creation and inserts the entry_attachments
        # join row so the blob is retrievable via the entry afterwards.
        #
        # Failure mode: if pypdf can't read the bytes, `extract_pdf`
        # returns ("", "") and we still create a staging row — an empty
        # digest is preferable to a 500 that hides the uploaded blob.
        if digest and effective_ct == "application/pdf":
            title, text = extract_pdf(data, filename=file.filename)

            # Build a Tier-assigned staging row mirroring submit_staging's
            # tier selection for agent/api sources (Tier 2 for agents on
            # non-sensitive creates); mirroring the helper rather than
            # hardcoding keeps the behavior aligned if the matrix evolves.
            governance_tier = _assign_governance_tier(
                change_type="create",
                sensitivity=None,
                source=user.source,
                role=user.role,
            )

            # Sanitize title for the target_path segment: strip anything
            # that isn't alnum/dash/underscore/space, collapse whitespace.
            fallback_title = title or (
                os.path.splitext(os.path.basename(file.filename or ""))[0]
                or "Attachment"
            )
            safe_title = re.sub(r"[^\w\- ]+", "", fallback_title).strip()
            safe_title = re.sub(r"\s+", "-", safe_title) or "Attachment"
            target_path = f"Attachments/{sha256_hex[:12]}/{safe_title}"

            proposed_meta = {
                "title": fallback_title,
                "source_filename": file.filename,
                "blob_id": blob_id,
                "content_type": "resource",
            }
            content_hash = hashlib.sha256((text or "").encode()).hexdigest()
            initial_status = (
                "auto_approved" if governance_tier in (1, 2) else "pending"
            )

            cur = await conn.execute(
                """
                INSERT INTO staging (
                    org_id, target_entry_id, target_path, change_type,
                    proposed_title, proposed_content, proposed_meta, content_hash,
                    submitted_by, source,
                    governance_tier, submission_category,
                    status, priority
                ) VALUES (
                    %(org_id)s, NULL, %(target_path)s, 'create',
                    %(proposed_title)s, %(proposed_content)s,
                    %(proposed_meta)s, %(content_hash)s,
                    %(submitted_by)s, %(source)s,
                    %(governance_tier)s, 'attachment_digest',
                    %(status)s, 3
                )
                RETURNING id, org_id, target_entry_id, target_path, change_type,
                          proposed_title, proposed_content, proposed_meta,
                          submitted_by, source, governance_tier,
                          submission_category, status
                """,
                {
                    "org_id": user.org_id,
                    "target_path": target_path,
                    "proposed_title": fallback_title,
                    "proposed_content": text or "",
                    "proposed_meta": json.dumps(proposed_meta),
                    "content_hash": content_hash,
                    "submitted_by": user.id,
                    "source": user.source,
                    "governance_tier": governance_tier,
                    "status": initial_status,
                },
            )
            cur.row_factory = dict_row
            staging_row = await cur.fetchone()
            response["staging_id"] = str(staging_row["id"])

            # Tier 1/2 auto-promotion: mirror submit_staging's fast path.
            # Escalating to kb_admin is required because kb_agent/kb_editor
            # cannot INSERT into entries; the existing submit_staging flow
            # does the same dance. _promote_staging_item will also insert
            # the entry_attachments row via the digest hook we added to it.
            if governance_tier in (1, 2):
                await conn.execute("SET LOCAL ROLE kb_admin")
                # _promote_staging_item expects a dict with proposed_meta as
                # a python dict — but the RETURNING gave us the JSONB parsed
                # as a dict already, which psycopg does by default.
                entry_row = await _promote_staging_item(
                    conn, staging_row, user.id
                )
                await conn.execute(
                    "UPDATE staging SET promoted_entry_id = %s WHERE id = %s",
                    (entry_row["id"], staging_row["id"]),
                )

        return response


# ---------------------------------------------------------------------------
# GET /attachments/_local/{key:path} — signed-URL handler for LocalStorage
# ---------------------------------------------------------------------------
#
# IMPORTANT: this route is registered BEFORE `GET /attachments/{blob_id}` so
# the `_local` literal prefix wins over the parameterized match. FastAPI
# matches in registration order for ambiguous paths.
#
# The `_local` path is only meaningful when `STORAGE_BACKEND=local`.
# Signatures are HMAC'd by `services.storage.LocalStorage` and verified here
# via `verify_local_signed_url(key, exp, sig)`. Auth is intentionally not
# required — the signed URL itself is the bearer token, with a 5-minute TTL
# enforced by the HMAC payload.


@router.get("/_local/{key:path}")
async def get_local_signed(
    key: str,
    request: Request,
    exp: str = Query(..., description="Signed URL expiry (unix seconds)"),
    sig: str = Query(..., description="HMAC-SHA256 signature"),
):
    """Serve the bytes for a LocalStorage-backed blob via a signed URL.

    Verifies the HMAC signature + expiry; 403s on any failure. On success
    reads the bytes through the LocalStorage backend and returns them with
    the caller-hinted Content-Type (falling back to `mimetypes.guess_type`
    on the storage key, then `application/octet-stream`).
    """
    backend = (os.environ.get("STORAGE_BACKEND") or "local").strip().lower()
    if backend != "local":
        # Shouldn't be reachable in non-local mode — signed URLs are only
        # minted by LocalStorage — but fail closed if a caller crafts one.
        raise HTTPException(status_code=404, detail="Not found")

    if not verify_local_signed_url(key, exp, sig):
        raise HTTPException(status_code=403, detail="Invalid or expired signature")

    storage = get_storage()
    try:
        data = await storage.get(key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Blob not found")
    except ValueError:
        # `_path_for` rejects traversal attempts.
        raise HTTPException(status_code=400, detail="Invalid storage key")

    # Prefer the content-type recorded on the blobs row when we can look it
    # up, but the signed URL handler runs unauthenticated so we can't filter
    # by RLS here. Do a raw-pool lookup by storage_key (keys are globally
    # unique within a backend: {org_id}/{sha[:2]}/{sha}). Falls back to a
    # mimetype guess if the row is missing.
    content_type = "application/octet-stream"
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT content_type FROM blobs WHERE storage_key = %s LIMIT 1",
                (key,),
            )
            row = await cur.fetchone()
            if row and row[0]:
                content_type = row[0]
            else:
                guess, _ = mimetypes.guess_type(key)
                if guess:
                    content_type = guess
    except Exception:
        # Lookup is a nice-to-have; never block byte delivery on it.
        guess, _ = mimetypes.guess_type(key)
        if guess:
            content_type = guess

    return Response(content=data, media_type=content_type)


# ---------------------------------------------------------------------------
# GET /attachments/{blob_id} — auth-gated 302 redirect to signed URL
# ---------------------------------------------------------------------------


@router.get("/{blob_id}")
async def get_attachment(
    blob_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Return a 302 redirect to a time-limited signed URL for this blob.

    Visibility model (two-step):

    1. Under the caller's RLS context, probe `entry_attachments` joined to
       `entries`. Entry RLS (004/011/019) filters out entries the caller
       cannot see, and the `entry_attachments_select_via_entry` policy
       further hides attachment rows whose entry is invisible. If no row
       is returned, the caller either has no entry with this blob attached
       or the blob doesn't exist — either way we answer **404** to avoid
       leaking existence of blobs the caller has no read access to.

    2. After authorization clears, fetch the blob's storage pointer via a
       raw pool connection (superuser, bypasses RLS). This two-stage split
       keeps authorization entirely in RLS while letting the signer access
       the `storage_key` without needing a role that can SELECT all blobs.

    Returns 302 with `Location:` set to `Storage.signed_url(key, ttl=300)`.
    """
    # --- Step 1: authorization under user RLS ---
    async with get_db(user) as conn:
        cur = await conn.execute(
            """
            SELECT 1
            FROM entry_attachments ea
            JOIN entries e ON e.id = ea.entry_id
            WHERE ea.blob_id = %s
            LIMIT 1
            """,
            (blob_id,),
        )
        has_access = await cur.fetchone()

    if has_access is None:
        # Don't leak existence — 404 whether the blob doesn't exist or the
        # caller just can't see any entry that references it.
        raise HTTPException(status_code=404, detail="Not found")

    # --- Step 2: fetch storage pointer bypassing RLS ---
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT storage_backend, storage_key FROM blobs WHERE id = %s",
            (blob_id,),
        )
        cur.row_factory = dict_row
        blob = await cur.fetchone()

    if blob is None:
        # Shouldn't happen — authorization just confirmed an attachment row
        # referencing this blob_id — but guard against races / FK gaps.
        raise HTTPException(status_code=404, detail="Not found")

    storage = get_storage()
    url = storage.signed_url(blob["storage_key"], ttl_seconds=300)

    return RedirectResponse(url=url, status_code=302)
