-- 001_init.sql
-- xiReactor Cortex — complete database schema
--
-- This single migration creates the full schema. Run once on first boot
-- via Docker's /docker-entrypoint-initdb.d/ mount.
--
-- Tables: organizations, users, api_keys, entries, entry_links,
--         entry_versions, project_assignments, staging, audit_log,
--         oauth_clients/tokens, content_type_registry, invitations,
--         entry_permissions, path_permissions, import_batches
--
-- Includes: PG roles, RLS policies, full-text search trigger, seed data

-- ============================================================
-- Extensions
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- gen_random_uuid(), crypt()
CREATE EXTENSION IF NOT EXISTS "vector";     -- pgvector for semantic search

-- ============================================================
-- Organizations
-- ============================================================

CREATE TABLE organizations (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    settings    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Users (Google Workspace role model)
-- ============================================================

CREATE TABLE users (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL REFERENCES organizations(id),
    display_name    TEXT NOT NULL,
    email_hash      TEXT NOT NULL,
    email           TEXT,
    password_hash   TEXT,
    role            TEXT NOT NULL CHECK (role IN (
                        'admin', 'editor', 'commenter', 'viewer'
                    )),
    department      TEXT,
    trust_weight    NUMERIC(3,2) NOT NULL DEFAULT 0.50
                        CHECK (trust_weight BETWEEN 0 AND 1),
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_users_org ON users (org_id);
CREATE UNIQUE INDEX idx_users_org_email ON users (org_id, email)
    WHERE email IS NOT NULL;

-- ============================================================
-- API Keys
-- ============================================================

CREATE TABLE api_keys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT NOT NULL REFERENCES users(id),
    org_id      TEXT NOT NULL REFERENCES organizations(id),
    key_hash    TEXT NOT NULL,
    key_prefix  TEXT NOT NULL,
    key_type    TEXT NOT NULL CHECK (key_type IN (
                    'interactive', 'agent', 'api_integration'
                )),
    label       TEXT,
    expires_at  TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    is_revoked  BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_api_keys_prefix ON api_keys (key_prefix) WHERE NOT is_revoked;
CREATE INDEX idx_api_keys_user ON api_keys (user_id);

-- ============================================================
-- Entries (Knowledge Base Content)
-- ============================================================

CREATE TABLE entries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          TEXT NOT NULL REFERENCES organizations(id),

    -- Content
    title           TEXT NOT NULL,
    content         TEXT NOT NULL,
    summary         TEXT,
    content_hash    TEXT NOT NULL,

    -- Classification (validated against content_type_registry, no CHECK)
    content_type    TEXT NOT NULL,
    logical_path    TEXT NOT NULL,
    sensitivity     TEXT NOT NULL DEFAULT 'shared' CHECK (sensitivity IN (
                        'system', 'strategic', 'operational', 'private',
                        'project', 'meeting', 'shared'
                    )),
    department      TEXT,
    owner_id        TEXT REFERENCES users(id),
    project_id      UUID,

    -- Two-layer metadata
    tags            TEXT[] NOT NULL DEFAULT '{}',
    domain_meta     JSONB NOT NULL DEFAULT '{}',

    -- Versioning
    version         INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'published' CHECK (status IN (
                        'draft', 'published', 'archived', 'superseded'
                    )),

    -- Import batch tracking
    import_batch_id UUID,  -- FK added after import_batches table

    -- Audit
    created_by      TEXT NOT NULL REFERENCES users(id),
    updated_by      TEXT NOT NULL REFERENCES users(id),
    source          TEXT NOT NULL CHECK (source IN ('web_ui', 'agent', 'api')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Search
    search_vector   TSVECTOR,
    embedding       vector(1536),
    word_count      INTEGER,

    -- Constraints
    UNIQUE (org_id, logical_path)
);

CREATE INDEX idx_entries_org ON entries (org_id);
CREATE INDEX idx_entries_search ON entries USING GIN (search_vector);
CREATE INDEX idx_entries_embedding ON entries USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_entries_type ON entries (org_id, content_type);
CREATE INDEX idx_entries_dept ON entries (org_id, department);
CREATE INDEX idx_entries_owner ON entries (owner_id);
CREATE INDEX idx_entries_status ON entries (org_id, status) WHERE status = 'published';
CREATE INDEX idx_entries_path ON entries (org_id, logical_path);
CREATE INDEX idx_entries_updated ON entries (org_id, updated_at DESC);
CREATE INDEX idx_entries_tags ON entries USING GIN (tags);
CREATE INDEX idx_entries_domain_meta ON entries USING GIN (domain_meta);

-- ============================================================
-- Full-Text Search Trigger
-- ============================================================

CREATE OR REPLACE FUNCTION entries_search_trigger() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', coalesce(NEW.title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(NEW.summary, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(NEW.content, '')), 'C') ||
        setweight(to_tsvector('english', coalesce(array_to_string(NEW.tags, ' '), '')), 'B');
    NEW.word_count := array_length(regexp_split_to_array(coalesce(NEW.content, ''), '\s+'), 1);
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trig_entries_search
    BEFORE INSERT OR UPDATE OF title, summary, content, tags ON entries
    FOR EACH ROW EXECUTE FUNCTION entries_search_trigger();

-- ============================================================
-- Entry Links (wiki-link relationships, CTE-first approach)
-- ============================================================

CREATE TABLE entry_links (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          TEXT NOT NULL REFERENCES organizations(id),
    source_entry_id UUID NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    target_entry_id UUID NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    link_type       TEXT NOT NULL CHECK (link_type IN (
                        'relates_to', 'supersedes', 'contradicts',
                        'depends_on', 'part_of', 'tagged_with'
                    )),
    weight          NUMERIC(3,2) DEFAULT 1.0 CHECK (weight BETWEEN 0 AND 1),
    metadata        JSONB NOT NULL DEFAULT '{}',
    import_batch_id UUID,  -- FK added after import_batches table
    created_by      TEXT NOT NULL REFERENCES users(id),
    source          TEXT NOT NULL CHECK (source IN ('web_ui', 'agent', 'api')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (org_id, source_entry_id, target_entry_id, link_type)
);

CREATE INDEX idx_links_source ON entry_links (source_entry_id);
CREATE INDEX idx_links_target ON entry_links (target_entry_id);
CREATE INDEX idx_links_org_type ON entry_links (org_id, link_type);

-- ============================================================
-- Entry Versions (append-only history)
-- ============================================================

CREATE TABLE entry_versions (
    id                  BIGSERIAL PRIMARY KEY,
    entry_id            UUID NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    org_id              TEXT NOT NULL REFERENCES organizations(id),
    version             INTEGER NOT NULL,
    title               TEXT NOT NULL,
    content             TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    domain_meta         JSONB,
    tags                TEXT[],
    status              TEXT NOT NULL,
    changed_by          TEXT NOT NULL REFERENCES users(id),
    source              TEXT NOT NULL CHECK (source IN ('web_ui', 'agent', 'api')),
    change_summary      TEXT,
    governance_action   TEXT CHECK (governance_action IN (
                            'created', 'updated', 'published', 'archived',
                            'reverted', 'auto_approved', 'evaluator_approved'
                        )),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (entry_id, version)
);

CREATE INDEX idx_versions_entry ON entry_versions (entry_id, version DESC);
CREATE INDEX idx_versions_org ON entry_versions (org_id);

-- ============================================================
-- Project Assignments (user-to-project access grants for RLS)
-- ============================================================

CREATE TABLE project_assignments (
    user_id     TEXT NOT NULL REFERENCES users(id),
    project_id  UUID NOT NULL,
    org_id      TEXT NOT NULL REFERENCES organizations(id),
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    assigned_by TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY (user_id, project_id)
);

CREATE INDEX idx_assignments_project ON project_assignments (project_id);

-- ============================================================
-- Staging (governance pipeline queue, 4-tier model)
-- ============================================================

CREATE TABLE staging (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id              TEXT NOT NULL REFERENCES organizations(id),

    -- What's being proposed
    target_entry_id     UUID REFERENCES entries(id),
    target_path         TEXT NOT NULL,
    change_type         TEXT NOT NULL CHECK (change_type IN (
                            'create', 'update', 'append', 'delete', 'create_link'
                        )),
    proposed_title      TEXT,
    proposed_content    TEXT NOT NULL,
    proposed_meta       JSONB,
    content_hash        TEXT NOT NULL,

    -- Who submitted
    submitted_by        TEXT NOT NULL REFERENCES users(id),
    source              TEXT NOT NULL CHECK (source IN ('web_ui', 'agent', 'api')),

    -- Governance tier (1-4)
    governance_tier     INTEGER NOT NULL DEFAULT 1 CHECK (governance_tier IN (1, 2, 3, 4)),
    submission_category TEXT NOT NULL CHECK (submission_category IN (
                            'teaching_loop', 'auto_save', 'compress', 'preserve',
                            'meeting_intel', 'project_intel', 'user_direct'
                        )),

    -- Evaluator processing
    status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                            'pending', 'approved', 'rejected', 'deferred',
                            'superseded', 'merged', 'auto_approved'
                        )),
    priority            INTEGER NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    evaluator_decision  JSONB,
    evaluator_notes     TEXT,
    reviewed_at         TIMESTAMPTZ,
    reviewed_by         TEXT REFERENCES users(id),
    promoted_entry_id   UUID REFERENCES entries(id),

    -- Import batch tracking
    import_batch_id     UUID,  -- FK added after import_batches table

    -- Timestamps
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_staging_pending ON staging (org_id, priority, created_at) WHERE status = 'pending';
CREATE INDEX idx_staging_target ON staging (target_entry_id);
CREATE INDEX idx_staging_user ON staging (submitted_by);
CREATE INDEX idx_staging_promoted ON staging (promoted_entry_id)
    WHERE promoted_entry_id IS NOT NULL;

-- ============================================================
-- Audit Log (append-only)
-- ============================================================

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

-- ============================================================
-- OAuth 2.1 Token Store (MCP server)
-- ============================================================

CREATE TABLE oauth_clients (
    client_id           TEXT PRIMARY KEY,
    client_secret       TEXT NOT NULL,
    client_id_issued_at BIGINT NOT NULL,
    client_info         JSONB NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE oauth_auth_codes (
    code                TEXT PRIMARY KEY,
    client_id           TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    scopes              TEXT[] NOT NULL DEFAULT '{}',
    expires_at          DOUBLE PRECISION NOT NULL,
    code_challenge      TEXT,
    redirect_uri        TEXT,
    redirect_uri_provided_explicitly BOOLEAN NOT NULL DEFAULT false,
    resource            TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE oauth_access_tokens (
    token               TEXT PRIMARY KEY,
    client_id           TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    scopes              TEXT[] NOT NULL DEFAULT '{}',
    expires_at          BIGINT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE oauth_refresh_tokens (
    token               TEXT PRIMARY KEY,
    client_id           TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    scopes              TEXT[] NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_oauth_access_tokens_expires ON oauth_access_tokens (expires_at);
CREATE INDEX idx_oauth_auth_codes_expires ON oauth_auth_codes (expires_at);

-- ============================================================
-- Content Type Registry
-- ============================================================

CREATE TABLE content_type_registry (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    alias_of    TEXT REFERENCES content_type_registry(name),
    is_active   BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Invitations (single-use org onboarding codes)
-- ============================================================

CREATE TABLE invitations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          TEXT NOT NULL REFERENCES organizations(id),
    invite_code     TEXT NOT NULL UNIQUE
                        CHECK (invite_code ~ '^CTX-[A-Z0-9]{4}-[A-Z0-9]{4}$'),
    token_hash      TEXT NOT NULL,
    default_role    TEXT NOT NULL DEFAULT 'viewer'
                        CHECK (default_role IN (
                            'admin', 'editor', 'commenter', 'viewer'
                        )),
    invited_by      TEXT REFERENCES users(id),
    redeemed_by     TEXT REFERENCES users(id),
    email_hint      TEXT,
    expires_at      TIMESTAMPTZ NOT NULL,
    redeemed_at     TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN (
                            'pending', 'redeemed', 'expired', 'revoked'
                        )),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_invitations_org_status ON invitations (org_id, status);

-- ============================================================
-- Entry Permissions (granular ACL)
-- ============================================================

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

-- ============================================================
-- Path Permissions (pattern-based ACL)
-- ============================================================

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

CREATE INDEX idx_path_permissions_pattern
    ON path_permissions (org_id, path_pattern text_pattern_ops);
CREATE INDEX idx_path_permissions_user ON path_permissions (user_id);

-- ============================================================
-- Import Batches (Obsidian vault import tracking)
-- ============================================================

CREATE TABLE import_batches (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          TEXT NOT NULL REFERENCES organizations(id),
    source_vault    TEXT NOT NULL,
    base_path       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
                        'active', 'rolled_back'
                    )),
    file_count      INTEGER NOT NULL,
    created_count   INTEGER NOT NULL DEFAULT 0,
    staged_count    INTEGER NOT NULL DEFAULT 0,
    linked_count    INTEGER NOT NULL DEFAULT 0,
    skipped_count   INTEGER NOT NULL DEFAULT 0,
    error_count     INTEGER NOT NULL DEFAULT 0,
    created_by      TEXT NOT NULL REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    rolled_back_at  TIMESTAMPTZ,
    rolled_back_by  TEXT REFERENCES users(id)
);

CREATE INDEX idx_import_batches_org ON import_batches (org_id);
CREATE INDEX idx_import_batches_status ON import_batches (org_id, status);

-- ============================================================
-- Add import_batch_id foreign keys (deferred until import_batches exists)
-- ============================================================

ALTER TABLE entries
    ADD CONSTRAINT entries_import_batch_fk
    FOREIGN KEY (import_batch_id) REFERENCES import_batches(id);

CREATE INDEX idx_entries_import_batch ON entries (import_batch_id)
    WHERE import_batch_id IS NOT NULL;

ALTER TABLE staging
    ADD CONSTRAINT staging_import_batch_fk
    FOREIGN KEY (import_batch_id) REFERENCES import_batches(id);

CREATE INDEX idx_staging_import_batch ON staging (import_batch_id)
    WHERE import_batch_id IS NOT NULL;

ALTER TABLE entry_links
    ADD CONSTRAINT entry_links_import_batch_fk
    FOREIGN KEY (import_batch_id) REFERENCES import_batches(id);

CREATE INDEX idx_entry_links_import_batch ON entry_links (import_batch_id)
    WHERE import_batch_id IS NOT NULL;

-- ============================================================
-- Admin bootstrap placeholder
-- ============================================================
-- Admin user creation is handled by the API server at startup.
-- See api/admin_bootstrap.py — reads ADMIN_EMAIL / ADMIN_PASSWORD from env vars.


-- #############################################################################
-- PG ROLES
-- #############################################################################

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


-- #############################################################################
-- GRANT TABLE PERMISSIONS
-- #############################################################################

-- kb_admin: full CRUD on all tables
GRANT SELECT, INSERT, UPDATE, DELETE ON
    entries, entry_links, entry_versions, staging, audit_log
    TO kb_admin;

-- kb_editor
GRANT SELECT, INSERT, UPDATE, DELETE ON entries, entry_links, staging TO kb_editor;
GRANT SELECT, INSERT ON entry_versions TO kb_editor;
GRANT SELECT ON audit_log TO kb_editor;

-- kb_commenter
GRANT SELECT ON entries, entry_links, entry_versions TO kb_commenter;
GRANT SELECT, INSERT ON staging TO kb_commenter;
GRANT SELECT ON audit_log TO kb_commenter;

-- kb_viewer
GRANT SELECT ON entries, entry_links, entry_versions TO kb_viewer;
GRANT SELECT ON staging TO kb_viewer;
GRANT SELECT ON audit_log TO kb_viewer;

-- kb_agent
GRANT SELECT ON entries, entry_links, entry_versions TO kb_agent;
GRANT SELECT, INSERT ON staging TO kb_agent;
GRANT SELECT ON audit_log TO kb_agent;

-- Sequences
GRANT USAGE ON SEQUENCE entry_versions_id_seq TO kb_admin, kb_editor;
GRANT USAGE ON SEQUENCE audit_log_id_seq TO kb_admin;

-- Project assignments (needed for RLS subqueries)
GRANT SELECT ON project_assignments TO kb_admin, kb_editor, kb_commenter, kb_viewer, kb_agent;

-- Content type registry
GRANT SELECT ON content_type_registry TO kb_admin, kb_editor, kb_commenter, kb_viewer, kb_agent;
GRANT INSERT, UPDATE, DELETE ON content_type_registry TO kb_admin;

-- Invitations
GRANT SELECT, INSERT, UPDATE, DELETE ON invitations TO kb_admin;

-- Entry/path permissions
GRANT SELECT, INSERT, UPDATE, DELETE ON entry_permissions, path_permissions TO kb_admin;
GRANT SELECT ON entry_permissions, path_permissions TO kb_editor;
GRANT SELECT ON entry_permissions, path_permissions TO kb_commenter, kb_viewer, kb_agent;

-- Import batches
GRANT SELECT, INSERT, UPDATE, DELETE ON import_batches TO kb_admin;
GRANT SELECT ON import_batches TO kb_editor, kb_commenter, kb_viewer, kb_agent;

-- Users table
GRANT SELECT ON users TO kb_admin, kb_editor, kb_commenter, kb_viewer, kb_agent;
GRANT UPDATE ON users TO kb_admin;


-- #############################################################################
-- ROW-LEVEL SECURITY
-- #############################################################################

-- ===================== ENTRIES =====================

ALTER TABLE entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE entries FORCE ROW LEVEL SECURITY;

CREATE POLICY entries_admin_all ON entries
    TO kb_admin
    USING (org_id = current_setting('app.org_id'))
    WITH CHECK (org_id = current_setting('app.org_id'));

CREATE POLICY entries_editor_select ON entries
    FOR SELECT TO kb_editor
    USING (
        org_id = current_setting('app.org_id')
        AND (
            sensitivity IN ('shared', 'operational', 'meeting')
            OR department = current_setting('app.department')
            OR owner_id = current_setting('app.user_id')
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

CREATE POLICY entries_editor_insert ON entries
    FOR INSERT TO kb_editor
    WITH CHECK (
        org_id = current_setting('app.org_id')
        AND (
            department = current_setting('app.department')
            OR owner_id = current_setting('app.user_id')
            OR EXISTS (
                SELECT 1 FROM path_permissions pp
                WHERE pp.user_id = current_setting('app.user_id')
                  AND pp.org_id = current_setting('app.org_id')
                  AND entries.logical_path LIKE pp.path_pattern || '/%'
                  AND pp.role IN ('editor', 'admin')
            )
        )
    );

CREATE POLICY entries_editor_update ON entries
    FOR UPDATE TO kb_editor
    USING (
        org_id = current_setting('app.org_id')
        AND (
            department = current_setting('app.department')
            OR owner_id = current_setting('app.user_id')
            OR id IN (
                SELECT entry_id FROM entry_permissions
                WHERE user_id = current_setting('app.user_id')
                  AND org_id = current_setting('app.org_id')
                  AND role IN ('editor', 'admin')
            )
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
            department = current_setting('app.department')
            OR owner_id = current_setting('app.user_id')
            OR id IN (
                SELECT entry_id FROM entry_permissions
                WHERE user_id = current_setting('app.user_id')
                  AND org_id = current_setting('app.org_id')
                  AND role IN ('editor', 'admin')
            )
            OR EXISTS (
                SELECT 1 FROM path_permissions pp
                WHERE pp.user_id = current_setting('app.user_id')
                  AND pp.org_id = current_setting('app.org_id')
                  AND entries.logical_path LIKE pp.path_pattern || '/%'
                  AND pp.role IN ('editor', 'admin')
            )
        )
    );

CREATE POLICY entries_editor_delete ON entries
    FOR DELETE TO kb_editor
    USING (
        org_id = current_setting('app.org_id')
        AND (
            department = current_setting('app.department')
            OR owner_id = current_setting('app.user_id')
            OR id IN (
                SELECT entry_id FROM entry_permissions
                WHERE user_id = current_setting('app.user_id')
                  AND org_id = current_setting('app.org_id')
                  AND role IN ('editor', 'admin')
            )
            OR EXISTS (
                SELECT 1 FROM path_permissions pp
                WHERE pp.user_id = current_setting('app.user_id')
                  AND pp.org_id = current_setting('app.org_id')
                  AND entries.logical_path LIKE pp.path_pattern || '/%'
                  AND pp.role IN ('editor', 'admin')
            )
        )
    );

CREATE POLICY entries_commenter_select ON entries
    FOR SELECT TO kb_commenter
    USING (
        org_id = current_setting('app.org_id')
        AND (
            sensitivity = 'shared'
            OR project_id IN (
                SELECT project_id FROM project_assignments
                WHERE user_id = current_setting('app.user_id')
            )
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

CREATE POLICY entries_viewer_select ON entries
    FOR SELECT TO kb_viewer
    USING (
        org_id = current_setting('app.org_id')
        AND (
            sensitivity NOT IN ('private', 'system')
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

CREATE POLICY entries_agent_select ON entries
    FOR SELECT TO kb_agent
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

-- ===================== ENTRY_LINKS =====================

ALTER TABLE entry_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE entry_links FORCE ROW LEVEL SECURITY;

CREATE POLICY entry_links_select ON entry_links
    FOR SELECT
    TO kb_admin, kb_editor, kb_commenter, kb_viewer, kb_agent
    USING (org_id = current_setting('app.org_id'));

CREATE POLICY entry_links_admin_editor_insert ON entry_links
    FOR INSERT TO kb_admin, kb_editor
    WITH CHECK (org_id = current_setting('app.org_id'));

CREATE POLICY entry_links_admin_editor_update ON entry_links
    FOR UPDATE TO kb_admin, kb_editor
    USING (org_id = current_setting('app.org_id'))
    WITH CHECK (org_id = current_setting('app.org_id'));

CREATE POLICY entry_links_admin_editor_delete ON entry_links
    FOR DELETE TO kb_admin, kb_editor
    USING (org_id = current_setting('app.org_id'));

-- ===================== ENTRY_VERSIONS =====================

ALTER TABLE entry_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE entry_versions FORCE ROW LEVEL SECURITY;

CREATE POLICY entry_versions_select ON entry_versions
    FOR SELECT
    TO kb_admin, kb_editor, kb_commenter, kb_viewer, kb_agent
    USING (org_id = current_setting('app.org_id'));

CREATE POLICY entry_versions_insert ON entry_versions
    FOR INSERT TO kb_admin, kb_editor
    WITH CHECK (org_id = current_setting('app.org_id'));

-- ===================== STAGING =====================

ALTER TABLE staging ENABLE ROW LEVEL SECURITY;
ALTER TABLE staging FORCE ROW LEVEL SECURITY;

CREATE POLICY staging_select_own ON staging
    FOR SELECT TO kb_editor, kb_commenter, kb_viewer, kb_agent
    USING (
        org_id = current_setting('app.org_id')
        AND submitted_by = current_setting('app.user_id')
    );

CREATE POLICY staging_select_admin ON staging
    FOR SELECT TO kb_admin
    USING (org_id = current_setting('app.org_id'));

CREATE POLICY staging_insert ON staging
    FOR INSERT TO kb_admin, kb_editor, kb_commenter, kb_agent
    WITH CHECK (org_id = current_setting('app.org_id'));

CREATE POLICY staging_update_admin ON staging
    FOR UPDATE TO kb_admin
    USING (org_id = current_setting('app.org_id'))
    WITH CHECK (org_id = current_setting('app.org_id'));

CREATE POLICY staging_delete_admin ON staging
    FOR DELETE TO kb_admin
    USING (org_id = current_setting('app.org_id'));

-- ===================== AUDIT_LOG =====================

ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;

CREATE POLICY audit_log_select_admin ON audit_log
    FOR SELECT TO kb_admin
    USING (org_id = current_setting('app.org_id'));

CREATE POLICY audit_log_select_own ON audit_log
    FOR SELECT TO kb_editor, kb_commenter, kb_viewer, kb_agent
    USING (
        org_id = current_setting('app.org_id')
        AND actor_id = current_setting('app.user_id')
    );

CREATE POLICY audit_log_insert ON audit_log
    FOR INSERT TO kb_admin
    WITH CHECK (org_id = current_setting('app.org_id'));

-- ===================== INVITATIONS =====================

ALTER TABLE invitations ENABLE ROW LEVEL SECURITY;
ALTER TABLE invitations FORCE ROW LEVEL SECURITY;

CREATE POLICY invitations_admin_all ON invitations
    TO kb_admin
    USING (org_id = current_setting('app.org_id'))
    WITH CHECK (org_id = current_setting('app.org_id'));

-- ===================== ENTRY_PERMISSIONS =====================

ALTER TABLE entry_permissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE entry_permissions FORCE ROW LEVEL SECURITY;

CREATE POLICY entry_perms_admin_all ON entry_permissions
    TO kb_admin
    USING  (org_id = current_setting('app.org_id'))
    WITH CHECK (org_id = current_setting('app.org_id'));

CREATE POLICY entry_perms_select_own ON entry_permissions
    FOR SELECT TO kb_editor, kb_commenter, kb_viewer, kb_agent
    USING (
        org_id = current_setting('app.org_id')
        AND user_id = current_setting('app.user_id')
    );

CREATE POLICY entry_perms_owner_insert ON entry_permissions
    FOR INSERT TO kb_editor
    WITH CHECK (
        org_id = current_setting('app.org_id')
        AND entry_id IN (
            SELECT id FROM entries
            WHERE owner_id = current_setting('app.user_id')
              AND org_id = current_setting('app.org_id')
        )
    );

CREATE POLICY entry_perms_owner_delete ON entry_permissions
    FOR DELETE TO kb_editor
    USING (
        org_id = current_setting('app.org_id')
        AND entry_id IN (
            SELECT id FROM entries
            WHERE owner_id = current_setting('app.user_id')
              AND org_id = current_setting('app.org_id')
        )
    );

CREATE POLICY entry_perms_owner_select ON entry_permissions
    FOR SELECT TO kb_editor
    USING (
        org_id = current_setting('app.org_id')
        AND entry_id IN (
            SELECT id FROM entries
            WHERE owner_id = current_setting('app.user_id')
              AND org_id = current_setting('app.org_id')
        )
    );

-- ===================== PATH_PERMISSIONS =====================

ALTER TABLE path_permissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE path_permissions FORCE ROW LEVEL SECURITY;

CREATE POLICY path_perms_admin_all ON path_permissions
    TO kb_admin
    USING  (org_id = current_setting('app.org_id'))
    WITH CHECK (org_id = current_setting('app.org_id'));

CREATE POLICY path_perms_select_own ON path_permissions
    FOR SELECT TO kb_editor, kb_commenter, kb_viewer, kb_agent
    USING (
        org_id = current_setting('app.org_id')
        AND user_id = current_setting('app.user_id')
    );

-- ===================== IMPORT_BATCHES =====================

ALTER TABLE import_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE import_batches FORCE ROW LEVEL SECURITY;

CREATE POLICY import_batches_admin_all ON import_batches
    TO kb_admin
    USING (org_id = current_setting('app.org_id'))
    WITH CHECK (org_id = current_setting('app.org_id'));

CREATE POLICY import_batches_editor_select ON import_batches
    FOR SELECT TO kb_editor
    USING (org_id = current_setting('app.org_id'));

CREATE POLICY import_batches_viewer_select ON import_batches
    FOR SELECT TO kb_commenter, kb_viewer, kb_agent
    USING (org_id = current_setting('app.org_id'));


-- #############################################################################
-- SEED DATA
-- #############################################################################

BEGIN;

-- Canonical content types
INSERT INTO content_type_registry (name, description) VALUES
    ('context', 'General organizational context'),
    ('project', 'Project-specific knowledge'),
    ('meeting', 'Meeting notes and summaries'),
    ('decision', 'Decision records and rationale'),
    ('intelligence', 'Market/competitive intelligence'),
    ('daily', 'Daily notes and updates'),
    ('resource', 'Reusable resources and templates'),
    ('department', 'Department-specific knowledge'),
    ('team', 'Team-specific knowledge'),
    ('system', 'System configuration and metadata'),
    ('onboarding', 'Onboarding materials and guides'),
    ('session', 'Session logs and activity records');

-- Common aliases
INSERT INTO content_type_registry (name, description, alias_of) VALUES
    ('note', 'Alias for daily', 'daily'),
    ('notes', 'Alias for daily', 'daily'),
    ('task', 'Alias for project', 'project'),
    ('tasks', 'Alias for project', 'project'),
    ('doc', 'Alias for resource', 'resource'),
    ('docs', 'Alias for resource', 'resource');

-- Demo organization
INSERT INTO organizations (id, name, settings) VALUES
    ('org_demo', 'Demo Organization', '{"governance_tier_default": 2, "max_entries": 10000}');

-- Demo users (one per role)
INSERT INTO users (id, org_id, display_name, email_hash, email, role, department, trust_weight) VALUES
    ('usr_admin',     'org_demo', 'Alice Admin',     encode(digest('alice@demo.org', 'sha256'), 'hex'), 'alice@demo.org',  'admin',     'leadership',  0.95),
    ('usr_editor',    'org_demo', 'Eddie Editor',    encode(digest('eddie@demo.org', 'sha256'), 'hex'), 'eddie@demo.org',  'editor',    'engineering', 0.75),
    ('usr_commenter', 'org_demo', 'Carol Commenter', encode(digest('carol@demo.org', 'sha256'), 'hex'), 'carol@demo.org',  'commenter', 'sales',       0.60),
    ('usr_viewer',    'org_demo', 'Victor Viewer',   encode(digest('victor@demo.org', 'sha256'), 'hex'), 'victor@demo.org', 'viewer',    NULL,          0.50);

-- Demo API keys
-- Plaintext keys (for demo scripts only):
--   Admin:     bkai_adm1_testkey_admin
--   Editor:    bkai_edit_testkey_editor
--   Commenter: bkai_comm_testkey_commenter
--   Viewer:    bkai_view_testkey_viewer
--   Agent:     bkai_agnt_testkey_agent
INSERT INTO api_keys (user_id, org_id, key_hash, key_prefix, key_type, label) VALUES
    ('usr_admin',     'org_demo', crypt('bkai_adm1_testkey_admin',     gen_salt('bf')), 'bkai_adm1', 'interactive',     'Admin interactive key'),
    ('usr_editor',    'org_demo', crypt('bkai_edit_testkey_editor',    gen_salt('bf')), 'bkai_edit', 'interactive',     'Editor interactive key'),
    ('usr_commenter', 'org_demo', crypt('bkai_comm_testkey_commenter', gen_salt('bf')), 'bkai_comm', 'interactive',     'Commenter interactive key'),
    ('usr_viewer',    'org_demo', crypt('bkai_view_testkey_viewer',    gen_salt('bf')), 'bkai_view', 'interactive',     'Viewer interactive key'),
    ('usr_editor',    'org_demo', crypt('bkai_agnt_testkey_agent',     gen_salt('bf')), 'bkai_agnt', 'agent',           'Editor agent key');

-- Demo entries (12 entries, various types/sensitivities)
INSERT INTO entries (id, org_id, title, content, summary, content_hash, content_type, logical_path, sensitivity, department, owner_id, tags, source, created_by, updated_by, status) VALUES
    ('a0000000-0000-0000-0000-000000000001', 'org_demo',
     'Company Mission Statement',
     'Our mission is to democratize institutional knowledge through AI-powered systems that ensure every team member has access to the context they need.',
     'Core mission statement for the organization.',
     md5('Our mission is to democratize institutional knowledge through AI-powered systems that ensure every team member has access to the context they need.'),
     'context', 'Context/mission', 'shared', NULL, 'usr_admin',
     ARRAY['mission', 'core'], 'web_ui', 'usr_admin', 'usr_admin', 'published'),

    ('a0000000-0000-0000-0000-000000000002', 'org_demo',
     'Project Alpha - API Redesign',
     'Project Alpha aims to redesign the core API layer for better performance and scalability. Target: Q3 launch with 10x throughput improvement.',
     'API redesign project targeting Q3 launch.',
     md5('Project Alpha aims to redesign the core API layer for better performance and scalability. Target: Q3 launch with 10x throughput improvement.'),
     'project', 'Projects/alpha', 'project', 'engineering', 'usr_editor',
     ARRAY['api', 'performance', 'q3'], 'web_ui', 'usr_editor', 'usr_editor', 'published'),

    ('a0000000-0000-0000-0000-000000000003', 'org_demo',
     'Weekly Standup 2026-04-01',
     'Attendees: Alice, Eddie, Carol. Topics: API progress at 60%, sales pipeline review, hiring update. Action items: Eddie to finalize schema by Friday.',
     'Weekly standup covering API progress and sales pipeline.',
     md5('Attendees: Alice, Eddie, Carol. Topics: API progress at 60%, sales pipeline review, hiring update. Action items: Eddie to finalize schema by Friday.'),
     'meeting', 'Meetings/standup/2026-04-01', 'meeting', NULL, 'usr_admin',
     ARRAY['standup', 'weekly'], 'web_ui', 'usr_admin', 'usr_admin', 'published'),

    ('a0000000-0000-0000-0000-000000000004', 'org_demo',
     'Decision: Multi-tenant Architecture',
     'After evaluating options, we decided on row-level multi-tenancy with org_id + RLS over per-client instances. Rationale: lower ops cost, simpler deployment, good enough isolation for our threat model.',
     'Decided on row-level multi-tenancy over per-client instances.',
     md5('After evaluating options, we decided on row-level multi-tenancy with org_id + RLS over per-client instances. Rationale: lower ops cost, simpler deployment, good enough isolation for our threat model.'),
     'decision', 'Decisions/architecture/multi-tenant', 'strategic', 'leadership', 'usr_admin',
     ARRAY['architecture', 'multi-tenant', 'rls'], 'web_ui', 'usr_admin', 'usr_admin', 'published'),

    ('a0000000-0000-0000-0000-000000000005', 'org_demo',
     'Competitive Intel: Acme Corp Pricing',
     'Acme Corp has moved to usage-based pricing at $0.10/query. Their enterprise tier starts at $2K/mo. Key differentiator: they lack multi-agent support.',
     'Competitive pricing intel on Acme Corp.',
     md5('Acme Corp has moved to usage-based pricing at $0.10/query. Their enterprise tier starts at $2K/mo. Key differentiator: they lack multi-agent support.'),
     'intelligence', 'Intelligence/competitive/acme', 'operational', 'sales', 'usr_commenter',
     ARRAY['competitive', 'pricing', 'acme'], 'agent', 'usr_commenter', 'usr_commenter', 'published'),

    ('a0000000-0000-0000-0000-000000000006', 'org_demo',
     'Personal Research Notes',
     'Looking into CRDT-based sync as alternative to Git for real-time collaboration. Papers to read: Automerge, Yjs internals.',
     'Personal notes on CRDT research.',
     md5('Looking into CRDT-based sync as alternative to Git for real-time collaboration. Papers to read: Automerge, Yjs internals.'),
     'resource', 'Resources/research/crdt-notes', 'private', 'engineering', 'usr_editor',
     ARRAY['crdt', 'research', 'sync'], 'web_ui', 'usr_editor', 'usr_editor', 'draft'),

    ('a0000000-0000-0000-0000-000000000007', 'org_demo',
     'System: RLS Policy Definitions',
     'Row-level security policies are defined in 004_rls.sql. Admin sees all, viewer excludes private/system, agent writes go to staging.',
     'System documentation for RLS policies.',
     md5('Row-level security policies are defined in 004_rls.sql. Admin sees all, viewer excludes private/system, agent writes go to staging.'),
     'system', 'System/rls-policies', 'system', NULL, 'usr_admin',
     ARRAY['system', 'rls', 'security'], 'web_ui', 'usr_admin', 'usr_admin', 'published'),

    ('a0000000-0000-0000-0000-000000000008', 'org_demo',
     'New Employee Onboarding Guide',
     'Welcome! Start by reading the mission statement, then review your department folder. Set up your API key through the GUI. Contact your manager for project assignments.',
     'Step-by-step onboarding for new employees.',
     md5('Welcome! Start by reading the mission statement, then review your department folder. Set up your API key through the GUI. Contact your manager for project assignments.'),
     'onboarding', 'Onboarding/new-employee', 'shared', NULL, 'usr_admin',
     ARRAY['onboarding', 'getting-started'], 'web_ui', 'usr_admin', 'usr_admin', 'published'),

    ('a0000000-0000-0000-0000-000000000009', 'org_demo',
     'API Authentication Spec',
     'All API requests require Bearer token in Authorization header. Tokens are validated by prefix lookup + bcrypt verify. Rate limits: 100 req/min interactive, 500 req/min agent.',
     'API authentication specification and rate limits.',
     md5('All API requests require Bearer token in Authorization header. Tokens are validated by prefix lookup + bcrypt verify. Rate limits: 100 req/min interactive, 500 req/min agent.'),
     'resource', 'Resources/engineering/auth-spec', 'operational', 'engineering', 'usr_editor',
     ARRAY['api', 'auth', 'spec'], 'web_ui', 'usr_editor', 'usr_editor', 'published'),

    ('a0000000-0000-0000-0000-00000000000a', 'org_demo',
     'Market Research: KB Tools Landscape',
     'Analysis of 15 knowledge base tools shows convergence on AI-augmented search. Key trends: vector embeddings standard, graph relationships emerging, multi-agent collaboration rare.',
     'Market landscape analysis of KB tools.',
     md5('Analysis of 15 knowledge base tools shows convergence on AI-augmented search. Key trends: vector embeddings standard, graph relationships emerging, multi-agent collaboration rare.'),
     'intelligence', 'Intelligence/market/kb-tools', 'shared', NULL, 'usr_editor',
     ARRAY['market-research', 'kb', 'ai'], 'agent', 'usr_editor', 'usr_editor', 'published'),

    ('a0000000-0000-0000-0000-00000000000b', 'org_demo',
     'Sales Daily Brief 2026-04-03',
     'Pipeline: 3 new leads, 2 demos scheduled. Follow-up needed: TechCorp (sent proposal), DataSoft (awaiting budget approval). Revenue forecast on track.',
     'Daily sales pipeline update.',
     md5('Pipeline: 3 new leads, 2 demos scheduled. Follow-up needed: TechCorp (sent proposal), DataSoft (awaiting budget approval). Revenue forecast on track.'),
     'daily', 'Daily/sales/2026-04-03', 'operational', 'sales', 'usr_commenter',
     ARRAY['sales', 'daily', 'pipeline'], 'web_ui', 'usr_commenter', 'usr_commenter', 'published'),

    ('a0000000-0000-0000-0000-00000000000c', 'org_demo',
     'Q3 Strategic Priorities',
     'Three priorities for Q3: (1) Launch multi-tenant platform, (2) Close 5 enterprise deals, (3) Hire 3 engineers. Budget allocated: $150K for infrastructure.',
     'Q3 strategic priorities and budget.',
     md5('Three priorities for Q3: (1) Launch multi-tenant platform, (2) Close 5 enterprise deals, (3) Hire 3 engineers. Budget allocated: $150K for infrastructure.'),
     'context', 'Context/strategy/q3-priorities', 'strategic', 'leadership', 'usr_admin',
     ARRAY['strategy', 'q3', 'priorities'], 'web_ui', 'usr_admin', 'usr_admin', 'published');

-- Demo entry links
INSERT INTO entry_links (org_id, source_entry_id, target_entry_id, link_type, weight, created_by, source) VALUES
    ('org_demo', 'a0000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000008', 'relates_to', 0.90, 'usr_admin', 'web_ui'),
    ('org_demo', 'a0000000-0000-0000-0000-000000000002', 'a0000000-0000-0000-0000-000000000009', 'depends_on', 0.95, 'usr_editor', 'web_ui'),
    ('org_demo', 'a0000000-0000-0000-0000-000000000004', 'a0000000-0000-0000-0000-000000000002', 'relates_to', 0.85, 'usr_admin', 'web_ui'),
    ('org_demo', 'a0000000-0000-0000-0000-00000000000a', 'a0000000-0000-0000-0000-000000000005', 'relates_to', 0.80, 'usr_editor', 'agent'),
    ('org_demo', 'a0000000-0000-0000-0000-00000000000c', 'a0000000-0000-0000-0000-000000000001', 'part_of', 0.70, 'usr_admin', 'web_ui'),
    ('org_demo', 'a0000000-0000-0000-0000-000000000003', 'a0000000-0000-0000-0000-000000000002', 'tagged_with', 0.60, 'usr_admin', 'web_ui');

-- Demo entry versions
INSERT INTO entry_versions (entry_id, org_id, version, title, content, content_hash, tags, status, changed_by, source, change_summary, governance_action) VALUES
    ('a0000000-0000-0000-0000-000000000001', 'org_demo', 1,
     'Company Mission Statement',
     'Our mission is to build the best knowledge base platform.',
     md5('Our mission is to build the best knowledge base platform.'),
     ARRAY['mission'], 'published', 'usr_admin', 'web_ui',
     'Initial creation of mission statement', 'created'),
    ('a0000000-0000-0000-0000-000000000009', 'org_demo', 1,
     'API Authentication Spec',
     'API requests require Bearer token. Tokens validated by bcrypt. No rate limits yet.',
     md5('API requests require Bearer token. Tokens validated by bcrypt. No rate limits yet.'),
     ARRAY['api', 'auth'], 'published', 'usr_editor', 'web_ui',
     'Initial auth spec before rate limits added', 'created');

-- Demo staging items
INSERT INTO staging (org_id, target_entry_id, target_path, change_type, proposed_title, proposed_content, proposed_meta, content_hash, submitted_by, source, governance_tier, submission_category, status, priority) VALUES
    ('org_demo', 'a0000000-0000-0000-0000-00000000000a',
     'Intelligence/market/kb-tools', 'update',
     'Market Research: KB Tools Landscape (Updated)',
     'Updated analysis of 20 knowledge base tools. New entrant: CortexDB with graph-native approach. Vector search now table stakes.',
     '{"update_reason": "quarterly refresh"}',
     md5('Updated analysis of 20 knowledge base tools. New entrant: CortexDB with graph-native approach. Vector search now table stakes.'),
     'usr_editor', 'agent', 2, 'teaching_loop', 'pending', 2),
    ('org_demo', NULL,
     'Resources/sales/playbook-enterprise', 'create',
     'Enterprise Sales Playbook',
     'Step 1: Identify decision maker. Step 2: Schedule discovery call. Step 3: Present ROI analysis. Step 4: Pilot proposal.',
     '{"category": "sales_enablement"}',
     md5('Step 1: Identify decision maker. Step 2: Schedule discovery call. Step 3: Present ROI analysis. Step 4: Pilot proposal.'),
     'usr_commenter', 'web_ui', 1, 'user_direct', 'approved', 3);

-- Demo audit log
INSERT INTO audit_log (org_id, actor_id, actor_role, source, action, target_table, target_id, target_path, change_summary) VALUES
    ('org_demo', 'usr_admin', 'admin', 'web_ui', 'create', 'entries', 'a0000000-0000-0000-0000-000000000001', 'Context/mission', 'Created mission statement'),
    ('org_demo', 'usr_editor', 'editor', 'web_ui', 'update', 'entries', 'a0000000-0000-0000-0000-000000000009', 'Resources/engineering/auth-spec', 'Added rate limits to auth spec'),
    ('org_demo', 'usr_editor', 'agent', 'agent', 'create', 'staging', NULL, 'Intelligence/market/kb-tools', 'Agent submitted market research update for review');

-- Demo project assignment
INSERT INTO project_assignments (user_id, project_id, org_id, assigned_at, assigned_by) VALUES
    ('usr_commenter', 'a0000000-0000-0000-0000-000000000002', 'org_demo', now(), 'usr_admin');

COMMIT;
