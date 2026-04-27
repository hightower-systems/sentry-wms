-- ============================================================
-- Migration 031: integration_events visibility NOTIFY trigger (v1.6.0 #164)
-- ============================================================
-- v1.6.0 introduces an outbound webhook dispatcher that POSTs
-- integration_events to consumer-supplied URLs as rows become
-- visible. The dispatcher's wake path mirrors the v1.5.0
-- snapshot-keeper pattern (#131 / migration 024): a dedicated
-- LISTEN connection on a pg_notify channel reduces wake latency
-- to sub-millisecond, with a 2-second fallback poll bounding the
-- consequences of a missed NOTIFY (NOTIFY is best-effort and is
-- not durable across a listener disconnect).
--
-- The chain at runtime is:
--
--   1. Emit-site INSERT into integration_events inside its own
--      transaction.
--   2. v1.5.0 deferred CONSTRAINT TRIGGER tr_integration_events_visible_at
--      fires at COMMIT and runs
--      "UPDATE integration_events SET visible_at = clock_timestamp()
--       WHERE event_id = NEW.event_id".
--   3. This migration's AFTER UPDATE OF visible_at trigger fires
--      and runs pg_notify('integration_events_visible', event_id::text)
--      when the row transitions visible_at NULL -> NOT NULL.
--   4. Outer COMMIT releases the queued NOTIFY to listeners.
--
-- Correctness lives on the per-subscription cursor
-- (webhook_subscriptions.last_delivered_event_id, added in
-- migration 029); NOTIFY is latency reduction only. The fallback
-- poll catches any NOTIFY lost to listener restart or backend
-- shutdown.
--
-- The trigger is intentionally bound to UPDATE OF visible_at, not
-- AFTER UPDATE in general, so the trigger does not fire on
-- unrelated UPDATEs (none exist in v1.6.0, but we want the trigger
-- to remain a no-op if a future migration adds an UPDATE path on
-- another column). The function additionally gates on the
-- NULL -> NOT NULL transition so an idempotent re-stamp of
-- visible_at (e.g. an admin tool re-running the deferred trigger
-- by updating a row that already has visible_at set) does not
-- emit a duplicate NOTIFY that would force the dispatcher to
-- re-evaluate an already-considered event.
--
-- Corollary of the gate: an emit site that bypasses
-- events_service.emit() and INSERTs with visible_at supplied
-- inline will land a row whose visible_at is already NOT NULL by
-- the time the deferred trigger's UPDATE runs, so the gate
-- silences the NOTIFY for that row. The 2s fallback poll wakes
-- the dispatcher in that case. Use events_service.emit()
-- (visible_at NULL at INSERT) to keep the wake path warm.
--
-- Wrapped in BEGIN/COMMIT per V-213 #152 migration discipline so
-- a partial apply does not leave the function defined without
-- the trigger that calls it.
-- ============================================================

BEGIN;

CREATE OR REPLACE FUNCTION notify_integration_event_visible()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.visible_at IS NOT NULL AND OLD.visible_at IS NULL THEN
        PERFORM pg_notify('integration_events_visible', NEW.event_id::text);
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS tr_integration_events_notify ON integration_events;
CREATE TRIGGER tr_integration_events_notify
    AFTER UPDATE OF visible_at ON integration_events
    FOR EACH ROW
    EXECUTE FUNCTION notify_integration_event_visible();

COMMIT;
