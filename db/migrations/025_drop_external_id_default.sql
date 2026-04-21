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
-- ============================================================

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
