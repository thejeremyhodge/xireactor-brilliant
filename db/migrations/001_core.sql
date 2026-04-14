-- 001_core.sql
-- Core schema: extensions, organizations, users, api_keys, entries
-- Roles use Google Workspace model: admin, editor, commenter, viewer

-- ============================================================
-- Extensions
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- gen_random_uuid(), crypt()
CREATE EXTENSION IF NOT EXISTS "vector";     -- pgvector for semantic search

-- ============================================================
-- Organizations
-- ============================================================

CREATE TABLE organizations (
    id          TEXT PRIMARY KEY,                  -- e.g. 'org_acme'
    name        TEXT NOT NULL,
    settings    JSONB NOT NULL DEFAULT '{}',       -- evaluator thresholds, org-level config
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Users (Google Workspace role model)
-- ============================================================

CREATE TABLE users (
    id              TEXT PRIMARY KEY,              -- e.g. 'usr_7f3a2e91b4c08d15'
    org_id          TEXT NOT NULL REFERENCES organizations(id),
    display_name    TEXT NOT NULL,
    email_hash      TEXT NOT NULL,                 -- SHA-256 of email (dedup, no PII)
    role            TEXT NOT NULL CHECK (role IN (
                        'admin', 'editor', 'commenter', 'viewer'
                    )),
    department      TEXT,                          -- NULL for cross-department roles
    trust_weight    NUMERIC(3,2) NOT NULL DEFAULT 0.50
                        CHECK (trust_weight BETWEEN 0 AND 1),
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_users_org ON users (org_id);

-- ============================================================
-- API Keys
-- ============================================================

CREATE TABLE api_keys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT NOT NULL REFERENCES users(id),
    org_id      TEXT NOT NULL REFERENCES organizations(id),
    key_hash    TEXT NOT NULL,                     -- bcrypt hash of the full key
    key_prefix  TEXT NOT NULL,                     -- first 8 chars for display (e.g. 'bkai_7f3a')
    key_type    TEXT NOT NULL CHECK (key_type IN (
                    'interactive', 'agent', 'api_integration'
                )),
    label       TEXT,                              -- user-assigned name
    expires_at  TIMESTAMPTZ,                       -- NULL = no expiry
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
    content         TEXT NOT NULL,                 -- full markdown body
    summary         TEXT,                          -- agent-generated, ~1 sentence
    content_hash    TEXT NOT NULL,                 -- SHA-256 for dedup/conflict detection

    -- Classification
    content_type    TEXT NOT NULL CHECK (content_type IN (
                        'context', 'project', 'meeting', 'decision', 'intelligence',
                        'daily', 'resource', 'department', 'team', 'system', 'onboarding'
                    )),
    logical_path    TEXT NOT NULL,                 -- e.g. 'Context/strategy', 'Projects/alpha/sprint-1'
    sensitivity     TEXT NOT NULL DEFAULT 'shared' CHECK (sensitivity IN (
                        'system', 'strategic', 'operational', 'private',
                        'project', 'meeting', 'shared'
                    )),
    department      TEXT,                          -- NULL for cross-department content
    owner_id        TEXT REFERENCES users(id),     -- primary owner/maintainer
    project_id      UUID,                          -- self-ref to a project-type entry if applicable

    -- Two-layer metadata
    tags            TEXT[] NOT NULL DEFAULT '{}',  -- system-level tags, GIN-indexed
    domain_meta     JSONB NOT NULL DEFAULT '{}',   -- org-specific fields

    -- Versioning
    version         INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'published' CHECK (status IN (
                        'draft', 'published', 'archived', 'superseded'
                    )),

    -- Audit
    created_by      TEXT NOT NULL REFERENCES users(id),
    updated_by      TEXT NOT NULL REFERENCES users(id),
    source          TEXT NOT NULL CHECK (source IN ('web_ui', 'agent', 'api')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Search
    search_vector   TSVECTOR,                     -- populated by trigger
    embedding       vector(1536),                 -- for semantic search
    word_count      INTEGER,

    -- Constraints
    UNIQUE (org_id, logical_path)
);

-- Indexes
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
