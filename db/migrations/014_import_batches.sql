-- 014_import_batches.sql
-- Import batches table for tracking Obsidian vault imports as rollback-able units.
-- Adds import_batch_id FK to entries, staging, and entry_links for batch traceability.
--
-- Depends on: 001_core.sql (organizations, users, entries),
--             002_relationships.sql (entry_links),
--             003_governance.sql (staging),
--             004_rls.sql (RLS patterns)

-- =============================================================================
-- IMPORT BATCHES — Track vault imports as rollback-able batches
-- =============================================================================

CREATE TABLE import_batches (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          TEXT NOT NULL REFERENCES organizations(id),
    source_vault    TEXT NOT NULL,                     -- vault name/path identifier
    base_path       TEXT NOT NULL,                     -- logical_path prefix used
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
                        'active', 'rolled_back'
                    )),
    file_count      INTEGER NOT NULL,                  -- total files submitted
    created_count   INTEGER NOT NULL DEFAULT 0,        -- entries directly created
    staged_count    INTEGER NOT NULL DEFAULT 0,        -- entries routed to staging
    linked_count    INTEGER NOT NULL DEFAULT 0,        -- wiki-links resolved
    skipped_count   INTEGER NOT NULL DEFAULT 0,        -- collision skips
    error_count     INTEGER NOT NULL DEFAULT 0,
    created_by      TEXT NOT NULL REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    rolled_back_at  TIMESTAMPTZ,
    rolled_back_by  TEXT REFERENCES users(id)
);

CREATE INDEX idx_import_batches_org ON import_batches (org_id);
CREATE INDEX idx_import_batches_status ON import_batches (org_id, status);

-- =============================================================================
-- ADD import_batch_id FK TO EXISTING TABLES
-- =============================================================================

ALTER TABLE entries
    ADD COLUMN import_batch_id UUID REFERENCES import_batches(id);

CREATE INDEX idx_entries_import_batch ON entries (import_batch_id)
    WHERE import_batch_id IS NOT NULL;

ALTER TABLE staging
    ADD COLUMN import_batch_id UUID REFERENCES import_batches(id);

CREATE INDEX idx_staging_import_batch ON staging (import_batch_id)
    WHERE import_batch_id IS NOT NULL;

ALTER TABLE entry_links
    ADD COLUMN import_batch_id UUID REFERENCES import_batches(id);

CREATE INDEX idx_entry_links_import_batch ON entry_links (import_batch_id)
    WHERE import_batch_id IS NOT NULL;

-- =============================================================================
-- RLS POLICIES — import_batches
-- =============================================================================

ALTER TABLE import_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE import_batches FORCE ROW LEVEL SECURITY;

-- Admin: full access within org
CREATE POLICY import_batches_admin_all ON import_batches
    TO kb_admin
    USING (org_id = current_setting('app.org_id'))
    WITH CHECK (org_id = current_setting('app.org_id'));

-- Editor: SELECT within org (imports are admin-initiated but editors can view)
CREATE POLICY import_batches_editor_select ON import_batches
    FOR SELECT
    TO kb_editor
    USING (org_id = current_setting('app.org_id'));

-- Other roles: SELECT within org
CREATE POLICY import_batches_viewer_select ON import_batches
    FOR SELECT
    TO kb_commenter, kb_viewer, kb_agent
    USING (org_id = current_setting('app.org_id'));

-- =============================================================================
-- GRANTS — import_batches
-- =============================================================================

GRANT SELECT, INSERT, UPDATE, DELETE ON import_batches TO kb_admin;
GRANT SELECT ON import_batches TO kb_editor, kb_commenter, kb_viewer, kb_agent;
