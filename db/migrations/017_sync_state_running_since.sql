-- ============================================================
-- Migration 017: Sync state running-since timestamp (V-012)
-- ============================================================
-- Adds a running_since TIMESTAMPTZ column to sync_state so a crashed
-- or killed worker that left the state as 'running' cannot block
-- future syncs forever. The service layer treats a running row older
-- than RUNNING_TIMEOUT (1 hour) as stale and allows a new run to
-- take over. A manual admin reset endpoint covers cases where the
-- timeout is not enough.
-- ============================================================

ALTER TABLE sync_state
    ADD COLUMN IF NOT EXISTS running_since TIMESTAMPTZ;

-- Back-fill: any row currently in 'running' state gets NOW() so it
-- does not immediately become stale on deploy. Rows not in running
-- state stay NULL.
UPDATE sync_state SET running_since = NOW()
    WHERE sync_status = 'running' AND running_since IS NULL;
