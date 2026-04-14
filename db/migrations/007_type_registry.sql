-- 007_type_registry.sql
-- Content type registry: canonical types as database rows with alias support

CREATE TABLE content_type_registry (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    alias_of    TEXT REFERENCES content_type_registry(name),
    is_active   BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed with the 11 canonical types
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
    ('onboarding', 'Onboarding materials and guides');

-- Common aliases
INSERT INTO content_type_registry (name, description, alias_of) VALUES
    ('note', 'Alias for daily', 'daily'),
    ('notes', 'Alias for daily', 'daily'),
    ('task', 'Alias for project', 'project'),
    ('tasks', 'Alias for project', 'project'),
    ('doc', 'Alias for resource', 'resource'),
    ('docs', 'Alias for resource', 'resource');

-- Grant SELECT to all KB roles so validation queries work under RLS
GRANT SELECT ON content_type_registry TO kb_admin, kb_editor, kb_commenter, kb_viewer, kb_agent;
-- Only admin can manage types
GRANT INSERT, UPDATE, DELETE ON content_type_registry TO kb_admin;
