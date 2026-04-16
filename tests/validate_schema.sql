-- validate_schema.sql
-- Validation script for RLS enforcement, CTE traversal, and full-text search.
-- Run after all migrations (001-005) have been applied.
--
-- Usage: psql -f tests/validate_schema.sql

-- =============================================================================
-- GRANT ROLES TO CURRENT USER (needed so SET ROLE works)
-- =============================================================================

GRANT kb_admin TO CURRENT_USER;
GRANT kb_editor TO CURRENT_USER;
GRANT kb_commenter TO CURRENT_USER;
GRANT kb_viewer TO CURRENT_USER;
GRANT kb_agent TO CURRENT_USER;

-- =============================================================================
-- TEST 1: Admin sees ALL 12 entries
-- =============================================================================

DO $$
DECLARE
    entry_count INTEGER;
BEGIN
    -- Switch to admin role
    SET LOCAL ROLE kb_admin;
    SET LOCAL app.user_id = 'usr_admin';
    SET LOCAL app.org_id = 'org_demo';
    SET LOCAL app.role = 'admin';
    SET LOCAL app.department = 'leadership';

    SELECT count(*) INTO entry_count FROM entries;

    IF entry_count = 12 THEN
        RAISE NOTICE 'PASS: Admin sees % entries (expected 12)', entry_count;
    ELSE
        RAISE NOTICE 'FAIL: Admin sees % entries (expected 12)', entry_count;
    END IF;

    -- Reset role
    RESET ROLE;
END;
$$;

-- =============================================================================
-- TEST 2: Viewer sees only non-private, non-system entries (10 of 12)
-- =============================================================================

DO $$
DECLARE
    entry_count INTEGER;
BEGIN
    SET LOCAL ROLE kb_viewer;
    SET LOCAL app.user_id = 'usr_viewer';
    SET LOCAL app.org_id = 'org_demo';
    SET LOCAL app.role = 'viewer';
    SET LOCAL app.department = '';

    SELECT count(*) INTO entry_count FROM entries;

    IF entry_count = 10 THEN
        RAISE NOTICE 'PASS: Viewer sees % entries (expected 10, excludes private + system)', entry_count;
    ELSE
        RAISE NOTICE 'FAIL: Viewer sees % entries (expected 10, excludes private + system)', entry_count;
    END IF;

    RESET ROLE;
END;
$$;

-- =============================================================================
-- TEST 3: Agent role CANNOT INSERT directly into entries
-- =============================================================================

DO $$
BEGIN
    SET LOCAL ROLE kb_agent;
    SET LOCAL app.user_id = 'usr_editor';
    SET LOCAL app.org_id = 'org_demo';
    SET LOCAL app.role = 'editor';
    SET LOCAL app.department = 'engineering';

    -- Attempt an INSERT that should be denied by RLS (no INSERT policy for kb_agent)
    INSERT INTO entries (
        org_id, title, content, content_hash, content_type, logical_path,
        sensitivity, created_by, updated_by, source
    ) VALUES (
        'org_demo', 'Agent Direct Write Attempt', 'This should fail',
        md5('This should fail'), 'context', 'Test/agent-write',
        'shared', 'usr_editor', 'usr_editor', 'agent'
    );

    -- If we get here, the INSERT succeeded (should not happen)
    RAISE NOTICE 'FAIL: Agent INSERT into entries succeeded (should have been denied)';

    RESET ROLE;
EXCEPTION
    WHEN insufficient_privilege THEN
        RAISE NOTICE 'PASS: Agent INSERT into entries correctly denied (insufficient_privilege)';
        RESET ROLE;
    WHEN OTHERS THEN
        RAISE NOTICE 'PASS: Agent INSERT into entries correctly denied (%)', SQLERRM;
        RESET ROLE;
END;
$$;

-- =============================================================================
-- TEST 4: CTE traversal — 1-hop neighborhood from Mission Statement
-- Expected neighbors: entry 8 (Onboarding Guide, relates_to from 1->8)
--                     entry c (Q3 Priorities, part_of from c->1)
--                     entry 3 is NOT a direct neighbor of 1
-- =============================================================================

DO $$
DECLARE
    neighbor_count INTEGER;
    neighbor_titles TEXT;
BEGIN
    SET LOCAL ROLE kb_admin;
    SET LOCAL app.user_id = 'usr_admin';
    SET LOCAL app.org_id = 'org_demo';
    SET LOCAL app.role = 'admin';
    SET LOCAL app.department = 'leadership';

    WITH one_hop AS (
        -- Outgoing links from entry 1
        SELECT el.target_entry_id AS neighbor_id, el.link_type
        FROM entry_links el
        WHERE el.source_entry_id = 'a0000000-0000-0000-0000-000000000001'

        UNION

        -- Incoming links to entry 1
        SELECT el.source_entry_id AS neighbor_id, el.link_type
        FROM entry_links el
        WHERE el.target_entry_id = 'a0000000-0000-0000-0000-000000000001'
    )
    SELECT count(DISTINCT oh.neighbor_id),
           string_agg(DISTINCT e.title, ', ' ORDER BY e.title)
    INTO neighbor_count, neighbor_titles
    FROM one_hop oh
    JOIN entries e ON e.id = oh.neighbor_id;

    IF neighbor_count = 2 THEN
        RAISE NOTICE 'PASS: CTE 1-hop from Mission Statement found % neighbors: %', neighbor_count, neighbor_titles;
    ELSE
        RAISE NOTICE 'FAIL: CTE 1-hop from Mission Statement found % neighbors (expected 2): %', neighbor_count, neighbor_titles;
    END IF;

    RESET ROLE;
END;
$$;

-- =============================================================================
-- TEST 5: Full-text search returns results
-- =============================================================================

DO $$
DECLARE
    search_count INTEGER;
    found_title TEXT;
BEGIN
    SET LOCAL ROLE kb_admin;
    SET LOCAL app.user_id = 'usr_admin';
    SET LOCAL app.org_id = 'org_demo';
    SET LOCAL app.role = 'admin';
    SET LOCAL app.department = 'leadership';

    -- Search for "multi-tenant" which appears in the decision entry
    SELECT count(*), string_agg(title, ', ')
    INTO search_count, found_title
    FROM entries
    WHERE search_vector @@ plainto_tsquery('english', 'multi-tenant architecture');

    IF search_count >= 1 THEN
        RAISE NOTICE 'PASS: Full-text search for "multi-tenant architecture" found % result(s): %', search_count, found_title;
    ELSE
        RAISE NOTICE 'FAIL: Full-text search for "multi-tenant architecture" found 0 results';
    END IF;

    RESET ROLE;
END;
$$;

-- =============================================================================
-- TEST 6: Agent CAN insert into staging (writes go through governance)
-- =============================================================================

DO $$
DECLARE
    staging_id UUID;
BEGIN
    SET LOCAL ROLE kb_agent;
    SET LOCAL app.user_id = 'usr_editor';
    SET LOCAL app.org_id = 'org_demo';
    SET LOCAL app.role = 'editor';
    SET LOCAL app.department = 'engineering';

    INSERT INTO staging (
        org_id, target_path, change_type, proposed_content, content_hash,
        submitted_by, source, governance_tier, submission_category
    ) VALUES (
        'org_demo', 'Test/agent-staging-write', 'create',
        'Agent writes should go through staging', md5('Agent writes should go through staging'),
        'usr_editor', 'agent', 2, 'teaching_loop'
    ) RETURNING id INTO staging_id;

    IF staging_id IS NOT NULL THEN
        RAISE NOTICE 'PASS: Agent successfully wrote to staging (id: %)', staging_id;
    END IF;

    RESET ROLE;
EXCEPTION
    WHEN insufficient_privilege THEN
        RAISE NOTICE 'FAIL: Agent was denied staging INSERT (should be allowed)';
        RESET ROLE;
    WHEN OTHERS THEN
        RAISE NOTICE 'FAIL: Agent staging INSERT failed: %', SQLERRM;
        RESET ROLE;
END;
$$;

-- Clean up: agent can't DELETE from staging, so clean up as admin
DO $$
BEGIN
    SET LOCAL ROLE kb_admin;
    SET LOCAL app.user_id = 'usr_admin';
    SET LOCAL app.org_id = 'org_demo';
    SET LOCAL app.role = 'admin';
    SET LOCAL app.department = 'leadership';

    DELETE FROM staging WHERE target_path = 'Test/agent-staging-write';

    RESET ROLE;
END;
$$;

-- =============================================================================
-- TEST 7: Blobs + entry_attachments — 2-org isolation (migration 022)
--
-- Insert an org_alpha organization/user, insert a blob + attachment there,
-- and verify org_demo callers see zero rows. Also verify that the same sha256
-- can coexist across orgs (per-org UNIQUE, not global UNIQUE).
-- =============================================================================

-- Setup: a second org with one admin user, run as superuser (bypasses RLS
-- for seed inserts only). The API never does this at runtime.
DO $$
BEGIN
    INSERT INTO organizations (id, name, settings) VALUES
        ('org_alpha', 'Alpha Org (validation)', '{}')
    ON CONFLICT (id) DO NOTHING;

    INSERT INTO users (id, org_id, display_name, email_hash, role, department)
    VALUES (
        'usr_alpha_admin', 'org_alpha', 'Alpha Admin',
        encode(digest('alpha-admin@alpha.org', 'sha256'), 'hex'),
        'admin', 'leadership'
    ) ON CONFLICT (id) DO NOTHING;

    INSERT INTO users (id, org_id, display_name, email_hash, role, department)
    VALUES (
        'usr_demo_uploader', 'org_demo', 'Demo Uploader',
        encode(digest('uploader@demo.org', 'sha256'), 'hex'),
        'admin', 'leadership'
    ) ON CONFLICT (id) DO NOTHING;
END;
$$;

-- Insert a blob + attachment in org_alpha as kb_admin@org_alpha.
DO $$
DECLARE
    alpha_blob_id UUID;
    alpha_entry_id UUID;
    alpha_attachment_id UUID;
BEGIN
    SET LOCAL ROLE kb_admin;
    SET LOCAL app.user_id = 'usr_alpha_admin';
    SET LOCAL app.org_id = 'org_alpha';
    SET LOCAL app.role = 'admin';
    SET LOCAL app.department = 'leadership';

    -- Need an entry in org_alpha to attach to.
    INSERT INTO entries (
        org_id, title, content, content_hash, content_type, logical_path,
        sensitivity, created_by, updated_by, source
    ) VALUES (
        'org_alpha', 'Alpha Entry', 'Alpha content',
        md5('Alpha content'), 'context', 'Test/alpha-attach',
        'shared', 'usr_alpha_admin', 'usr_alpha_admin', 'web_ui'
    ) RETURNING id INTO alpha_entry_id;

    INSERT INTO blobs (
        org_id, sha256, content_type, size_bytes,
        storage_backend, storage_key, uploaded_by
    ) VALUES (
        'org_alpha',
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        'application/pdf', 1024, 'local',
        'org_alpha/aa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        'usr_alpha_admin'
    ) RETURNING id INTO alpha_blob_id;

    INSERT INTO entry_attachments (org_id, entry_id, blob_id, role)
    VALUES ('org_alpha', alpha_entry_id, alpha_blob_id, 'source')
    RETURNING id INTO alpha_attachment_id;

    IF alpha_blob_id IS NOT NULL AND alpha_attachment_id IS NOT NULL THEN
        RAISE NOTICE 'PASS: org_alpha seeded blob % and attachment %', alpha_blob_id, alpha_attachment_id;
    END IF;

    RESET ROLE;
EXCEPTION
    WHEN OTHERS THEN
        RAISE NOTICE 'FAIL: org_alpha blob/attachment seed failed: %', SQLERRM;
        RESET ROLE;
END;
$$;

-- Verify org_demo admin cannot see any org_alpha blobs or attachments.
DO $$
DECLARE
    cross_blob_count INTEGER;
    cross_att_count  INTEGER;
BEGIN
    SET LOCAL ROLE kb_admin;
    SET LOCAL app.user_id = 'usr_admin';
    SET LOCAL app.org_id = 'org_demo';
    SET LOCAL app.role = 'admin';
    SET LOCAL app.department = 'leadership';

    SELECT count(*) INTO cross_blob_count
    FROM blobs WHERE org_id = 'org_alpha';

    SELECT count(*) INTO cross_att_count
    FROM entry_attachments WHERE org_id = 'org_alpha';

    IF cross_blob_count = 0 AND cross_att_count = 0 THEN
        RAISE NOTICE 'PASS: org_demo sees 0 org_alpha blobs and 0 org_alpha attachments (cross-org isolation)';
    ELSE
        RAISE NOTICE 'FAIL: cross-org leak — org_demo sees % blobs and % attachments from org_alpha',
            cross_blob_count, cross_att_count;
    END IF;

    RESET ROLE;
END;
$$;

-- Verify that the same sha256 can exist in a different org (per-org UNIQUE,
-- not global). Insert identical content in org_demo and confirm it gets a
-- distinct blob_id.
DO $$
DECLARE
    demo_blob_id UUID;
    total_with_sha INTEGER;
BEGIN
    SET LOCAL ROLE kb_admin;
    SET LOCAL app.user_id = 'usr_demo_uploader';
    SET LOCAL app.org_id = 'org_demo';
    SET LOCAL app.role = 'admin';
    SET LOCAL app.department = 'leadership';

    INSERT INTO blobs (
        org_id, sha256, content_type, size_bytes,
        storage_backend, storage_key, uploaded_by
    ) VALUES (
        'org_demo',
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        'application/pdf', 1024, 'local',
        'org_demo/aa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        'usr_demo_uploader'
    ) RETURNING id INTO demo_blob_id;

    -- Superuser-style global count: switch to admin on org_alpha AND verify
    -- it still sees exactly one row (its own), proving isolation holds
    -- symmetrically.
    RESET ROLE;
    SET LOCAL ROLE kb_admin;
    SET LOCAL app.user_id = 'usr_alpha_admin';
    SET LOCAL app.org_id = 'org_alpha';
    SET LOCAL app.role = 'admin';
    SET LOCAL app.department = 'leadership';

    SELECT count(*) INTO total_with_sha
    FROM blobs
    WHERE sha256 = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';

    -- org_alpha admin sees only its own row despite matching sha256 in org_demo
    IF total_with_sha = 1 THEN
        RAISE NOTICE 'PASS: identical sha256 across orgs yields distinct rows visible only within their org';
    ELSE
        RAISE NOTICE 'FAIL: org_alpha admin sees % rows for shared sha256 (expected 1)', total_with_sha;
    END IF;

    RESET ROLE;
EXCEPTION
    WHEN OTHERS THEN
        RAISE NOTICE 'FAIL: cross-org sha256 test failed: %', SQLERRM;
        RESET ROLE;
END;
$$;

-- Clean up org_alpha fixtures so a re-run of this script stays idempotent-ish.
DO $$
BEGIN
    SET LOCAL ROLE kb_admin;
    SET LOCAL app.user_id = 'usr_alpha_admin';
    SET LOCAL app.org_id = 'org_alpha';
    SET LOCAL app.role = 'admin';
    SET LOCAL app.department = 'leadership';

    DELETE FROM entry_attachments WHERE org_id = 'org_alpha';
    DELETE FROM blobs WHERE org_id = 'org_alpha';
    DELETE FROM entries WHERE org_id = 'org_alpha' AND logical_path = 'Test/alpha-attach';

    RESET ROLE;
END;
$$;

DO $$
BEGIN
    SET LOCAL ROLE kb_admin;
    SET LOCAL app.user_id = 'usr_demo_uploader';
    SET LOCAL app.org_id = 'org_demo';
    SET LOCAL app.role = 'admin';
    SET LOCAL app.department = 'leadership';

    DELETE FROM blobs
    WHERE org_id = 'org_demo'
      AND sha256 = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';

    RESET ROLE;
END;
$$;

DO $$ BEGIN RAISE NOTICE '--- All validation tests complete ---'; END; $$;
