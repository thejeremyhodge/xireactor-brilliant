-- 030_oauth_user_binding.sql
-- OAuth user binding + pending-authorization handoff table.
--
-- Part of sprint 0039 (.xireactor/specs/0039--2026-04-18--oauth-user-bound-auth.md).
-- Replaces the DCR auto-approve flow with a 3-gate OAuth 2.1 code+PKCE flow:
--   gate 1: pre-registered client_id/client_secret (oauth_clients, 006)
--   gate 2: user login at API-hosted /oauth/login (this table holds the tx
--           across the browser redirect hop between MCP and API)
--   gate 3: per-user RLS via X-Act-As-User (user_id bound on issued tokens)
--
-- Schema note
-- -----------
-- The spec text says `user_id UUID` but users.id is TEXT in 001_core.sql
-- (`usr_<hex>` format). We keep TEXT here so the FK resolves.
--
-- Idempotent: every DDL is guarded with IF NOT EXISTS or a DO block.
-- Depends on: 006_oauth_store.sql, 013_user_auth.sql, 028_grant_kb_roles.sql.

BEGIN;

-- ------------------------------------------------------------------
-- 1. user_id on oauth_access_tokens
-- ------------------------------------------------------------------
ALTER TABLE oauth_access_tokens
    ADD COLUMN IF NOT EXISTS user_id TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'oauth_access_tokens_user_id_fkey'
    ) THEN
        ALTER TABLE oauth_access_tokens
            ADD CONSTRAINT oauth_access_tokens_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_oauth_access_tokens_user
    ON oauth_access_tokens (user_id);

-- ------------------------------------------------------------------
-- 2. user_id on oauth_auth_codes
-- ------------------------------------------------------------------
ALTER TABLE oauth_auth_codes
    ADD COLUMN IF NOT EXISTS user_id TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'oauth_auth_codes_user_id_fkey'
    ) THEN
        ALTER TABLE oauth_auth_codes
            ADD CONSTRAINT oauth_auth_codes_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
    END IF;
END $$;

-- ------------------------------------------------------------------
-- 3. oauth_pending_authorizations — in-flight authorize tx.
--    MCP writes on /authorize, API reads+deletes on /oauth/login,
--    MCP re-reads+deletes on /oauth/continue (belt-and-suspenders).
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS oauth_pending_authorizations (
    tx_id                            TEXT PRIMARY KEY,
    client_id                        TEXT NOT NULL
        REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    scopes                           TEXT[] NOT NULL DEFAULT '{}',
    code_challenge                   TEXT,
    code_challenge_method            TEXT,
    redirect_uri                     TEXT NOT NULL,
    redirect_uri_provided_explicitly BOOLEAN NOT NULL DEFAULT false,
    state                            TEXT,
    resource                         TEXT,
    expires_at                       DOUBLE PRECISION NOT NULL,
    created_at                       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_oauth_pending_authz_expires
    ON oauth_pending_authorizations (expires_at);

-- ------------------------------------------------------------------
-- 4. Privileges.
--    The API's /oauth/login route runs inside the standard request
--    middleware, which does `SET LOCAL ROLE kb_*`. Grant kb_admin
--    the CRUD it needs on the pending-authz table. Consistent with
--    028's intent of making kb_* roles actually work on non-superuser
--    Render-style deploys.
-- ------------------------------------------------------------------
GRANT SELECT, INSERT, DELETE ON oauth_pending_authorizations TO kb_admin;

COMMIT;
