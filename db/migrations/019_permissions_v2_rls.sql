-- 019_permissions_v2_rls.sql
-- Permissions v2 (P1): backfill legacy entry_permissions + path_permissions
-- into the new unified `permissions` table as principal_type='user' rows,
-- rewrite entries RLS policies to union across (permissions + group_members)
-- so that group-principal grants also count, then DROP the legacy tables.
--
-- Depends on: 011_entry_permissions.sql, 018_principals_and_groups.sql
-- Spec: 0026 — Permissions v2 (P1) + Comments
-- Task: T-0135
--
-- RECURSION NOTE
-- --------------
-- Migration 011 left a subtle RLS-recursion bug: entries policies referenced
-- entry_permissions, and entry_permissions policies referenced entries back,
-- which Postgres detected as infinite recursion when running as kb_editor.
-- To guarantee the new policies cannot self-reference entries, the union
-- check is pushed into a pair of SECURITY DEFINER helper functions that
-- bypass RLS on `permissions` and `group_members`. The helpers take the
-- entry's id + logical_path as arguments, so they never re-query entries.

-- =============================================================================
-- STEP 1 — BACKFILL legacy rows into the unified `permissions` table
--   - entry_permissions  → resource_type='entry', entry_id=entry_id
--   - path_permissions   → resource_type='path',  path_pattern=path_pattern
--   All legacy rows are user-principal grants.
--   ON CONFLICT DO NOTHING in case migration is re-run.
-- =============================================================================

INSERT INTO permissions (
    org_id, principal_type, principal_id,
    resource_type, entry_id, path_pattern,
    role, granted_by, created_at
)
SELECT
    org_id,
    'user'        AS principal_type,
    user_id       AS principal_id,
    'entry'       AS resource_type,
    entry_id,
    NULL          AS path_pattern,
    role,
    granted_by,
    created_at
FROM entry_permissions
ON CONFLICT DO NOTHING;

INSERT INTO permissions (
    org_id, principal_type, principal_id,
    resource_type, entry_id, path_pattern,
    role, granted_by, created_at
)
SELECT
    org_id,
    'user'        AS principal_type,
    user_id       AS principal_id,
    'path'        AS resource_type,
    NULL          AS entry_id,
    path_pattern,
    role,
    granted_by,
    created_at
FROM path_permissions
ON CONFLICT DO NOTHING;

-- =============================================================================
-- STEP 2 — SECURITY DEFINER helper functions
--
-- These run with the privileges of the function owner (postgres) and bypass
-- RLS on `permissions` and `group_members`. They take primitive args
-- (entry_id, logical_path, min role-ladder set) so they never re-query the
-- `entries` table — eliminating any possibility of the 011 recursion pattern.
--
-- has_entry_perm(p_entry_id, p_logical_path, p_roles):
--   TRUE iff the current session user has any permissions row in any of the
--   requested roles, attached either directly (principal_type='user') or via
--   a group they belong to, matching either the exact entry or a path prefix
--   covering the entry's logical_path.
-- =============================================================================

CREATE OR REPLACE FUNCTION has_entry_perm(
    p_entry_id      UUID,
    p_logical_path  TEXT,
    p_roles         TEXT[]
) RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM permissions p
        WHERE p.org_id = current_setting('app.org_id')
          AND p.role   = ANY (p_roles)
          AND (
                (p.principal_type = 'user'
                 AND p.principal_id = current_setting('app.user_id'))
             OR (p.principal_type = 'group'
                 AND p.principal_id IN (
                     SELECT gm.group_id::text
                     FROM group_members gm
                     WHERE gm.user_id = current_setting('app.user_id')
                       AND gm.org_id  = current_setting('app.org_id')
                 ))
          )
          AND (
                (p.resource_type = 'entry' AND p.entry_id = p_entry_id)
             OR (p.resource_type = 'path'
                 AND p_logical_path IS NOT NULL
                 AND p_logical_path LIKE p.path_pattern || '/%')
          )
    );
$$;

-- The helpers must be callable by every app role. They are SECURITY DEFINER
-- so they run with the owner's privileges regardless of the caller.
GRANT EXECUTE ON FUNCTION has_entry_perm(UUID, TEXT, TEXT[])
    TO kb_admin, kb_editor, kb_commenter, kb_viewer, kb_agent;

-- Role ladders for convenience within policies. Keeping as SQL arrays rather
-- than dedicated functions to keep the SQL self-documenting.

-- =============================================================================
-- STEP 3 — REWRITE entries RLS policies to use the helper + new table
-- Preserves the exact sensitivity ceilings from 011:
--   kb_editor    : ceiling = shared/operational/meeting
--   kb_commenter : ceiling = shared
--   kb_viewer    : ceiling = NOT (private, system)
--   kb_agent     : ceiling = shared
-- Editor-tier writes (INSERT/UPDATE/DELETE) still require an editor+ grant
-- (role in ('editor','admin')) either directly or via a group.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- kb_editor: SELECT
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS entries_editor_select ON entries;
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
            -- ACL: any grant on this entry/path (viewer+ is enough for read),
            -- but sensitivity ceiling still applies for editor-tier reads.
            OR (
                sensitivity IN ('shared', 'operational', 'meeting')
                AND has_entry_perm(
                        id,
                        logical_path,
                        ARRAY['viewer','commenter','editor','admin']
                    )
            )
        )
    );

-- ---------------------------------------------------------------------------
-- kb_editor: INSERT — requires editor+ grant (if not via owner/department)
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS entries_editor_insert ON entries;
CREATE POLICY entries_editor_insert ON entries
    FOR INSERT
    TO kb_editor
    WITH CHECK (
        org_id = current_setting('app.org_id')
        AND (
            department = current_setting('app.department')
            OR owner_id = current_setting('app.user_id')
            -- ACL: editor+ grant (inline because id isn't stable pre-insert;
            -- path grants are the only ACL path that matters for new rows).
            OR has_entry_perm(
                    id,
                    logical_path,
                    ARRAY['editor','admin']
                )
        )
    );

-- ---------------------------------------------------------------------------
-- kb_editor: UPDATE
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS entries_editor_update ON entries;
CREATE POLICY entries_editor_update ON entries
    FOR UPDATE
    TO kb_editor
    USING (
        org_id = current_setting('app.org_id')
        AND (
            department = current_setting('app.department')
            OR owner_id = current_setting('app.user_id')
            OR has_entry_perm(
                    id,
                    logical_path,
                    ARRAY['editor','admin']
                )
        )
    )
    WITH CHECK (
        org_id = current_setting('app.org_id')
        AND (
            department = current_setting('app.department')
            OR owner_id = current_setting('app.user_id')
            OR has_entry_perm(
                    id,
                    logical_path,
                    ARRAY['editor','admin']
                )
        )
    );

-- ---------------------------------------------------------------------------
-- kb_editor: DELETE
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS entries_editor_delete ON entries;
CREATE POLICY entries_editor_delete ON entries
    FOR DELETE
    TO kb_editor
    USING (
        org_id = current_setting('app.org_id')
        AND (
            department = current_setting('app.department')
            OR owner_id = current_setting('app.user_id')
            OR has_entry_perm(
                    id,
                    logical_path,
                    ARRAY['editor','admin']
                )
        )
    );

-- ---------------------------------------------------------------------------
-- kb_commenter: SELECT — ceiling = shared
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS entries_commenter_select ON entries;
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
            -- ACL: commenter ceiling is shared sensitivity
            OR (
                sensitivity = 'shared'
                AND has_entry_perm(
                        id,
                        logical_path,
                        ARRAY['viewer','commenter','editor','admin']
                    )
            )
        )
    );

-- ---------------------------------------------------------------------------
-- kb_viewer: SELECT — ceiling = NOT (private, system)
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS entries_viewer_select ON entries;
CREATE POLICY entries_viewer_select ON entries
    FOR SELECT
    TO kb_viewer
    USING (
        org_id = current_setting('app.org_id')
        AND (
            sensitivity NOT IN ('private', 'system')
            OR (
                sensitivity NOT IN ('private', 'system')
                AND has_entry_perm(
                        id,
                        logical_path,
                        ARRAY['viewer','commenter','editor','admin']
                    )
            )
        )
    );

-- ---------------------------------------------------------------------------
-- kb_agent: SELECT — ceiling = shared
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS entries_agent_select ON entries;
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
            OR (
                sensitivity = 'shared'
                AND has_entry_perm(
                        id,
                        logical_path,
                        ARRAY['viewer','commenter','editor','admin']
                    )
            )
        )
    );

-- =============================================================================
-- STEP 4 — DROP the legacy tables
-- No compat shim in P1 (per spec 0026 design decisions). The policies that
-- referenced these tables were replaced above, so CASCADE cleans up the old
-- owner-select/insert/delete policies on entry_permissions itself.
-- =============================================================================

DROP TABLE IF EXISTS entry_permissions CASCADE;
DROP TABLE IF EXISTS path_permissions  CASCADE;
