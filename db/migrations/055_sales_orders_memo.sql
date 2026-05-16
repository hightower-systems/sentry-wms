-- ============================================================
-- Migration 055: sales_orders.memo (v1.9.0)
-- ============================================================
-- Free-text operator-facing note pushed in by the source ERP. Used
-- by the picker / packer / shipper screens to surface customer notes
-- ("leave at back door", "call before delivery", "fragile, double-
-- box", etc.) that don't belong in any structured column.
--
-- TEXT (not VARCHAR(N)): notes can be long, no business reason to
-- cap. Nullable: existing orders carry no memo and the source ERP
-- may or may not push one per order.
--
-- v1.8.0 mig discipline (#213): SET lock_timeout / statement_timeout
-- at the top so a slow ALTER fails fast. BEGIN/COMMIT-wrapped.
-- Forward-only; matches house style on 049-054.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

ALTER TABLE sales_orders
    ADD COLUMN IF NOT EXISTS memo TEXT;

COMMIT;
