-- 006_oauth_store.sql
-- Persistent OAuth 2.1 token store for the MCP server.
-- Survives container restarts; expired rows cleaned by sweep query.

CREATE TABLE oauth_clients (
    client_id           TEXT PRIMARY KEY,
    client_secret       TEXT NOT NULL,
    client_id_issued_at BIGINT NOT NULL,
    client_info         JSONB NOT NULL,            -- full OAuthClientInformationFull payload
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE oauth_auth_codes (
    code                TEXT PRIMARY KEY,
    client_id           TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    scopes              TEXT[] NOT NULL DEFAULT '{}',
    expires_at          DOUBLE PRECISION NOT NULL,
    code_challenge      TEXT,
    redirect_uri        TEXT,
    redirect_uri_provided_explicitly BOOLEAN NOT NULL DEFAULT false,
    resource            TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE oauth_access_tokens (
    token               TEXT PRIMARY KEY,
    client_id           TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    scopes              TEXT[] NOT NULL DEFAULT '{}',
    expires_at          BIGINT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE oauth_refresh_tokens (
    token               TEXT PRIMARY KEY,
    client_id           TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    scopes              TEXT[] NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index for TTL sweep
CREATE INDEX idx_oauth_access_tokens_expires ON oauth_access_tokens (expires_at);
CREATE INDEX idx_oauth_auth_codes_expires ON oauth_auth_codes (expires_at);
