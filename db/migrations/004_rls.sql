-- 004_rls.sql
-- Row-Level Security policies and PG role creation.
-- Google Workspace role model: admin, editor, commenter, viewer + agent.
--
-- Session variables (set by API at connection time):
--   app.user_id    -- the authenticated user's ID
--   app.org_id     -- the user's organization ID
--   app.role       -- one of: admin, editor, commenter, viewer
--   app.department -- the user's department (may be empty string for cross-dept)
--
-- Depends on: 001_core.sql, 002_relationships.sql, 003_governance.sql

-- =============================================================================
-- PG ROLES
-- CREATE ROLE does not support IF NOT EXISTS, so we use DO blocks.
-- =============================================================================

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_admin') THEN
        CREATE ROLE kb_admin NOLOGIN;
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_editor') THEN
        CREATE ROLE kb_editor NOLOGIN;
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_commenter') THEN
        CREATE ROLE kb_commenter NOLOGIN;
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_viewer') THEN
        CREATE ROLE kb_viewer NOLOGIN;
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_agent') THEN
        CREATE ROLE kb_agent NOLOGIN;
    END IF;
END $$;

-- =============================================================================
-- GRANT TABLE PERMISSIONS
-- Grants control which SQL commands a role can issue.
-- RLS policies further filter which rows are visible/writable.
-- =============================================================================

-- kb_admin: full CRUD on all tables
GRANT SELECT, INSERT, UPDATE, DELETE ON
    entries, entry_links, entry_versions, staging, audit_log
    TO kb_admin;

-- kb_editor: full CRUD on entries, entry_links; INSERT on entry_versions (append-only);
--            full CRUD on staging; SELECT on audit_log
GRANT SELECT, INSERT, UPDATE, DELETE ON entries, entry_links, staging TO kb_editor;
GRANT SELECT, INSERT ON entry_versions TO kb_editor;
GRANT SELECT ON audit_log TO kb_editor;

-- kb_commenter: SELECT on entries, entry_links, entry_versions;
--               INSERT on staging (propose changes); SELECT on staging (own items);
--               SELECT on audit_log (own actions)
GRANT SELECT ON entries, entry_links, entry_versions TO kb_commenter;
GRANT SELECT, INSERT ON staging TO kb_commenter;
GRANT SELECT ON audit_log TO kb_commenter;

-- kb_viewer: SELECT only on content tables; no staging write, no audit
GRANT SELECT ON entries, entry_links, entry_versions TO kb_viewer;
GRANT SELECT ON staging TO kb_viewer;
GRANT SELECT ON audit_log TO kb_viewer;

-- kb_agent: SELECT on entries, entry_links, entry_versions;
--           INSERT on staging (all writes go to staging); SELECT on staging (own items);
--           SELECT on audit_log (own actions).
--           NO INSERT/UPDATE/DELETE on entries -- writes must go through staging.
GRANT SELECT ON entries, entry_links, entry_versions TO kb_agent;
GRANT SELECT, INSERT ON staging TO kb_agent;
GRANT SELECT ON audit_log TO kb_agent;

-- Grant USAGE on sequences needed for INSERT operations
GRANT USAGE ON SEQUENCE entry_versions_id_seq TO kb_admin, kb_editor;
GRANT USAGE ON SEQUENCE audit_log_id_seq TO kb_admin;

-- Grant access to project_assignments for subquery use in RLS policies
GRANT SELECT ON project_assignments TO kb_admin, kb_editor, kb_commenter, kb_viewer, kb_agent;

-- =============================================================================
-- HELPER: org_id match predicate (used in nearly every policy)
-- =============================================================================
-- All policies include: org_id = current_setting('app.org_id')
-- This ensures strict tenant isolation.


-- #############################################################################
-- ENTRIES — RLS POLICIES
-- #############################################################################

ALTER TABLE entries ENABLE ROW LEVEL SECURITY;
-- Force RLS even for table owners (important for testing with superuser)
ALTER TABLE entries FORCE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- kb_admin: full CRUD within org
-- ---------------------------------------------------------------------------
CREATE POLICY entries_admin_all ON entries
    TO kb_admin
    USING (org_id = current_setting('app.org_id'))
    WITH CHECK (org_id = current_setting('app.org_id'));

-- ---------------------------------------------------------------------------
-- kb_editor: SELECT — org match AND (shared/operational/meeting sensitivity
--            OR department match OR owner match)
-- ---------------------------------------------------------------------------
CREATE POLICY entries_editor_select ON entries
    FOR SELECT
    TO kb_editor
    USING (
        org_id = current_setting('app.org_id')
        AND (
            sensitivity IN ('shared', 'operational', 'meeting')
            OR department = current_setting('app.department')
            OR owner_id = current_setting('app.user_id')
        )
    );

-- kb_editor: INSERT/UPDATE/DELETE — org match AND (department match OR owner match)
CREATE POLICY entries_editor_insert ON entries
    FOR INSERT
    TO kb_editor
    WITH CHECK (
        org_id = current_setting('app.org_id')
        AND (
            department = current_setting('app.department')
            OR owner_id = current_setting('app.user_id')
        )
    );

CREATE POLICY entries_editor_update ON entries
    FOR UPDATE
    TO kb_editor
    USING (
        org_id = current_setting('app.org_id')
        AND (
            department = current_setting('app.department')
            OR owner_id = current_setting('app.user_id')
        )
    )
    WITH CHECK (
        org_id = current_setting('app.org_id')
        AND (
            department = current_setting('app.department')
            OR owner_id = current_setting('app.user_id')
        )
    );

CREATE POLICY entries_editor_delete ON entries
    FOR DELETE
    TO kb_editor
    USING (
        org_id = current_setting('app.org_id')
        AND (
            department = current_setting('app.department')
            OR owner_id = current_setting('app.user_id')
        )
    );

-- ---------------------------------------------------------------------------
-- kb_commenter: SELECT only — org match AND (shared sensitivity OR
--              project_id in user's assigned projects)
-- ---------------------------------------------------------------------------
CREATE POLICY entries_commenter_select ON entries
    FOR SELECT
    TO kb_commenter
    USING (
        org_id = current_setting('app.org_id')
        AND (
            sensitivity = 'shared'
            OR project_id IN (
                SELECT project_id FROM project_assignments
                WHERE user_id = current_setting('app.user_id')
            )
        )
    );
-- No INSERT/UPDATE/DELETE policies for kb_commenter on entries.
-- Writes must go through the staging table.

-- ---------------------------------------------------------------------------
-- kb_viewer: SELECT only — org match AND sensitivity NOT private/system
-- ---------------------------------------------------------------------------
CREATE POLICY entries_viewer_select ON entries
    FOR SELECT
    TO kb_viewer
    USING (
        org_id = current_setting('app.org_id')
        AND sensitivity NOT IN ('private', 'system')
    );
-- No INSERT/UPDATE/DELETE policies for kb_viewer on entries.

-- ---------------------------------------------------------------------------
-- kb_agent: SELECT only — org match AND (shared OR owner match OR
--           department match OR project_id in assigned projects)
-- ---------------------------------------------------------------------------
CREATE POLICY entries_agent_select ON entries
    FOR SELECT
    TO kb_agent
    USING (
        org_id = current_setting('app.org_id')
        AND (
            sensitivity = 'shared'
            OR owner_id = current_setting('app.user_id')
            OR department = current_setting('app.department')
            OR project_id IN (
                SELECT project_id FROM project_assignments
                WHERE user_id = current_setting('app.user_id')
            )
        )
    );
-- No INSERT/UPDATE/DELETE policies for kb_agent on entries.
-- All agent writes are routed to the staging table.


-- #############################################################################
-- ENTRY_LINKS — RLS POLICIES
-- #############################################################################

ALTER TABLE entry_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE entry_links FORCE ROW LEVEL SECURITY;

-- All roles: SELECT within org
CREATE POLICY entry_links_select ON entry_links
    FOR SELECT
    TO kb_admin, kb_editor, kb_commenter, kb_viewer, kb_agent
    USING (org_id = current_setting('app.org_id'));

-- kb_admin, kb_editor: INSERT/UPDATE/DELETE within org
CREATE POLICY entry_links_admin_editor_insert ON entry_links
    FOR INSERT
    TO kb_admin, kb_editor
    WITH CHECK (org_id = current_setting('app.org_id'));

CREATE POLICY entry_links_admin_editor_update ON entry_links
    FOR UPDATE
    TO kb_admin, kb_editor
    USING (org_id = current_setting('app.org_id'))
    WITH CHECK (org_id = current_setting('app.org_id'));

CREATE POLICY entry_links_admin_editor_delete ON entry_links
    FOR DELETE
    TO kb_admin, kb_editor
    USING (org_id = current_setting('app.org_id'));


-- #############################################################################
-- ENTRY_VERSIONS — RLS POLICIES
-- #############################################################################

ALTER TABLE entry_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE entry_versions FORCE ROW LEVEL SECURITY;

-- All roles: SELECT within org
CREATE POLICY entry_versions_select ON entry_versions
    FOR SELECT
    TO kb_admin, kb_editor, kb_commenter, kb_viewer, kb_agent
    USING (org_id = current_setting('app.org_id'));

-- kb_admin, kb_editor: INSERT within org (append-only — no UPDATE or DELETE)
CREATE POLICY entry_versions_insert ON entry_versions
    FOR INSERT
    TO kb_admin, kb_editor
    WITH CHECK (org_id = current_setting('app.org_id'));
-- No UPDATE or DELETE policies: version history is immutable.


-- #############################################################################
-- STAGING — RLS POLICIES
-- #############################################################################

ALTER TABLE staging ENABLE ROW LEVEL SECURITY;
ALTER TABLE staging FORCE ROW LEVEL SECURITY;

-- SELECT: own submissions OR admin role
CREATE POLICY staging_select_own ON staging
    FOR SELECT
    TO kb_editor, kb_commenter, kb_viewer, kb_agent
    USING (
        org_id = current_setting('app.org_id')
        AND submitted_by = current_setting('app.user_id')
    );

CREATE POLICY staging_select_admin ON staging
    FOR SELECT
    TO kb_admin
    USING (org_id = current_setting('app.org_id'));

-- INSERT: anyone within org can submit
CREATE POLICY staging_insert ON staging
    FOR INSERT
    TO kb_admin, kb_editor, kb_commenter, kb_agent
    WITH CHECK (org_id = current_setting('app.org_id'));

-- UPDATE: admin only (for approve/reject workflow)
CREATE POLICY staging_update_admin ON staging
    FOR UPDATE
    TO kb_admin
    USING (org_id = current_setting('app.org_id'))
    WITH CHECK (org_id = current_setting('app.org_id'));

-- DELETE: admin only (cleanup)
CREATE POLICY staging_delete_admin ON staging
    FOR DELETE
    TO kb_admin
    USING (org_id = current_setting('app.org_id'));


-- #############################################################################
-- AUDIT_LOG — RLS POLICIES
-- #############################################################################

ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;

-- SELECT: admin sees all within org; others see only own actions
CREATE POLICY audit_log_select_admin ON audit_log
    FOR SELECT
    TO kb_admin
    USING (org_id = current_setting('app.org_id'));

CREATE POLICY audit_log_select_own ON audit_log
    FOR SELECT
    TO kb_editor, kb_commenter, kb_viewer, kb_agent
    USING (
        org_id = current_setting('app.org_id')
        AND actor_id = current_setting('app.user_id')
    );

-- INSERT: admin only (the API server runs as admin to write audit entries).
-- No direct user inserts — the API layer handles audit logging.
CREATE POLICY audit_log_insert ON audit_log
    FOR INSERT
    TO kb_admin
    WITH CHECK (org_id = current_setting('app.org_id'));

-- No UPDATE or DELETE policies: audit log is append-only and immutable.
