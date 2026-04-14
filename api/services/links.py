"""Shared helper for populating `entry_links` from wiki-link references in entry content.

Called from every write path (`create_entry`, `update_entry`, bulk
`import_files`) so that the read-time resolver (`services/render.py::resolve_wiki_links`,
spec 0028) has data to resolve. Before this existed, only the bulk importer
populated `entry_links` and MCP/UI-authored entries rendered `[[...]]` literally.

Match strategy mirrors migration 021 + the original inlined logic in
`import_files.py`: resolve by `LOWER(logical_path)` tail segment first (the
form wiki-links use — `[[person-gareth-prenderson]]`), then fall back to
full `LOWER(logical_path)` and `LOWER(title)`.
"""

from __future__ import annotations

import re

_WIKI_LINK_RE = re.compile(r"\[\[([^\]|#]+)")


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

    if not content or "[[" not in content:
        return 0

    targets = _WIKI_LINK_RE.findall(content)
    if not targets:
        return 0

    # Dedup while preserving order.
    seen: set[str] = set()
    unique_targets: list[str] = []
    for t in targets:
        key = t.strip()
        if key and key.lower() not in seen:
            seen.add(key.lower())
            unique_targets.append(key)

    linked = 0
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

    return linked
