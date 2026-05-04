-- ============================================================
-- Migration 041: inbound_customers staging + new canonical customers (v1.7.0)
-- ============================================================
-- First of two new-canonical-table migrations. customers does not
-- yet exist in the warehouse-floor schema; v1.7 ships it from day
-- one with the V-216 external_id pattern, the canonical UUID PK
-- shape, and the v1.7 inbound forensic pointer.
--
-- Conservative NOT NULL posture per plan §1.4: only canonical_id,
-- created_at, updated_at, latest_inbound_id NOT NULL. Everything
-- else nullable until v2.0 has signal from a real consumer
-- (NetSuite). first-writer must satisfy the canonical NOT NULL
-- set; mapping docs that can be first-writers cover those four
-- columns trivially (canonical_id default, timestamps default,
-- latest_inbound_id is set by the handler in the same transaction).
--
-- canonical_id is the UUID PK and the identifier the inbound
-- staging table's canonical_id column points at. external_id is
-- retained as a UUID UNIQUE alias for V-216 parity with the
-- existing canonical tables (items, sales_orders, purchase_orders);
-- the two converge on a single value at insert time.
--
-- Same staging-table shape as 039 / 040.
-- ============================================================

BEGIN;

-- New canonical table: customers.
CREATE TABLE IF NOT EXISTS customers (
    canonical_id      UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    external_id       UUID         UNIQUE NOT NULL DEFAULT gen_random_uuid(),
    customer_name     VARCHAR(200),
    email             VARCHAR(255),
    phone             VARCHAR(50),
    billing_address   TEXT,
    shipping_address  TEXT,
    tax_id            VARCHAR(64),
    is_active         BOOLEAN,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    latest_inbound_id BIGINT       NOT NULL DEFAULT 0
);

-- Inbound staging.
CREATE TABLE IF NOT EXISTS inbound_customers (
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

CREATE UNIQUE INDEX IF NOT EXISTS inbound_customers_idempotency
    ON inbound_customers (source_system, external_id, external_version);

CREATE INDEX IF NOT EXISTS inbound_customers_current
    ON inbound_customers (source_system, external_id, received_at DESC)
    WHERE status = 'applied';

CREATE INDEX IF NOT EXISTS inbound_customers_canonical
    ON inbound_customers (canonical_id);

COMMIT;
