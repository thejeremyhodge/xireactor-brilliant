-- 010_invitations.sql
-- Single-use invite codes for org onboarding.
-- Format: CTX-XXXX-XXXX (uppercase alphanumeric).
-- Token is bcrypt-hashed; invite_code is the public identifier.
--
-- Depends on: 001_core.sql (organizations, users), 004_rls.sql (PG roles)

-- ============================================================
-- Invitations Table
-- ============================================================

CREATE TABLE invitations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          TEXT NOT NULL REFERENCES organizations(id),
    invite_code     TEXT NOT NULL UNIQUE
                        CHECK (invite_code ~ '^CTX-[A-Z0-9]{4}-[A-Z0-9]{4}$'),
    token_hash      TEXT NOT NULL,                   -- bcrypt hash of the bearer token
    default_role    TEXT NOT NULL DEFAULT 'viewer'
                        CHECK (default_role IN (
                            'admin', 'editor', 'commenter', 'viewer'
                        )),
    invited_by      TEXT REFERENCES users(id),       -- admin who created the invite
    redeemed_by     TEXT REFERENCES users(id),       -- user who consumed the invite
    email_hint      TEXT,                            -- optional email hint for display
    expires_at      TIMESTAMPTZ NOT NULL,
    redeemed_at     TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN (
                            'pending', 'redeemed', 'expired', 'revoked'
                        )),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- invite_code already has a UNIQUE index; add composite for listing queries
CREATE INDEX idx_invitations_org_status ON invitations (org_id, status);

-- ============================================================
-- Grant Table Permissions
-- ============================================================

-- Only admins manage invitations
GRANT SELECT, INSERT, UPDATE, DELETE ON invitations TO kb_admin;

-- ============================================================
-- Row-Level Security
-- ============================================================

ALTER TABLE invitations ENABLE ROW LEVEL SECURITY;
ALTER TABLE invitations FORCE ROW LEVEL SECURITY;

-- kb_admin: full CRUD within own org
CREATE POLICY invitations_admin_all ON invitations
    TO kb_admin
    USING (org_id = current_setting('app.org_id'))
    WITH CHECK (org_id = current_setting('app.org_id'));

-- No policies for other roles — they cannot access this table.
