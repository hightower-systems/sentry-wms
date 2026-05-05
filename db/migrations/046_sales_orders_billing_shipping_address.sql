-- ============================================================
-- Migration 046: sales_orders billing_address + shipping_address (v1.7.0)
-- ============================================================
-- Ecommerce ship-to / bill-to use case: shipping_address drives
-- the actual shipment, billing_address drives customer-service
-- order lookups + dispute handling. Both are per-ORDER, not
-- per-customer; addresses vary order-to-order.
--
-- Pre-v1.7.0 sales_orders had only `ship_address VARCHAR(500)`,
-- a warehouse-floor field used by the existing pick / pack / ship
-- flow. The new columns sit alongside it -- ship_address stays
-- the warehouse-floor field, billing_address + shipping_address
-- are the canonical-side values inbound consumers populate per
-- the v1.7.0 inbound contract.
--
-- Both nullable so existing rows backfill silently and the
-- migration applies on any deployment. Mapping docs that don't
-- declare these fields leave them NULL on first-write
-- (field-set isolation contract).
--
-- This commit ships the column-level surface only. Rollout to
-- CSV import/export, OpenAPI, webhooks, and admin UI tracks in
-- #268 before production deploy.
-- ============================================================

BEGIN;

ALTER TABLE sales_orders
    ADD COLUMN IF NOT EXISTS billing_address  TEXT,
    ADD COLUMN IF NOT EXISTS shipping_address TEXT;

COMMIT;
