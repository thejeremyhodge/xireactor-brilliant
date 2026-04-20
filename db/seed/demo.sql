-- demo.sql
-- Opt-in demo seed data: 1 org (shared with admin bootstrap), 4 demo users,
-- 5 API keys, 12 entries, links, versions, staging, audit.
--
-- This file is NOT in db/migrations/ and does NOT auto-run on a clean
-- install. Opt in via `install.sh --seed-demo` (which applies this script
-- via `docker exec -i brilliant-db psql … < db/seed/demo.sql` after the
-- stack is healthy). Remove at any time with:
--
--   python tools/remove_demo_data.py --yes
--
-- Every entry row carries the `demo:seed` tag so the removal script can
-- identify and delete demo-only rows without touching real content.
-- Non-entry rows (api_keys, users, staging, audit_log, project_assignments)
-- are identified by their stable demo IDs (`usr_admin`, `usr_editor`,
-- `usr_commenter`, `usr_viewer`) — real admin-bootstrap users have UUID
-- ids, so the two populations do not collide.
--
-- Plaintext API keys (for demo scripts):
--   Admin:     bkai_adm1_testkey_admin
--   Editor:    bkai_edit_testkey_editor
--   Commenter: bkai_comm_testkey_commenter
--   Viewer:    bkai_view_testkey_viewer
--   Agent:     bkai_agnt_testkey_agent
--
-- Idempotent-ish: inserts use explicit IDs, so re-running will raise a
-- unique-constraint error. Run `tools/remove_demo_data.py --yes` first
-- if you need to re-seed.
--
-- Depends on: 001_core.sql, 002_relationships.sql, 003_governance.sql,
--             004_rls.sql, and the admin-bootstrap'd organizations row.

BEGIN;

-- =============================================================================
-- ORGANIZATION
-- =============================================================================
-- On a fresh install the admin-bootstrap flow creates org_demo with the
-- operator-chosen name. If it hasn't run yet (pure --seed-demo with no
-- /setup call first), insert a baseline row. ON CONFLICT DO NOTHING so we
-- don't clobber an operator-chosen name.

INSERT INTO organizations (id, name, settings) VALUES
    ('org_demo', 'Demo Organization', '{"governance_tier_default": 2, "max_entries": 10000}')
ON CONFLICT (id) DO NOTHING;

-- =============================================================================
-- USERS (one per role)
-- =============================================================================

INSERT INTO users (id, org_id, display_name, email_hash, role, department, trust_weight) VALUES
    ('usr_admin',     'org_demo', 'Alice Admin',     encode(digest('alice@demo.org', 'sha256'), 'hex'), 'admin',     'leadership',  0.95),
    ('usr_editor',    'org_demo', 'Eddie Editor',    encode(digest('eddie@demo.org', 'sha256'), 'hex'), 'editor',    'engineering', 0.75),
    ('usr_commenter', 'org_demo', 'Carol Commenter', encode(digest('carol@demo.org', 'sha256'), 'hex'), 'commenter', 'sales',       0.60),
    ('usr_viewer',    'org_demo', 'Victor Viewer',   encode(digest('victor@demo.org', 'sha256'), 'hex'), 'viewer',    NULL,          0.50);

-- Migration 013_user_auth.sql back-fills emails when id matches + email IS NULL.
-- Run that logic inline here so --seed-demo works regardless of migration order.
UPDATE users SET email = 'alice@demo.org'  WHERE id = 'usr_admin'     AND email IS NULL;
UPDATE users SET email = 'eddie@demo.org'  WHERE id = 'usr_editor'    AND email IS NULL;
UPDATE users SET email = 'carol@demo.org'  WHERE id = 'usr_commenter' AND email IS NULL;
UPDATE users SET email = 'victor@demo.org' WHERE id = 'usr_viewer'    AND email IS NULL;

-- =============================================================================
-- API KEYS (4 interactive + 1 agent)
-- =============================================================================

INSERT INTO api_keys (user_id, org_id, key_hash, key_prefix, key_type, label) VALUES
    ('usr_admin',     'org_demo', crypt('bkai_adm1_testkey_admin',     gen_salt('bf')), 'bkai_adm1', 'interactive',     'Admin interactive key'),
    ('usr_editor',    'org_demo', crypt('bkai_edit_testkey_editor',    gen_salt('bf')), 'bkai_edit', 'interactive',     'Editor interactive key'),
    ('usr_commenter', 'org_demo', crypt('bkai_comm_testkey_commenter', gen_salt('bf')), 'bkai_comm', 'interactive',     'Commenter interactive key'),
    ('usr_viewer',    'org_demo', crypt('bkai_view_testkey_viewer',    gen_salt('bf')), 'bkai_view', 'interactive',     'Viewer interactive key'),
    ('usr_editor',    'org_demo', crypt('bkai_agnt_testkey_agent',     gen_salt('bf')), 'bkai_agnt', 'agent',           'Editor agent key');

-- =============================================================================
-- ENTRIES (12 entries, various types/sensitivities/departments)
-- Every entry carries the `demo:seed` tag for one-shot cleanup via
-- tools/remove_demo_data.py --yes.
-- =============================================================================

INSERT INTO entries (id, org_id, title, content, summary, content_hash, content_type, logical_path, sensitivity, department, owner_id, tags, source, created_by, updated_by, status) VALUES

-- 1. Shared context entry (visible to everyone)
('a0000000-0000-0000-0000-000000000001', 'org_demo',
 'Company Mission Statement',
 'Our mission is to democratize institutional knowledge through AI-powered systems that ensure every team member has access to the context they need.',
 'Core mission statement for the organization.',
 md5('Our mission is to democratize institutional knowledge through AI-powered systems that ensure every team member has access to the context they need.'),
 'context', 'Context/mission', 'shared', NULL, 'usr_admin',
 ARRAY['mission', 'core', 'demo:seed'], 'web_ui', 'usr_admin', 'usr_admin', 'published'),

-- 2. Engineering project entry
('a0000000-0000-0000-0000-000000000002', 'org_demo',
 'Project Alpha - API Redesign',
 'Project Alpha aims to redesign the core API layer for better performance and scalability. Target: Q3 launch with 10x throughput improvement.',
 'API redesign project targeting Q3 launch.',
 md5('Project Alpha aims to redesign the core API layer for better performance and scalability. Target: Q3 launch with 10x throughput improvement.'),
 'project', 'Projects/alpha', 'project', 'engineering', 'usr_editor',
 ARRAY['api', 'performance', 'q3', 'demo:seed'], 'web_ui', 'usr_editor', 'usr_editor', 'published'),

-- 3. Meeting notes
('a0000000-0000-0000-0000-000000000003', 'org_demo',
 'Weekly Standup 2026-04-01',
 'Attendees: Alice, Eddie, Carol. Topics: API progress at 60%, sales pipeline review, hiring update. Action items: Eddie to finalize schema by Friday.',
 'Weekly standup covering API progress and sales pipeline.',
 md5('Attendees: Alice, Eddie, Carol. Topics: API progress at 60%, sales pipeline review, hiring update. Action items: Eddie to finalize schema by Friday.'),
 'meeting', 'Meetings/standup/2026-04-01', 'meeting', NULL, 'usr_admin',
 ARRAY['standup', 'weekly', 'demo:seed'], 'web_ui', 'usr_admin', 'usr_admin', 'published'),

-- 4. Strategic decision (admin/leadership only)
('a0000000-0000-0000-0000-000000000004', 'org_demo',
 'Decision: Multi-tenant Architecture',
 'After evaluating options, we decided on row-level multi-tenancy with org_id + RLS over per-client instances. Rationale: lower ops cost, simpler deployment, good enough isolation for our threat model.',
 'Decided on row-level multi-tenancy over per-client instances.',
 md5('After evaluating options, we decided on row-level multi-tenancy with org_id + RLS over per-client instances. Rationale: lower ops cost, simpler deployment, good enough isolation for our threat model.'),
 'decision', 'Decisions/architecture/multi-tenant', 'strategic', 'leadership', 'usr_admin',
 ARRAY['architecture', 'multi-tenant', 'rls', 'demo:seed'], 'web_ui', 'usr_admin', 'usr_admin', 'published'),

-- 5. Sales intelligence (operational, sales dept)
('a0000000-0000-0000-0000-000000000005', 'org_demo',
 'Competitive Intel: Acme Corp Pricing',
 'Acme Corp has moved to usage-based pricing at $0.10/query. Their enterprise tier starts at $2K/mo. Key differentiator: they lack multi-agent support.',
 'Competitive pricing intel on Acme Corp.',
 md5('Acme Corp has moved to usage-based pricing at $0.10/query. Their enterprise tier starts at $2K/mo. Key differentiator: they lack multi-agent support.'),
 'intelligence', 'Intelligence/competitive/acme', 'operational', 'sales', 'usr_commenter',
 ARRAY['competitive', 'pricing', 'acme', 'demo:seed'], 'agent', 'usr_commenter', 'usr_commenter', 'published'),

-- 6. Private note (only owner sees)
('a0000000-0000-0000-0000-000000000006', 'org_demo',
 'Personal Research Notes',
 'Looking into CRDT-based sync as alternative to Git for real-time collaboration. Papers to read: Automerge, Yjs internals.',
 'Personal notes on CRDT research.',
 md5('Looking into CRDT-based sync as alternative to Git for real-time collaboration. Papers to read: Automerge, Yjs internals.'),
 'resource', 'Resources/research/crdt-notes', 'private', 'engineering', 'usr_editor',
 ARRAY['crdt', 'research', 'sync', 'demo:seed'], 'web_ui', 'usr_editor', 'usr_editor', 'draft'),

-- 7. System entry (only admin sees)
('a0000000-0000-0000-0000-000000000007', 'org_demo',
 'System: RLS Policy Definitions',
 'Row-level security policies are defined in 004_rls.sql. Admin sees all, viewer excludes private/system, agent writes go to staging.',
 'System documentation for RLS policies.',
 md5('Row-level security policies are defined in 004_rls.sql. Admin sees all, viewer excludes private/system, agent writes go to staging.'),
 'system', 'System/rls-policies', 'system', NULL, 'usr_admin',
 ARRAY['system', 'rls', 'security', 'demo:seed'], 'web_ui', 'usr_admin', 'usr_admin', 'published'),

-- 8. Onboarding guide (shared)
('a0000000-0000-0000-0000-000000000008', 'org_demo',
 'New Employee Onboarding Guide',
 'Welcome! Start by reading the mission statement, then review your department folder. Set up your API key through the GUI. Contact your manager for project assignments.',
 'Step-by-step onboarding for new employees.',
 md5('Welcome! Start by reading the mission statement, then review your department folder. Set up your API key through the GUI. Contact your manager for project assignments.'),
 'onboarding', 'Onboarding/new-employee', 'shared', NULL, 'usr_admin',
 ARRAY['onboarding', 'getting-started', 'demo:seed'], 'web_ui', 'usr_admin', 'usr_admin', 'published'),

-- 9. Engineering resource (operational)
('a0000000-0000-0000-0000-000000000009', 'org_demo',
 'API Authentication Spec',
 'All API requests require Bearer token in Authorization header. Tokens are validated by prefix lookup + bcrypt verify. Rate limits: 100 req/min interactive, 500 req/min agent.',
 'API authentication specification and rate limits.',
 md5('All API requests require Bearer token in Authorization header. Tokens are validated by prefix lookup + bcrypt verify. Rate limits: 100 req/min interactive, 500 req/min agent.'),
 'resource', 'Resources/engineering/auth-spec', 'operational', 'engineering', 'usr_editor',
 ARRAY['api', 'auth', 'spec', 'demo:seed'], 'web_ui', 'usr_editor', 'usr_editor', 'published'),

-- 10. Agent-created intelligence entry
('a0000000-0000-0000-0000-00000000000a', 'org_demo',
 'Market Research: KB Tools Landscape',
 'Analysis of 15 knowledge base tools shows convergence on AI-augmented search. Key trends: vector embeddings standard, graph relationships emerging, multi-agent collaboration rare.',
 'Market landscape analysis of KB tools.',
 md5('Analysis of 15 knowledge base tools shows convergence on AI-augmented search. Key trends: vector embeddings standard, graph relationships emerging, multi-agent collaboration rare.'),
 'intelligence', 'Intelligence/market/kb-tools', 'shared', NULL, 'usr_editor',
 ARRAY['market-research', 'kb', 'ai', 'demo:seed'], 'agent', 'usr_editor', 'usr_editor', 'published'),

-- 11. Sales department daily context
('a0000000-0000-0000-0000-00000000000b', 'org_demo',
 'Sales Daily Brief 2026-04-03',
 'Pipeline: 3 new leads, 2 demos scheduled. Follow-up needed: TechCorp (sent proposal), DataSoft (awaiting budget approval). Revenue forecast on track.',
 'Daily sales pipeline update.',
 md5('Pipeline: 3 new leads, 2 demos scheduled. Follow-up needed: TechCorp (sent proposal), DataSoft (awaiting budget approval). Revenue forecast on track.'),
 'daily', 'Daily/sales/2026-04-03', 'operational', 'sales', 'usr_commenter',
 ARRAY['sales', 'daily', 'pipeline', 'demo:seed'], 'web_ui', 'usr_commenter', 'usr_commenter', 'published'),

-- 12. Leadership strategic entry
('a0000000-0000-0000-0000-00000000000c', 'org_demo',
 'Q3 Strategic Priorities',
 'Three priorities for Q3: (1) Launch multi-tenant platform, (2) Close 5 enterprise deals, (3) Hire 3 engineers. Budget allocated: $150K for infrastructure.',
 'Q3 strategic priorities and budget.',
 md5('Three priorities for Q3: (1) Launch multi-tenant platform, (2) Close 5 enterprise deals, (3) Hire 3 engineers. Budget allocated: $150K for infrastructure.'),
 'context', 'Context/strategy/q3-priorities', 'strategic', 'leadership', 'usr_admin',
 ARRAY['strategy', 'q3', 'priorities', 'demo:seed'], 'web_ui', 'usr_admin', 'usr_admin', 'published');

-- =============================================================================
-- ENTRY LINKS (6 links connecting entries)
-- =============================================================================

INSERT INTO entry_links (org_id, source_entry_id, target_entry_id, link_type, weight, created_by, source) VALUES
    -- Mission statement relates to onboarding guide
    ('org_demo', 'a0000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000008', 'relates_to', 0.90, 'usr_admin', 'web_ui'),
    -- Project Alpha depends on API auth spec
    ('org_demo', 'a0000000-0000-0000-0000-000000000002', 'a0000000-0000-0000-0000-000000000009', 'depends_on', 0.95, 'usr_editor', 'web_ui'),
    -- Multi-tenant decision relates to Project Alpha
    ('org_demo', 'a0000000-0000-0000-0000-000000000004', 'a0000000-0000-0000-0000-000000000002', 'relates_to', 0.85, 'usr_admin', 'web_ui'),
    -- Market research relates to competitive intel
    ('org_demo', 'a0000000-0000-0000-0000-00000000000a', 'a0000000-0000-0000-0000-000000000005', 'relates_to', 0.80, 'usr_editor', 'agent'),
    -- Q3 priorities part_of mission statement
    ('org_demo', 'a0000000-0000-0000-0000-00000000000c', 'a0000000-0000-0000-0000-000000000001', 'part_of', 0.70, 'usr_admin', 'web_ui'),
    -- Standup meeting tagged_with Project Alpha
    ('org_demo', 'a0000000-0000-0000-0000-000000000003', 'a0000000-0000-0000-0000-000000000002', 'tagged_with', 0.60, 'usr_admin', 'web_ui');

-- =============================================================================
-- ENTRY VERSIONS (2 version history entries)
-- =============================================================================

INSERT INTO entry_versions (entry_id, org_id, version, title, content, content_hash, tags, status, changed_by, source, change_summary, governance_action) VALUES
    -- Version 1 of mission statement (original)
    ('a0000000-0000-0000-0000-000000000001', 'org_demo', 1,
     'Company Mission Statement',
     'Our mission is to build the best knowledge base platform.',
     md5('Our mission is to build the best knowledge base platform.'),
     ARRAY['mission', 'demo:seed'], 'published', 'usr_admin', 'web_ui',
     'Initial creation of mission statement', 'created'),
    -- Version 1 of API auth spec (before update to v2)
    ('a0000000-0000-0000-0000-000000000009', 'org_demo', 1,
     'API Authentication Spec',
     'API requests require Bearer token. Tokens validated by bcrypt. No rate limits yet.',
     md5('API requests require Bearer token. Tokens validated by bcrypt. No rate limits yet.'),
     ARRAY['api', 'auth', 'demo:seed'], 'published', 'usr_editor', 'web_ui',
     'Initial auth spec before rate limits added', 'created');

-- =============================================================================
-- STAGING (2 items: one pending, one approved)
-- =============================================================================

INSERT INTO staging (org_id, target_entry_id, target_path, change_type, proposed_title, proposed_content, proposed_meta, content_hash, submitted_by, source, governance_tier, submission_category, status, priority) VALUES
    -- Pending: agent proposes update to market research
    ('org_demo', 'a0000000-0000-0000-0000-00000000000a',
     'Intelligence/market/kb-tools', 'update',
     'Market Research: KB Tools Landscape (Updated)',
     'Updated analysis of 20 knowledge base tools. New entrant: CortexDB with graph-native approach. Vector search now table stakes.',
     '{"update_reason": "quarterly refresh", "demo_seed": true}',
     md5('Updated analysis of 20 knowledge base tools. New entrant: CortexDB with graph-native approach. Vector search now table stakes.'),
     'usr_editor', 'agent', 2, 'teaching_loop', 'pending', 2),
    -- Approved: commenter proposed new sales playbook
    ('org_demo', NULL,
     'Resources/sales/playbook-enterprise', 'create',
     'Enterprise Sales Playbook',
     'Step 1: Identify decision maker. Step 2: Schedule discovery call. Step 3: Present ROI analysis. Step 4: Pilot proposal.',
     '{"category": "sales_enablement", "demo_seed": true}',
     md5('Step 1: Identify decision maker. Step 2: Schedule discovery call. Step 3: Present ROI analysis. Step 4: Pilot proposal.'),
     'usr_commenter', 'web_ui', 1, 'user_direct', 'approved', 3);

-- =============================================================================
-- AUDIT LOG (3 entries)
-- =============================================================================

INSERT INTO audit_log (org_id, actor_id, actor_role, source, action, target_table, target_id, target_path, change_summary) VALUES
    ('org_demo', 'usr_admin', 'admin', 'web_ui', 'create', 'entries', 'a0000000-0000-0000-0000-000000000001', 'Context/mission', 'Created mission statement'),
    ('org_demo', 'usr_editor', 'editor', 'web_ui', 'update', 'entries', 'a0000000-0000-0000-0000-000000000009', 'Resources/engineering/auth-spec', 'Added rate limits to auth spec'),
    ('org_demo', 'usr_editor', 'agent', 'agent', 'create', 'staging', NULL, 'Intelligence/market/kb-tools', 'Agent submitted market research update for review');

-- =============================================================================
-- PROJECT ASSIGNMENT (1 assignment: commenter assigned to Project Alpha)
-- =============================================================================

INSERT INTO project_assignments (user_id, project_id, org_id, assigned_at, assigned_by) VALUES
    ('usr_commenter', 'a0000000-0000-0000-0000-000000000002', 'org_demo', now(), 'usr_admin');

COMMIT;
