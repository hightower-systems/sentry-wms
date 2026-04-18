-- ============================================================
-- Migration 018: Sync state run_id for race detection (V-102)
-- ============================================================
-- Adds a run_id UUID column to sync_state. _set_running_impl mints a
-- new UUID every time it marks a row running and persists it. Celery
-- workers carry the run_id forward and pass it to set_success_standalone
-- / set_error_standalone, which only apply when the row's current
-- run_id matches. When a stale 'running' row is taken over (V-012
-- RUNNING_TIMEOUT), the takeover writes a fresh run_id; the original
-- worker's completion therefore no-ops against the new run's state
-- instead of clobbering it.
-- ============================================================

ALTER TABLE sync_state
    ADD COLUMN IF NOT EXISTS run_id UUID;

-- Back-fill any currently-running rows with a fresh UUID so completions
-- in flight at deploy time can still match (they don't know about the
-- migration; their later set_success_standalone calls omit run_id and
-- fall through the no-match path). Non-running rows stay NULL.
UPDATE sync_state
    SET run_id = gen_random_uuid()
    WHERE sync_status = 'running' AND run_id IS NULL;
