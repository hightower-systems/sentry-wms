-- ============================================================
-- Migration 024: snapshot_scans table + keeper NOTIFY trigger (v1.5.0 #131)
-- ============================================================
-- Per-scan metadata for GET /api/v1/snapshot/inventory (plan 4.1).
-- The API tier INSERTs a 'pending' row to request a scan; the
-- snapshot-keeper daemon (#132) picks it up, opens a REPEATABLE READ
-- transaction, captures snapshot_event_id, exports a pg_snapshot_id
-- via pg_export_snapshot(), stores both back on the row, and flips
-- status to 'active'. The API then pages through the snapshot using
-- SET TRANSACTION SNAPSHOT on short-lived connections.
--
-- Keeper wake-up: AFTER INSERT trigger fires NOTIFY
-- 'snapshot_scans_pending' so the keeper's LISTEN sees new pending
-- rows with sub-ms latency. A 1-second fallback poll catches NOTIFYs
-- missed during keeper restarts (NOTIFY is not durable across a
-- disconnect).
--
-- Dependencies:
-- - #109 (integration_events) for snapshot_event_id capture
-- - #127 (wms_tokens) for the created_by_token_id FK, used by the
--   cursor-tamper check in the snapshot endpoint (#133)
-- ============================================================

CREATE TABLE IF NOT EXISTS snapshot_scans (
    scan_id              UUID          PRIMARY KEY,
    pg_snapshot_id       TEXT,
    snapshot_event_id    BIGINT,
    warehouse_id         INTEGER       NOT NULL,
    started_at           TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    last_accessed_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    status               VARCHAR(16)   NOT NULL DEFAULT 'pending',
    created_by_token_id  BIGINT        REFERENCES wms_tokens(token_id)
);

-- Keeper's poll query: "SELECT ... FROM snapshot_scans WHERE
-- status='pending' ORDER BY started_at LIMIT N". The composite index
-- covers both the filter and the sort in one scan.
CREATE INDEX IF NOT EXISTS snapshot_scans_status_started
    ON snapshot_scans (status, started_at);

CREATE OR REPLACE FUNCTION notify_snapshot_scans_pending()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'pending' THEN
        PERFORM pg_notify('snapshot_scans_pending', NEW.scan_id::text);
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS tr_snapshot_scans_notify ON snapshot_scans;
CREATE TRIGGER tr_snapshot_scans_notify
    AFTER INSERT ON snapshot_scans
    FOR EACH ROW EXECUTE FUNCTION notify_snapshot_scans_pending();
