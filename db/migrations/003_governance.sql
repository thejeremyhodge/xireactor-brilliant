-- 003_governance.sql
-- Staging table (governance pipeline queue with three tiers) and audit log.
--
-- Depends on: 001_core.sql (organizations, users, entries)

-- =============================================================================
-- STAGING — Governance induction pipeline
-- Agent writes and flagged content land here before promotion to entries.
-- Three governance tiers:
--   Tier 1: No governance (direct write, auto-approved)
--   Tier 2: Deterministic rules (SQL checks, auto-resolve)
--   Tier 3: AI evaluation required (batch processed)
-- =============================================================================

CREATE TABLE staging (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id              TEXT NOT NULL REFERENCES organizations(id),

    -- What's being proposed
    target_entry_id     UUID REFERENCES entries(id),
    target_path         TEXT NOT NULL,
    change_type         TEXT NOT NULL CHECK (change_type IN (
                            'create', 'update', 'append', 'delete'
                        )),
    proposed_title      TEXT,
    proposed_content    TEXT NOT NULL,
    proposed_meta       JSONB,
    content_hash        TEXT NOT NULL,

    -- Who submitted
    submitted_by        TEXT NOT NULL REFERENCES users(id),
    source              TEXT NOT NULL CHECK (source IN ('web_ui', 'agent', 'api')),

    -- Governance tier assignment
    governance_tier     INTEGER NOT NULL DEFAULT 1 CHECK (governance_tier IN (1, 2, 3)),
    submission_category TEXT NOT NULL CHECK (submission_category IN (
                            'teaching_loop', 'auto_save', 'compress', 'preserve',
                            'meeting_intel', 'project_intel', 'user_direct'
                        )),

    -- Evaluator processing
    status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                            'pending', 'approved', 'rejected', 'deferred',
                            'superseded', 'merged'
                        )),
    priority            INTEGER NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    evaluator_decision  JSONB,
    evaluator_notes     TEXT,
    reviewed_at         TIMESTAMPTZ,
    reviewed_by         TEXT REFERENCES users(id),

    -- Timestamps
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Partial index for efficient pending-item queries
CREATE INDEX idx_staging_pending ON staging (org_id, priority, created_at) WHERE status = 'pending';
CREATE INDEX idx_staging_target ON staging (target_entry_id);
CREATE INDEX idx_staging_user ON staging (submitted_by);


-- =============================================================================
-- AUDIT LOG — All mutations logged
-- Append-only from the API server (system role), not directly writable by users.
-- =============================================================================

CREATE TABLE audit_log (
    id                  BIGSERIAL PRIMARY KEY,
    org_id              TEXT NOT NULL,
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Actor
    actor_id            TEXT NOT NULL,
    actor_role          TEXT NOT NULL,
    source              TEXT NOT NULL CHECK (source IN ('web_ui', 'agent', 'api', 'system')),

    -- Action
    action              TEXT NOT NULL,
    target_table        TEXT NOT NULL,
    target_id           TEXT,
    target_path         TEXT,

    -- Change details
    change_summary      TEXT,
    content_hash_before TEXT,
    content_hash_after  TEXT,

    -- Request context
    ip_address          INET,
    request_duration_ms INTEGER
);

CREATE INDEX idx_audit_org_time ON audit_log (org_id, timestamp DESC);
CREATE INDEX idx_audit_actor ON audit_log (actor_id, timestamp DESC);
CREATE INDEX idx_audit_target ON audit_log (target_id, timestamp DESC);
CREATE INDEX idx_audit_action ON audit_log (org_id, action, timestamp DESC);
