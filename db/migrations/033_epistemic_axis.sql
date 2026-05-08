-- 033_epistemic_axis.sql
-- Epistemic axis schema additions for the 3-axis LOD architecture
-- (Sprint 0050, realizes ADR #69).
--
-- Adds four columns to `entries` plus three Postgres enums that capture
-- the *epistemic* status of every entry — what kind of statement it is,
-- how trustworthy the source was, whether it's been verified, and what
-- (if anything) it conflicts with. Together with the structural axis
-- (already shipped) and the heat axis (entry_access_log, migration 023)
-- these complete the three orthogonal axes the LOD service exposes.
--
-- Defaults
-- --------
-- Existing rows backfill via `NOT NULL DEFAULT`:
--   claim_type           = 'observation'
--   source_confidence    = 'reported'
--   verification_status  = 'pending'
--   conflict_with        = ARRAY[]::uuid[]
--
-- Existing entries are deliberately NOT auto-promoted to 'verified' —
-- promotion is reviewer-agent or human-driven (per ADR #69).
--
-- RLS
-- ---
-- No new policies. The four new columns inherit the existing entry-level
-- RLS on `entries`. Verified by tests in this sprint.
--
-- Index
-- -----
-- A single composite covering index supports the LOD0/LOD2 epistemic
-- histogram aggregate (`GROUP BY claim_type, verification_status`). Per
-- the no-precompute discipline established in Sprint 0049, the histogram
-- is computed on read; this index keeps it cheap.
--
-- Idempotent: CREATE TYPE / ADD COLUMN / CREATE INDEX guards make the
-- migration safe to re-apply.
--
-- Additive only: no DROPs, no type changes on existing columns.
--
-- Depends on: 001_core.sql (defines `entries`).

-- =============================================================================
-- Enums (claim_type_t, source_confidence_t, verification_status_t)
-- =============================================================================
-- `CREATE TYPE` has no `IF NOT EXISTS` — wrap each in a DO block so the
-- migration is idempotent across re-applies.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'claim_type_t') THEN
        CREATE TYPE claim_type_t AS ENUM (
            'event',
            'observation',
            'claim',
            'rule'
        );
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'source_confidence_t') THEN
        CREATE TYPE source_confidence_t AS ENUM (
            'verified',
            'reported',
            'inferred',
            'rumor'
        );
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'verification_status_t') THEN
        CREATE TYPE verification_status_t AS ENUM (
            'verified',
            'pending',
            'disputed',
            'superseded'
        );
    END IF;
END$$;

-- =============================================================================
-- Columns on entries
-- =============================================================================

ALTER TABLE entries
    ADD COLUMN IF NOT EXISTS claim_type claim_type_t
        NOT NULL DEFAULT 'observation';

ALTER TABLE entries
    ADD COLUMN IF NOT EXISTS source_confidence source_confidence_t
        NOT NULL DEFAULT 'reported';

ALTER TABLE entries
    ADD COLUMN IF NOT EXISTS verification_status verification_status_t
        NOT NULL DEFAULT 'pending';

ALTER TABLE entries
    ADD COLUMN IF NOT EXISTS conflict_with uuid[]
        NOT NULL DEFAULT ARRAY[]::uuid[];

-- =============================================================================
-- Composite index for the LOD0/LOD2 epistemic histogram
-- =============================================================================

CREATE INDEX IF NOT EXISTS entries_epistemic_histogram_idx
    ON entries (claim_type, verification_status);
