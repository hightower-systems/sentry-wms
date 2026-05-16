-- ============================================================
-- Migration 056: sales_orders POS columns (v1.10.0)
-- ============================================================
-- Additive columns supporting the /api/v1/pos/ endpoint surface
-- (counter sales + refunds). All columns are nullable or have a
-- backwards-compatible default so existing web orders are
-- untouched.
--
--   order_source            -- 'web' (BigCommerce/Shopify/inbound)
--                              vs 'pos' (counter sale)
--   external_txn_ref        -- Windcave DpsTxnRef for POS sales,
--                              keyed for refund lookups
--   idempotency_key         -- UUID4 from the POS Service so a
--                              checkout retry after a transient
--                              failure cannot double-create an SO
--   idempotency_body_hash   -- SHA-256 of the request body with
--                              idempotency_key excluded; lets the
--                              replay path detect 'same key,
--                              different body' and return 409
--   cached_response_body    -- the committed response body so a
--                              replay returns the exact original
--                              bytes (mirrors dockd's response
--                              cache pattern from mig 054)
--   order_type              -- 'sale' or 'refund'; refund SOs are
--                              negative-quantity siblings of the
--                              original
--   parent_so_id            -- on a refund SO, points back at the
--                              original sale
--   refunded_at             -- on the original SO, set when a
--                              refund commits
--   refund_so_id            -- on the original SO, points at the
--                              credit-memo SO
--
-- v1.8.0 mig discipline (#213): SET lock_timeout / statement_timeout
-- at the top so a slow ALTER fails fast. BEGIN/COMMIT-wrapped.
-- Forward-only; matches house style on 049-055.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

ALTER TABLE sales_orders
    ADD COLUMN IF NOT EXISTS order_source          VARCHAR(20)  NOT NULL DEFAULT 'web',
    ADD COLUMN IF NOT EXISTS external_txn_ref      VARCHAR(128),
    ADD COLUMN IF NOT EXISTS idempotency_key       VARCHAR(64),
    ADD COLUMN IF NOT EXISTS idempotency_body_hash CHAR(64),
    ADD COLUMN IF NOT EXISTS cached_response_body  JSONB,
    ADD COLUMN IF NOT EXISTS order_type            VARCHAR(20)  NOT NULL DEFAULT 'sale',
    ADD COLUMN IF NOT EXISTS parent_so_id          INT,
    ADD COLUMN IF NOT EXISTS refunded_at           TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS refund_so_id          INT;

-- Constraints declared after the columns exist so a re-run of the
-- migration on a partially-applied state does not fail. Each in
-- its own ALTER so a constraint that already exists from a prior
-- partial apply does not abort the rest.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sales_orders_idempotency_key_uniq'
    ) THEN
        ALTER TABLE sales_orders
            ADD CONSTRAINT sales_orders_idempotency_key_uniq UNIQUE (idempotency_key);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sales_orders_order_type_check'
    ) THEN
        ALTER TABLE sales_orders
            ADD CONSTRAINT sales_orders_order_type_check CHECK (order_type IN ('sale','refund'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sales_orders_parent_so_id_fkey'
    ) THEN
        ALTER TABLE sales_orders
            ADD CONSTRAINT sales_orders_parent_so_id_fkey
            FOREIGN KEY (parent_so_id) REFERENCES sales_orders(so_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sales_orders_refund_so_id_fkey'
    ) THEN
        ALTER TABLE sales_orders
            ADD CONSTRAINT sales_orders_refund_so_id_fkey
            FOREIGN KEY (refund_so_id) REFERENCES sales_orders(so_id);
    END IF;
END $$;

-- Partial indexes: the columns are NULL for the overwhelming
-- majority of historical web orders, so a partial index keeps
-- the on-disk footprint small while still serving the POS
-- replay / refund-lookup queries.
CREATE INDEX IF NOT EXISTS idx_so_idempotency
    ON sales_orders (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_so_external_txn
    ON sales_orders (external_txn_ref)
    WHERE external_txn_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_so_parent
    ON sales_orders (parent_so_id)
    WHERE parent_so_id IS NOT NULL;

COMMIT;
