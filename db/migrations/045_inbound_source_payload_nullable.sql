-- ============================================================
-- Migration 045: inbound_<resource>.source_payload NULLABLE (v1.7.0 R6)
-- ============================================================
-- Migrations 039-043 shipped source_payload JSONB NOT NULL across
-- all five inbound_<resource> tables. The retention beat task
-- (jobs.cleanup_tasks.cleanup_inbound_source_payload) needs to NULL
-- the column past the SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS
-- window while preserving canonical_payload (R6 mitigation: drop
-- per-row size at scale, keep the canonical history queryable).
--
-- canonical_payload stays NOT NULL -- losing the canonical shape
-- would break forensic replay. INSERT-time validation already
-- enforces source_payload NOT NULL via the inbound handler's
-- Pydantic body model; the column-level constraint becomes
-- redundant once the retention task can NULL it post-hoc.
-- ============================================================

BEGIN;

ALTER TABLE inbound_sales_orders     ALTER COLUMN source_payload DROP NOT NULL;
ALTER TABLE inbound_items            ALTER COLUMN source_payload DROP NOT NULL;
ALTER TABLE inbound_customers        ALTER COLUMN source_payload DROP NOT NULL;
ALTER TABLE inbound_vendors          ALTER COLUMN source_payload DROP NOT NULL;
ALTER TABLE inbound_purchase_orders  ALTER COLUMN source_payload DROP NOT NULL;

COMMIT;
