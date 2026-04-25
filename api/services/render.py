"""Content rendering helpers.

Currently exposes `resolve_wiki_links`, which rewrites Obsidian-style
`[[slug]]` / `[[slug|alias]]` references in entry markdown into standard
markdown links using the `entry_links` table as the authoritative target
resolver. See spec 0028.
"""

import re

# Matches [[slug]] or [[slug|alias]], including the Obsidian table-cell
# escape form `[[slug\|alias]]` (a literal `\` is required inside table
# cells to avoid the cell delimiter eating the alias pipe). The slug
# stop class adds `\\` so `target-a\` doesn't bleed into the slug
# capture; the optional `\\?` after the slug consumes the escape so the
# alternation `(?:\|...)` can still match the alias pipe. Stop class
# also excludes `]` and `|` so nested brackets / pipes pass through.
_WIKI_LINK_RE = re.compile(r"\[\[([^\]|\\]+?)\\?(?:\|([^\]]+))?\]\]")


async def resolve_wiki_links(content: str, conn, source_entry_id: str) -> str:
    """Resolve `[[slug]]` / `[[slug|alias]]` references in `content`.

    Looks up the entry's outgoing links (`entry_links.source_entry_id = ?`),
    joins `entries` to obtain the target title and `logical_path`, and
    builds a map keyed by the tail segment of the logical path (the form
    used in seeded bodies). Matches whose slug resolves are rewritten to
    `[alias_or_title](/kb/<id>)`; unresolved matches are left as
    literal `[[...]]` text (graceful degradation per spec 0028).

    Short-circuits with no DB query when `content` contains no `[[`.

    The `conn` is the same RLS-scoped connection used by the caller, so
    this helper inherits the caller's permission context automatically.
    """
    if not content or "[[" not in content:
        return content

    cur = await conn.execute(
        """
        SELECT el.target_entry_id, e.title, e.logical_path
        FROM entry_links el
        JOIN entries e ON e.id = el.target_entry_id
        WHERE el.source_entry_id = %s
        """,
        (source_entry_id,),
    )
    rows = await cur.fetchall()

    # Build {tail_slug: (target_id, title)} map.
    slug_map: dict[str, tuple[str, str]] = {}
    for row in rows:
        target_id, title, logical_path = row[0], row[1], row[2]
        if not logical_path:
            continue
        tail = logical_path.rsplit("/", 1)[-1]
        slug_map[tail] = (str(target_id), title)

    if not slug_map:
        return content

    def _replace(match: re.Match) -> str:
        slug = match.group(1).strip()
        alias = match.group(2)
        hit = slug_map.get(slug)
        if hit is None:
            return match.group(0)  # leave literal
        target_id, title = hit
        # Strip leading "prefix/" from the title so labels read as
        # `client-redwood-textiles` instead of `client/client-redwood-textiles`.
        display_title = title.split("/", 1)[1] if title and "/" in title else title
        label = (alias.strip() if alias else display_title) or display_title
        return f"[{label}](/kb/{target_id})"

    return _WIKI_LINK_RE.sub(_replace, content)
