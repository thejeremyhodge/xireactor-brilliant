"""Bulk markdown file import endpoint with preview, collision detection, and batch tracking."""

import hashlib
import io
import json
import logging
import os
import re
import secrets
import tarfile
import zipfile

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse
from psycopg.rows import dict_row

from auth import UserContext, get_current_user
from database import get_db, get_pool
from services.frontmatter import (
    build_domain_meta,
    extract_governance_fields,
    extract_title,
    parse_frontmatter,
)
from services import audit
from services.audit import _app_role_to_pg_role
from services.links import sync_entry_links
from services.storage import get_storage
from services.vault_walker import iter_archive_md, resolve_exclude_patterns
from models import (
    ImportFile,
    ImportRequest,
    ImportSummary,
    ImportPreviewRequest,
    ImportPreviewResponse,
    ImportExecuteRequest,
    ImportExecuteResponse,
    CollisionEntry,
    ImportBatchResponse,
    RollbackResponse,
    VaultFromBlobRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["import"])

# Regex to detect [[wiki-links]] in markdown content.
# Captures up to the first | (display text) or # (heading anchor) or ]],
# handling [[Note]], [[Note|display]], and [[Note#Heading]] formats.
_WIKI_LINK_RE = re.compile(r"\[\[([^\]|#\\]+)")

# Regex for inline #tags in content (but not ## headings)
_INLINE_TAG_RE = re.compile(r"(?:^|\s)#([a-zA-Z][\w-]*)", re.MULTILINE)

# Regex for daily note filenames like 2024-01-15.md
_DAILY_NOTE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")


# ---------------------------------------------------------------------------
# Vault-from-blob size caps (read at request time, env-overridable)
# ---------------------------------------------------------------------------
#
# Compressed cap is enforced via the recorded ``blobs.size_bytes`` before
# the tarball is even fetched from storage. Uncompressed cap is enforced
# mid-iteration by ``vault_walker.iter_tarball_md`` which raises
# ``ValueError`` when the running total crosses the threshold.

_DEFAULT_MAX_VAULT_TARBALL_BYTES = 25_000_000  # 25MB compressed
_DEFAULT_MAX_VAULT_UNCOMPRESSED_BYTES = 200_000_000  # 200MB uncompressed


def _env_int(name: str, default: int) -> int:
    """Read a positive int env var, falling back on missing/invalid values."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _max_vault_tarball_bytes() -> int:
    """Compressed tarball size cap in bytes (default 25MB)."""
    return _env_int("MAX_VAULT_TARBALL_BYTES", _DEFAULT_MAX_VAULT_TARBALL_BYTES)


def _max_vault_uncompressed_bytes() -> int:
    """Expanded tarball size cap in bytes (default 200MB, zip-bomb guard)."""
    return _env_int(
        "MAX_VAULT_UNCOMPRESSED_BYTES", _DEFAULT_MAX_VAULT_UNCOMPRESSED_BYTES
    )


def infer_content_type(logical_path: str) -> str:
    """Infer content_type from the logical_path pattern."""
    path_lower = logical_path.lower()
    if "meeting" in path_lower:
        return "meeting"
    if "project" in path_lower:
        return "project"
    if "decision" in path_lower:
        return "decision"
    if "onboarding" in path_lower:
        return "onboarding"
    if "resource" in path_lower:
        return "resource"
    if "daily" in path_lower:
        return "daily"
    if "intel" in path_lower:
        return "intelligence"
    return "context"


# parse_frontmatter / extract_title / extract_governance_fields /
# build_domain_meta live in services/frontmatter (pure-python, unit-testable
# without DB setup) and are imported at the top of this module.


def extract_tags(meta: dict, content: str) -> list[str]:
    """Extract tags from frontmatter and inline #tags, merged and deduplicated."""
    tags: set[str] = set()
    # Frontmatter tags
    raw = meta.get("tags", "")
    if raw:
        if isinstance(raw, list):
            # Already parsed as a list by parse_frontmatter
            tags.update(str(t).strip() for t in raw if str(t).strip())
        else:
            # Handle both "tag1, tag2" and "[tag1, tag2]" formats (legacy)
            raw = str(raw).strip("[]")
            tags.update(
                t.strip().strip('"').strip("'") for t in raw.split(",") if t.strip()
            )
    # Inline #tags
    tags.update(_INLINE_TAG_RE.findall(content))
    return sorted(tags)


def _build_logical_path(filename: str, base_path: str) -> str:
    """Build logical_path from filename and base_path."""
    name_part = filename
    if name_part.lower().endswith(".md"):
        name_part = name_part[:-3]
    if base_path:
        return f"{base_path.rstrip('/')}/{name_part}"
    return name_part


def _parse_file(file: ImportFile, base_path: str) -> dict:
    """Parse a single import file and return extracted data dict.

    The persisted ``content`` is the frontmatter-stripped body. `_WIKI_LINK_RE`
    still runs over that stripped body (frontmatter-embedded `[[...]]` would
    be spurious link rows).

    Returns a dict with keys: filename, title, content (stripped),
    remaining_content (same as content, kept for back-compat), meta, tags,
    governance (sensitivity/content_type/department/summary overrides),
    domain_meta, logical_path, content_hash, wiki_targets.
    """
    meta, remaining_content = parse_frontmatter(file.content)
    title = extract_title(remaining_content, file.filename, meta)
    tags = extract_tags(meta, remaining_content)
    governance = extract_governance_fields(meta)
    domain_meta = build_domain_meta(meta)
    logical_path = _build_logical_path(file.filename, base_path)
    # Hash the stripped content so frontmatter edits that only touch
    # structural fields (title, tags, sensitivity) don't mask a true
    # body-level content collision.
    content_hash = hashlib.sha256(remaining_content.encode()).hexdigest()
    wiki_targets = _WIKI_LINK_RE.findall(remaining_content)
    return {
        "filename": file.filename,
        "title": title,
        "content": remaining_content,
        "remaining_content": remaining_content,
        "meta": meta,
        "tags": tags,
        "governance": governance,
        "domain_meta": domain_meta,
        "logical_path": logical_path,
        "content_hash": content_hash,
        "wiki_targets": wiki_targets,
    }


async def detect_collisions(
    conn, org_id: str, files_data: list[dict]
) -> list[CollisionEntry]:
    """Detect collisions between import files and existing published entries.

    Args:
        conn: async psycopg connection with RLS context set.
        org_id: organization ID.
        files_data: list of dicts with keys: filename, title, logical_path, content_hash.

    Returns:
        List of CollisionEntry for each detected collision.
    """
    collisions: list[CollisionEntry] = []

    for fd in files_data:
        # Path collision
        cur = await conn.execute(
            """SELECT id, title FROM entries
               WHERE logical_path = %(path)s
                 AND org_id = %(org_id)s
                 AND status = 'published'
               LIMIT 1""",
            {"path": fd["logical_path"], "org_id": org_id},
        )
        row = await cur.fetchone()
        if row:
            collisions.append(
                CollisionEntry(
                    filename=fd["filename"],
                    proposed_title=fd["title"],
                    proposed_path=fd["logical_path"],
                    existing_entry_id=str(row[0]),
                    existing_title=str(row[1]),
                    collision_type="path",
                )
            )
            continue  # path collision is the most specific; skip other checks for this file

        # Title collision (case-insensitive)
        cur = await conn.execute(
            """SELECT id, title FROM entries
               WHERE LOWER(title) = LOWER(%(title)s)
                 AND org_id = %(org_id)s
                 AND status = 'published'
               LIMIT 1""",
            {"title": fd["title"], "org_id": org_id},
        )
        row = await cur.fetchone()
        if row:
            collisions.append(
                CollisionEntry(
                    filename=fd["filename"],
                    proposed_title=fd["title"],
                    proposed_path=fd["logical_path"],
                    existing_entry_id=str(row[0]),
                    existing_title=str(row[1]),
                    collision_type="title",
                )
            )
            continue

        # Content hash collision (duplicate content)
        cur = await conn.execute(
            """SELECT id, title FROM entries
               WHERE content_hash = %(hash)s
                 AND org_id = %(org_id)s
                 AND status = 'published'
               LIMIT 1""",
            {"hash": fd["content_hash"], "org_id": org_id},
        )
        row = await cur.fetchone()
        if row:
            collisions.append(
                CollisionEntry(
                    filename=fd["filename"],
                    proposed_title=fd["title"],
                    proposed_path=fd["logical_path"],
                    existing_entry_id=str(row[0]),
                    existing_title=str(row[1]),
                    collision_type="content_hash",
                )
            )

    return collisions


async def _resolve_content_type(
    conn,
    meta: dict,
    filename: str,
    logical_path: str,
    user: UserContext,
):
    """Resolve content_type from frontmatter, registry, daily note pattern, or path inference.

    Returns (content_type, type_mapping_or_None, unrecognized_type_or_None).
    Accepts both `content_type` (preferred, #25) and the legacy `type` alias.

    When frontmatter declares a type that is not in
    `content_type_registry`, the type is auto-registered with
    `is_active=false` inside a privilege-scoped savepoint
    (`SET LOCAL ROLE kb_admin`) mirroring `api/services/audit.py::record`.
    The declared type is then used verbatim on the entry — the registry
    change is runtime-only (no migration) and admins promote the row via
    an explicit `UPDATE ... SET is_active=true` grant. The raw type is
    still returned in the `unrecognized_type` slot so callers can surface
    it in `unrecognized_types` for admin review (spec 0046 / T-0272.2).
    """
    raw_type = meta.get("content_type", "") or meta.get("type", "")
    if isinstance(raw_type, list):
        raw_type = raw_type[0] if raw_type else ""
    raw_type = str(raw_type).strip()

    if raw_type:
        cur = await conn.execute(
            "SELECT name, alias_of FROM content_type_registry WHERE name = %s AND is_active = true",
            (raw_type,),
        )
        reg_row = await cur.fetchone()
        if reg_row:
            canonical = reg_row[1] if reg_row[1] else reg_row[0]
            return canonical, (raw_type, canonical), None

        # Auto-register the unknown type as is_active=false. RLS grants
        # INSERT on `content_type_registry` to `kb_admin` only, so we
        # briefly elevate inside a SAVEPOINT and restore the caller's
        # role before RELEASE — identical pattern to audit.record.
        sp_name = f"sp_autoreg_{secrets.token_hex(6)}"
        pg_role = _app_role_to_pg_role(user.role, user.source)
        try:
            await conn.execute(f"SAVEPOINT {sp_name}")
            try:
                await conn.execute("SET LOCAL ROLE kb_admin")
                await conn.execute(
                    """
                    INSERT INTO content_type_registry (name, description, is_active)
                    VALUES (%s, 'Auto-registered from vault import — promote via admin', false)
                    ON CONFLICT (name) DO NOTHING
                    """,
                    (raw_type,),
                )
            finally:
                # Restore caller's role before RELEASE so the outer
                # transaction continues under the original role.
                try:
                    await conn.execute(f"SET LOCAL ROLE {pg_role}")
                except Exception:  # pragma: no cover — defensive
                    logger.exception(
                        "_resolve_content_type: failed to restore role to %s",
                        pg_role,
                    )
            await conn.execute(f"RELEASE SAVEPOINT {sp_name}")
        except Exception:
            # Don't block the import — log + rollback the savepoint so the
            # outer transaction remains valid. The declared type is still
            # used on the entry; only the registry side-effect is lost.
            logger.exception(
                "_resolve_content_type: auto-register failed for %r — continuing",
                raw_type,
            )
            try:
                await conn.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
                await conn.execute(f"RELEASE SAVEPOINT {sp_name}")
            except Exception:  # pragma: no cover — defensive
                logger.exception(
                    "_resolve_content_type: failed to rollback savepoint %s",
                    sp_name,
                )

        # Use the declared type verbatim; still flag via unrecognized_types.
        return raw_type, None, raw_type
    elif _DAILY_NOTE_RE.match(filename):
        return "daily", None, None
    else:
        return infer_content_type(logical_path), None, None


# ---------------------------------------------------------------------------
# POST /import/preview — Dry-run collision analysis
# ---------------------------------------------------------------------------


@router.post("/preview", response_model=ImportPreviewResponse)
async def import_preview(
    body: ImportPreviewRequest,
    user: UserContext = Depends(get_current_user),
):
    """Dry-run analysis of an import: parse files, detect collisions, count outcomes.

    Does NOT write anything to the database.
    """
    errors: list[str] = []
    type_mappings: dict[str, str] = {}
    unrecognized_types: set[str] = set()

    use_staging = user.role in ("commenter", "viewer") or user.key_type == "agent"

    parsed_files: list[dict] = []
    would_create = 0
    would_stage = 0
    would_link = 0

    # Phase 1: Parse all files
    for file in body.files:
        try:
            fd = _parse_file(file, body.base_path)
            parsed_files.append(fd)
        except Exception as exc:
            errors.append(f"{file.filename}: {str(exc)}")

    # Phase 2: Resolve content types and detect collisions (needs DB)
    async with get_db(user) as conn:
        for fd in parsed_files:
            try:
                content_type, mapping, unrec = await _resolve_content_type(
                    conn, fd["meta"], fd["filename"], fd["logical_path"], user
                )
                fd["content_type"] = content_type
                if mapping:
                    type_mappings[mapping[0]] = mapping[1]
                if unrec:
                    unrecognized_types.add(unrec)
            except Exception as exc:
                errors.append(f"{fd['filename']}: type resolution: {str(exc)}")

        # Count outcomes
        title_to_id: dict[str, bool] = {}
        for fd in parsed_files:
            if use_staging:
                would_stage += 1
            else:
                would_create += 1
                title_to_id[fd["title"].lower()] = True

            # Count potential wiki-links
            for target_name in fd.get("wiki_targets", []):
                target_lower = target_name.strip().lower()
                if target_lower in title_to_id:
                    would_link += 1
                else:
                    # Check existing DB entries
                    cur = await conn.execute(
                        """SELECT id FROM entries
                           WHERE LOWER(title) = LOWER(%(title)s)
                             AND org_id = %(org_id)s
                             AND status = 'published'
                           LIMIT 1""",
                        {"title": target_name.strip(), "org_id": user.org_id},
                    )
                    if await cur.fetchone():
                        would_link += 1

        # Collision detection
        files_data = [
            {
                "filename": fd["filename"],
                "title": fd["title"],
                "logical_path": fd["logical_path"],
                "content_hash": fd["content_hash"],
            }
            for fd in parsed_files
        ]
        collisions = await detect_collisions(conn, user.org_id, files_data)

    return ImportPreviewResponse(
        files_analyzed=len(body.files),
        would_create=would_create,
        would_stage=would_stage,
        would_link=would_link,
        collisions=collisions,
        type_mappings=type_mappings,
        unrecognized_types=sorted(unrecognized_types),
        errors=errors,
    )


# ---------------------------------------------------------------------------
# POST /import — Execute import with batch tracking and collision resolution
# ---------------------------------------------------------------------------


async def _execute_import(
    conn,
    user: UserContext,
    files_data: list[ImportFile],
    base_path: str,
    source_vault: str,
    collisions: list[CollisionEntry],
) -> ImportExecuteResponse:
    """Core import pipeline: batch row + per-file parse/route + link resolution.

    Expects an already-opened async psycopg connection with RLS context set
    (i.e. inside an ``async with get_db(user) as conn`` block). Returns the
    same ``ImportExecuteResponse`` shape as the HTTP handler.

    Callers:
    - ``import_files()`` — HTTP ``POST /import`` thin wrapper below.
    - ``POST /import/vault-from-blob`` — streams a tarball through the
      shared walker and hands the resulting ``ImportFile`` list here.
    """
    created = 0
    staged = 0
    linked = 0
    skipped = 0
    collisions_resolved = 0
    errors: list[str] = []
    type_mappings: dict[str, str] = {}
    unrecognized_types: set[str] = set()
    # Aggregate unresolved wikilink / internal-markdown targets across every
    # file so the HTTP response can surface them (see T-0272.3). Set-dedup
    # means a target referenced from many MOCs counts once.
    all_unresolved: set[str] = set()

    # Whether this user goes through staging
    use_staging = user.role in ("commenter", "viewer") or user.key_type == "agent"

    # Build collision lookup: filename -> CollisionEntry
    collision_lookup: dict[str, CollisionEntry] = {}
    for c in collisions:
        collision_lookup[c.filename] = c

    # Track (entry_id, content) for per-entry link resolution after all INSERTs
    # complete. Deferred so that wiki-link targets created later in the same
    # batch are queryable when we resolve.
    pending_links: list[tuple[str, str]] = []

    # Create import batch record
    cur = await conn.execute(
        """INSERT INTO import_batches (
               org_id, source_vault, base_path, file_count, created_by
           ) VALUES (
               %(org_id)s, %(source_vault)s, %(base_path)s, %(file_count)s, %(created_by)s
           )
           RETURNING id""",
        {
            "org_id": user.org_id,
            "source_vault": source_vault,
            "base_path": base_path,
            "file_count": len(files_data),
            "created_by": user.id,
        },
    )
    batch_row = await cur.fetchone()
    batch_id = str(batch_row[0])

    for file in files_data:
        try:
            # Check collision resolution for this file
            collision = collision_lookup.get(file.filename)
            if collision:
                collisions_resolved += 1
                if collision.resolution == "skip":
                    skipped += 1
                    continue

            # Parse file (frontmatter stripped; governance + domain_meta
            # split out; content_hash computed over the stripped body).
            fd = _parse_file(file, base_path)
            logical_path = fd["logical_path"]
            title = fd["title"]
            tags = fd["tags"]
            meta = fd["meta"]
            governance = fd["governance"]
            domain_meta = fd["domain_meta"]
            # `stripped_content` is the persisted body — frontmatter is
            # removed upstream so downstream render/search/link paths see
            # only the user-authored markdown (#25).
            stripped_content = fd["content"]
            content_hash = fd["content_hash"]
            wiki_targets = fd["wiki_targets"]

            # Apply rename resolution
            if collision and collision.resolution == "rename":
                logical_path = f"{logical_path}-imported-1"

            # Apply merge resolution: append content to existing entry
            if collision and collision.resolution == "merge" and collision.existing_entry_id:
                await conn.execute(
                    """UPDATE entries
                       SET content = content || %(separator)s || %(new_content)s,
                           content_hash = %(content_hash)s,
                           updated_by = %(updated_by)s,
                           version = version + 1,
                           updated_at = now()
                       WHERE id = %(entry_id)s::uuid
                         AND org_id = %(org_id)s""",
                    {
                        "separator": "\n\n",
                        "new_content": stripped_content,
                        "content_hash": content_hash,
                        "updated_by": user.id,
                        "entry_id": collision.existing_entry_id,
                        "org_id": user.org_id,
                    },
                )
                created += 1  # count as processed
                continue

            # Resolve content_type unconditionally. `_resolve_content_type`
            # handles the full spectrum: registry hit (known type, no side
            # effect), registry miss with frontmatter-declared type
            # (auto-register `is_active=false` + flag as unrecognized), and
            # frontmatter-silent (fall back to path inference). Short-circuiting
            # on `governance["content_type"]` skips the auto-register side
            # effect and breaks `moc` round-tripping (spec 0046 AC #3).
            content_type, mapping, unrec = await _resolve_content_type(
                conn, meta, file.filename, logical_path, user
            )
            if mapping:
                type_mappings[mapping[0]] = mapping[1]
            if unrec:
                unrecognized_types.add(unrec)

            # Sensitivity / department / summary come from frontmatter
            # when provided; otherwise fall back to existing defaults.
            sensitivity = governance.get("sensitivity", "shared")
            department = governance.get("department", user.department)
            summary = governance.get("summary")

            if use_staging:
                # Build proposed_meta including governance + tags. The
                # staging approver promotes these back onto the entry on
                # accept, so include the full frontmatter echo here.
                proposed_meta = dict(domain_meta) if domain_meta else {}
                proposed_meta["tags"] = tags
                if "sensitivity" in governance:
                    proposed_meta["sensitivity"] = governance["sensitivity"]
                if "content_type" in governance:
                    proposed_meta["content_type"] = governance["content_type"]
                if "department" in governance:
                    proposed_meta["department"] = governance["department"]
                if "summary" in governance:
                    proposed_meta["summary"] = governance["summary"]

                # Route to staging table
                await conn.execute(
                    """INSERT INTO staging (
                           org_id, target_path, change_type,
                           proposed_title, proposed_content, proposed_meta,
                           content_hash, submitted_by, source,
                           governance_tier, submission_category, status, priority,
                           import_batch_id
                       ) VALUES (
                           %(org_id)s, %(target_path)s, %(change_type)s,
                           %(proposed_title)s, %(proposed_content)s, %(proposed_meta)s,
                           %(content_hash)s, %(submitted_by)s, %(source)s,
                           %(governance_tier)s, %(submission_category)s, %(status)s, %(priority)s,
                           %(import_batch_id)s::uuid
                       )""",
                    {
                        "org_id": user.org_id,
                        "target_path": logical_path,
                        "change_type": "create",
                        "proposed_title": title,
                        "proposed_content": stripped_content,
                        "proposed_meta": json.dumps(proposed_meta),
                        "content_hash": content_hash,
                        "submitted_by": user.id,
                        "source": user.source,
                        "governance_tier": 2,
                        "submission_category": "user_direct",
                        "status": "pending",
                        "priority": 3,
                        "import_batch_id": batch_id,
                    },
                )
                staged += 1
            else:
                # Direct insert into entries
                cur = await conn.execute(
                    """INSERT INTO entries (
                           org_id, title, content, summary, content_hash,
                           content_type, logical_path, sensitivity, department,
                           owner_id, tags, domain_meta,
                           source, created_by, updated_by,
                           import_batch_id
                       ) VALUES (
                           %(org_id)s, %(title)s, %(content)s, %(summary)s, %(content_hash)s,
                           %(content_type)s, %(logical_path)s, %(sensitivity)s, %(department)s,
                           %(owner_id)s, %(tags)s, %(domain_meta)s,
                           %(source)s, %(created_by)s, %(updated_by)s,
                           %(import_batch_id)s::uuid
                       )
                       RETURNING id""",
                    {
                        "org_id": user.org_id,
                        "title": title,
                        "content": stripped_content,
                        "summary": summary,
                        "content_hash": content_hash,
                        "content_type": content_type,
                        "logical_path": logical_path,
                        "sensitivity": sensitivity,
                        "department": department,
                        "owner_id": user.id,
                        "tags": tags,
                        "domain_meta": json.dumps(domain_meta) if domain_meta else "{}",
                        "source": user.source,
                        "created_by": user.id,
                        "updated_by": user.id,
                        "import_batch_id": batch_id,
                    },
                )
                cur.row_factory = dict_row
                row = await cur.fetchone()
                entry_id = str(row["id"])
                created += 1

                # Defer link resolution until all inserts complete so
                # that targets created later in this batch are
                # resolvable. We hand the *stripped* content to
                # sync_entry_links so frontmatter-embedded links don't
                # count as entry_links rows.
                if wiki_targets:
                    pending_links.append((entry_id, stripped_content))

        except Exception as exc:
            errors.append(f"{file.filename}: {str(exc)}")

    # Resolve wiki-links via the shared helper (spec 0030). Runs after all
    # INSERTs so cross-file references within this batch resolve correctly.
    for source_id, content in pending_links:
        try:
            batch_linked, batch_unresolved = await sync_entry_links(
                conn,
                source_id,
                content,
                user.org_id,
                user.id,
                user.source,
                import_batch_id=batch_id,
            )
            linked += batch_linked
            all_unresolved.update(batch_unresolved)
        except Exception as exc:
            errors.append(f"link {source_id}: {str(exc)}")

    # Update batch with final counts
    await conn.execute(
        """UPDATE import_batches
           SET created_count = %(created)s,
               staged_count = %(staged)s,
               linked_count = %(linked)s,
               skipped_count = %(skipped)s,
               error_count = %(errors)s
           WHERE id = %(batch_id)s::uuid""",
        {
            "created": created,
            "staged": staged,
            "linked": linked,
            "skipped": skipped,
            "errors": len(errors),
            "batch_id": batch_id,
        },
    )

    return ImportExecuteResponse(
        created=created,
        staged=staged,
        linked=linked,
        errors=errors,
        type_mappings=type_mappings,
        unrecognized_types=sorted(unrecognized_types),
        batch_id=batch_id,
        collisions_resolved=collisions_resolved,
        unresolved_links=len(all_unresolved),
        # Truncate the sample to 20 — a 1000-target list over HTTP is noisy
        # and the per-entry INFO log keeps the full set available for ops.
        unresolved_links_sample=sorted(all_unresolved)[:20],
    )


@router.post("", response_model=ImportExecuteResponse, status_code=201)
async def import_files(
    body: ImportExecuteRequest,
    user: UserContext = Depends(get_current_user),
):
    """Bulk import markdown files into the knowledge base with batch tracking.

    For each file: extracts title, parses frontmatter, infers content_type,
    computes content_hash. After all entries are created, scans for
    [[wiki-links]] and creates entry_links.

    Creates an import_batches record and tags all created entities with the
    batch_id. Honors collision resolutions (skip/rename/merge).

    Governance routing:
    - admin/editor with interactive key: direct INSERT into entries
    - agent/commenter: INSERT into staging table

    Thin wrapper around ``_execute_import`` — the core pipeline is also
    reused by ``POST /import/vault-from-blob``.
    """
    async with get_db(user) as conn:
        return await _execute_import(
            conn,
            user,
            body.files,
            body.base_path,
            body.source_vault,
            body.collisions,
        )


# ---------------------------------------------------------------------------
# POST /import/vault-from-blob — Tar → server-parse bulk import
# ---------------------------------------------------------------------------


@router.post(
    "/vault-from-blob",
    response_model=ImportExecuteResponse,
    status_code=201,
)
async def import_vault_from_blob(
    body: VaultFromBlobRequest,
    user: UserContext = Depends(get_current_user),
):
    """Import a previously-uploaded vault tarball by ``blob_id``.

    Flow (wet-test-driven replacement for the deleted ``import_vault_content``
    MCP tool):

    1. Co-work Claude tars a vault locally and POSTs it to ``/attachments`` —
       that endpoint returns a ``blob_id`` scoped to the caller's org via RLS
       on the ``blobs`` table.
    2. This endpoint receives the ``blob_id``, RLS-probes the ``blobs`` table
       to confirm the caller can see it (cross-org blobs → 404, no existence
       leak), pulls the bytes through the configured storage backend, and
       streams the tarball member-by-member via ``vault_walker.iter_tarball_md``.
    3. The resulting ``[(rel_path, content)]`` stream is materialized into
       ``list[ImportFile]`` and handed to the shared ``_execute_import`` core
       pipeline under the same ``get_db(user)`` connection.

    Size caps (env-overridable, read at request time):

    * ``MAX_VAULT_TARBALL_BYTES`` — default 25MB. Enforced via the recorded
      ``blobs.size_bytes`` before any tar bytes are fetched. 413 on breach.
    * ``MAX_VAULT_UNCOMPRESSED_BYTES`` — default 200MB. Enforced mid-iteration
      by ``iter_tarball_md``. 413 on breach; the import transaction rolls
      back so no entries / staging rows are written.

    Defaults ``source_vault`` and ``base_path`` to ``"cowork-upload"`` when
    not provided — keeps batch listings consistent with the prior
    ``import_vault_content`` tool behavior.
    """
    max_tarball = _max_vault_tarball_bytes()
    max_uncompressed = _max_vault_uncompressed_bytes()

    # --- Step 1: RLS-scoped probe — does the caller's org own this blob? ---
    # Mirrors routes/attachments.py::get_attachment step 1. We run a probe
    # under the user's Postgres role so RLS on ``blobs`` hides cross-org
    # rows entirely (returns no row). 404 on any miss — never leak existence.
    async with get_db(user) as conn:
        cur = await conn.execute(
            "SELECT id FROM blobs WHERE id = %s LIMIT 1",
            (body.blob_id,),
        )
        probe = await cur.fetchone()

        if probe is None:
            raise HTTPException(status_code=404, detail="Blob not found")

        # --- Step 2: raw-pool lookup for storage pointer + size ---
        # Once authorization clears, grab storage_key + size_bytes via the
        # raw pool. Matches the two-stage pattern in get_attachment: RLS
        # gates access, the raw pool fetches the pointer without needing a
        # role that can SELECT all blobs.
        pool = get_pool()
        async with pool.connection() as raw_conn:
            raw_cur = await raw_conn.execute(
                "SELECT storage_key, size_bytes, content_type FROM blobs WHERE id = %s",
                (body.blob_id,),
            )
            raw_cur.row_factory = dict_row
            blob_row = await raw_cur.fetchone()

        if blob_row is None:
            # Shouldn't happen — the RLS probe just confirmed the row — but
            # guard against FK gaps / races anyway.
            raise HTTPException(status_code=404, detail="Blob not found")

        size_bytes = int(blob_row["size_bytes"])
        if size_bytes > max_tarball:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Vault tarball exceeds MAX_VAULT_TARBALL_BYTES "
                    f"({max_tarball} bytes)"
                ),
            )

        # --- Step 3: pull bytes from storage ---
        storage = get_storage()
        try:
            data = await storage.get(blob_row["storage_key"])
        except FileNotFoundError:
            # Pointer row exists but the backend lost the bytes — treat as
            # a 404 for the caller; operator-visible error lives in logs.
            logger.warning(
                "Blob pointer row %s exists but storage bytes missing at key %s",
                body.blob_id,
                blob_row["storage_key"],
            )
            raise HTTPException(status_code=404, detail="Blob data unavailable")

        # --- Step 4: iterate tarball, collect ImportFile list ---
        # Excludes merge user-provided globs with the always-on DEFAULT_EXCLUDES
        # (.obsidian, .trash). The walker raises ValueError if cumulative
        # uncompressed bytes cross the cap — translate that to a 413.
        excludes = resolve_exclude_patterns(body.excludes)
        files_data: list[ImportFile] = []
        try:
            for rel_path, content in iter_archive_md(
                data, excludes, max_uncompressed
            ):
                files_data.append(
                    ImportFile(filename=rel_path, content=content)
                )
        except ValueError as exc:
            # Zip-bomb guard tripped. The transaction has not yet started
            # writing entries/staging rows, so rolling out of the
            # ``async with get_db(user)`` block leaves the DB untouched.
            raise HTTPException(status_code=413, detail=str(exc))
        except (tarfile.TarError, zipfile.BadZipFile) as exc:  # pragma: no cover - defensive
            raise HTTPException(
                status_code=400,
                detail=f"Invalid archive: {exc}",
            )

        # --- Step 5: defaults + dispatch to shared core pipeline ---
        source_vault = body.source_vault or "cowork-upload"
        base_path = body.base_path if body.base_path is not None else "cowork-upload"

        return await _execute_import(
            conn,
            user,
            files_data,
            base_path,
            source_vault,
            [],  # no caller-supplied collision resolutions on the blob path
        )


# ---------------------------------------------------------------------------
# POST /import/vault-upload — Browser multipart tar → blob → import
# ---------------------------------------------------------------------------


@router.post(
    "/vault-upload",
    response_model=ImportExecuteResponse,
    status_code=201,
)
async def import_vault_upload(
    file: UploadFile = File(...),
    source_vault: str | None = Form(None),
    base_path: str | None = Form(None),
    excludes: str | None = Form(
        None,
        description="Comma-separated list of glob patterns to exclude (merged with DEFAULT_EXCLUDES).",
    ),
    user: UserContext = Depends(get_current_user),
):
    """Browser-driven bulk vault import (Sprint 0040b).

    Accepts a multipart tarball uploaded directly from the user's browser,
    writes the bytes to the blob store via the same ``services.storage``
    helper ``POST /attachments`` uses, records a ``blobs`` row, then hands
    the tarball contents to the shared ``_execute_import`` pipeline.

    This is the MCP-bypass path: it avoids Claude's per-turn output cap,
    Co-work's restricted outbound allowlist, and the
    ``upload_attachment(path=...)`` filesystem gap on remote MCP.

    Size caps (env-overridable, read at request time):

    * ``MAX_VAULT_TARBALL_BYTES`` — default 25MB. Enforced **before** the
      bytes are written to storage so oversize uploads do not leak a
      ``blobs`` row or an ``import_batches`` row.
    * ``MAX_VAULT_UNCOMPRESSED_BYTES`` — default 200MB. Enforced
      mid-iteration by ``iter_tarball_md``; the import transaction rolls
      back on breach.

    Auth is required (``Depends(get_current_user)``); anonymous callers
    receive 401 via the standard dependency.

    On success returns 201 JSON with the ``ImportExecuteResponse`` shape
    plus ``blob_id`` pointing at the persisted tarball (useful for
    rollback / reprocessing).
    """
    max_tarball = _max_vault_tarball_bytes()
    max_uncompressed = _max_vault_uncompressed_bytes()

    # --- Step 1: stream bytes into memory, hashing + size-capping as we go ---
    # Mirrors routes/attachments.py::upload_attachment. ``UploadFile.size``
    # isn't populated until the full body is drained so enforcement has to
    # happen inline on the running byte counter. Rejecting **before** any
    # blob write keeps the 413 path clean of blob + batch row leaks.
    _READ_CHUNK = 65536
    hasher = hashlib.sha256()
    buf = io.BytesIO()
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_tarball:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Vault tarball exceeds MAX_VAULT_TARBALL_BYTES "
                    f"({max_tarball} bytes)"
                ),
            )
        hasher.update(chunk)
        buf.write(chunk)

    sha256_hex = hasher.hexdigest()
    data = buf.getvalue()

    # Archive content type — browsers typically send application/zip,
    # application/gzip, or application/x-tar; fall back to octet-stream so
    # the blobs row always has a non-null value regardless of format.
    effective_ct = file.content_type or "application/octet-stream"

    # --- Step 2: parse excludes (multipart Form field is a single string) ---
    # Accept comma-separated globs for convenience; None / empty strings
    # yield just the DEFAULT_EXCLUDES set.
    excludes_list: list[str] | None = None
    if excludes:
        excludes_list = [p.strip() for p in excludes.split(",") if p.strip()] or None

    # --- Step 3: persist bytes + run import under the caller's RLS context ---
    # We put the bytes to storage first (outside the DB txn) because the
    # storage backend is not transactional. If the subsequent ``blobs``
    # INSERT fails we may leak orphan bytes — matches the existing
    # /attachments semantics (acceptable for v1; sweep job reconciles).
    async with get_db(user) as conn:
        # Dedup probe — same bytes already uploaded by this org?
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
        else:
            storage = get_storage()
            storage_key = await storage.put(
                user.org_id, sha256_hex, effective_ct, data
            )
            storage_backend = (
                os.environ.get("STORAGE_BACKEND") or "local"
            ).strip().lower()

            cur = await conn.execute(
                """
                INSERT INTO blobs (
                    org_id, sha256, content_type, size_bytes,
                    storage_backend, storage_key, uploaded_by
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (org_id, sha256) DO NOTHING
                RETURNING id
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
                # Lost a race with a concurrent upload — re-read the row.
                cur = await conn.execute(
                    """
                    SELECT id FROM blobs WHERE org_id = %s AND sha256 = %s
                    """,
                    (user.org_id, sha256_hex),
                )
                cur.row_factory = dict_row
                inserted = await cur.fetchone()
                if inserted is None:
                    raise HTTPException(
                        status_code=500,
                        detail="Blob persisted but row not visible; retry.",
                    )
            blob_id = str(inserted["id"])

        # --- Step 4: iterate tarball and collect ImportFile list ---
        # Reuses the same walker + exclude logic as /vault-from-blob so
        # there's a single parsing code path. ValueError from the walker =
        # zip-bomb guard tripped; translate to 413 before any import_batches
        # row is created.
        default_excludes = resolve_exclude_patterns(excludes_list)
        files_data: list[ImportFile] = []
        try:
            for rel_path, content in iter_archive_md(
                data, default_excludes, max_uncompressed
            ):
                files_data.append(
                    ImportFile(filename=rel_path, content=content)
                )
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc))
        except (tarfile.TarError, zipfile.BadZipFile) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid archive: {exc}",
            )

        # --- Step 5: defaults + dispatch to shared core pipeline ---
        source_vault_effective = source_vault or "browser-upload"
        base_path_effective = (
            base_path if base_path is not None else "browser-upload"
        )

        result = await _execute_import(
            conn,
            user,
            files_data,
            base_path_effective,
            source_vault_effective,
            [],  # no caller-supplied collision resolutions on the upload path
        )

    # Echo the blob_id on the response so the UI can show it alongside the
    # batch_id (handy for rollback + reprocessing).
    result.blob_id = blob_id
    return result


# ---------------------------------------------------------------------------
# GET /import/vault — Browser-facing upload page (inline HTML/CSS/JS)
# ---------------------------------------------------------------------------


_VAULT_PAGE_STYLE = """
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 640px; margin: 48px auto; padding: 0 16px; color: #111;
         line-height: 1.5; }
  h1 { font-size: 1.6rem; margin-bottom: 0.25rem; }
  p.sub { color: #555; margin-top: 0; }
  form { display: flex; flex-direction: column; gap: 14px; margin-top: 24px; }
  label { font-size: 0.9rem; color: #333; display: block; margin-bottom: 4px; }
  input[type=file], input[type=text], input[type=password] {
    padding: 10px 12px; font-size: 1rem; border: 1px solid #ccc;
    border-radius: 6px; width: 100%; box-sizing: border-box;
  }
  input[type=checkbox] { margin-right: 6px; }
  button { padding: 10px 14px; font-size: 1rem; background: #111; color: #fff;
           border: 0; border-radius: 6px; cursor: pointer; }
  button:hover { background: #333; }
  button[disabled] { opacity: 0.5; cursor: not-allowed; }
  .field { margin: 0; }
  .hint { color: #666; font-size: 0.85rem; margin-top: 4px; }
  .panel { padding: 12px 14px; border-radius: 6px; margin-top: 20px;
           font-size: 0.95rem; white-space: pre-wrap; word-break: break-word; }
  .panel.ok { background: #e8f6ec; border: 1px solid #9fd6b1; color: #1a5a2d; }
  .panel.err { background: #fdecec; border: 1px solid #f5b5b5; color: #8a1f1f; }
  .panel.info { background: #f1f5ff; border: 1px solid #c5d3f2; color: #22346b; }
  code { background: #f4f4f4; border: 1px solid #ddd; padding: 2px 6px;
         border-radius: 4px; font-size: 0.9rem; word-break: break-all; }
  code.block { display: block; padding: 10px 12px; margin-top: 6px; }
  .hidden { display: none; }
  details { margin-top: 8px; }
  summary { cursor: pointer; color: #444; font-size: 0.9rem; }
  .spinner {
    display: inline-block;
    width: 14px; height: 14px;
    border: 2px solid #ddd;
    border-top-color: #111;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    vertical-align: middle;
    margin-right: 8px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
"""


def _render_vault_upload_page() -> str:
    """Render the ``GET /import/vault`` browser-upload page.

    Self-contained: zero external fetches (no CDN, no webfonts, no images).
    The page:

    1. On load, checks ``window.location.hash`` for an ``#api_key=<key>``
       fragment (handoff from the ``/setup`` Import Vault button, spec
       0043 / T-0255). If present, writes the key to
       ``localStorage.brilliant_api_key`` and clears the fragment via
       ``history.replaceState`` so the secret does not survive into the
       visible URL / back-nav / copy-paste surface.
    2. Then checks ``localStorage.brilliant_api_key``. If present the
       Bearer token is attached automatically to the POST; if absent, a
       password-style input renders with an optional "save in this browser"
       checkbox that writes the pasted key back to the same localStorage
       key so the next visit auto-populates.
    3. On submit, POSTs multipart to ``/import/vault-upload`` with the
       tarball file and optional ``source_vault`` / ``base_path`` /
       ``excludes`` fields.
    4. On success, renders ``{created, staged, batch_id}`` inline plus a
       rollback hint pointing at the MCP ``rollback_import`` tool.
    5. On HTTP error, renders the server's JSON ``detail`` verbatim — no
       generic fallback message.
    """
    # All JS lives in a single <script> block. Curly-braces intended for
    # JavaScript must be doubled (``{{`` / ``}}``) because the outer string
    # is an f-string. No f-string substitutions happen inside the script.
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Import vault — Brilliant</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_VAULT_PAGE_STYLE}</style>
</head>
<body>
  <h1>Import an Obsidian vault</h1>
  <p class="sub">
    Upload a <code>.zip</code> or tarball (<code>.tgz</code> /
    <code>.tar.gz</code>) of your vault. The file is parsed server-side
    and imported into your knowledge base in a single batch.
  </p>

  <div id="auth-missing" class="panel info hidden">
    <strong>API key required.</strong> Paste the admin API key you received
    during setup. Get a new one by signing in at
    <code>/auth/login</code> if you've lost it.
  </div>

  <form id="upload-form">
    <div id="key-field" class="field hidden">
      <label for="api_key">API key</label>
      <input id="api_key" name="api_key" type="password"
             placeholder="brilliant_..." autocomplete="off" required>
      <div>
        <label style="display:inline-block; font-weight:normal;">
          <input id="remember" type="checkbox" checked>
          Save in this browser
        </label>
      </div>
    </div>

    <div class="field">
      <label for="file">Vault archive</label>
      <input id="file" name="file" type="file"
             accept=".zip,.tgz,.tar.gz,.tar,application/zip,application/gzip,application/x-tar"
             required>
      <div class="hint">
        Easiest: right-click your vault folder &rarr; <em>Compress</em> (macOS) or
        <em>Send to &rarr; Compressed (zipped) folder</em> (Windows).
        Tarball alternative: <code>tar czf vault.tgz -C path/to/vault .</code>
      </div>
    </div>

    <details>
      <summary>Advanced options</summary>
      <div class="field" style="margin-top: 10px;">
        <label for="source_vault">source_vault (optional)</label>
        <input id="source_vault" name="source_vault" type="text"
               placeholder="browser-upload">
      </div>
      <div class="field" style="margin-top: 10px;">
        <label for="base_path">base_path (optional)</label>
        <input id="base_path" name="base_path" type="text"
               placeholder="browser-upload">
      </div>
      <div class="field" style="margin-top: 10px;">
        <label for="excludes">excludes — comma-separated globs (optional)</label>
        <input id="excludes" name="excludes" type="text"
               placeholder="e.g. Attachments/*,Templates/*">
      </div>
    </details>

    <div>
      <button id="submit-btn" type="submit">Upload and import</button>
    </div>
  </form>

  <div id="result" class="hidden"></div>

<script>
  (function () {{
    var STORAGE_KEY = "brilliant_api_key";
    var form = document.getElementById("upload-form");
    var keyField = document.getElementById("key-field");
    var authMissing = document.getElementById("auth-missing");
    var apiKeyInput = document.getElementById("api_key");
    var rememberInput = document.getElementById("remember");
    var submitBtn = document.getElementById("submit-btn");
    var resultPanel = document.getElementById("result");

    // ----- URL-fragment handoff from /setup (spec 0043 / T-0255) -----
    // The /setup credentials page renders an Import Obsidian vault
    // button whose href carries the freshly-minted admin API key as a
    // URL fragment (#api_key=...). Fragments never leave the browser,
    // so the secret doesn't ride the wire to the server. We read it
    // here, stuff it into localStorage so the form's fetch() can
    // attach the Authorization header, populate the input field for
    // operator visibility, and then replace history state so the
    // fragment is scrubbed from the address bar / back-nav / copy-URL.
    try {{
      var rawHash = window.location.hash || "";
      if (rawHash.length > 1) {{
        var inner = rawHash.charAt(0) === "#" ? rawHash.slice(1) : rawHash;
        var params = new URLSearchParams(inner);
        var frag = params.get("api_key");
        if (frag && frag.length > 0) {{
          try {{
            window.localStorage.setItem(STORAGE_KEY, frag);
          }} catch (e) {{ /* localStorage disabled — continue with in-memory flow */ }}
          if (apiKeyInput) {{
            apiKeyInput.value = frag;
          }}
          // Strip the fragment from the visible URL. ``replaceState``
          // does NOT trigger a navigation so the page state / form
          // values are preserved.
          if (window.history && typeof window.history.replaceState === "function") {{
            window.history.replaceState(null, "", window.location.pathname + window.location.search);
          }}
        }}
      }}
    }} catch (e) {{
      // URLSearchParams or history API unavailable — fall through to
      // the normal localStorage / paste-your-key path.
    }}

    var storedKey = null;
    try {{
      storedKey = window.localStorage.getItem(STORAGE_KEY);
    }} catch (e) {{
      storedKey = null;
    }}

    if (storedKey && storedKey.length > 0) {{
      keyField.classList.add("hidden");
      authMissing.classList.add("hidden");
      apiKeyInput.required = false;
    }} else {{
      keyField.classList.remove("hidden");
      authMissing.classList.remove("hidden");
      apiKeyInput.required = true;
    }}

    function renderPanel(kind, html) {{
      resultPanel.className = "panel " + kind;
      resultPanel.innerHTML = html;
      resultPanel.classList.remove("hidden");
    }}

    function escapeHtml(s) {{
      if (s === null || s === undefined) return "";
      return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }}

    form.addEventListener("submit", function (ev) {{
      ev.preventDefault();
      resultPanel.classList.add("hidden");
      resultPanel.innerHTML = "";

      var key = storedKey;
      if (!key || key.length === 0) {{
        key = apiKeyInput.value.trim();
        if (!key) {{
          renderPanel("err", "API key is required.");
          return;
        }}
        if (rememberInput.checked) {{
          try {{
            window.localStorage.setItem(STORAGE_KEY, key);
            storedKey = key;
          }} catch (e) {{
            // localStorage may be disabled; continue with in-memory key.
          }}
        }}
      }}

      var fileInput = document.getElementById("file");
      if (!fileInput.files || fileInput.files.length === 0) {{
        renderPanel("err", "Please choose a tarball file.");
        return;
      }}

      var fd = new FormData();
      fd.append("file", fileInput.files[0]);
      var sv = document.getElementById("source_vault").value.trim();
      var bp = document.getElementById("base_path").value.trim();
      var ex = document.getElementById("excludes").value.trim();
      if (sv) fd.append("source_vault", sv);
      if (bp) fd.append("base_path", bp);
      if (ex) fd.append("excludes", ex);

      submitBtn.disabled = true;
      submitBtn.innerHTML = '<span class="spinner"></span>Uploading...';
      renderPanel("info", '<span class="spinner"></span>Uploading and importing... this can take a moment for large vaults.');

      fetch("/import/vault-upload", {{
        method: "POST",
        headers: {{ "Authorization": "Bearer " + key }},
        body: fd
      }})
        .then(function (resp) {{
          return resp.text().then(function (text) {{
            var payload = null;
            try {{
              payload = text ? JSON.parse(text) : null;
            }} catch (e) {{
              payload = null;
            }}
            return {{ status: resp.status, ok: resp.ok, payload: payload, text: text }};
          }});
        }})
        .then(function (res) {{
          submitBtn.disabled = false;
          submitBtn.textContent = "Upload and import";
          if (res.ok && res.payload) {{
            var p = res.payload;
            var html =
              "<strong>Import complete.</strong><br>" +
              "Created: <code>" + escapeHtml(p.created) + "</code>  " +
              "Staged: <code>" + escapeHtml(p.staged) + "</code>  " +
              "Linked: <code>" + escapeHtml(p.linked) + "</code><br>" +
              "batch_id: <code class=\\"block\\">" + escapeHtml(p.batch_id) + "</code>";
            if (p.blob_id) {{
              html += "blob_id: <code class=\\"block\\">" + escapeHtml(p.blob_id) + "</code>";
            }}
            if (p.errors && p.errors.length > 0) {{
              html += "<br><strong>Errors:</strong><br>";
              for (var i = 0; i < p.errors.length; i++) {{
                html += "<code class=\\"block\\">" + escapeHtml(p.errors[i]) + "</code>";
              }}
            }}
            html +=
              "<br>To undo this import, run the MCP tool " +
              "<code>rollback_import(batch_id=\\"" + escapeHtml(p.batch_id) +
              "\\")</code> from Claude, or " +
              "<code>DELETE /import/" + escapeHtml(p.batch_id) + "</code>.";
            renderPanel("ok", html);
          }} else {{
            var detail = null;
            if (res.payload && res.payload.detail !== undefined) {{
              if (typeof res.payload.detail === "string") {{
                detail = res.payload.detail;
              }} else {{
                try {{
                  detail = JSON.stringify(res.payload.detail);
                }} catch (e) {{
                  detail = String(res.payload.detail);
                }}
              }}
            }} else if (res.text) {{
              detail = res.text;
            }} else {{
              detail = "HTTP " + res.status;
            }}
            var msg =
              "<strong>Upload failed (HTTP " + res.status + ").</strong><br>" +
              escapeHtml(detail);
            if (res.status === 401) {{
              msg +=
                "<br><br>Your saved API key may be invalid. " +
                "<button type=\\"button\\" id=\\"clear-key\\">Clear saved key</button>";
            }}
            renderPanel("err", msg);
            var clearBtn = document.getElementById("clear-key");
            if (clearBtn) {{
              clearBtn.addEventListener("click", function () {{
                try {{ window.localStorage.removeItem(STORAGE_KEY); }} catch (e) {{}}
                window.location.reload();
              }});
            }}
          }}
        }})
        .catch(function (err) {{
          submitBtn.disabled = false;
          submitBtn.textContent = "Upload and import";
          renderPanel("err",
            "<strong>Network error.</strong><br>" + escapeHtml(err && err.message ? err.message : String(err)));
        }});
    }});
  }})();
</script>
</body>
</html>
"""


@router.get("/vault", response_class=HTMLResponse)
async def vault_upload_page() -> HTMLResponse:
    """Render the browser-facing vault-upload page (Sprint 0040b).

    Returns an inline self-contained HTML page — zero external fetches,
    no CDN, no template engine. Mirrors the ``/setup`` rendering pattern
    (see ``api/routes/setup.py``).

    The page POSTs multipart to ``/import/vault-upload`` with a Bearer
    token sourced from ``localStorage.brilliant_api_key`` (set by the
    login page after ``/auth/login`` success). When the key is missing
    the page renders a paste-your-key fallback with an opt-in "save in
    this browser" checkbox.

    This route is always available post-migration — it is NOT gated by
    the first-run latch. The upload endpoint itself
    (``POST /import/vault-upload``) requires authentication via
    ``Depends(get_current_user)``; this GET surface is intentionally
    unauthenticated so the page can render the paste-your-key fallback.
    """
    return HTMLResponse(_render_vault_upload_page())


# ---------------------------------------------------------------------------
# DELETE /import/{batch_id} — Rollback an entire import batch
# ---------------------------------------------------------------------------


@router.delete("/{batch_id}", response_model=RollbackResponse)
async def rollback_import(
    batch_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Rollback an import batch: archive entries, remove links, clean pending staging."""
    async with get_db(user) as conn:
        # Verify batch exists and belongs to user's org
        cur = await conn.execute(
            """SELECT id, org_id, status FROM import_batches
               WHERE id = %(batch_id)s::uuid AND org_id = %(org_id)s""",
            {"batch_id": batch_id, "org_id": user.org_id},
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Import batch not found")

        batch_status = row[2]
        if batch_status == "rolled_back":
            raise HTTPException(
                status_code=409, detail="Batch has already been rolled back"
            )

        # Archive entries (soft-delete)
        cur = await conn.execute(
            """UPDATE entries SET status = 'archived'
               WHERE import_batch_id = %(batch_id)s::uuid
                 AND org_id = %(org_id)s
                 AND status != 'archived'""",
            {"batch_id": batch_id, "org_id": user.org_id},
        )
        entries_archived = cur.rowcount

        # Remove entry_links
        cur = await conn.execute(
            """DELETE FROM entry_links
               WHERE import_batch_id = %(batch_id)s::uuid
                 AND org_id = %(org_id)s""",
            {"batch_id": batch_id, "org_id": user.org_id},
        )
        links_removed = cur.rowcount

        # Remove pending staging items
        cur = await conn.execute(
            """DELETE FROM staging
               WHERE import_batch_id = %(batch_id)s::uuid
                 AND org_id = %(org_id)s
                 AND status = 'pending'""",
            {"batch_id": batch_id, "org_id": user.org_id},
        )
        staging_removed = cur.rowcount

        # Update batch status
        await conn.execute(
            """UPDATE import_batches
               SET status = 'rolled_back',
                   rolled_back_at = now(),
                   rolled_back_by = %(user_id)s
               WHERE id = %(batch_id)s::uuid""",
            {"batch_id": batch_id, "user_id": user.id},
        )

        # Audit log — wrapped in SAVEPOINT + kb_admin elevation via the
        # shared helper so an audit failure can't poison the outer rollback
        # transaction (which previously silently ROLLBACK'd the archive).
        await audit.record(
            conn,
            actor_id=user.id,
            actor_role=user.role,
            source=user.source,
            org_id=user.org_id,
            action="import_rollback",
            target_table="import_batches",
            target_id=batch_id,
            metadata={
                "entries_archived": entries_archived,
                "links_removed": links_removed,
                "staging_removed": staging_removed,
            },
        )

    return RollbackResponse(
        batch_id=batch_id,
        entries_archived=entries_archived,
        links_removed=links_removed,
        staging_removed=staging_removed,
    )


# ---------------------------------------------------------------------------
# GET /import/batches — List import batches for the org
# ---------------------------------------------------------------------------


@router.get("/batches", response_model=list[ImportBatchResponse])
async def list_import_batches(
    status: str | None = Query(None, description="Filter by status: active or rolled_back"),
    user: UserContext = Depends(get_current_user),
):
    """List all import batches for the user's org, optionally filtered by status."""
    async with get_db(user) as conn:
        if status:
            cur = await conn.execute(
                """SELECT id, org_id, source_vault, base_path, status,
                          file_count, created_count, staged_count, linked_count,
                          skipped_count, error_count, created_by, created_at,
                          rolled_back_at, rolled_back_by
                   FROM import_batches
                   WHERE org_id = %(org_id)s AND status = %(status)s
                   ORDER BY created_at DESC""",
                {"org_id": user.org_id, "status": status},
            )
        else:
            cur = await conn.execute(
                """SELECT id, org_id, source_vault, base_path, status,
                          file_count, created_count, staged_count, linked_count,
                          skipped_count, error_count, created_by, created_at,
                          rolled_back_at, rolled_back_by
                   FROM import_batches
                   WHERE org_id = %(org_id)s
                   ORDER BY created_at DESC""",
                {"org_id": user.org_id},
            )
        cur.row_factory = dict_row
        rows = await cur.fetchall()

    return [
        ImportBatchResponse(
            id=str(r["id"]),
            org_id=r["org_id"],
            source_vault=r["source_vault"],
            base_path=r["base_path"],
            status=r["status"],
            file_count=r["file_count"],
            created_count=r["created_count"],
            staged_count=r["staged_count"],
            linked_count=r["linked_count"],
            skipped_count=r["skipped_count"],
            error_count=r["error_count"],
            created_by=r["created_by"],
            created_at=r["created_at"],
            rolled_back_at=r.get("rolled_back_at"),
            rolled_back_by=r.get("rolled_back_by"),
        )
        for r in rows
    ]
