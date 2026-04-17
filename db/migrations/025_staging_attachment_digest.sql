-- 025_staging_attachment_digest.sql
-- Expand staging.submission_category CHECK constraint to include
-- 'attachment_digest' so PDF digest uploads (spec 0034b, T-0183) can
-- route through the normal Tier 1/2 staging flow.
--
-- Depends on: 003_governance.sql
-- Idempotent: safe to re-run.

ALTER TABLE staging DROP CONSTRAINT IF EXISTS staging_submission_category_check;
ALTER TABLE staging ADD CONSTRAINT staging_submission_category_check
    CHECK (submission_category IN (
        'teaching_loop', 'auto_save', 'compress', 'preserve',
        'meeting_intel', 'project_intel', 'user_direct',
        'attachment_digest'
    ));
