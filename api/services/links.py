"""Shared helper for populating `entry_links` from cross-entry references in entry content.

Called from every write path (`create_entry`, `update_entry`, bulk
`import_files`) so that the read-time resolver (`services/render.py::resolve_wiki_links`,
spec 0028) has data to resolve. Before this existed, only the bulk importer
populated `entry_links` and MCP/UI-authored entries rendered `[[...]]` literally.

Two reference forms are extracted:

- ``[[wiki-link]]`` — Obsidian-style wiki links (preferred for cross-entry refs).
- ``[label](target)`` — standard markdown links, when ``target`` looks like an
  internal reference (no scheme, not an anchor, not an absolute path, not an
  image). External links (URLs / mailto / images / anchors) are skipped.

Match strategy mirrors migration 021 + the original inlined logic in
`import_files.py`: resolve by `LOWER(logical_path)` tail segment first (the
form wiki-links use — `[[person-gareth-prenderson]]`), then fall back to
full `LOWER(logical_path)` and `LOWER(title)`. Both reference forms use the
same resolver and dedup against each other so a single target referenced as
both ``[[foo]]`` and ``[Foo](foo)`` produces exactly one ``entry_links`` row.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_WIKI_LINK_RE = re.compile(r"\[\[([^\]|#]+)")
# Capture the character preceding the `[` so we can reject image syntax
# (`![alt](src)`). Group 1 is the leading char (or empty at string start),
# group 2 is the link label, group 3 is the target.
_MARKDOWN_LINK_RE = re.compile(r"(^|[^!])\[([^\]]+)\]\(([^)]+)\)")


def _is_internal_md_target(target: str) -> bool:
    """Return True if a markdown link target looks like an internal entry ref.

    Skip URLs (anything containing `://` — http, https, mailto:, ftp, etc.),
    in-page anchors (starting with `#`), and absolute filesystem/URL paths
    (starting with `/`). Image syntax is filtered upstream by the regex
    refusing to match when the preceding char is `!`.
    """
    if not target:
        return False
    t = target.strip()
    if not t:
        return False
    if "://" in t:
        return False
    if t.startswith("#") or t.startswith("/"):
        return False
    return True


async def sync_entry_links(
    conn,
    entry_id,
    content: str,
    org_id: str,
    user_id: str,
    source: str,
    import_batch_id: str | None = None,
) -> int:
    """Delete existing entry_links for `entry_id` then re-insert from `content`.

    Returns the number of link rows written. Unresolved targets are skipped
    silently (the read-time resolver will pass them through as literal text).
    Idempotent: safe to call repeatedly with the same content.

    Operates on the caller's connection so it participates in the caller's
    transaction — partial failures roll back with the parent write.
    """
    entry_id_s = str(entry_id)

    # Always clear existing outgoing links so removal-via-edit works.
    await conn.execute(
        "DELETE FROM entry_links WHERE source_entry_id = %s",
        (entry_id_s,),
    )

    # Cheap sniff: bail only when neither reference form can possibly match.
    if not content or ("[[" not in content and "](" not in content):
        return 0

    wiki_targets = _WIKI_LINK_RE.findall(content)
    md_targets = [
        target
        for _prev, _label, target in _MARKDOWN_LINK_RE.findall(content)
        if _is_internal_md_target(target)
    ]
    if not wiki_targets and not md_targets:
        return 0

    # Dedup across both forms while preserving order. Wiki links scan first
    # (historical order) so a target referenced both ways resolves once.
    seen: set[str] = set()
    unique_targets: list[str] = []
    for t in (*wiki_targets, *md_targets):
        key = t.strip()
        if key and key.lower() not in seen:
            seen.add(key.lower())
            unique_targets.append(key)

    linked = 0
    unresolved: list[str] = []
    for target in unique_targets:
        cur = await conn.execute(
            """SELECT id FROM entries
               WHERE org_id = %(org_id)s
                 AND status = 'published'
                 AND (
                     LOWER(split_part(logical_path, '/', -1)) = LOWER(%(target)s)
                     OR LOWER(logical_path) = LOWER(%(target)s)
                     OR LOWER(title) = LOWER(%(target)s)
                 )
               LIMIT 1""",
            {"target": target, "org_id": org_id},
        )
        row = await cur.fetchone()
        if row is None:
            unresolved.append(target)
            continue
        target_id = str(row[0])
        if target_id == entry_id_s:
            continue

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
                "org_id": org_id,
                "source_entry_id": entry_id_s,
                "target_entry_id": target_id,
                "link_type": "relates_to",
                "weight": 1.0,
                "metadata": "{}",
                "created_by": user_id,
                "source": source,
                "import_batch_id": import_batch_id,
            },
        )
        linked += 1

    if unresolved:
        # Non-fatal: the read-time resolver will pass unresolved refs through
        # as literal text. Surface them at INFO so importers can see which
        # wikilinks point at notes that don't exist yet.
        logger.info(
            "sync_entry_links: %d unresolved link target(s) for entry %s: %s",
            len(unresolved),
            entry_id_s,
            ", ".join(unresolved[:10]),
        )

    return linked
