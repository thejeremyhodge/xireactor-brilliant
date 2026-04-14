-- 017_comments.sql
-- First-class comments subsystem (spec 0026, task T-0131).
-- Activates the dormant kb_commenter role for its intended purpose: commenting.
--
-- Visibility rule: a comment is visible iff the caller can SELECT the
-- referenced entry. Existing entries RLS (004 + 011) does that work --
-- we simply filter comments by `entry_id IN (SELECT id FROM entries)`,
-- which transparently respects the caller's entry visibility.
--
-- Session variables (set by API at connection time):
--   app.user_id    -- authenticated user's ID
--   app.org_id     -- user's organization ID
--
-- Depends on: 001_core.sql, 004_rls.sql, 011_entry_permissions.sql

-- =============================================================================
-- COMMENTS — table
-- =============================================================================

CREATE TABLE comments (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id             TEXT NOT NULL REFERENCES organizations(id),
    entry_id           UUID NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    author_id          TEXT NOT NULL REFERENCES users(id),
    author_kind        TEXT NOT NULL DEFAULT 'user'
                           CHECK (author_kind IN ('user', 'agent')),
    body               TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'open'
                           CHECK (status IN ('open', 'resolved', 'escalated', 'dismissed')),
    escalated_to       TEXT REFERENCES users(id),   -- nullable; group escalation deferred
    parent_comment_id  UUID REFERENCES comments(id) ON DELETE CASCADE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at        TIMESTAMPTZ,
    resolved_by        TEXT REFERENCES users(id)
);

CREATE INDEX idx_comments_entry       ON comments (entry_id);
CREATE INDEX idx_comments_org_status  ON comments (org_id, status);
CREATE INDEX idx_comments_author      ON comments (author_id);

-- =============================================================================
-- GRANT TABLE PERMISSIONS
-- RLS policies further filter which rows are visible/writable.
-- =============================================================================

-- Read for everyone (RLS will scope to entries the caller can see)
GRANT SELECT ON comments TO kb_admin, kb_editor, kb_commenter, kb_viewer, kb_agent;

-- Write (INSERT) for roles that can author comments. kb_viewer cannot.
GRANT INSERT ON comments TO kb_admin, kb_editor, kb_commenter, kb_agent;

-- Status transitions (UPDATE) -- RLS further restricts to author/owner/admin.
GRANT UPDATE ON comments TO kb_admin, kb_editor, kb_commenter, kb_agent;

-- Hard delete is admin-only.
GRANT DELETE ON comments TO kb_admin;

-- =============================================================================
-- ROW-LEVEL SECURITY
-- =============================================================================

ALTER TABLE comments ENABLE ROW LEVEL SECURITY;
ALTER TABLE comments FORCE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- kb_admin: full CRUD within org
-- ---------------------------------------------------------------------------
CREATE POLICY comments_admin_all ON comments
    TO kb_admin
    USING      (org_id = current_setting('app.org_id'))
    WITH CHECK (org_id = current_setting('app.org_id'));

-- ---------------------------------------------------------------------------
-- SELECT: all non-admin roles see comments iff the referenced entry is
-- visible to them. We defer to entries RLS via the sub-select: a user who
-- cannot SELECT the entry gets zero rows for entries.id, so the IN clause
-- fails and the comment row is filtered out.
-- ---------------------------------------------------------------------------
CREATE POLICY comments_select_via_entry ON comments
    FOR SELECT
    TO kb_editor, kb_commenter, kb_viewer, kb_agent
    USING (
        org_id = current_setting('app.org_id')
        AND entry_id IN (
            SELECT id FROM entries
            WHERE org_id = current_setting('app.org_id')
        )
    );

-- ---------------------------------------------------------------------------
-- INSERT: org match + author_id must be the caller + the entry must be
-- visible to the caller (which, by the entries RLS ceiling, implies the
-- caller has >= commenter access). kb_viewer has no INSERT grant and is
-- therefore blocked at the GRANT layer before RLS.
-- ---------------------------------------------------------------------------
CREATE POLICY comments_insert ON comments
    FOR INSERT
    TO kb_editor, kb_commenter, kb_agent
    WITH CHECK (
        org_id = current_setting('app.org_id')
        AND author_id = current_setting('app.user_id')
        AND entry_id IN (
            SELECT id FROM entries
            WHERE org_id = current_setting('app.org_id')
        )
    );

-- ---------------------------------------------------------------------------
-- UPDATE: only the author or the entry owner may update status fields.
-- (Admin is covered by comments_admin_all.) Body is immutable in P1, but
-- we do not enforce that at RLS -- the API layer restricts the updatable
-- column set to status / resolved_at / resolved_by / escalated_to.
-- ---------------------------------------------------------------------------
CREATE POLICY comments_update_author_or_owner ON comments
    FOR UPDATE
    TO kb_editor, kb_commenter, kb_agent
    USING (
        org_id = current_setting('app.org_id')
        AND (
            author_id = current_setting('app.user_id')
            OR entry_id IN (
                SELECT id FROM entries
                WHERE owner_id = current_setting('app.user_id')
                  AND org_id  = current_setting('app.org_id')
            )
        )
    )
    WITH CHECK (
        org_id = current_setting('app.org_id')
        AND (
            author_id = current_setting('app.user_id')
            OR entry_id IN (
                SELECT id FROM entries
                WHERE owner_id = current_setting('app.user_id')
                  AND org_id  = current_setting('app.org_id')
            )
        )
    );

-- No DELETE policy for non-admin roles: delete is admin-only (via
-- comments_admin_all + the GRANT table). Comment retraction is modelled
-- as status='dismissed', not as a row delete.
