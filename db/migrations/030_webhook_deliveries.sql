-- ============================================================
-- Migration 030: webhook_deliveries (v1.6.0 #166)
-- ============================================================
-- Per-attempt log for the v1.6.0 outbound webhook dispatcher.
-- Append-only with one exception: the terminal `dlq` transition
-- flips the same row that was last `in_flight` (no fresh row),
-- so the table represents N+1 rows for an event that retried N
-- times before terminating. Every retry slot is a fresh
-- `pending` row; cursor advances strictly on terminal state
-- (`succeeded` or `dlq`), never on in-progress rows.
--
-- Key shape decisions (locked at the top of the v1.6.0 plan):
--
--   * BIGSERIAL delivery_id is per-attempt, not per-event. The
--     column doubles as a chronological order key for triage,
--     which is why the `latest` index sorts by delivery_id DESC
--     within (subscription_id, event_id).
--
--   * subscription_id FK is ON DELETE RESTRICT, not CASCADE.
--     Losing delivery history on a subscription delete would
--     erase forensic state; soft delete (status='revoked') is
--     the supported path. Hard delete via the admin endpoint
--     requires `?purge=true` AND no `pending`/`in_flight` rows
--     to exist (separate commit; the FK enforces).
--
--   * event_id has NO FK to integration_events. The plan
--     defers integration_events partitioning to v2.1; a
--     partitioned parent makes the FK awkward, and logical
--     integrity is sufficient given the cursor-based dispatch
--     contract (events that vanish from integration_events
--     during the delivery window are anomalies the dispatcher
--     should surface as a fault, not a silent success).
--
--   * attempt_number is bounded 1..8 by CHECK because the
--     retry schedule is hard-coded
--     [1s, 4s, 15s, 60s, 5m, 30m, 2h, 12h] (~15h cumulative).
--     The CHECK is the bottom rung that catches a refactor
--     that accidentally extended the schedule without revisiting
--     the DLQ ceiling math.
--
--   * status is CHECKed against the five enumerated states.
--     error_kind is NOT CHECKed at the column because the enum
--     will likely grow with consumer feedback in v1.6.x; the
--     application enum is the source of truth, the column is a
--     forensic record that should not refuse a value the
--     dispatcher chose to write.
--
--   * Four indexes pinned to specific query paths:
--
--       1. webhook_deliveries_dispatch
--          (subscription_id, scheduled_at) WHERE status='pending'
--          - dispatcher: "what's next for this subscription?"
--
--       2. webhook_deliveries_latest
--          (subscription_id, event_id, delivery_id DESC)
--          - admin/triage: "what was the latest attempt for
--            this (subscription, event)?" no predicate; covers
--            cross-status lookups.
--
--       3. webhook_deliveries_dlq
--          (subscription_id, completed_at) WHERE status='dlq'
--          - DLQ viewer pagination.
--
--       4. webhook_deliveries_pending_count
--          (subscription_id) WHERE status IN ('pending','in_flight')
--          - pending-count check used by the pending-ceiling
--            auto-pause path.
--
--     Storing response bodies would explode the table under
--     fan-out; response_body_hash (sha256 hex) plus
--     error_detail (first 512 chars) diagnoses most failures.
--
-- Wrapped in BEGIN/COMMIT per V-213 #152 discipline so a
-- partial apply cannot leave the table present without all
-- four indexes (any subset would silently slow the dispatcher's
-- hot loop until the next migration runner pass).
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id          BIGSERIAL    PRIMARY KEY,
    subscription_id      UUID         NOT NULL REFERENCES webhook_subscriptions(subscription_id) ON DELETE RESTRICT,
    event_id             BIGINT       NOT NULL,
    attempt_number       SMALLINT     NOT NULL,
    status               VARCHAR(16)  NOT NULL,
    scheduled_at         TIMESTAMPTZ  NOT NULL,
    attempted_at         TIMESTAMPTZ,
    completed_at         TIMESTAMPTZ,
    http_status          SMALLINT,
    response_body_hash   CHAR(64),
    response_time_ms     INTEGER,
    error_kind           VARCHAR(32),
    error_detail         VARCHAR(512),
    secret_generation    SMALLINT     NOT NULL,
    CONSTRAINT webhook_deliveries_attempt_number_range
        CHECK (attempt_number BETWEEN 1 AND 8),
    CONSTRAINT webhook_deliveries_status_enum
        CHECK (status IN ('pending', 'in_flight', 'succeeded', 'failed', 'dlq'))
);

CREATE INDEX IF NOT EXISTS webhook_deliveries_dispatch
    ON webhook_deliveries (subscription_id, scheduled_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS webhook_deliveries_latest
    ON webhook_deliveries (subscription_id, event_id, delivery_id DESC);

CREATE INDEX IF NOT EXISTS webhook_deliveries_dlq
    ON webhook_deliveries (subscription_id, completed_at)
    WHERE status = 'dlq';

CREATE INDEX IF NOT EXISTS webhook_deliveries_pending_count
    ON webhook_deliveries (subscription_id)
    WHERE status IN ('pending', 'in_flight');

COMMIT;
