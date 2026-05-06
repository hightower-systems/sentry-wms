-- ============================================================
-- Migration 040: inbound_items staging + canonical hook (v1.7.0)
-- ============================================================
-- Per-resource staging table for Pipe B inbound, items resource.
-- Same shape as 039 (sales_orders): append-only with status flag,
-- idempotency UNIQUE on (source_system, external_id, external_version),
-- partial 'applied' index, canonical_id index, BIGINT FK to wms_tokens
-- ON DELETE RESTRICT, status CHECK.
--
-- canonical_id resolves to items.external_id (UUID) per V-216
-- retrofit (mig 020). items.latest_inbound_id added unindexed and
-- without an FK; rationale identical to mig 039.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS inbound_items (
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

CREATE UNIQUE INDEX IF NOT EXISTS inbound_items_idempotency
    ON inbound_items (source_system, external_id, external_version);

CREATE INDEX IF NOT EXISTS inbound_items_current
    ON inbound_items (source_system, external_id, received_at DESC)
    WHERE status = 'applied';

CREATE INDEX IF NOT EXISTS inbound_items_canonical
    ON inbound_items (canonical_id);

ALTER TABLE items
    ADD COLUMN IF NOT EXISTS latest_inbound_id BIGINT;

COMMIT;
