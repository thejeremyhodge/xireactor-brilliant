-- 028_grant_kb_roles.sql
-- Grant the kb_* role set to the connection user so that `SET LOCAL ROLE`
-- works against the authenticated principal.
--
-- Context
-- -------
-- 004_rls.sql creates five NOLOGIN roles (kb_admin, kb_editor,
-- kb_commenter, kb_viewer, kb_agent) and GRANTs table privileges *to*
-- them. However, it never grants membership in those roles to any
-- specific login user. In local docker-compose this is invisible because
-- migrations run as the `postgres` superuser, which has implicit SET
-- ROLE privileges to any role in the cluster.
--
-- On Render (or any managed Postgres that issues a non-superuser login
-- user), the connection user is NOT a member of kb_* and `SET LOCAL
-- ROLE kb_admin` fails with "permission denied to set role". This
-- surfaces as:
--   - request_log middleware warnings on every request
--   - /setup 500 errors when create_admin_via_post tries to scope the
--     admin-row INSERT under kb_admin
--
-- Fix
-- ---
-- Grant each kb_* role TO the CURRENT_USER at migration time. GRANT is
-- idempotent, and granting to the postgres superuser locally is a
-- harmless no-op (superuser already has membership).
--
-- Depends on: 004_rls.sql
-- Surfaced during: T-0219 Render wet-test (2026-04-18)

BEGIN;

DO $$
DECLARE
    target_user TEXT := CURRENT_USER;
BEGIN
    EXECUTE format('GRANT kb_admin     TO %I', target_user);
    EXECUTE format('GRANT kb_editor    TO %I', target_user);
    EXECUTE format('GRANT kb_commenter TO %I', target_user);
    EXECUTE format('GRANT kb_viewer    TO %I', target_user);
    EXECUTE format('GRANT kb_agent     TO %I', target_user);
END $$;

COMMIT;
