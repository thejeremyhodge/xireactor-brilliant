-- 024_staging_proposed_content_nullable.sql
-- Allow `staging.proposed_content` to be NULL for metadata-only updates.
--
-- Issue: #12 — submit_staging 500s on proposed_meta-only updates.
-- Root cause: db/migrations/003_governance.sql:26 declares
--   `proposed_content TEXT NOT NULL`, but the Pydantic model
--   (api/models.py:219) and submit_staging handler intentionally
--   accept `None` for metadata-only updates (e.g. tag-only edits).
--   The unhandled NotNullViolation surfaces as HTTP 500.
--
-- Fix: drop the NOT NULL on proposed_content. The promote path
-- (api/routes/staging.py::_promote_staging_item, line 218)
-- already coalesces `staging["proposed_content"] or current["content"]`,
-- so promotion is unaffected.
--
-- Idempotent: `ALTER COLUMN ... DROP NOT NULL` is a no-op when the
-- constraint is already absent in PostgreSQL.
--
-- We also drop NOT NULL on `content_hash` for the same reason — when
-- `proposed_content` is NULL there is nothing to hash, and storing
-- sha256("") for every meta-only submission would falsely collide in
-- the Tier 2 duplicate-content check.

ALTER TABLE staging
    ALTER COLUMN proposed_content DROP NOT NULL;

ALTER TABLE staging
    ALTER COLUMN content_hash DROP NOT NULL;
