-- ============================================================
-- Migration 054: dockd integration support (v1.9.0)
-- ============================================================
-- Adds the schema dockd's shipping surface needs:
--
--   item_fulfillments.pre_ship_status -- revert target for void
--   item_fulfillments.voided_at       -- audit + status timeline
--   item_fulfillments.voided_by       -- operator attribution
--   item_fulfillments.void_reason     -- free-text reason (<=500c)
--   item_fulfillments.shipping_cost   -- ShipRush-returned cost,
--                                        v1.x carrier-optimization
--                                        reporting will read this
--
--   dockd_idempotency                 -- HTTP-layer idempotency
--                                        cache, 72h TTL, pruned
--                                        daily by the existing
--                                        prune cron
--
-- v1.8.0 mig discipline (#213): SET lock_timeout / statement_timeout
-- at the top so a slow ALTER fails fast. BEGIN/COMMIT-wrapped.
-- Column widths follow the surrounding item_fulfillments shape:
-- VARCHAR(20) for status, VARCHAR(100) for username (matching
-- shipped_by). Money column is NUMERIC(12,2), matching the
-- order_total / customer_shipping_paid widths added in mig 050.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

ALTER TABLE item_fulfillments
    ADD COLUMN IF NOT EXISTS pre_ship_status VARCHAR(20),
    ADD COLUMN IF NOT EXISTS voided_at       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS voided_by       VARCHAR(100),
    ADD COLUMN IF NOT EXISTS void_reason     VARCHAR(500),
    ADD COLUMN IF NOT EXISTS shipping_cost   NUMERIC(12,2);

-- Backfill: pre-migration SHIPPED fulfillments have NULL
-- pre_ship_status. The dockd void route reads this column for the
-- revert target. 'PICKED' is the safe default; the void flow does
-- not depend on whether packing was required at original ship time.
UPDATE item_fulfillments
   SET pre_ship_status = 'PICKED'
 WHERE status = 'SHIPPED'
   AND pre_ship_status IS NULL;

-- HTTP-layer idempotency for the dockd surface. Per-token namespace
-- so collisions across tokens are fine. Pruned by the daily cron.
-- response_body / response_status are NULLABLE because the row is
-- inserted as a sentinel at the start of the request transaction
-- and populated before commit.
CREATE TABLE IF NOT EXISTS dockd_idempotency (
    token_id            BIGINT       NOT NULL REFERENCES wms_tokens(token_id) ON DELETE CASCADE,
    idempotency_key     VARCHAR(64)  NOT NULL,
    endpoint            VARCHAR(50)  NOT NULL,
    so_number           VARCHAR(128) NOT NULL,
    request_body_sha256 CHAR(64)     NOT NULL,
    response_body       JSONB,
    response_status     SMALLINT,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (token_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS dockd_idempotency_prune ON dockd_idempotency(created_at);

COMMIT;
