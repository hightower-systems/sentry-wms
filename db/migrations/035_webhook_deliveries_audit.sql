-- ============================================================
-- Migration 035: webhook_deliveries DELETE+TRUNCATE forensic
-- instrumentation (#235; mirrors migration 032 / V-157)
-- ============================================================
-- Migration 032 instrumented webhook_subscriptions and
-- webhook_secrets with statement-level DELETE / TRUNCATE
-- forensic triggers; webhook_deliveries was left out.
-- cleanup_webhook_deliveries (90-day retention beat task) and
-- the cascade in the hard-delete admin path both DELETE rows
-- here, so a privileged-role error or compromised cleanup-task
-- role could mass-DELETE the per-attempt history with no
-- forensic surface. The audit_log table covers the original
-- mutations (subscription create, replay-single, replay-batch);
-- this trigger covers the wipe surface those records cannot
-- reconstruct from, bringing v1.6 to parity with the v1.5.1
-- forensic posture (#235).
--
-- Statement-level (FOR EACH STATEMENT) means a chunked cleanup
-- run produces one audit row per chunk, not per deleted row;
-- the rolling beat task (#228) lays down a small bounded
-- forensic trail.
--
-- TRUNCATE is recorded with rows_affected NULL because
-- transition tables expose DELETEd rows but not TRUNCATE rows;
-- the absence of a count is itself the signal that the
-- statement was a TRUNCATE (event_type column also names it).
--
-- Wrapped in BEGIN/COMMIT per V-213 #152: a partial apply
-- would leave webhook_deliveries with one trigger but not the
-- other, which is worse than no instrumentation because
-- investigators would assume coverage and miss the gap.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS webhook_deliveries_audit (
    audit_id          BIGSERIAL    PRIMARY KEY,
    event_type        VARCHAR(16)  NOT NULL,  -- 'DELETE' | 'TRUNCATE'
    rows_affected     INTEGER,
    sess_user         TEXT         NOT NULL,
    curr_user         TEXT         NOT NULL,
    backend_pid       INTEGER      NOT NULL,
    application_name  TEXT,
    event_at          TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS webhook_deliveries_audit_event_at
    ON webhook_deliveries_audit (event_at DESC);

CREATE OR REPLACE FUNCTION webhook_deliveries_audit_delete()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    _count INTEGER;
BEGIN
    SELECT COUNT(*) INTO _count FROM deleted_rows;
    INSERT INTO webhook_deliveries_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'DELETE', _count, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tr_webhook_deliveries_audit_delete ON webhook_deliveries;
CREATE TRIGGER tr_webhook_deliveries_audit_delete
    AFTER DELETE ON webhook_deliveries
    REFERENCING OLD TABLE AS deleted_rows
    FOR EACH STATEMENT EXECUTE FUNCTION webhook_deliveries_audit_delete();

CREATE OR REPLACE FUNCTION webhook_deliveries_audit_truncate()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO webhook_deliveries_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'TRUNCATE', NULL, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tr_webhook_deliveries_audit_truncate ON webhook_deliveries;
CREATE TRIGGER tr_webhook_deliveries_audit_truncate
    AFTER TRUNCATE ON webhook_deliveries
    FOR EACH STATEMENT EXECUTE FUNCTION webhook_deliveries_audit_truncate();

COMMIT;
