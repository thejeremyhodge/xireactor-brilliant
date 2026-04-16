-- 022_blobs_and_attachments.sql
-- File uploads: blob store + entry attachment join table.
--
-- Adds two tables:
--   blobs             -- immutable, content-addressed file records (sha256 per org)
--   entry_attachments -- many-to-many link between entries and blobs
--
-- Storage bytes live in an external backend (local FS or S3-compatible); the
-- blobs row carries the pointer (storage_backend, storage_key). Blob rows are
-- deduped per-org by sha256 — the same content uploaded twice by the same org
-- yields a single blobs row; the same bytes uploaded by two different orgs
-- yields two rows (so tenant isolation holds even if the backend is shared).
--
-- RLS mirrors the entries pattern:
--   - FORCE ROW LEVEL SECURITY; all policies key off current_setting('app.org_id')
--   - kb_admin has full CRUD within org
--   - kb_editor + kb_agent can SELECT + INSERT within org (no UPDATE/DELETE —
--     blobs are immutable once written; attachments get deleted via ON DELETE
--     CASCADE from entries, not directly)
--   - kb_commenter + kb_viewer get SELECT only
--   - entry_attachments SELECT further joins to entries so entry RLS (004 + 011
--     + 019) transparently filters attachment visibility
--
-- Idempotent: safe to re-run. Uses CREATE TABLE IF NOT EXISTS, CREATE INDEX
-- IF NOT EXISTS, and DROP POLICY IF EXISTS before each CREATE POLICY.
--
-- Spec: 0034b — File uploads / PDF digest
-- Task: T-0180
-- Depends on: 001_core.sql, 004_rls.sql

-- =============================================================================
-- BLOBS — content-addressed blob store
-- =============================================================================

CREATE TABLE IF NOT EXISTS blobs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id            TEXT NOT NULL REFERENCES organizations(id),
    sha256            TEXT NOT NULL,
    content_type      TEXT NOT NULL,
    size_bytes        BIGINT NOT NULL,
    storage_backend   TEXT NOT NULL,
    storage_key       TEXT NOT NULL,
    uploaded_by       TEXT NOT NULL REFERENCES users(id),
    uploaded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (org_id, sha256)
);

-- Covering index for dedup lookups (already satisfied by the UNIQUE constraint
-- above, but declared explicitly for clarity and EXPLAIN readability).
CREATE INDEX IF NOT EXISTS idx_blobs_org_sha256 ON blobs (org_id, sha256);

-- =============================================================================
-- ENTRY_ATTACHMENTS — many-to-many link between entries and blobs
-- =============================================================================

CREATE TABLE IF NOT EXISTS entry_attachments (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      TEXT NOT NULL REFERENCES organizations(id),
    entry_id    UUID NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    blob_id     UUID NOT NULL REFERENCES blobs(id),
    role        TEXT NOT NULL DEFAULT 'source'
                    CHECK (role IN ('source', 'derived', 'thumbnail')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (entry_id, blob_id)
);

CREATE INDEX IF NOT EXISTS idx_entry_attachments_entry ON entry_attachments (entry_id);
CREATE INDEX IF NOT EXISTS idx_entry_attachments_blob  ON entry_attachments (blob_id);

-- =============================================================================
-- GRANT TABLE PERMISSIONS TO PG ROLES
-- =============================================================================

-- kb_admin: full CRUD on both tables
GRANT SELECT, INSERT, UPDATE, DELETE ON blobs, entry_attachments TO kb_admin;

-- kb_editor, kb_agent: can add blobs/attachments (upload path) + read
GRANT SELECT, INSERT ON blobs, entry_attachments TO kb_editor, kb_agent;

-- kb_commenter, kb_viewer: read-only
GRANT SELECT ON blobs, entry_attachments TO kb_commenter, kb_viewer;

-- =============================================================================
-- ROW-LEVEL SECURITY — blobs
-- =============================================================================

ALTER TABLE blobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE blobs FORCE ROW LEVEL SECURITY;

-- kb_admin: full access within org
DROP POLICY IF EXISTS blobs_admin_all ON blobs;
CREATE POLICY blobs_admin_all ON blobs
    TO kb_admin
    USING      (org_id = current_setting('app.org_id'))
    WITH CHECK (org_id = current_setting('app.org_id'));

-- Non-admin roles: SELECT within org
DROP POLICY IF EXISTS blobs_select_org ON blobs;
CREATE POLICY blobs_select_org ON blobs
    FOR SELECT
    TO kb_editor, kb_commenter, kb_viewer, kb_agent
    USING (org_id = current_setting('app.org_id'));

-- kb_editor, kb_agent: INSERT within org (uploader must match current user)
DROP POLICY IF EXISTS blobs_insert_editor_agent ON blobs;
CREATE POLICY blobs_insert_editor_agent ON blobs
    FOR INSERT
    TO kb_editor, kb_agent
    WITH CHECK (
        org_id = current_setting('app.org_id')
        AND uploaded_by = current_setting('app.user_id')
    );

-- No UPDATE or DELETE policies for non-admin roles: blobs are immutable once
-- written. Admins can still clean up via blobs_admin_all.

-- =============================================================================
-- ROW-LEVEL SECURITY — entry_attachments
-- =============================================================================

ALTER TABLE entry_attachments ENABLE ROW LEVEL SECURITY;
ALTER TABLE entry_attachments FORCE ROW LEVEL SECURITY;

-- kb_admin: full access within org
DROP POLICY IF EXISTS entry_attachments_admin_all ON entry_attachments;
CREATE POLICY entry_attachments_admin_all ON entry_attachments
    TO kb_admin
    USING      (org_id = current_setting('app.org_id'))
    WITH CHECK (org_id = current_setting('app.org_id'));

-- Non-admin roles: SELECT iff the referenced entry is visible to the caller.
-- Defers to entries RLS via the sub-select: a caller who cannot SELECT the
-- entry gets zero rows for entries.id, so the IN clause filters the
-- attachment row out. This is the same pattern used by comments (017).
DROP POLICY IF EXISTS entry_attachments_select_via_entry ON entry_attachments;
CREATE POLICY entry_attachments_select_via_entry ON entry_attachments
    FOR SELECT
    TO kb_editor, kb_commenter, kb_viewer, kb_agent
    USING (
        org_id = current_setting('app.org_id')
        AND entry_id IN (
            SELECT id FROM entries
            WHERE org_id = current_setting('app.org_id')
        )
    );

-- kb_editor, kb_agent: INSERT iff the referenced entry is visible to the
-- caller AND the referenced blob is in the same org. The entries sub-select
-- naturally restricts to entries the caller can SELECT (ownership / dept /
-- ACL rules all flow through).
DROP POLICY IF EXISTS entry_attachments_insert_editor_agent ON entry_attachments;
CREATE POLICY entry_attachments_insert_editor_agent ON entry_attachments
    FOR INSERT
    TO kb_editor, kb_agent
    WITH CHECK (
        org_id = current_setting('app.org_id')
        AND entry_id IN (
            SELECT id FROM entries
            WHERE org_id = current_setting('app.org_id')
        )
        AND blob_id IN (
            SELECT id FROM blobs
            WHERE org_id = current_setting('app.org_id')
        )
    );

-- No non-admin UPDATE/DELETE: attachments are torn down via ON DELETE CASCADE
-- when the owning entry is deleted; admins can clean up directly.
