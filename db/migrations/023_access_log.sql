-- 023_access_log.sql
-- Observability tables: per-entry read log + per-request log.
--
-- Both tables are admin-only SELECT (mirrors audit_log), admin can see all rows
-- within their org; other roles cannot. All org-scoped via RLS. Append-only
-- (no UPDATE/DELETE policies).
--
-- The API layer inserts rows from request middleware (request_log) and from
-- decorated read paths (entry_access_log). All write paths run under a
-- kb_admin-scoped connection for request_log (because the request may be
-- unauthenticated — e.g. a 401 on a bad token) and reuse the current user's
-- RLS-scoped connection for entry_access_log.
--
-- Depends on: 001_core.sql, 003_governance.sql, 004_rls.sql

-- =============================================================================
-- entry_access_log — one row per (actor, entry) surfaced in a read response
-- =============================================================================

CREATE TABLE IF NOT EXISTS entry_access_log (
    id          BIGSERIAL PRIMARY KEY,
    org_id      TEXT NOT NULL,
    actor_type  TEXT NOT NULL CHECK (actor_type IN ('user', 'agent', 'api')),
    actor_id    TEXT NOT NULL,
    entry_id    UUID NOT NULL,
    source      TEXT,  -- web_ui | agent | api (nullable for compatibility)
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_entry_access_org_ts
    ON entry_access_log (org_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_entry_access_org_entry_ts
    ON entry_access_log (org_id, entry_id, ts DESC);

-- =============================================================================
-- request_log — one row per HTTP request (excluding /health + /static)
-- =============================================================================

CREATE TABLE IF NOT EXISTS request_log (
    id              BIGSERIAL PRIMARY KEY,
    org_id          TEXT,              -- nullable: unauthenticated requests
    actor_id        TEXT,              -- nullable: unauthenticated requests
    endpoint        TEXT NOT NULL,     -- path template, truncated to <=256
    method          TEXT NOT NULL,
    status          INTEGER NOT NULL,
    response_bytes  INTEGER,
    approx_tokens   INTEGER,
    duration_ms     INTEGER NOT NULL,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_request_log_org_ts
    ON request_log (org_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_request_log_org_endpoint_ts
    ON request_log (org_id, endpoint, ts DESC);

-- =============================================================================
-- GRANTS — mirror audit_log: all kb roles can INSERT; only kb_admin can SELECT
-- (RLS policies enforce per-row scoping)
-- =============================================================================

-- SELECT granted to all roles; RLS policies below restrict rows so that
-- only kb_admin actually sees anything (non-admin roles have no SELECT
-- policy and FORCE RLS defaults to deny — 0 rows, no error).
GRANT SELECT, INSERT ON entry_access_log
    TO kb_admin, kb_editor, kb_commenter, kb_viewer, kb_agent;
GRANT USAGE ON SEQUENCE entry_access_log_id_seq
    TO kb_admin, kb_editor, kb_commenter, kb_viewer, kb_agent;

GRANT SELECT, INSERT ON request_log
    TO kb_admin, kb_editor, kb_commenter, kb_viewer, kb_agent;
GRANT USAGE ON SEQUENCE request_log_id_seq
    TO kb_admin, kb_editor, kb_commenter, kb_viewer, kb_agent;

-- =============================================================================
-- RLS — entry_access_log
-- =============================================================================

ALTER TABLE entry_access_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE entry_access_log FORCE ROW LEVEL SECURITY;

-- Drop-and-recreate pattern for idempotency across re-applies.
DROP POLICY IF EXISTS entry_access_log_select_admin ON entry_access_log;
CREATE POLICY entry_access_log_select_admin ON entry_access_log
    FOR SELECT
    TO kb_admin
    USING (org_id = current_setting('app.org_id'));

-- INSERT policy: any kb role can insert within their org (or unauth → NULL).
DROP POLICY IF EXISTS entry_access_log_insert ON entry_access_log;
CREATE POLICY entry_access_log_insert ON entry_access_log
    FOR INSERT
    TO kb_admin, kb_editor, kb_commenter, kb_viewer, kb_agent
    WITH CHECK (
        org_id IS NULL
        OR org_id = current_setting('app.org_id')
    );

-- No UPDATE or DELETE policies: append-only.

-- =============================================================================
-- RLS — request_log
-- =============================================================================

ALTER TABLE request_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE request_log FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS request_log_select_admin ON request_log;
CREATE POLICY request_log_select_admin ON request_log
    FOR SELECT
    TO kb_admin
    USING (org_id = current_setting('app.org_id'));

DROP POLICY IF EXISTS request_log_insert ON request_log;
CREATE POLICY request_log_insert ON request_log
    FOR INSERT
    TO kb_admin, kb_editor, kb_commenter, kb_viewer, kb_agent
    WITH CHECK (
        org_id IS NULL
        OR org_id = current_setting('app.org_id')
    );

-- No UPDATE or DELETE policies: append-only.
