-- 021_backfill_entry_links.sql
-- One-shot backfill: populate entry_links for the 504 Meridian seed rows.
--
-- ST-0115 root cause: api/routes/import_files.py resolved wiki-link targets
-- by LOWER(title), but the Meridian seed emits `title == logical_path`
-- (e.g. `person/person-gareth-prenderson`) while `[[...]]` slugs in content
-- are the bare tail (`person-gareth-prenderson`). Every link-creation during
-- /import silently no-op'd, leaving entry_links empty on staging and the
-- spec-0028 wiki-link resolver with nothing to resolve.
--
-- import_files.py is patched to fall back to logical_path + tail match; this
-- migration does the same rewrite against already-imported rows.
--
-- Resolution priority (any match wins):
--   1. lower(title)                                   -- exact title
--   2. lower(logical_path)                            -- "person/person-foo"
--   3. lower(split_part(logical_path, '/', -1))       -- "person-foo" (tail)
--
-- Idempotent: ON CONFLICT DO NOTHING on the (org_id, source, target, link_type)
-- unique index. Self-links (source_id = target_id) are filtered out.
--
-- Spec: 0028 — Content Rendering Cleanup (follow-up)
-- Task: slug-mismatch fix

BEGIN;

WITH extracted AS (
    SELECT
        e.id                                                   AS source_id,
        e.org_id                                               AS org_id,
        e.created_by                                           AS created_by,
        lower(trim(split_part(m[1], '|', 1)))                  AS target_slug
    FROM entries e,
         LATERAL regexp_matches(e.content, '\[\[([^\]]+)\]\]', 'g') AS m
    WHERE e.content LIKE '%[[%'
),
resolved AS (
    SELECT DISTINCT
        x.org_id,
        x.source_id,
        t.id            AS target_id,
        x.created_by
    FROM extracted x
    JOIN entries t
      ON t.org_id = x.org_id
     AND t.status = 'published'
     AND (
             lower(t.title) = x.target_slug
          OR lower(t.logical_path) = x.target_slug
          OR lower(split_part(t.logical_path, '/', -1)) = x.target_slug
         )
    WHERE t.id <> x.source_id
),
inserted AS (
    INSERT INTO entry_links (
        org_id, source_entry_id, target_entry_id, link_type, weight,
        metadata, created_by, source
    )
    SELECT
        org_id, source_id, target_id, 'relates_to', 1.0,
        '{}'::jsonb, created_by, 'api'
    FROM resolved
    ON CONFLICT (org_id, source_entry_id, target_entry_id, link_type)
    DO NOTHING
    RETURNING 1
)
SELECT COUNT(*) AS inserted_count FROM inserted;

DO $$
DECLARE
    link_count int;
BEGIN
    SELECT COUNT(*) INTO link_count FROM entry_links;
    RAISE NOTICE 'Migration 021: entry_links total after backfill = %', link_count;
END$$;

COMMIT;
