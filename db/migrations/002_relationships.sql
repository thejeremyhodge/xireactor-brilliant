-- 002_relationships.sql
-- Entry links (typed wiki-links for CTE traversal), entry versions (append-only history),
-- and project assignments (user-to-project access grants).
--
-- Depends on: 001_core.sql (organizations, users, entries)

-- =============================================================================
-- ENTRY LINKS — Wiki-link relationships (replaces AGE graph, CTE-first approach)
-- =============================================================================

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
    created_by      TEXT NOT NULL REFERENCES users(id),
    source          TEXT NOT NULL CHECK (source IN ('web_ui', 'agent', 'api')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Prevent duplicate links of same type between same entries
    UNIQUE (org_id, source_entry_id, target_entry_id, link_type)
);

CREATE INDEX idx_links_source ON entry_links (source_entry_id);
CREATE INDEX idx_links_target ON entry_links (target_entry_id);
CREATE INDEX idx_links_org_type ON entry_links (org_id, link_type);


-- =============================================================================
-- ENTRY VERSIONS — Append-only version history
-- =============================================================================

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


-- =============================================================================
-- PROJECT ASSIGNMENTS — User-to-project access grants (for RLS scoping)
-- =============================================================================

CREATE TABLE project_assignments (
    user_id     TEXT NOT NULL REFERENCES users(id),
    project_id  UUID NOT NULL,
    org_id      TEXT NOT NULL REFERENCES organizations(id),
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    assigned_by TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY (user_id, project_id)
);

CREATE INDEX idx_assignments_project ON project_assignments (project_id);
