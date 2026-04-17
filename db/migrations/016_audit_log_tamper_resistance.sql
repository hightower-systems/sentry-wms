-- ============================================================
-- Migration 016: Audit log tamper resistance (V-025)
-- ============================================================
-- Adds a SHA-256 hash chain to audit_log so tampering is detectable,
-- and installs triggers that reject UPDATE and DELETE on the table.
-- TRUNCATE is intentionally NOT blocked so the test suite can reset
-- state between runs; production deployments should REVOKE TRUNCATE
-- on audit_log from the application role.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE audit_log
    ADD COLUMN IF NOT EXISTS prev_hash BYTEA,
    ADD COLUMN IF NOT EXISTS row_hash BYTEA;

-- Chain hash: every row's row_hash = sha256(prev_hash || payload), and
-- prev_hash is the row_hash of the most recent existing row. Any
-- retroactive edit to a committed row breaks the chain because
-- downstream row_hash values no longer match. Auditors run
-- verify_audit_log_chain() (see below) to detect tampering.
CREATE OR REPLACE FUNCTION audit_log_chain_hash() RETURNS TRIGGER AS $$
DECLARE
    prev BYTEA;
    payload TEXT;
BEGIN
    SELECT row_hash INTO prev FROM audit_log ORDER BY log_id DESC LIMIT 1;
    NEW.prev_hash := COALESCE(prev, '\x00'::bytea);

    payload := COALESCE(NEW.action_type, '') || '|' ||
               COALESCE(NEW.entity_type, '') || '|' ||
               COALESCE(NEW.entity_id::text, '') || '|' ||
               COALESCE(NEW.user_id, '') || '|' ||
               COALESCE(NEW.warehouse_id::text, '') || '|' ||
               COALESCE(NEW.details::text, '') || '|' ||
               COALESCE(NEW.created_at::text, NOW()::text);

    NEW.row_hash := digest(NEW.prev_hash || payload::bytea, 'sha256');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_chain_before_insert ON audit_log;
CREATE TRIGGER audit_log_chain_before_insert
    BEFORE INSERT ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_chain_hash();

-- Reject UPDATE and DELETE so an attacker who somehow acquires the
-- application DB role cannot redact or forge past events via DML.
-- A dedicated archival role (out of scope for v1.3) would have these
-- privileges granted separately; by default the app role can only
-- INSERT into audit_log.
CREATE OR REPLACE FUNCTION audit_log_reject_mutation() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_log rows are append-only (V-025 tamper resistance)';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log;
CREATE TRIGGER audit_log_no_update
    BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_reject_mutation();

DROP TRIGGER IF EXISTS audit_log_no_delete ON audit_log;
CREATE TRIGGER audit_log_no_delete
    BEFORE DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_reject_mutation();

-- Chain verification helper. Returns NULL when the chain is intact;
-- returns the log_id of the first corrupted/missing link otherwise.
-- Run periodically from operations tooling:
--     SELECT verify_audit_log_chain();
CREATE OR REPLACE FUNCTION verify_audit_log_chain() RETURNS BIGINT AS $$
DECLARE
    prev BYTEA := '\x00'::bytea;
    r RECORD;
    computed BYTEA;
    payload TEXT;
BEGIN
    FOR r IN SELECT * FROM audit_log ORDER BY log_id ASC LOOP
        IF r.prev_hash IS DISTINCT FROM prev THEN
            RETURN r.log_id;
        END IF;
        payload := COALESCE(r.action_type, '') || '|' ||
                   COALESCE(r.entity_type, '') || '|' ||
                   COALESCE(r.entity_id::text, '') || '|' ||
                   COALESCE(r.user_id, '') || '|' ||
                   COALESCE(r.warehouse_id::text, '') || '|' ||
                   COALESCE(r.details::text, '') || '|' ||
                   COALESCE(r.created_at::text, '');
        computed := digest(r.prev_hash || payload::bytea, 'sha256');
        IF computed IS DISTINCT FROM r.row_hash THEN
            RETURN r.log_id;
        END IF;
        prev := r.row_hash;
    END LOOP;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
