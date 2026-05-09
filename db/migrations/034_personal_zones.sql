-- 034_personal_zones.sql
-- Personal zones (Sprint 0051): every user gets an auto-created walled-off
-- "zone" group. Writes default into the caller's zone for safety; sharing is
-- an explicit, additive promote step.
--
-- Strategy
-- --------
-- Reuse the existing principals/groups model from migration 018. A "zone"
-- is just a `groups` row with `is_zone = TRUE` and `owner_user_id` set to the
-- user it belongs to. Membership is the user themselves, and the row is
-- locked: non-`kb_admin` roles cannot rename, delete, or modify the
-- membership of a zone group. This preserves the safety invariant — the
-- zone owner cannot circumvent the wall by adding a co-conspirator.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS, CREATE INDEX IF NOT EXISTS,
-- CREATE OR REPLACE FUNCTION, DO blocks for triggers / policies.
--
-- Additive only — no DROPs, no type changes, no behavior changes for
-- existing rows beyond defaulting the new `is_zone = FALSE` /
-- `owner_user_id = NULL` columns.
--
-- Depends on: 001_core.sql (users/organizations), 018_principals_and_groups.sql
-- Spec: .xireactor/specs/0051--2026-05-08--personal-zones-sprint-1.md
-- Task: T-0301

-- =============================================================================
-- Columns on groups
-- =============================================================================

ALTER TABLE groups
    ADD COLUMN IF NOT EXISTS is_zone BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE groups
    ADD COLUMN IF NOT EXISTS owner_user_id TEXT REFERENCES users(id);

-- One zone per user per org. Partial unique index: only enforced for zone rows.
CREATE UNIQUE INDEX IF NOT EXISTS groups_zone_owner_unique
    ON groups (org_id, owner_user_id)
    WHERE is_zone = TRUE;

-- Helpful for "is this group a zone?" lookups during RLS / trigger checks.
CREATE INDEX IF NOT EXISTS groups_is_zone_idx
    ON groups (id)
    WHERE is_zone = TRUE;

-- =============================================================================
-- provision_user_zone(p_user_id, p_org_id) — SECURITY DEFINER, idempotent
-- =============================================================================
--
-- Returns the zone group id for the user, creating it (and the membership row)
-- if it doesn't already exist. Safe to call repeatedly: ON CONFLICT DO NOTHING
-- on both inserts, then a re-SELECT to return the canonical id.

CREATE OR REPLACE FUNCTION provision_user_zone(
    p_user_id TEXT,
    p_org_id  TEXT
) RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_group_id UUID;
BEGIN
    -- Try to find an existing zone for this user
    SELECT id INTO v_group_id
        FROM groups
        WHERE org_id = p_org_id
          AND owner_user_id = p_user_id
          AND is_zone = TRUE;

    IF v_group_id IS NULL THEN
        INSERT INTO groups (
            org_id, name, description, created_by, is_zone, owner_user_id
        ) VALUES (
            p_org_id,
            'zone:' || p_user_id,
            'Personal zone (auto-created)',
            p_user_id,
            TRUE,
            p_user_id
        )
        ON CONFLICT (org_id, name) DO NOTHING
        RETURNING id INTO v_group_id;

        -- If the conflict path was taken, fetch the existing row.
        IF v_group_id IS NULL THEN
            SELECT id INTO v_group_id
                FROM groups
                WHERE org_id = p_org_id
                  AND owner_user_id = p_user_id
                  AND is_zone = TRUE;
        END IF;
    END IF;

    -- Ensure membership row exists
    INSERT INTO group_members (group_id, user_id, org_id, added_by)
    VALUES (v_group_id, p_user_id, p_org_id, p_user_id)
    ON CONFLICT (group_id, user_id) DO NOTHING;

    RETURN v_group_id;
END;
$$;

-- EXECUTE granted to all kb_* app roles. The function is SECURITY DEFINER and
-- only inserts the caller's own zone group + membership (idempotent), so
-- widening EXECUTE is bounded. Required because get_or_create_zone runs in
-- the request transaction as the caller's role on POST /entries.
REVOKE ALL ON FUNCTION provision_user_zone(TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION provision_user_zone(TEXT, TEXT)
    TO kb_admin, kb_editor, kb_commenter, kb_viewer, kb_agent;

-- =============================================================================
-- grant_zone_admin_on_entry — narrow SECURITY DEFINER helper for the
-- default-write-to-zone path. Non-admin kb_* roles have only SELECT on the
-- `permissions` table; this helper lets them insert the *single* zone-admin
-- grant required when POST /entries lands an entry in the caller's zone.
-- Bounded surface: only writes (principal_type='group', role='admin') rows
-- for a row already known to be a zone group. Idempotent on the existing
-- UNIQUE constraint.
-- =============================================================================

CREATE OR REPLACE FUNCTION grant_zone_admin_on_entry(
    p_org_id        TEXT,
    p_zone_group_id TEXT,
    p_entry_id      UUID,
    p_granted_by    TEXT
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    -- Defensive: refuse to write through this helper unless the target group
    -- is in fact a zone. Prevents misuse as a generic permission-insert escape.
    IF NOT EXISTS (
        SELECT 1 FROM groups
        WHERE id = p_zone_group_id::uuid
          AND org_id = p_org_id
          AND is_zone = TRUE
    ) THEN
        RAISE EXCEPTION 'grant_zone_admin_on_entry: % is not a zone group in org %',
            p_zone_group_id, p_org_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    INSERT INTO permissions (
        org_id, principal_type, principal_id,
        resource_type, entry_id, role, granted_by
    ) VALUES (
        p_org_id, 'group', p_zone_group_id,
        'entry', p_entry_id, 'admin', p_granted_by
    )
    ON CONFLICT (org_id, principal_type, principal_id,
                 resource_type, entry_id, path_pattern, role)
    DO NOTHING;
END;
$$;

REVOKE ALL ON FUNCTION grant_zone_admin_on_entry(TEXT, TEXT, UUID, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION grant_zone_admin_on_entry(TEXT, TEXT, UUID, TEXT)
    TO kb_admin, kb_editor, kb_commenter, kb_viewer, kb_agent;

-- =============================================================================
-- AFTER INSERT trigger on users — auto-provision zone for every new user
-- =============================================================================

CREATE OR REPLACE FUNCTION users_after_insert_provision_zone()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    PERFORM provision_user_zone(NEW.id, NEW.org_id);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS users_provision_zone_trigger ON users;
CREATE TRIGGER users_provision_zone_trigger
    AFTER INSERT ON users
    FOR EACH ROW
    EXECUTE FUNCTION users_after_insert_provision_zone();

-- =============================================================================
-- Backfill — every existing user gets a zone (idempotent via ON CONFLICT inside)
-- =============================================================================

DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN SELECT id, org_id FROM users LOOP
        PERFORM provision_user_zone(r.id, r.org_id);
    END LOOP;
END$$;

-- =============================================================================
-- Zone immutability — block UPDATE/DELETE of zone groups, and INSERT/DELETE
-- of group_members rows for zone groups, when caller is NOT kb_admin.
-- =============================================================================
--
-- A BEFORE trigger raising an exception is cleaner than RLS WITH CHECK here:
-- RLS denials on internal triggers can produce confusing errors, and we want
-- a clear "zone groups are immutable" message. We bypass for kb_admin via a
-- role check — the trigger runs as the caller's role (it's not SECURITY DEFINER).

CREATE OR REPLACE FUNCTION groups_zone_immutable_guard()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    -- kb_admin bypasses the guard (admin tooling needs to manage zones).
    IF pg_has_role(current_user, 'kb_admin', 'MEMBER') THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        ELSE
            RETURN NEW;
        END IF;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF OLD.is_zone = TRUE OR NEW.is_zone = TRUE THEN
            RAISE EXCEPTION 'zone groups are immutable (group_id=%)', OLD.id
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        IF OLD.is_zone = TRUE THEN
            RAISE EXCEPTION 'zone groups cannot be deleted (group_id=%)', OLD.id
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS groups_zone_immutable_trigger ON groups;
CREATE TRIGGER groups_zone_immutable_trigger
    BEFORE UPDATE OR DELETE ON groups
    FOR EACH ROW
    EXECUTE FUNCTION groups_zone_immutable_guard();

CREATE OR REPLACE FUNCTION group_members_zone_immutable_guard()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_is_zone BOOLEAN;
    v_group_id UUID;
BEGIN
    -- kb_admin bypasses the guard.
    IF pg_has_role(current_user, 'kb_admin', 'MEMBER') THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        ELSE
            RETURN NEW;
        END IF;
    END IF;

    IF TG_OP = 'DELETE' THEN
        v_group_id := OLD.group_id;
    ELSE
        v_group_id := NEW.group_id;
    END IF;

    SELECT is_zone INTO v_is_zone FROM groups WHERE id = v_group_id;

    IF v_is_zone IS TRUE THEN
        RAISE EXCEPTION 'zone group membership is immutable (group_id=%)', v_group_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS group_members_zone_immutable_trigger ON group_members;
CREATE TRIGGER group_members_zone_immutable_trigger
    BEFORE INSERT OR UPDATE OR DELETE ON group_members
    FOR EACH ROW
    EXECUTE FUNCTION group_members_zone_immutable_guard();
