-- 008_staging_auto_approve.sql
-- Add 'auto_approved' status to staging table and a promoted_entry_id column
-- to link auto-approved staging rows to their resulting entries.
--
-- Depends on: 003_governance.sql (staging table), 001_core.sql (entries table)

-- 1. Expand the status CHECK constraint to include 'auto_approved'
ALTER TABLE staging DROP CONSTRAINT IF EXISTS staging_status_check;
ALTER TABLE staging ADD CONSTRAINT staging_status_check
    CHECK (status IN (
        'pending', 'approved', 'rejected', 'deferred',
        'superseded', 'merged', 'auto_approved'
    ));

-- 2. Add promoted_entry_id — links staging row to the entry it created/updated
ALTER TABLE staging ADD COLUMN IF NOT EXISTS promoted_entry_id UUID REFERENCES entries(id);

-- 3. Partial index for efficient promoted-entry lookups
CREATE INDEX IF NOT EXISTS idx_staging_promoted ON staging (promoted_entry_id)
    WHERE promoted_entry_id IS NOT NULL;
