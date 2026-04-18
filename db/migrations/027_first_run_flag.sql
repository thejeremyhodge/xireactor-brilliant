-- 027_first_run_flag.sql
-- Singleton global-settings row so the API can tell whether the admin user
-- has already been claimed. Gates `/setup` (T-0214) and the POST-driven
-- admin_bootstrap path (T-0216) — both 404/no-op once this flips TRUE.
--
-- Singleton invariant: exactly one row, `id = 1`, enforced by the CHECK.
-- Re-running this migration is safe (IF NOT EXISTS + ON CONFLICT DO NOTHING).
--
-- Depends on: 004_rls.sql (PG roles: kb_admin, kb_editor, kb_commenter, kb_viewer, kb_agent)

CREATE TABLE IF NOT EXISTS brilliant_settings (
    id                  INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    first_run_complete  BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO brilliant_settings (id) VALUES (1)
ON CONFLICT (id) DO NOTHING;

-- kb_admin manages the latch; other roles read it (so `/setup` can SELECT
-- without needing admin context before the admin even exists).
GRANT SELECT, UPDATE ON brilliant_settings
    TO kb_admin;
GRANT SELECT ON brilliant_settings
    TO kb_editor, kb_commenter, kb_viewer, kb_agent;
