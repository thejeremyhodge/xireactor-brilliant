"""Tag-triangulation motif registry — counts entries matching multi-tag patterns.

A "motif" is a named tag pattern that combines literal tags (exact match) with
wildcard prefixes (e.g. ``project:*``). Counting motifs at session start gives
agents and dashboards a quick read on cross-cutting work shapes — "how many
project tasks are blocked right now?" — without scanning the corpus.

## Structure

Each motif is a dict with three fields:

- ``name`` (str): Human-readable label surfaced in the manifest.
- ``required_tags`` (list[str]): Literal tags that MUST all be present on
  the entry. Matched via PostgreSQL array containment (``tags @> ARRAY[...]``),
  which uses the GIN index on ``entries.tags`` for cheap lookups.
- ``wildcards`` (list[str]): Tag prefixes (each ending in ``:``) where the
  entry must carry at least one tag starting with that prefix. Matched via
  ``EXISTS (SELECT 1 FROM unnest(tags) t WHERE t LIKE 'prefix:%')``.

Both lists may be empty; an empty motif (no required, no wildcard) would
match every published entry, which is rarely useful — keep at least one
constraint per motif.

## Adding a 4th motif

To add e.g. an "Open ADRs" motif (any entry tagged with an ``adr:*`` slug
plus the literal ``status:open``), append to ``MOTIFS``:

    MOTIFS.append({
        "name": "Open ADRs",
        "required_tags": ["status:open"],
        "wildcards": ["adr:"],
    })

The count function picks it up automatically — no other changes required.
Keep the registry small (<= 10 motifs); each motif is one DB roundtrip.
"""

from __future__ import annotations

MOTIFS: list[dict] = [
    {
        "name": "Project tasks completed",
        "required_tags": ["task", "task:completed"],
        "wildcards": ["project:"],
    },
    {
        "name": "Project tasks blocked",
        "required_tags": ["task", "task:blocked"],
        "wildcards": ["project:"],
    },
    {
        "name": "Project tasks in-progress",
        "required_tags": ["task", "task:in-progress"],
        "wildcards": ["project:"],
    },
]


async def count_motifs(conn) -> list[dict]:
    """Return ``[{"name": ..., "count": int}, ...]`` for every registered motif.

    One query per motif (acceptable for the <= 10 motif scale we expect).
    Filters to ``status = 'published'`` so the manifest reflects the agent-
    visible corpus. RLS is enforced by the caller's connection role.

    Empty corpus returns ``count: 0`` for each motif (no crash).
    """
    results: list[dict] = []

    for motif in MOTIFS:
        required = motif.get("required_tags") or []
        wildcards = motif.get("wildcards") or []

        clauses: list[str] = ["status = 'published'"]
        params: list = []

        if required:
            clauses.append("tags @> %s")
            params.append(list(required))

        for prefix in wildcards:
            clauses.append(
                "EXISTS (SELECT 1 FROM unnest(tags) t WHERE t LIKE %s)"
            )
            params.append(f"{prefix}%")

        sql = f"SELECT count(*) FROM entries WHERE {' AND '.join(clauses)}"
        cur = await conn.execute(sql, tuple(params))
        row = await cur.fetchone()
        count = (row[0] if row else 0) or 0

        results.append({"name": motif["name"], "count": count})

    return results
