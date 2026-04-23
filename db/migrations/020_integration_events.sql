-- ============================================================
-- Migration 020: integration_events outbox + external_id retrofit (v1.5.0)
-- ============================================================
-- v1.5.0 introduces a transactional outbox so external systems can poll
-- every inventory-changing write via /api/v1/events. This migration
-- delivers three pieces:
--
-- 1. An external_id UUID column on ten aggregate/actor tables that now
--    carry a UUID on the wire (event envelope references never expose
--    internal integer PKs). Column is added with
--    DEFAULT gen_random_uuid() so existing INSERTs continue to work
--    unchanged; a follow-up migration 025 drops the DEFAULT after every
--    insert site is retrofitted to supply an explicit value (issue #108).
--
-- 2. The integration_events table itself, plus four btree indexes that
--    support the poll query shapes in v1.5.0 (cursor, per-warehouse,
--    per-type, and the visibility gate).
--
-- 3. A deferred-constraint trigger that sets visible_at at COMMIT time.
--    BIGSERIAL assigns event_id at INSERT time but transactions commit
--    out of order; without a visibility gate a poller can skip events
--    when a high-id transaction commits before a low-id one. Readers
--    filter "visible_at <= NOW() - INTERVAL '2 seconds' AND event_id >
--    cursor"; combined with the DEFERRABLE trigger this gives
--    per-aggregate FIFO without discipline at each emit site.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- v1.5.1 V-213 (#152): wrap the ten ALTER TABLE statements below in a
-- single transaction so a failure on one table (lock timeout, disk
-- full, unexpected schema drift) rolls back ALL of them. Pre-v1.5.1
-- each ALTER committed on its own; a partial apply followed by an
-- operator skipping ahead to migration 025 (which drops the DEFAULT)
-- left the later tables without an external_id column, and every
-- subsequent insert failed with a NOT NULL violation. The transaction
-- wrapper keeps the schema change all-or-nothing.
BEGIN;

-- External UUID retrofit on the ten aggregate/actor tables. UNIQUE is
-- inline so the implicit index name is consistent across fresh installs
-- (schema.sql) and upgrades (this migration). IF NOT EXISTS keeps the
-- ALTER idempotent on re-run.
ALTER TABLE users                 ADD COLUMN IF NOT EXISTS external_id UUID UNIQUE NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE items                 ADD COLUMN IF NOT EXISTS external_id UUID UNIQUE NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE bins                  ADD COLUMN IF NOT EXISTS external_id UUID UNIQUE NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE sales_orders          ADD COLUMN IF NOT EXISTS external_id UUID UNIQUE NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE purchase_orders       ADD COLUMN IF NOT EXISTS external_id UUID UNIQUE NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE item_receipts         ADD COLUMN IF NOT EXISTS external_id UUID UNIQUE NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE inventory_adjustments ADD COLUMN IF NOT EXISTS external_id UUID UNIQUE NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE bin_transfers         ADD COLUMN IF NOT EXISTS external_id UUID UNIQUE NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE cycle_counts          ADD COLUMN IF NOT EXISTS external_id UUID UNIQUE NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE item_fulfillments     ADD COLUMN IF NOT EXISTS external_id UUID UNIQUE NOT NULL DEFAULT gen_random_uuid();

COMMIT;

-- Transactional outbox. Every inventory-changing handler writes one row
-- here inside its existing transaction (see api/services/events_service.py
-- for the emit helper). aggregate_external_id is denormalized from the
-- aggregate row at emit time; the column is immutable on the aggregate
-- (UNIQUE) so copying it is safe and the poll query does not join back.
CREATE TABLE IF NOT EXISTS integration_events (
    event_id              BIGSERIAL    PRIMARY KEY,
    event_type            VARCHAR(64)  NOT NULL,
    event_version         SMALLINT     NOT NULL DEFAULT 1,
    event_timestamp       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    aggregate_type        VARCHAR(32)  NOT NULL,
    aggregate_id          BIGINT       NOT NULL,
    aggregate_external_id UUID         NOT NULL,
    warehouse_id          INT          NOT NULL REFERENCES warehouses(warehouse_id),
    source_txn_id         UUID         NOT NULL,
    visible_at            TIMESTAMPTZ,
    payload               JSONB        NOT NULL,
    CONSTRAINT integration_events_idempotency_key
        UNIQUE (aggregate_type, aggregate_id, event_type, source_txn_id)
);

CREATE INDEX IF NOT EXISTS ix_integration_events_warehouse_event
    ON integration_events (warehouse_id, event_id);
CREATE INDEX IF NOT EXISTS ix_integration_events_type_event
    ON integration_events (event_type, event_id);
CREATE INDEX IF NOT EXISTS ix_integration_events_visible_at
    ON integration_events (visible_at)
    WHERE visible_at IS NOT NULL;

-- Deferred trigger: set visible_at at COMMIT time so readers see events
-- in commit order even when BIGSERIAL assigned event_ids in a different
-- order. DEFERRABLE INITIALLY DEFERRED fires the trigger at the end of
-- the transaction, right before COMMIT releases locks. The trigger
-- function is a no-op on UPDATE/DELETE since it is bound to INSERT only.
CREATE OR REPLACE FUNCTION set_integration_event_visible_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    UPDATE integration_events
       SET visible_at = clock_timestamp()
     WHERE event_id = NEW.event_id;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tr_integration_events_visible_at ON integration_events;
CREATE CONSTRAINT TRIGGER tr_integration_events_visible_at
    AFTER INSERT ON integration_events
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION set_integration_event_visible_at();
