-- ============================================================
-- Migration 043: inbound_purchase_orders staging + canonical hook (v1.7.0)
-- ============================================================
-- Last per-resource staging table on the v1.7.0 branch. Same shape
-- as 039 (sales_orders) and 040 (items): append-only with status
-- flag, idempotency UNIQUE, partial 'applied' index, canonical_id
-- index, status CHECK, BIGINT FK to wms_tokens ON DELETE RESTRICT.
--
-- canonical_id resolves to purchase_orders.external_id (UUID) per
-- V-216 retrofit (mig 020). purchase_orders.latest_inbound_id
-- added unindexed and without an FK; rationale identical to mig 039.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS inbound_purchase_orders (
    inbound_id            BIGSERIAL    PRIMARY KEY,
    source_system         VARCHAR(64)  NOT NULL REFERENCES inbound_source_systems_allowlist(source_system),
    external_id           VARCHAR(128) NOT NULL,
    external_version      VARCHAR(64)  NOT NULL,
    canonical_id          UUID         NOT NULL,
    canonical_payload     JSONB        NOT NULL,
    source_payload        JSONB        NOT NULL,
    received_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    status                VARCHAR(16)  NOT NULL DEFAULT 'applied',
    superseded_at         TIMESTAMPTZ,
    ingested_via_token_id BIGINT       NOT NULL REFERENCES wms_tokens(token_id) ON DELETE RESTRICT,
    CHECK (status IN ('applied','superseded'))
);

CREATE UNIQUE INDEX IF NOT EXISTS inbound_purchase_orders_idempotency
    ON inbound_purchase_orders (source_system, external_id, external_version);

CREATE INDEX IF NOT EXISTS inbound_purchase_orders_current
    ON inbound_purchase_orders (source_system, external_id, received_at DESC)
    WHERE status = 'applied';

CREATE INDEX IF NOT EXISTS inbound_purchase_orders_canonical
    ON inbound_purchase_orders (canonical_id);

ALTER TABLE purchase_orders
    ADD COLUMN IF NOT EXISTS latest_inbound_id BIGINT;

COMMIT;
