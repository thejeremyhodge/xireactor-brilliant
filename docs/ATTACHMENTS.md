# Attachments

File attachments let users and agents upload arbitrary files to Brilliant, dedupe them by content hash, and — for PDFs — digest them straight into staged entries for normal Tier 1/2 review. Added in v0.3.0 (spec [`0034b`](../.xireactor/specs/0034b--2026-04-16--sprint-lane2-file-uploads.md), issue [#17](https://github.com/thejeremyhodge/xireactor-brilliant/issues/17)).

For exact request/response shapes, see [`skill/references/api-reference.md` Section 16](../skill/references/api-reference.md). This doc covers the model and operational concerns only.

## Overview

Two tables back the feature:

- **`blobs`** — content-addressed binary objects. One row per unique `(org_id, sha256)` pair. Tracks `content_type`, `size_bytes`, `storage_backend`, `storage_key`, `uploaded_by`, `uploaded_at`.
- **`entry_attachments`** — join table. Connects `entry_id → blob_id` with a `role` (`source`, `reference`, etc.) and `created_at`.

Uploads always land in `blobs` first. Attaching a blob to an entry is a separate concern (for PDF digests, the system creates the staged entry and the link automatically).

## Storage Backends

Choose the backend via the `STORAGE_BACKEND` env var. Both backends implement the same `Storage` protocol in `api/services/storage.py`, so switching is a config change — no code edits.

| Var | Backend | Purpose |
|---|---|---|
| `STORAGE_BACKEND` | `local` \| `s3` | Selects the implementation (default `local`). |
| `LOCAL_STORAGE_ROOT` | `local` | Root dir for blob files. Default `/data/uploads`. Blobs land at `{root}/{org_id}/{sha[:2]}/{sha}`. |
| `LOCAL_STORAGE_SIGNING_KEY` | `local` | HMAC key for signing download URLs. Auto-generated and persisted to `{root}/.signing_key` if unset. |
| `S3_ENDPOINT_URL` | `s3` | Full endpoint (e.g. `https://<account>.r2.cloudflarestorage.com`). Leave unset for real AWS. |
| `S3_BUCKET` | `s3` | Target bucket. |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | `s3` | Credentials. |
| `S3_REGION` | `s3` | Region (e.g. `us-east-1`, `auto` for R2). Default `auto`. |
| `MAX_ATTACHMENT_BYTES` | both | Per-upload size cap. Default 50 MiB. Exceeding returns 413. |

**Local** is the default and the right choice for single-node deployments. Files live under `LOCAL_STORAGE_ROOT`; signed URLs point back to the API at `GET /attachments/_local/{key}?exp=...&sig=...` and are validated against the HMAC key.

**S3** covers AWS S3, Cloudflare R2, and MinIO (and any other endpoint that speaks the S3 API). Signed URLs in this mode are S3 presigned URLs pointing directly at the object store — the API is out of the bytes path.

## Retention

- Blobs are kept **indefinitely**. There is no TTL or auto-cleanup in v1.
- Deleting an entry cascades to its `entry_attachments` rows (via FK) but **does not** delete the underlying blob. This is dedup-safe: the same blob may be attached to other entries in the same org.
- **Orphan-blob GC is out of scope for v1.** A follow-up task will add a reaper for blobs with zero `entry_attachments` rows older than N days. Track if/when operators report storage cost pressure.

## Dedup Semantics

Deduplication is scoped to `(org_id, sha256)`:

- Uploading identical bytes to the **same org** twice returns the original `blob_id` with `"dedup": true` on the second call. No second copy is written to storage.
- Uploading identical bytes to **different orgs** produces **two distinct `blob_id`s** with independent storage keys. There is no cross-org dedup — tenant isolation takes precedence over storage efficiency.

This keeps the RLS model simple: a blob belongs to one org, full stop. Auditors do not need to reason about shared-content edge cases across tenants.

## Permission Model

Blob reads are gated by **entry visibility**, not by direct blob ownership.

To `GET /attachments/{blob_id}`, the caller must have read access (via RLS) to at least one entry that references the blob through `entry_attachments`. The evaluation:

1. Resolve all entries linked to the blob.
2. Filter through the caller's entry RLS.
3. If any survive → issue a 302 to a signed URL.
4. If none survive (or the blob does not exist) → return **404**.

**404, not 403.** Returning 403 would confirm the blob exists to an unauthorized caller, which leaks existence. The 404 covers both "does not exist" and "you cannot see any entry using it."

Uploads (`POST /attachments`) require any authenticated user; the blob is tagged with the caller's `org_id` from the auth context.

## API Cheatsheet

Full specs in [api-reference.md §16](../skill/references/api-reference.md). Quick reference:

```bash
# Upload a PDF and digest it into a staged entry
curl -X POST "http://localhost:8010/attachments?digest=true&content_type=application/pdf" \
  -H "Authorization: Bearer $KEY" \
  -F "file=@./whitepaper.pdf"
# → { "blob_id": "...", "sha256": "...", "dedup": false, "staging_id": "..." }

# Follow the redirect to fetch the original
curl -L "http://localhost:8010/attachments/$BLOB_ID" \
  -H "Authorization: Bearer $KEY" -o whitepaper.pdf

# List attachments on an entry
curl "http://localhost:8010/entries/$ENTRY_ID/attachments" \
  -H "Authorization: Bearer $KEY"
```

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/attachments` | Multipart upload. `?digest=true&content_type=application/pdf` triggers PDF digestion. |
| `GET` | `/attachments/{blob_id}` | 302 to a 5-minute signed URL, or 404 if unreachable. |
| `GET` | `/entries/{id}/attachments` | List attachments on an entry. |

## MCP Tool

Co-work sessions call `upload_attachment` instead of crafting multipart requests directly:

```python
upload_attachment(path, digest=True, content_type=None) -> dict
```

- `path` — absolute path readable by the MCP server process. For Docker-hosted MCP, the file must live on a bind-mounted volume.
- `digest` — defaults to `True`. When combined with a PDF, triggers the staged-entry pipeline.
- `content_type` — explicit override. When omitted, derived from the file extension; falls back to `application/octet-stream`.

Returns the `POST /attachments` response verbatim (see api-reference §16a).

**Example session:**

```
> upload_attachment("/workspace/inbox/q4-plan.pdf")
{ "blob_id": "...", "sha256": "...", "staging_id": "stg_..." }

> list_staging()
# Shows q4-plan.pdf digest queued for review

> review_staging("stg_...", "approve")
# Entry promoted, entry_attachments row linked back to the blob
```

## PDF Digestion Flow

Triggered by `POST /attachments?digest=true&content_type=application/pdf` (or the MCP tool with a `.pdf`):

1. **Upload & dedup** — Bytes are hashed and persisted as a blob. Duplicates short-circuit here.
2. **Extract** — `api/services/pdf_extract.py` uses `pypdf` to pull text from the PDF. Title is taken from PDF metadata, falling back to the filename stem.
3. **Stage** — A single staged entry is created with `submission_category='attachment_digest'`, content set to the extracted text. The entry enters the **normal Tier 1/2 staging flow** — no special-casing downstream.
4. **Link on approval** — When the staged entry is approved, a row is written to `entry_attachments(entry_id, blob_id, role='source')`. The original PDF is now retrievable via `GET /attachments/{blob_id}` for any user who can see the entry.

**Limits:**

- `pypdf` handles born-digital PDFs well. **Scanned PDFs produce empty or garbage text.** OCR is out of scope for v1 — track a follow-up if users hit it.
- Only PDFs are digested. Other content types land as raw blobs; attaching them to entries is a manual operation.
- One staged entry per upload. Multi-document PDFs are not split.

## Follow-ups

- **Orphan-blob GC.** Reap blobs with zero `entry_attachments` rows older than N days.
- **OCR for scanned PDFs.** Add a Tesseract (or cloud OCR) path when frequency warrants.
- **More file types.** Images, docx, audio transcripts — each its own digest pipeline.
- **Per-tenant storage quotas.** Schema already tracks `size_bytes`; enforcement is the next step.
