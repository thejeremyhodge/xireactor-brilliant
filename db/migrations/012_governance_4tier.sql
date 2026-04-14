-- 012_governance_4tier.sql
-- Expand governance_tier CHECK constraint from 3 tiers to 4 tiers.
--
-- Four-tier governance model:
--   Tier 1: Auto-approve — creates, appends, links, tags on non-sensitive content.
--           No checks needed; committed synchronously at submission time.
--   Tier 2: Auto-approve with conflict detection — updates and modifications run
--           inline staleness/duplicate/conflict checks. Clean items auto-approve;
--           items with conflicts escalate to Tier 3.
--   Tier 3: AI/batch review — resolves conflicts, contradictions, and judgment calls.
--           Processed by batch endpoint (process_staging). Escalates to Tier 4 if
--           unresolvable.
--   Tier 4: Human-in-the-loop — deletions, sensitivity changes, governance rule
--           modifications, and AI-unresolvable items. Only resolvable via manual
--           approve/reject.
--
-- Idempotent: safe to run multiple times.
--
-- Depends on: 003_governance.sql

-- Drop the existing CHECK constraint on governance_tier (named by convention)
ALTER TABLE staging
    DROP CONSTRAINT IF EXISTS staging_governance_tier_check;

-- Add the expanded CHECK constraint allowing tiers 1-4
ALTER TABLE staging
    ADD CONSTRAINT staging_governance_tier_check
    CHECK (governance_tier IN (1, 2, 3, 4));
