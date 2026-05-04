-- ============================================================
-- Migration 044: inbound_cleanup_runs log table (v1.7.0)
-- ============================================================
-- Tracks executions of the Celery beat task that nullifies
-- source_payload on inbound_<resource> rows older than
-- SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS (floor 7, V-201
-- shape; boot fails loud on values < 7 to prevent a typo from
-- wiping forensic context).
--
-- The beat task itself is Python code and lands in a separate
-- commit; this migration only ships the log table so forensic
-- queries ("when did the beat last run? which resources? how
-- many rows nullified?") can run from day one.
--
-- One row per (resource, run) tuple. Each beat invocation iterates
-- the five staging tables and inserts one log row per resource so
-- a partial failure (e.g., a single resource hits a lock-timeout
-- and aborts) is visible without inferring it from missing rows.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS inbound_cleanup_runs (
    run_id              BIGSERIAL    PRIMARY KEY,
    resource            VARCHAR(32)  NOT NULL,
    started_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    finished_at         TIMESTAMPTZ,
    rows_nullified      INTEGER      NOT NULL DEFAULT 0,
    retention_days      INTEGER      NOT NULL,
    status              VARCHAR(16)  NOT NULL DEFAULT 'running',
    error_message       TEXT,
    CHECK (resource IN ('sales_orders','items','customers','vendors','purchase_orders')),
    CHECK (status   IN ('running','succeeded','failed'))
);

-- Operator queries: "show me the last beat run per resource"
-- and "show me failures across all resources in the last 24h".
CREATE INDEX IF NOT EXISTS inbound_cleanup_runs_resource_started
    ON inbound_cleanup_runs (resource, started_at DESC);

CREATE INDEX IF NOT EXISTS inbound_cleanup_runs_status_started
    ON inbound_cleanup_runs (status, started_at DESC)
    WHERE status = 'failed';

COMMIT;
