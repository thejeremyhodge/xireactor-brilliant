-- 011_entry_permissions.sql
-- Granular ACL: entry-level and path-level permission grants.
-- Complements the org-wide role (users.role) with per-entry and
-- per-path overrides.  Does NOT modify entries RLS — that is a
-- separate migration (see T-0053).
--
-- Depends on: 001_core.sql, 004_rls.sql

-- =============================================================================
-- ENTRY_PERMISSIONS — explicit (entry, user) grants
-- =============================================================================

CREATE TABLE entry_permissions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      TEXT NOT NULL REFERENCES organizations(id),
    entry_id    UUID NOT NULL REFERENCES entries(id),
    user_id     TEXT NOT NULL REFERENCES users(id),
    role        TEXT NOT NULL CHECK (role IN (
                    'admin', 'editor', 'commenter', 'viewer'
                )),
    granted_by  TEXT NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (entry_id, user_id)
);

CREATE INDEX idx_entry_permissions_org ON entry_permissions (org_id);
CREATE INDEX idx_entry_permissions_user ON entry_permissions (user_id);

-- =============================================================================
-- PATH_PERMISSIONS — pattern-based (path_pattern, user) grants
-- =============================================================================

CREATE TABLE path_permissions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      TEXT NOT NULL REFERENCES organizations(id),
    path_pattern TEXT NOT NULL,
    user_id     TEXT NOT NULL REFERENCES users(id),
    role        TEXT NOT NULL CHECK (role IN (
                    'admin', 'editor', 'commenter', 'viewer'
                )),
    granted_by  TEXT NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (org_id, path_pattern, user_id)
);

-- btree index with text_pattern_ops for efficient LIKE 'prefix/%' queries
CREATE INDEX idx_path_permissions_pattern
    ON path_permissions (org_id, path_pattern text_pattern_ops);

CREATE INDEX idx_path_permissions_user ON path_permissions (user_id);

-- =============================================================================
-- GRANT TABLE PERMISSIONS TO PG ROLES
-- =============================================================================

-- kb_admin: full CRUD on both permission tables
GRANT SELECT, INSERT, UPDATE, DELETE ON entry_permissions, path_permissions
    TO kb_admin;

-- kb_editor: can read permissions (needed to check access) but not modify
GRANT SELECT ON entry_permissions, path_permissions TO kb_editor;

-- kb_commenter, kb_viewer, kb_agent: read-only
GRANT SELECT ON entry_permissions, path_permissions
    TO kb_commenter, kb_viewer, kb_agent;

-- =============================================================================
-- ROW-LEVEL SECURITY — entry_permissions
-- =============================================================================

ALTER TABLE entry_permissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE entry_permissions FORCE ROW LEVEL SECURITY;

-- Admin: full access within org
CREATE POLICY entry_perms_admin_all ON entry_permissions
    TO kb_admin
    USING  (org_id = current_setting('app.org_id'))
    WITH CHECK (org_id = current_setting('app.org_id'));

-- Non-admin roles: SELECT own grants only
CREATE POLICY entry_perms_select_own ON entry_permissions
    FOR SELECT
    TO kb_editor, kb_commenter, kb_viewer, kb_agent
    USING (
        org_id = current_setting('app.org_id')
        AND user_id = current_setting('app.user_id')
    );

-- Entry owner can manage grants for their entries (INSERT/DELETE).
-- Owner is determined by joining entries.owner_id.
CREATE POLICY entry_perms_owner_insert ON entry_permissions
    FOR INSERT
    TO kb_editor
    WITH CHECK (
        org_id = current_setting('app.org_id')
        AND entry_id IN (
            SELECT id FROM entries
            WHERE owner_id = current_setting('app.user_id')
              AND org_id = current_setting('app.org_id')
        )
    );

CREATE POLICY entry_perms_owner_delete ON entry_permissions
    FOR DELETE
    TO kb_editor
    USING (
        org_id = current_setting('app.org_id')
        AND entry_id IN (
            SELECT id FROM entries
            WHERE owner_id = current_setting('app.user_id')
              AND org_id = current_setting('app.org_id')
        )
    );

-- Entry owner can also SELECT grants on their entries (not just own grants)
CREATE POLICY entry_perms_owner_select ON entry_permissions
    FOR SELECT
    TO kb_editor
    USING (
        org_id = current_setting('app.org_id')
        AND entry_id IN (
            SELECT id FROM entries
            WHERE owner_id = current_setting('app.user_id')
              AND org_id = current_setting('app.org_id')
        )
    );

-- =============================================================================
-- ROW-LEVEL SECURITY — path_permissions
-- =============================================================================

ALTER TABLE path_permissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE path_permissions FORCE ROW LEVEL SECURITY;

-- Admin: full access within org
CREATE POLICY path_perms_admin_all ON path_permissions
    TO kb_admin
    USING  (org_id = current_setting('app.org_id'))
    WITH CHECK (org_id = current_setting('app.org_id'));

-- Non-admin roles: SELECT own grants only
CREATE POLICY path_perms_select_own ON path_permissions
    FOR SELECT
    TO kb_editor, kb_commenter, kb_viewer, kb_agent
    USING (
        org_id = current_setting('app.org_id')
        AND user_id = current_setting('app.user_id')
    );


-- =============================================================================
-- UPDATE ENTRIES RLS POLICIES — incorporate ACL checks
-- =============================================================================
-- Drop and recreate each policy with added ACL OR clauses.
-- ACL is additive: existing role-based access preserved, ACL can only widen access.
-- Sensitivity remains an independent ceiling.
-- Admin policy (entries_admin_all) is NOT touched — full CRUD within org.

-- ---------------------------------------------------------------------------
-- kb_editor: SELECT — original + ACL OR clauses (with sensitivity ceiling)
-- ---------------------------------------------------------------------------
DROP POLICY entries_editor_select ON entries;
CREATE POLICY entries_editor_select ON entries
    FOR SELECT
    TO kb_editor
    USING (
        org_id = current_setting('app.org_id')
        AND (
            -- Original role-based access (unchanged)
            sensitivity IN ('shared', 'operational', 'meeting')
            OR department = current_setting('app.department')
            OR owner_id = current_setting('app.user_id')
            -- ACL: entry/path grants — sensitivity ceiling still applies
            OR (
                sensitivity IN ('shared', 'operational', 'meeting')
                AND (
                    id IN (
                        SELECT entry_id FROM entry_permissions
                        WHERE user_id = current_setting('app.user_id')
                          AND org_id = current_setting('app.org_id')
                    )
                    OR EXISTS (
                        SELECT 1 FROM path_permissions pp
                        WHERE pp.user_id = current_setting('app.user_id')
                          AND pp.org_id = current_setting('app.org_id')
                          AND entries.logical_path LIKE pp.path_pattern || '/%'
                    )
                )
            )
        )
    );

-- ---------------------------------------------------------------------------
-- kb_editor: INSERT — original + ACL path grant (editor+ required)
-- ---------------------------------------------------------------------------
DROP POLICY entries_editor_insert ON entries;
CREATE POLICY entries_editor_insert ON entries
    FOR INSERT
    TO kb_editor
    WITH CHECK (
        org_id = current_setting('app.org_id')
        AND (
            -- Original role-based access
            department = current_setting('app.department')
            OR owner_id = current_setting('app.user_id')
            -- ACL: path-level grant with editor+ role
            OR EXISTS (
                SELECT 1 FROM path_permissions pp
                WHERE pp.user_id = current_setting('app.user_id')
                  AND pp.org_id = current_setting('app.org_id')
                  AND entries.logical_path LIKE pp.path_pattern || '/%'
                  AND pp.role IN ('editor', 'admin')
            )
        )
    );

-- ---------------------------------------------------------------------------
-- kb_editor: UPDATE — original + ACL entry/path grants (editor+ required)
-- ---------------------------------------------------------------------------
DROP POLICY entries_editor_update ON entries;
CREATE POLICY entries_editor_update ON entries
    FOR UPDATE
    TO kb_editor
    USING (
        org_id = current_setting('app.org_id')
        AND (
            -- Original role-based access
            department = current_setting('app.department')
            OR owner_id = current_setting('app.user_id')
            -- ACL: entry-level grant with editor+ role
            OR id IN (
                SELECT entry_id FROM entry_permissions
                WHERE user_id = current_setting('app.user_id')
                  AND org_id = current_setting('app.org_id')
                  AND role IN ('editor', 'admin')
            )
            -- ACL: path-level grant with editor+ role
            OR EXISTS (
                SELECT 1 FROM path_permissions pp
                WHERE pp.user_id = current_setting('app.user_id')
                  AND pp.org_id = current_setting('app.org_id')
                  AND entries.logical_path LIKE pp.path_pattern || '/%'
                  AND pp.role IN ('editor', 'admin')
            )
        )
    )
    WITH CHECK (
        org_id = current_setting('app.org_id')
        AND (
            -- Original role-based access
            department = current_setting('app.department')
            OR owner_id = current_setting('app.user_id')
            -- ACL: entry-level grant with editor+ role
            OR id IN (
                SELECT entry_id FROM entry_permissions
                WHERE user_id = current_setting('app.user_id')
                  AND org_id = current_setting('app.org_id')
                  AND role IN ('editor', 'admin')
            )
            -- ACL: path-level grant with editor+ role
            OR EXISTS (
                SELECT 1 FROM path_permissions pp
                WHERE pp.user_id = current_setting('app.user_id')
                  AND pp.org_id = current_setting('app.org_id')
                  AND entries.logical_path LIKE pp.path_pattern || '/%'
                  AND pp.role IN ('editor', 'admin')
            )
        )
    );

-- ---------------------------------------------------------------------------
-- kb_editor: DELETE — original + ACL entry/path grants (editor+ required)
-- ---------------------------------------------------------------------------
DROP POLICY entries_editor_delete ON entries;
CREATE POLICY entries_editor_delete ON entries
    FOR DELETE
    TO kb_editor
    USING (
        org_id = current_setting('app.org_id')
        AND (
            -- Original role-based access
            department = current_setting('app.department')
            OR owner_id = current_setting('app.user_id')
            -- ACL: entry-level grant with editor+ role
            OR id IN (
                SELECT entry_id FROM entry_permissions
                WHERE user_id = current_setting('app.user_id')
                  AND org_id = current_setting('app.org_id')
                  AND role IN ('editor', 'admin')
            )
            -- ACL: path-level grant with editor+ role
            OR EXISTS (
                SELECT 1 FROM path_permissions pp
                WHERE pp.user_id = current_setting('app.user_id')
                  AND pp.org_id = current_setting('app.org_id')
                  AND entries.logical_path LIKE pp.path_pattern || '/%'
                  AND pp.role IN ('editor', 'admin')
            )
        )
    );

-- ---------------------------------------------------------------------------
-- kb_commenter: SELECT — original + ACL OR clauses (with sensitivity ceiling)
-- ---------------------------------------------------------------------------
DROP POLICY entries_commenter_select ON entries;
CREATE POLICY entries_commenter_select ON entries
    FOR SELECT
    TO kb_commenter
    USING (
        org_id = current_setting('app.org_id')
        AND (
            -- Original role-based access (unchanged)
            sensitivity = 'shared'
            OR project_id IN (
                SELECT project_id FROM project_assignments
                WHERE user_id = current_setting('app.user_id')
            )
            -- ACL: entry/path grants — commenter ceiling is shared sensitivity
            OR (
                sensitivity = 'shared'
                AND (
                    id IN (
                        SELECT entry_id FROM entry_permissions
                        WHERE user_id = current_setting('app.user_id')
                          AND org_id = current_setting('app.org_id')
                    )
                    OR EXISTS (
                        SELECT 1 FROM path_permissions pp
                        WHERE pp.user_id = current_setting('app.user_id')
                          AND pp.org_id = current_setting('app.org_id')
                          AND entries.logical_path LIKE pp.path_pattern || '/%'
                    )
                )
            )
        )
    );

-- ---------------------------------------------------------------------------
-- kb_viewer: SELECT — original + ACL OR clauses (with sensitivity ceiling)
-- ---------------------------------------------------------------------------
DROP POLICY entries_viewer_select ON entries;
CREATE POLICY entries_viewer_select ON entries
    FOR SELECT
    TO kb_viewer
    USING (
        org_id = current_setting('app.org_id')
        AND (
            -- Original role-based access (unchanged)
            sensitivity NOT IN ('private', 'system')
            -- ACL: entry/path grants — viewer ceiling excludes private/system
            OR (
                sensitivity NOT IN ('private', 'system')
                AND (
                    id IN (
                        SELECT entry_id FROM entry_permissions
                        WHERE user_id = current_setting('app.user_id')
                          AND org_id = current_setting('app.org_id')
                    )
                    OR EXISTS (
                        SELECT 1 FROM path_permissions pp
                        WHERE pp.user_id = current_setting('app.user_id')
                          AND pp.org_id = current_setting('app.org_id')
                          AND entries.logical_path LIKE pp.path_pattern || '/%'
                    )
                )
            )
        )
    );

-- ---------------------------------------------------------------------------
-- kb_agent: SELECT — original + ACL OR clauses (with sensitivity ceiling)
-- ---------------------------------------------------------------------------
DROP POLICY entries_agent_select ON entries;
CREATE POLICY entries_agent_select ON entries
    FOR SELECT
    TO kb_agent
    USING (
        org_id = current_setting('app.org_id')
        AND (
            -- Original role-based access (unchanged)
            sensitivity = 'shared'
            OR owner_id = current_setting('app.user_id')
            OR department = current_setting('app.department')
            OR project_id IN (
                SELECT project_id FROM project_assignments
                WHERE user_id = current_setting('app.user_id')
            )
            -- ACL: entry/path grants — agent ceiling is shared sensitivity
            OR (
                sensitivity = 'shared'
                AND (
                    id IN (
                        SELECT entry_id FROM entry_permissions
                        WHERE user_id = current_setting('app.user_id')
                          AND org_id = current_setting('app.org_id')
                    )
                    OR EXISTS (
                        SELECT 1 FROM path_permissions pp
                        WHERE pp.user_id = current_setting('app.user_id')
                          AND pp.org_id = current_setting('app.org_id')
                          AND entries.logical_path LIKE pp.path_pattern || '/%'
                    )
                )
            )
        )
    );
