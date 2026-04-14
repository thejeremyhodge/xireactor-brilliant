-- 016_drop_content_type_check.sql
-- Remove hard-coded CHECK constraint on entries.content_type.
-- The content_type_registry table (007) is now the sole authority.
-- Also adds 'session' as a canonical type.

-- Drop the legacy CHECK constraint
ALTER TABLE entries DROP CONSTRAINT IF EXISTS entries_content_type_check;

-- Add 'session' to the registry
INSERT INTO content_type_registry (name, description)
VALUES ('session', 'Session logs and activity records')
ON CONFLICT (name) DO NOTHING;
