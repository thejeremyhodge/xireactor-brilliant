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
-- TEST 7: Observability tables — admin can read, viewer sees zero
-- =============================================================================

DO $$
DECLARE
    admin_visible INTEGER;
    viewer_visible INTEGER;
BEGIN
    -- Admin seeds a row
    SET LOCAL ROLE kb_admin;
    SET LOCAL app.user_id = 'usr_admin';
    SET LOCAL app.org_id = 'org_demo';
    SET LOCAL app.role = 'admin';
    SET LOCAL app.department = 'leadership';

    INSERT INTO entry_access_log (org_id, actor_type, actor_id, entry_id, source)
    VALUES ('org_demo', 'user', 'usr_admin',
            'a0000000-0000-0000-0000-000000000001', 'web_ui');

    SELECT count(*) INTO admin_visible FROM entry_access_log;
    RESET ROLE;

    -- Viewer must see 0 rows (no SELECT policy for non-admin)
    SET LOCAL ROLE kb_viewer;
    SET LOCAL app.user_id = 'usr_viewer';
    SET LOCAL app.org_id = 'org_demo';
    SET LOCAL app.role = 'viewer';
    SET LOCAL app.department = '';

    SELECT count(*) INTO viewer_visible FROM entry_access_log;
    RESET ROLE;

    IF admin_visible >= 1 AND viewer_visible = 0 THEN
        RAISE NOTICE 'PASS: entry_access_log RLS — admin sees %, viewer sees %', admin_visible, viewer_visible;
    ELSE
        RAISE NOTICE 'FAIL: entry_access_log RLS — admin sees %, viewer sees % (expected admin>=1, viewer=0)', admin_visible, viewer_visible;
    END IF;

    -- Clean up (as table owner — tables are append-only for kb roles)
    DELETE FROM entry_access_log WHERE actor_id = 'usr_admin';
END;
$$;

DO $$
DECLARE
    admin_visible INTEGER;
    viewer_visible INTEGER;
BEGIN
    SET LOCAL ROLE kb_admin;
    SET LOCAL app.user_id = 'usr_admin';
    SET LOCAL app.org_id = 'org_demo';
    SET LOCAL app.role = 'admin';
    SET LOCAL app.department = 'leadership';

    INSERT INTO request_log (org_id, actor_id, endpoint, method, status,
                              response_bytes, approx_tokens, duration_ms)
    VALUES ('org_demo', 'usr_admin', '/entries', 'GET', 200, 1024, 256, 12);

    SELECT count(*) INTO admin_visible FROM request_log;
    RESET ROLE;

    SET LOCAL ROLE kb_viewer;
    SET LOCAL app.user_id = 'usr_viewer';
    SET LOCAL app.org_id = 'org_demo';
    SET LOCAL app.role = 'viewer';
    SET LOCAL app.department = '';

    SELECT count(*) INTO viewer_visible FROM request_log;
    RESET ROLE;

    IF admin_visible >= 1 AND viewer_visible = 0 THEN
        RAISE NOTICE 'PASS: request_log RLS — admin sees %, viewer sees %', admin_visible, viewer_visible;
    ELSE
        RAISE NOTICE 'FAIL: request_log RLS — admin sees %, viewer sees % (expected admin>=1, viewer=0)', admin_visible, viewer_visible;
    END IF;

    DELETE FROM request_log WHERE actor_id = 'usr_admin';
END;
$$;

DO $$ BEGIN RAISE NOTICE '--- All validation tests complete ---'; END; $$;
