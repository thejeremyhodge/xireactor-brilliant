-- 031_service_role_key.sql
-- Extend api_keys.key_type CHECK to allow a new 'service' value.
--
-- Context
-- -------
-- Part of sprint 0039 (.xireactor/specs/0039--2026-04-18--oauth-user-bound-auth.md).
-- The MCP service needs a dedicated API key whose sole purpose is to
-- authenticate *as the MCP service* and then present an X-Act-As-User
-- header naming the end-user the OAuth access token is bound to.
--
-- Separating this from the existing key_types ('interactive', 'agent',
-- 'api_integration') keeps the trust boundary explicit:
--   - only 'service' keys may ever honor X-Act-As-User (see api/auth.py)
--   - a compromised 'interactive' key cannot be used to impersonate users
--     just by adding a header
--
-- Origin of the existing constraint
-- ---------------------------------
-- Declared inline in 001_core.sql (api_keys.key_type column). Postgres
-- auto-names inline column CHECKs `<table>_<column>_check`, so the
-- constraint name we DROP+RECREATE below is `api_keys_key_type_check`.
--
-- Idempotency
-- -----------
-- The DO block DROPs the existing constraint (if present, by name) and
-- recreates it with the expanded value set. Re-running the migration is
-- a no-op: the DROP is guarded by a pg_constraint lookup, and ADD
-- CONSTRAINT always defines the same shape.
--
-- Depends on: 001_core.sql (api_keys.key_type CHECK constraint)

BEGIN;

DO $$
BEGIN
    -- Drop the existing CHECK if present. Name is the inline-auto-generated
    -- `api_keys_key_type_check` — verified against 001_core.sql, which
    -- declares `CHECK (key_type IN (...))` inline on the column.
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'api_keys_key_type_check'
          AND conrelid = 'api_keys'::regclass
    ) THEN
        ALTER TABLE api_keys
            DROP CONSTRAINT api_keys_key_type_check;
    END IF;

    -- Recreate with 'service' added. Safe on re-run since we just dropped.
    ALTER TABLE api_keys
        ADD CONSTRAINT api_keys_key_type_check
        CHECK (key_type IN (
            'interactive',
            'agent',
            'api_integration',
            'service'
        ));
END $$;

COMMIT;
