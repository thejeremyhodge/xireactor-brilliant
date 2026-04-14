-- 013_user_auth.sql
-- Add email and password_hash columns to users table for frontend auth.
-- email is nullable (existing API-key-only users don't need one).
-- password_hash is nullable (users can auth via API key without a password).
--
-- Depends on: 001_core.sql (users table), 004_rls.sql (PG roles)

-- =============================================================================
-- ADD COLUMNS
-- =============================================================================

-- email: plaintext for login, nullable, unique per org
ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT;

-- password_hash: bcrypt hash, nullable (API-key-only users skip this)
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;

-- =============================================================================
-- PARTIAL UNIQUE INDEX — one email per org (only where email is set)
-- =============================================================================

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_org_email
    ON users (org_id, email)
    WHERE email IS NOT NULL;

-- =============================================================================
-- GRANT TABLE PERMISSIONS
-- =============================================================================

-- All KB roles need SELECT on users (for user lookups, member listing)
GRANT SELECT ON users TO kb_admin, kb_editor, kb_commenter, kb_viewer, kb_agent;

-- kb_admin can UPDATE users (role changes, deactivation, email/password updates)
GRANT UPDATE ON users TO kb_admin;

-- =============================================================================
-- BACKFILL SEED USER EMAILS
-- =============================================================================

UPDATE users SET email = 'alice@demo.org'  WHERE id = 'usr_admin'     AND email IS NULL;
UPDATE users SET email = 'eddie@demo.org'  WHERE id = 'usr_editor'    AND email IS NULL;
UPDATE users SET email = 'carol@demo.org'  WHERE id = 'usr_commenter' AND email IS NULL;
UPDATE users SET email = 'victor@demo.org' WHERE id = 'usr_viewer'    AND email IS NULL;
