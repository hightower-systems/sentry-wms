-- ============================================================
-- Migration 037: wms_tokens inbound columns + allowlist (v1.7.0)
-- ============================================================
-- v1.7 adds Pipe B (inbound). Tokens issued for inbound use need
-- three new scope dimensions on top of the v1.5 vault:
--
--   source_system     -- which external system this token speaks
--                        for. Token-bound at issuance so the
--                        idempotency key (source_system, external_id,
--                        external_version) cannot collide across
--                        tenants.
--   inbound_resources -- the resource types the token may upsert
--                        (sales_orders / items / customers / vendors
--                        / purchase_orders). Empty array = deny all,
--                        same Decision-S alignment as event_types
--                        and endpoints in v1.5.
--   mapping_override  -- separate boolean capability: allows the
--                        token to ship per-request mapping overrides
--                        in the body. Default false; admin must
--                        explicitly opt a token in.
--
-- source_system FKs to inbound_source_systems_allowlist (created
-- in this same migration, before the FK is added). PostgreSQL
-- forbids subqueries in CHECK constraints, so an allowlist FK is
-- the correct shape; outbound-only tokens keep source_system NULL
-- and are naturally exempt from the FK.
--
-- The allowlist is a privilege table -- a row in it gates whether
-- a source_system can write to canonical at all. It gets the V-157
-- forensic-trigger pattern (DELETE / TRUNCATE statement-level
-- triggers writing to inbound_source_systems_allowlist_audit).
-- Same shape as wms_tokens_audit (mig 028) and the
-- cross_system_mappings_audit landing in mig 038.
--
-- Existing outbound-only tokens are unaffected: source_system
-- defaults NULL, inbound_resources defaults '{}', mapping_override
-- defaults FALSE. Their decorator path is unchanged.
-- ============================================================

BEGIN;

-- Allowlist must exist before the FK is added on wms_tokens.
CREATE TABLE IF NOT EXISTS inbound_source_systems_allowlist (
    source_system  VARCHAR(64)  PRIMARY KEY,
    kind           VARCHAR(16)  NOT NULL CHECK (kind IN ('connector','internal_tool','manual_import')),
    notes          TEXT,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

ALTER TABLE wms_tokens
    ADD COLUMN IF NOT EXISTS source_system     VARCHAR(64) REFERENCES inbound_source_systems_allowlist(source_system),
    ADD COLUMN IF NOT EXISTS inbound_resources TEXT[]      NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS mapping_override  BOOLEAN     NOT NULL DEFAULT FALSE;

-- Forensic audit (V-157 pattern, mirrors wms_tokens_audit / mig 028).
CREATE TABLE IF NOT EXISTS inbound_source_systems_allowlist_audit (
    audit_id         BIGSERIAL    PRIMARY KEY,
    event_type       VARCHAR(16)  NOT NULL,
    rows_affected    INTEGER,
    sess_user        TEXT         NOT NULL,
    curr_user        TEXT         NOT NULL,
    backend_pid      INTEGER      NOT NULL,
    application_name TEXT,
    event_at         TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS inbound_source_systems_allowlist_audit_event_at
    ON inbound_source_systems_allowlist_audit (event_at DESC);

CREATE OR REPLACE FUNCTION inbound_source_systems_allowlist_audit_delete()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    _count INTEGER;
BEGIN
    SELECT COUNT(*) INTO _count FROM deleted_rows;
    INSERT INTO inbound_source_systems_allowlist_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'DELETE', _count, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tr_inbound_source_systems_allowlist_audit_delete
    ON inbound_source_systems_allowlist;
CREATE TRIGGER tr_inbound_source_systems_allowlist_audit_delete
    AFTER DELETE ON inbound_source_systems_allowlist
    REFERENCING OLD TABLE AS deleted_rows
    FOR EACH STATEMENT EXECUTE FUNCTION inbound_source_systems_allowlist_audit_delete();

CREATE OR REPLACE FUNCTION inbound_source_systems_allowlist_audit_truncate()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO inbound_source_systems_allowlist_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'TRUNCATE', NULL, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

-- v1.7.0 #275: this AFTER TRUNCATE trigger fires only on
-- `TRUNCATE inbound_source_systems_allowlist CASCADE`. Plain
-- `TRUNCATE inbound_source_systems_allowlist` raises ForeignKeyViolation
-- before the trigger fires because the v1.7 inbound tables and
-- cross_system_mappings declare FKs into source_system. The CASCADE form
-- is the sole path that writes a forensic audit row; a direct plain
-- TRUNCATE leaves a Postgres error in the logs but no audit row.
-- See docs/audit-log.md for the operator-facing shape.
DROP TRIGGER IF EXISTS tr_inbound_source_systems_allowlist_audit_truncate
    ON inbound_source_systems_allowlist;
CREATE TRIGGER tr_inbound_source_systems_allowlist_audit_truncate
    AFTER TRUNCATE ON inbound_source_systems_allowlist
    FOR EACH STATEMENT EXECUTE FUNCTION inbound_source_systems_allowlist_audit_truncate();

COMMIT;
