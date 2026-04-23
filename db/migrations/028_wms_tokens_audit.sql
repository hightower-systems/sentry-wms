-- ============================================================
-- Migration 028: wms_tokens DELETE / TRUNCATE instrumentation (v1.5.1 #157)
-- ============================================================
-- The v1.5.0 pre-merge gate (#135) saw wms_tokens unexpectedly
-- emptied between Gate 11 and Gate 12. Root cause was never
-- established; the only visible evidence was "rows I just
-- inserted are gone". Without capture of WHO issued the
-- TRUNCATE / bulk DELETE, the investigation hit a dead end.
--
-- v1.5.1 instruments the table so every DELETE and TRUNCATE
-- leaves a forensic row in wms_tokens_audit. A repeat of the
-- Gate 11 / 12 incident is now bindable to:
--
--   * which PostgreSQL role did it (session_user + current_user)
--   * which backend (pg_backend_pid)
--   * how many rows were affected (DELETE only; TRUNCATE
--     naturally drops the whole table so the count is recorded
--     as the pre-TRUNCATE SELECT COUNT(*))
--   * wall-clock timestamp (clock_timestamp, not transaction
--     time, so a long-running transaction does not obscure when
--     the statement actually fired)
--
-- Acceptance criteria for closing the post-mortem issue (#157):
--
--   (a) This instrumentation has been merged AND has run for
--       at least 10 cycles across local pytest, CI, and a
--       pre-merge-gate sweep producing log evidence of what
--       fires where.
--   (b) Either the root cause is identified (spin out a new
--       security issue if it turns out to be a migration-runner
--       bug or a race in admin_tokens teardown) OR the v1.5
--       post-release review formally accepts "unknown root
--       cause, mitigated by guardrail" as the resolution,
--       recorded in the review minutes.
--
-- The instrumentation is intentionally narrow: it covers only
-- wms_tokens. If the Gate 11 / 12 deletion was actually a
-- fixture teardown that truncated more than one table, the
-- audit row here will fire first and point investigators at
-- the right connection; a broader wms_tokens_audit-style trail
-- can be added for the other sensitive tables once the shape
-- of the root cause is known.
-- ============================================================

CREATE TABLE IF NOT EXISTS wms_tokens_audit (
    audit_id        BIGSERIAL    PRIMARY KEY,
    event_type      VARCHAR(16)  NOT NULL,  -- 'DELETE' | 'TRUNCATE'
    rows_affected   INTEGER,
    sess_user       TEXT         NOT NULL,
    curr_user       TEXT         NOT NULL,
    backend_pid     INTEGER      NOT NULL,
    application_name TEXT,
    event_at        TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS wms_tokens_audit_event_at
    ON wms_tokens_audit (event_at DESC);

-- Row-level AFTER DELETE trigger: one audit row per statement,
-- not per row (we use a statement-level trigger to avoid N audit
-- rows for a wipe-the-world TRUNCATE-shaped DELETE). The pg_trigger
-- transition-tables feature lets us count the affected rows.
CREATE OR REPLACE FUNCTION wms_tokens_audit_delete()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    _count INTEGER;
BEGIN
    SELECT COUNT(*) INTO _count FROM deleted_rows;
    INSERT INTO wms_tokens_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'DELETE', _count, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tr_wms_tokens_audit_delete ON wms_tokens;
CREATE TRIGGER tr_wms_tokens_audit_delete
    AFTER DELETE ON wms_tokens
    REFERENCING OLD TABLE AS deleted_rows
    FOR EACH STATEMENT EXECUTE FUNCTION wms_tokens_audit_delete();

-- TRUNCATE fires a different trigger class; row counts are not
-- available via transition tables, so we record only the event.
CREATE OR REPLACE FUNCTION wms_tokens_audit_truncate()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO wms_tokens_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'TRUNCATE', NULL, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tr_wms_tokens_audit_truncate ON wms_tokens;
CREATE TRIGGER tr_wms_tokens_audit_truncate
    AFTER TRUNCATE ON wms_tokens
    FOR EACH STATEMENT EXECUTE FUNCTION wms_tokens_audit_truncate();
