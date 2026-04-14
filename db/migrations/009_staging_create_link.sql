-- 009_staging_create_link.sql
-- Extend staging change_type to support 'create_link' for agent link creation via governance.

-- Drop and recreate the CHECK constraint to include 'create_link'
ALTER TABLE staging DROP CONSTRAINT IF EXISTS staging_change_type_check;
ALTER TABLE staging ADD CONSTRAINT staging_change_type_check
    CHECK (change_type IN ('create', 'update', 'append', 'delete', 'create_link'));
