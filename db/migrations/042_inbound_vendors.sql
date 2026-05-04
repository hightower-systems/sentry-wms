-- ============================================================
-- Migration 042: inbound_vendors staging + new canonical vendors (v1.7.0)
-- ============================================================
-- Second new-canonical-table migration. vendors does not yet
-- exist in the warehouse-floor schema; v1.7 ships it from day one
-- with the same conservative NOT NULL posture as customers
-- (mig 041): only canonical_id, external_id, created_at,
-- updated_at, latest_inbound_id NOT NULL. Everything else nullable
-- until v2.0.
--
-- Same staging-table shape as 039 / 040 / 041. canonical_id is
-- the UUID PK. ingested_via_token_id is BIGINT FK to wms_tokens
-- ON DELETE RESTRICT.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS vendors (
    canonical_id      UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    external_id       UUID         UNIQUE NOT NULL DEFAULT gen_random_uuid(),
    vendor_name       VARCHAR(200),
    contact_name      VARCHAR(200),
    email             VARCHAR(255),
    phone             VARCHAR(50),
    billing_address   TEXT,
    remit_to_address  TEXT,
    tax_id            VARCHAR(64),
    payment_terms     VARCHAR(64),
    is_active         BOOLEAN,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    latest_inbound_id BIGINT       NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS inbound_vendors (
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

CREATE UNIQUE INDEX IF NOT EXISTS inbound_vendors_idempotency
    ON inbound_vendors (source_system, external_id, external_version);

CREATE INDEX IF NOT EXISTS inbound_vendors_current
    ON inbound_vendors (source_system, external_id, received_at DESC)
    WHERE status = 'applied';

CREATE INDEX IF NOT EXISTS inbound_vendors_canonical
    ON inbound_vendors (canonical_id);

COMMIT;
