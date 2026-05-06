-- ============================================================
-- Migration 038: cross_system_mappings + audit (v1.7.0)
-- ============================================================
-- Bidirectional mapping between external entities (source_system,
-- source_type, source_id) and Sentry canonical entities
-- (canonical_type, canonical_id). One row per (external entity ->
-- canonical entity) edge. Each external ID maps to exactly one
-- canonical entity (UNIQUE on the source side); a single canonical
-- entity may have mappings in many source systems (the canonical
-- side is an index, not a constraint).
--
-- Single bidirectional table, not per-resource-type tables, because
-- per-resource tables would proliferate to 25+ at v2.5 with five
-- connectors and cross-mapping needs. The composite UNIQUE on the
-- source side is the load-bearing constraint.
--
-- source_system FKs to inbound_source_systems_allowlist (mig 037)
-- so admin-side typos cannot create orphan source_systems.
--
-- Forensic instrumentation (V-157 pattern, mirrors wms_tokens_audit
-- mig 028 and inbound_source_systems_allowlist_audit mig 037):
-- statement-level DELETE / TRUNCATE triggers write to
-- cross_system_mappings_audit. cross_system_mappings is a
-- mapping-truth table; a silent wipe would unbind every external
-- entity from its canonical entity, so investigators need the same
-- "who / when / how-many / from-where" trail wms_tokens carries.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS cross_system_mappings (
    mapping_id       BIGSERIAL    PRIMARY KEY,
    source_system    VARCHAR(64)  NOT NULL REFERENCES inbound_source_systems_allowlist(source_system),
    source_type      VARCHAR(32)  NOT NULL,
    source_id        VARCHAR(128) NOT NULL,
    canonical_type   VARCHAR(32)  NOT NULL,
    canonical_id     UUID         NOT NULL,
    first_seen_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CHECK (source_type    IN ('sales_order','item','customer','vendor','purchase_order')),
    CHECK (canonical_type IN ('sales_order','item','customer','vendor','purchase_order'))
);

CREATE UNIQUE INDEX IF NOT EXISTS cross_system_mappings_source_unique
    ON cross_system_mappings (source_system, source_type, source_id);

CREATE INDEX IF NOT EXISTS cross_system_mappings_canonical
    ON cross_system_mappings (canonical_type, canonical_id);

-- Forensic audit (V-157 pattern, mirrors wms_tokens_audit / mig 028).
CREATE TABLE IF NOT EXISTS cross_system_mappings_audit (
    audit_id         BIGSERIAL    PRIMARY KEY,
    event_type       VARCHAR(16)  NOT NULL,
    rows_affected    INTEGER,
    sess_user        TEXT         NOT NULL,
    curr_user        TEXT         NOT NULL,
    backend_pid      INTEGER      NOT NULL,
    application_name TEXT,
    event_at         TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS cross_system_mappings_audit_event_at
    ON cross_system_mappings_audit (event_at DESC);

CREATE OR REPLACE FUNCTION cross_system_mappings_audit_delete()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    _count INTEGER;
BEGIN
    SELECT COUNT(*) INTO _count FROM deleted_rows;
    INSERT INTO cross_system_mappings_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'DELETE', _count, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tr_cross_system_mappings_audit_delete
    ON cross_system_mappings;
CREATE TRIGGER tr_cross_system_mappings_audit_delete
    AFTER DELETE ON cross_system_mappings
    REFERENCING OLD TABLE AS deleted_rows
    FOR EACH STATEMENT EXECUTE FUNCTION cross_system_mappings_audit_delete();

CREATE OR REPLACE FUNCTION cross_system_mappings_audit_truncate()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO cross_system_mappings_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'TRUNCATE', NULL, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tr_cross_system_mappings_audit_truncate
    ON cross_system_mappings;
CREATE TRIGGER tr_cross_system_mappings_audit_truncate
    AFTER TRUNCATE ON cross_system_mappings
    FOR EACH STATEMENT EXECUTE FUNCTION cross_system_mappings_audit_truncate();

COMMIT;
