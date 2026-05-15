-- ============================================================
-- Migration 054: sales_orders.memo + FRAUD_REVIEW status
-- ============================================================
-- Adds two pieces that together back the new Outbound > Fraud queue:
--
--   1. memo TEXT
--      Free-form note field. Customer service edits inline on the
--      Fraud page; intended for any human-readable context an agent
--      wants to leave on an order. NULL by default.
--
--   2. status enum gains 'FRAUD_REVIEW'
--      The status column is plain VARCHAR (no DB-level CHECK), so no
--      schema change is required for the new value -- the application
--      layer enforces the allowed set in api/constants.py. We extend
--      the inline comment on the column to document the new value so
--      schema readers see the full enum.
--
-- Forward-only: this migration does NOT scan or backfill existing
-- sales_orders. The fraud rule (billing != shipping at ingest) only
-- fires on new SOs landing from fabric after deploy, by design --
-- existing OPEN orders stay OPEN.
--
-- v1.8.0 migration discipline (V-213): SET lock_timeout /
-- statement_timeout, BEGIN/COMMIT-wrapped.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

ALTER TABLE sales_orders
    ADD COLUMN IF NOT EXISTS memo TEXT;

COMMENT ON COLUMN sales_orders.status IS
    'Lifecycle: OPEN, PICKING, PICKED, PACKING, PACKED, SHIPPED, CANCELLED, FRAUD_REVIEW. Enforced by api/constants.py, not by a DB CHECK.';

COMMIT;
