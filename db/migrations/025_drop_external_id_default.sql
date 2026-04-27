-- ============================================================
-- Migration 025: drop external_id DEFAULT on ten retrofitted tables (v1.5.0)
-- ============================================================
-- Migration 020 added external_id UUID to ten aggregate/actor tables with
-- DEFAULT gen_random_uuid() so existing INSERTs kept working unchanged.
-- Issue #108 retrofits every insert site (production routes, test
-- fixtures, seed SQL) to supply an explicit value. This migration drops
-- the DEFAULT so a future caller that forgets the column fails loudly
-- with a NOT NULL violation instead of silently getting a random UUID.
--
-- A dedicated CI guardrail (api/tests/test_external_id_inserts.py)
-- scans source for INSERT statements missing external_id on these ten
-- tables so regressions surface during review rather than at runtime.
--
-- v1.5.1 V-213 (#152): wrap the ten ALTERs in a single transaction.
-- A partial apply (lock timeout, unexpected contention) would leave
-- some tables with the DEFAULT and some without; a later insert site
-- that forgets external_id would then appear to work on "old" tables
-- and fail on "new" ones, an asymmetric bug that is miserable to
-- debug. All-or-nothing via BEGIN / COMMIT keeps the schema state
-- coherent across the set.
-- ============================================================

BEGIN;

ALTER TABLE users                 ALTER COLUMN external_id DROP DEFAULT;
ALTER TABLE items                 ALTER COLUMN external_id DROP DEFAULT;
ALTER TABLE bins                  ALTER COLUMN external_id DROP DEFAULT;
ALTER TABLE sales_orders          ALTER COLUMN external_id DROP DEFAULT;
ALTER TABLE purchase_orders       ALTER COLUMN external_id DROP DEFAULT;
ALTER TABLE item_receipts         ALTER COLUMN external_id DROP DEFAULT;
ALTER TABLE inventory_adjustments ALTER COLUMN external_id DROP DEFAULT;
ALTER TABLE bin_transfers         ALTER COLUMN external_id DROP DEFAULT;
ALTER TABLE cycle_counts          ALTER COLUMN external_id DROP DEFAULT;
ALTER TABLE item_fulfillments     ALTER COLUMN external_id DROP DEFAULT;

COMMIT;
