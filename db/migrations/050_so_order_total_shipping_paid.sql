-- ============================================================
-- Migration 050: sales_orders.order_total + customer_shipping_paid (v1.8.0 #282)
-- ============================================================
-- Two NUMERIC(12,2) columns extending the v1.7.0 inbound canonical
-- contract for sales orders:
--
--   order_total            -- total order value as reported by the
--                             source ERP. Currency is implied per
--                             Sentry instance (no per-order currency
--                             in v1.8).
--   customer_shipping_paid -- shipping amount the customer paid at
--                             checkout. Consumed by dockd's carrier
--                             optimization engine in v1.9.
--
-- Both nullable. Forward-only: existing rows have NULL after the
-- migration runs, and there is no admin-UI backfill in v1.8.
-- Optional historical backfill is a connector-config field, not a
-- runtime feature.
--
-- NUMERIC(12,2) supports values up to 9,999,999,999.99 and maps to
-- decimal.Decimal in psycopg2. Wire-level rejection of values with
-- decimal_places > 2 or out-of-range magnitude is a Pydantic concern
-- (see api/schemas/inbound.py); this migration ships only the
-- column. Postgres itself rounds excess scale silently, so the
-- Pydantic gate is the authoritative validator.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

ALTER TABLE sales_orders
    ADD COLUMN IF NOT EXISTS order_total            NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS customer_shipping_paid NUMERIC(12,2);

COMMIT;
