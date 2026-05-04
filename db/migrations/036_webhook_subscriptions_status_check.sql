-- ============================================================
-- Migration 036: webhook_subscriptions.status + pause_reason
-- CHECK constraints (#236)
-- ============================================================
-- Migration 029's comment acknowledged the gap: "Status validation
-- is application side; this migration enforces only that the
-- column exists with a default of 'active'." That asymmetry is
-- the bug. webhook_deliveries.status carries a CHECK enum
-- (migration 030); webhook_subscriptions.status does not. A
-- direct DB UPDATE could write any 16-character value, and the
-- dispatcher's status='active' gate would silently stop
-- dispatching without an audit-log surface.
--
-- The application layer rejects out-of-band values:
--   * Pydantic UpdateWebhookRequest accepts only 'active' /
--     'paused'.
--   * DELETE handler is the only writer of 'revoked'.
--   * Dispatcher auto-pause writes 'paused' with a known
--     pause_reason.
-- This migration adds the bottom-rung enforcement at the column
-- itself so a privileged-role error or malicious migration
-- cannot bypass the contract.
--
-- pause_reason gets the same treatment. Allowed values:
--   * 'manual'           -- admin PATCH status='paused'
--   * 'pending_ceiling'  -- dispatcher auto-pause #170
--   * 'dlq_ceiling'      -- dispatcher auto-pause #170
--   * 'malformed_filter' -- dispatcher auto-pause #232 (V-314)
--
-- The malformed_filter value MUST be in this CHECK list because
-- migration 035's V-314 helper writes it. Order matters: this
-- migration ships AFTER V-314 so the value is in use before the
-- constraint locks it down (any prior auto-pause rows already
-- have a valid value).
--
-- Step 1 cleans up any out-of-band rows so the ALTER TABLE ADD
-- CONSTRAINT does not fail. Defensive: there should be no such
-- rows in a healthy deployment, but a partial-application
-- recovery shape stays robust.
--
-- Wrapped in BEGIN/COMMIT (V-213): a partial apply that left
-- the table without one of the two CHECKs would silently re-
-- open the gap on the missing column.
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- Step 1: cleanup any out-of-band values before the constraint.
-- ------------------------------------------------------------
-- A row with an unknown status is operator error or migration
-- corruption; either way, the safest landing is to flip it to
-- 'paused' with pause_reason='manual' so an operator notices in
-- triage and explicitly resumes after investigation.
UPDATE webhook_subscriptions
   SET status = 'paused',
       pause_reason = 'manual'
 WHERE status NOT IN ('active', 'paused', 'revoked');

UPDATE webhook_subscriptions
   SET pause_reason = NULL
 WHERE pause_reason IS NOT NULL
   AND pause_reason NOT IN (
       'manual', 'pending_ceiling', 'dlq_ceiling', 'malformed_filter'
   );

-- ------------------------------------------------------------
-- Step 2: add CHECK constraints.
-- ------------------------------------------------------------
ALTER TABLE webhook_subscriptions
    ADD CONSTRAINT webhook_subscriptions_status_enum
        CHECK (status IN ('active', 'paused', 'revoked'));

ALTER TABLE webhook_subscriptions
    ADD CONSTRAINT webhook_subscriptions_pause_reason_enum
        CHECK (
            pause_reason IS NULL
            OR pause_reason IN (
                'manual',
                'pending_ceiling',
                'dlq_ceiling',
                'malformed_filter'
            )
        );

COMMIT;
