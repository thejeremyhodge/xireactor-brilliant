-- 018_principals_and_groups.sql
-- Permissions v2 (P1): introduce group principals and a unified permissions
-- table keyed on (principal_type, principal_id). This migration only creates
-- the new tables, indexes, grants, and RLS policies — it does NOT touch the
-- existing entry_permissions / path_permissions tables or the entries RLS
-- policies. That backfill + RLS rewrite + drop is handled by migration 019.
--
-- Depends on: 001_core.sql, 004_rls.sql, 011_entry_permissions.sql
-- Spec: 0026 — Permissions v2 (P1) + Comments
-- Task: T-0134

-- =============================================================================
-- GROUPS — named collections of users within an org
-- =============================================================================

CREATE TABLE groups (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      TEXT NOT NULL REFERENCES organizations(id),
    name        TEXT NOT NULL,
    description TEXT,
    created_by  TEXT NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, name)
);

CREATE INDEX idx_groups_org ON groups (org_id);

-- =============================================================================
-- GROUP_MEMBERS — explicit user-to-group memberships (no nesting in P1)
-- =============================================================================

CREATE TABLE group_members (
    group_id    UUID NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id      TEXT NOT NULL REFERENCES organizations(id),
    added_by    TEXT NOT NULL REFERENCES users(id),
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, user_id)
);

CREATE INDEX idx_group_members_user ON group_members (user_id, org_id);
CREATE INDEX idx_group_members_org ON group_members (org_id);

-- =============================================================================
-- PERMISSIONS — unified polymorphic grants
--   principal: (principal_type, principal_id)  where principal_type IN (user,group)
--   resource : (resource_type, entry_id | path_pattern)
-- =============================================================================

CREATE TABLE permissions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          TEXT NOT NULL REFERENCES organizations(id),
    principal_type  TEXT NOT NULL CHECK (principal_type IN ('user', 'group')),
    principal_id    TEXT NOT NULL,  -- users.id (TEXT) or groups.id (UUID cast to TEXT)
    resource_type   TEXT NOT NULL CHECK (resource_type IN ('entry', 'path')),
    entry_id        UUID REFERENCES entries(id) ON DELETE CASCADE,
    path_pattern    TEXT,
    role            TEXT NOT NULL CHECK (role IN (
                        'admin', 'editor', 'commenter', 'viewer'
                    )),
    granted_by      TEXT NOT NULL REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- XOR: entry grants have entry_id only; path grants have path_pattern only
    CHECK (
        (resource_type = 'entry' AND entry_id IS NOT NULL AND path_pattern IS NULL)
        OR
        (resource_type = 'path'  AND path_pattern IS NOT NULL AND entry_id IS NULL)
    ),

    -- Dedup: one row per (principal, resource, role)
    UNIQUE (org_id, principal_type, principal_id, resource_type, entry_id, path_pattern, role)
);

CREATE INDEX idx_perms_principal
    ON permissions (org_id, principal_type, principal_id);

CREATE INDEX idx_perms_entry
    ON permissions (entry_id)
    WHERE entry_id IS NOT NULL;

CREATE INDEX idx_perms_path
    ON permissions (org_id, path_pattern text_pattern_ops)
    WHERE path_pattern IS NOT NULL;

-- =============================================================================
-- GRANT TABLE PERMISSIONS TO PG ROLES
-- Mirror the pattern used by 011_entry_permissions.sql.
-- =============================================================================

-- kb_admin: full CRUD on all three new tables
GRANT SELECT, INSERT, UPDATE, DELETE ON groups, group_members, permissions
    TO kb_admin;

-- Non-admin roles: read-only (RLS policies further restrict to own rows)
GRANT SELECT ON groups, group_members, permissions
    TO kb_editor, kb_commenter, kb_viewer, kb_agent;

-- =============================================================================
-- ROW-LEVEL SECURITY — groups
-- =============================================================================

ALTER TABLE groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE groups FORCE ROW LEVEL SECURITY;

-- Admin: full CRUD within org
CREATE POLICY groups_admin_all ON groups
    TO kb_admin
    USING      (org_id = current_setting('app.org_id'))
    WITH CHECK (org_id = current_setting('app.org_id'));

-- Non-admin: SELECT any group in their org (needed so API can show group lists
-- and resolve group names for grants the user can see).
CREATE POLICY groups_select ON groups
    FOR SELECT
    TO kb_editor, kb_commenter, kb_viewer, kb_agent
    USING (org_id = current_setting('app.org_id'));

-- =============================================================================
-- ROW-LEVEL SECURITY — group_members
-- =============================================================================

ALTER TABLE group_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE group_members FORCE ROW LEVEL SECURITY;

-- Admin: full CRUD within org
CREATE POLICY group_members_admin_all ON group_members
    TO kb_admin
    USING      (org_id = current_setting('app.org_id'))
    WITH CHECK (org_id = current_setting('app.org_id'));

-- Non-admin: SELECT own memberships only
CREATE POLICY group_members_select_own ON group_members
    FOR SELECT
    TO kb_editor, kb_commenter, kb_viewer, kb_agent
    USING (
        org_id = current_setting('app.org_id')
        AND user_id = current_setting('app.user_id')
    );

-- =============================================================================
-- ROW-LEVEL SECURITY — permissions
-- =============================================================================

ALTER TABLE permissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE permissions FORCE ROW LEVEL SECURITY;

-- Admin: full CRUD within org
CREATE POLICY permissions_admin_all ON permissions
    TO kb_admin
    USING      (org_id = current_setting('app.org_id'))
    WITH CHECK (org_id = current_setting('app.org_id'));

-- Non-admin: SELECT rows where the principal is either:
--   (a) the current user directly, or
--   (b) a group the current user belongs to.
CREATE POLICY permissions_select_own ON permissions
    FOR SELECT
    TO kb_editor, kb_commenter, kb_viewer, kb_agent
    USING (
        org_id = current_setting('app.org_id')
        AND (
            (principal_type = 'user' AND principal_id = current_setting('app.user_id'))
            OR
            (principal_type = 'group' AND principal_id IN (
                SELECT group_id::text FROM group_members
                WHERE user_id = current_setting('app.user_id')
                  AND org_id  = current_setting('app.org_id')
            ))
        )
    );
