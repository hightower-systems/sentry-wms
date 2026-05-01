-- ============================================================
-- Migration 032: webhook_subscriptions / webhook_secrets DELETE+TRUNCATE
-- forensic instrumentation (v1.6.0 #167; mirrors V-157 #157)
-- ============================================================
-- The v1.5.0 pre-merge gate (#135) saw wms_tokens unexpectedly
-- emptied between Gate 11 and Gate 12. Root cause was never
-- established; the only visible evidence was "rows I just
-- inserted are gone". v1.5.1 migration 028 instrumented
-- wms_tokens so every DELETE / TRUNCATE leaves a forensic row
-- in wms_tokens_audit. v1.6.0 extends the same shape to the new
-- sensitive tables (webhook_subscriptions and webhook_secrets)
-- AT LANDING TIME, not after an incident.
--
-- The instrumentation is forensic-only. It does not block
-- DELETE / TRUNCATE; it records who did it. Each audit row
-- captures:
--
--   * event_type         -- 'DELETE' | 'TRUNCATE'
--   * rows_affected      -- DELETE only (transition table COUNT);
--                           TRUNCATE rows are not exposed via a
--                           transition table, so the column is NULL
--                           for TRUNCATE events.
--   * sess_user          -- SESSION_USER (the original login role)
--   * curr_user          -- CURRENT_USER (effective role after SET ROLE)
--   * backend_pid        -- pg_backend_pid() (which Postgres backend)
--   * application_name   -- libpq application_name if set
--   * event_at           -- clock_timestamp(), NOT NOW(); a long-running
--                           transaction would otherwise obscure when the
--                           statement actually fired
--
-- Triggers are statement-level (FOR EACH STATEMENT) so a
-- wipe-the-world DELETE produces exactly one audit row, not N.
-- The transition-tables feature (REFERENCING OLD TABLE AS
-- deleted_rows) lets the DELETE trigger COUNT the affected rows
-- without a row-level firing.
--
-- The two source tables have identical instrumentation shape;
-- the two audit tables are kept separate so a refactor of either
-- (e.g. adding a column to webhook_subscriptions_audit) does not
-- couple the schema of the other.
--
-- Wrapped in BEGIN/COMMIT per V-213 #152 discipline so a partial
-- apply cannot leave one audit table + trigger pair present
-- without the other (an asymmetric forensic surface is worse
-- than no surface, because investigators would assume coverage
-- and miss the gap).
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- webhook_subscriptions audit
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS webhook_subscriptions_audit (
    audit_id          BIGSERIAL    PRIMARY KEY,
    event_type        VARCHAR(16)  NOT NULL,  -- 'DELETE' | 'TRUNCATE'
    rows_affected     INTEGER,
    sess_user         TEXT         NOT NULL,
    curr_user         TEXT         NOT NULL,
    backend_pid       INTEGER      NOT NULL,
    application_name  TEXT,
    event_at          TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS webhook_subscriptions_audit_event_at
    ON webhook_subscriptions_audit (event_at DESC);

CREATE OR REPLACE FUNCTION webhook_subscriptions_audit_delete()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    _count INTEGER;
BEGIN
    SELECT COUNT(*) INTO _count FROM deleted_rows;
    INSERT INTO webhook_subscriptions_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'DELETE', _count, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tr_webhook_subscriptions_audit_delete ON webhook_subscriptions;
CREATE TRIGGER tr_webhook_subscriptions_audit_delete
    AFTER DELETE ON webhook_subscriptions
    REFERENCING OLD TABLE AS deleted_rows
    FOR EACH STATEMENT EXECUTE FUNCTION webhook_subscriptions_audit_delete();

CREATE OR REPLACE FUNCTION webhook_subscriptions_audit_truncate()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO webhook_subscriptions_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'TRUNCATE', NULL, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tr_webhook_subscriptions_audit_truncate ON webhook_subscriptions;
CREATE TRIGGER tr_webhook_subscriptions_audit_truncate
    AFTER TRUNCATE ON webhook_subscriptions
    FOR EACH STATEMENT EXECUTE FUNCTION webhook_subscriptions_audit_truncate();

-- ------------------------------------------------------------
-- webhook_secrets audit
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS webhook_secrets_audit (
    audit_id          BIGSERIAL    PRIMARY KEY,
    event_type        VARCHAR(16)  NOT NULL,
    rows_affected     INTEGER,
    sess_user         TEXT         NOT NULL,
    curr_user         TEXT         NOT NULL,
    backend_pid       INTEGER      NOT NULL,
    application_name  TEXT,
    event_at          TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS webhook_secrets_audit_event_at
    ON webhook_secrets_audit (event_at DESC);

CREATE OR REPLACE FUNCTION webhook_secrets_audit_delete()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    _count INTEGER;
BEGIN
    SELECT COUNT(*) INTO _count FROM deleted_rows;
    INSERT INTO webhook_secrets_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'DELETE', _count, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tr_webhook_secrets_audit_delete ON webhook_secrets;
CREATE TRIGGER tr_webhook_secrets_audit_delete
    AFTER DELETE ON webhook_secrets
    REFERENCING OLD TABLE AS deleted_rows
    FOR EACH STATEMENT EXECUTE FUNCTION webhook_secrets_audit_delete();

CREATE OR REPLACE FUNCTION webhook_secrets_audit_truncate()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO webhook_secrets_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'TRUNCATE', NULL, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tr_webhook_secrets_audit_truncate ON webhook_secrets;
CREATE TRIGGER tr_webhook_secrets_audit_truncate
    AFTER TRUNCATE ON webhook_secrets
    FOR EACH STATEMENT EXECUTE FUNCTION webhook_secrets_audit_truncate();

COMMIT;
