-- 020_strip_frontmatter_seed.sql
-- One-shot cleanup: strip leading YAML frontmatter from entries.content
-- for the 504 rows seeded via scripts/seed_demo/deploy.py prior to the
-- frontmatter-strip fix (T-0140).
--
-- Two frontmatter shapes appear in the seed corpus (both produced by the
-- Haiku generator at different times):
--
--   Shape A — bare:
--     ---\n
--     key: value\n
--     ---\n
--     <body>
--
--   Shape B — code-fence-wrapped (what the 504 staging rows actually look
--   like after T-0140 discovery):
--     ```yaml\n     (or just ```\n)
--     ---\n
--     key: value\n
--     ---\n
--     ```\n
--     <body>
--
-- This migration mirrors the Python `_FRONTMATTER_RE` in
-- scripts/seed_demo/deploy.py so both code paths clean identical content.
--
-- Frontmatter fields (title, content_type, slug, client_id, project_id,
-- tags) are already mapped to first-class columns and surfaced via the
-- properties panel, so the leading block renders as redundant backend
-- data in the detail view.
--
-- Regex notes:
--   - Postgres's default flavor supports `(?:...)?` groups and non-greedy
--     `.*?`. We use the default (no 'n' flag) so `.` matches newlines,
--     which is required to span the multi-line frontmatter body.
--   - The WHERE clause matches either shape so rows without frontmatter
--     (web_ui / agent writes) are untouched. Second run matches zero rows.
--
-- Pattern:
--   ^                        start of string
--   (?:```[^\n]*\n)?         optional opening code-fence line (```yaml, ```)
--   ---\n                    opening frontmatter delimiter
--   .*?\n                    frontmatter body (non-greedy)
--   ---                      closing frontmatter delimiter
--   (?:\n```)?               optional closing code-fence line
--   \n+                      one or more trailing newlines
--
-- Idempotent: second run matches zero rows.
--
-- Spec: 0028 — Content Rendering Cleanup
-- Task: T-0141

BEGIN;

UPDATE entries
SET
    content    = regexp_replace(
        content,
        '^(?:```[^\n]*\n)?---\n.*?\n---(?:\n```)?\n+',
        ''
    ),
    updated_at = now(),
    version    = version + 1
WHERE
    content ~ '^(?:```[^\n]*\n)?---\n';

-- Verification: expect 0 rows still carrying either frontmatter shape.
DO $$
DECLARE
    remaining int;
BEGIN
    SELECT COUNT(*) INTO remaining
    FROM entries
    WHERE content ~ '^(?:```[^\n]*\n)?---\n';
    IF remaining <> 0 THEN
        RAISE EXCEPTION
            'Migration 020: % entries still begin with frontmatter after strip',
            remaining;
    END IF;
    RAISE NOTICE
        'Migration 020: frontmatter strip complete (bare + code-fence-wrapped), 0 rows remain';
END$$;

COMMIT;
