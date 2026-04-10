"""Bulk markdown file import endpoint with preview, collision detection, and batch tracking."""

import hashlib
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg.rows import dict_row

from auth import UserContext, get_current_user
from database import get_db
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
)

router = APIRouter(tags=["import"])

# Regex to detect [[wiki-links]] in markdown content.
# Captures up to the first | (display text) or # (heading anchor) or ]],
# handling [[Note]], [[Note|display]], and [[Note#Heading]] formats.
_WIKI_LINK_RE = re.compile(r"\[\[([^\]|#]+)")

# Regex for inline #tags in content (but not ## headings)
_INLINE_TAG_RE = re.compile(r"(?:^|\s)#([a-zA-Z][\w-]*)", re.MULTILINE)

# Regex for daily note filenames like 2024-01-15.md
_DAILY_NOTE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")


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


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Extract YAML-like frontmatter and return (meta, remaining_content).

    Handles simple key: value pairs as well as YAML list syntax:
    - Inline lists: tags: [a, b, c]
    - Multi-line lists:
        tags:
          - a
          - b
          - c
    """
    if not content.startswith("---\n"):
        return {}, content
    end = content.find("\n---", 3)
    if end == -1:
        return {}, content
    meta: dict = {}
    lines = content[4:end].split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            # Check for inline list: [a, b, c]
            if val.startswith("[") and val.endswith("]"):
                inner = val[1:-1]
                meta[key] = [
                    item.strip().strip('"').strip("'")
                    for item in inner.split(",")
                    if item.strip()
                ]
            elif val == "":
                # Possibly a multi-line list; collect subsequent "  - item" lines
                items = []
                while i + 1 < len(lines):
                    next_line = lines[i + 1]
                    stripped = next_line.strip()
                    if stripped.startswith("- "):
                        items.append(stripped[2:].strip().strip('"').strip("'"))
                        i += 1
                    else:
                        break
                if items:
                    meta[key] = items
                else:
                    meta[key] = val
            else:
                meta[key] = val
        i += 1
    return meta, content[end + 4 :].lstrip("\n")


def extract_title(content: str, filename: str) -> str:
    """Extract title from the first # heading, or fall back to filename without .md."""
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            return stripped[2:].strip()
    # Fallback: filename without .md extension
    if filename.lower().endswith(".md"):
        return filename[:-3]
    return filename


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

    Returns dict with keys: filename, title, content, remaining_content, meta,
    tags, logical_path, content_hash, wiki_targets.
    """
    meta, remaining_content = parse_frontmatter(file.content)
    title = extract_title(remaining_content, file.filename)
    tags = extract_tags(meta, remaining_content)
    logical_path = _build_logical_path(file.filename, base_path)
    content_hash = hashlib.sha256(file.content.encode()).hexdigest()
    wiki_targets = _WIKI_LINK_RE.findall(file.content)
    return {
        "filename": file.filename,
        "title": title,
        "content": file.content,
        "remaining_content": remaining_content,
        "meta": meta,
        "tags": tags,
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


async def _resolve_content_type(conn, meta: dict, filename: str, logical_path: str):
    """Resolve content_type from frontmatter, registry, daily note pattern, or path inference.

    Returns (content_type, type_mapping_or_None, unrecognized_type_or_None).
    """
    raw_type = meta.get("type", "")
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
        else:
            return infer_content_type(logical_path), None, raw_type
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
                    conn, fd["meta"], fd["filename"], fd["logical_path"]
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
    """
    created = 0
    staged = 0
    linked = 0
    skipped = 0
    collisions_resolved = 0
    errors: list[str] = []
    type_mappings: dict[str, str] = {}
    unrecognized_types: set[str] = set()

    # Whether this user goes through staging
    use_staging = user.role in ("commenter", "viewer") or user.key_type == "agent"

    # Build collision lookup: filename -> CollisionEntry
    collision_lookup: dict[str, CollisionEntry] = {}
    for c in body.collisions:
        collision_lookup[c.filename] = c

    # Track created entries for wiki-link resolution: title_lower -> entry_id
    title_to_id: dict[str, str] = {}
    # Track which files have wiki-links: [(source_entry_id, [link_targets])]
    pending_links: list[tuple[str, list[str]]] = []

    async with get_db(user) as conn:
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
                "source_vault": body.source_vault,
                "base_path": body.base_path,
                "file_count": len(body.files),
                "created_by": user.id,
            },
        )
        batch_row = await cur.fetchone()
        batch_id = str(batch_row[0])

        for file in body.files:
            try:
                # Check collision resolution for this file
                collision = collision_lookup.get(file.filename)
                if collision:
                    collisions_resolved += 1
                    if collision.resolution == "skip":
                        skipped += 1
                        continue

                # Parse file
                fd = _parse_file(file, body.base_path)
                logical_path = fd["logical_path"]
                title = fd["title"]
                tags = fd["tags"]
                meta = fd["meta"]
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
                            "new_content": file.content,
                            "content_hash": content_hash,
                            "updated_by": user.id,
                            "entry_id": collision.existing_entry_id,
                            "org_id": user.org_id,
                        },
                    )
                    created += 1  # count as processed
                    continue

                # Resolve content_type
                content_type, mapping, unrec = await _resolve_content_type(
                    conn, meta, file.filename, logical_path
                )
                if mapping:
                    type_mappings[mapping[0]] = mapping[1]
                if unrec:
                    unrecognized_types.add(unrec)

                if use_staging:
                    # Build proposed_meta including tags
                    proposed_meta = dict(meta) if meta else {}
                    proposed_meta["tags"] = tags

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
                            "proposed_content": file.content,
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
                            "content": file.content,
                            "summary": None,
                            "content_hash": content_hash,
                            "content_type": content_type,
                            "logical_path": logical_path,
                            "sensitivity": "shared",
                            "department": user.department,
                            "owner_id": user.id,
                            "tags": tags,
                            "domain_meta": json.dumps(meta) if meta else "{}",
                            "source": user.source,
                            "created_by": user.id,
                            "updated_by": user.id,
                            "import_batch_id": batch_id,
                        },
                    )
                    cur.row_factory = dict_row
                    row = await cur.fetchone()
                    entry_id = str(row["id"])
                    title_to_id[title.lower()] = entry_id
                    created += 1

                    # Store wiki-link targets for later resolution
                    if wiki_targets:
                        pending_links.append((entry_id, wiki_targets))

            except Exception as exc:
                errors.append(f"{file.filename}: {str(exc)}")

        # Resolve wiki-links: create entry_links for targets that match
        # created entries OR existing DB entries
        for source_id, targets in pending_links:
            for target_name in targets:
                target_lower = target_name.strip().lower()
                target_id = title_to_id.get(target_lower)

                # If not found in import set, check existing DB entries
                if not target_id:
                    cur = await conn.execute(
                        """SELECT id FROM entries
                           WHERE LOWER(title) = LOWER(%(title)s)
                             AND org_id = %(org_id)s
                             AND status = 'published'
                           LIMIT 1""",
                        {"title": target_name.strip(), "org_id": user.org_id},
                    )
                    db_row = await cur.fetchone()
                    if db_row:
                        target_id = str(db_row[0])

                if target_id and target_id != source_id:
                    try:
                        await conn.execute(
                            """INSERT INTO entry_links (
                                   org_id, source_entry_id, target_entry_id,
                                   link_type, weight, metadata,
                                   created_by, source,
                                   import_batch_id
                               ) VALUES (
                                   %(org_id)s, %(source_entry_id)s, %(target_entry_id)s,
                                   %(link_type)s, %(weight)s, %(metadata)s,
                                   %(created_by)s, %(source)s,
                                   %(import_batch_id)s::uuid
                               )
                               ON CONFLICT (org_id, source_entry_id, target_entry_id, link_type)
                               DO NOTHING""",
                            {
                                "org_id": user.org_id,
                                "source_entry_id": source_id,
                                "target_entry_id": target_id,
                                "link_type": "relates_to",
                                "weight": 1.0,
                                "metadata": "{}",
                                "created_by": user.id,
                                "source": user.source,
                                "import_batch_id": batch_id,
                            },
                        )
                        linked += 1
                    except Exception as exc:
                        errors.append(f"link {source_id}->{target_name}: {str(exc)}")

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
    )


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

        # Audit log (table may not exist)
        try:
            await conn.execute(
                """INSERT INTO audit_log (
                       org_id, action, target_type, target_id,
                       details, performed_by, source
                   ) VALUES (
                       %(org_id)s, %(action)s, %(target_type)s, %(target_id)s,
                       %(details)s, %(performed_by)s, %(source)s
                   )""",
                {
                    "org_id": user.org_id,
                    "action": "import_rollback",
                    "target_type": "import_batch",
                    "target_id": batch_id,
                    "details": json.dumps(
                        {
                            "entries_archived": entries_archived,
                            "links_removed": links_removed,
                            "staging_removed": staging_removed,
                        }
                    ),
                    "performed_by": user.id,
                    "source": user.source,
                },
            )
        except Exception:
            pass  # audit_log table may not exist yet

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
