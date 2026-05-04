-- ============================================================
-- Migration 039: inbound_sales_orders staging + canonical hook (v1.7.0)
-- ============================================================
-- Per-resource staging table for Pipe B inbound. Append-only with a
-- status flag rather than UPDATE-in-place; each accepted inbound POST
-- inserts a fresh row and supersedes any prior 'applied' row for the
-- same (source_system, external_id). Forensics for free, conflict
-- resolution as SELECT-of-MAX rather than row-lock fights.
--
-- canonical_id resolves to sales_orders.external_id (UUID), the
-- canonical identifier per the V-216 retrofit (mig 020). The inbound
-- row carries it forward so the canonical -> inbound forensic chain
-- is bidirectional.
--
-- ingested_via_token_id is BIGINT (not UUID): wms_tokens.token_id
-- is BIGSERIAL. ON DELETE RESTRICT enforces operator discipline:
-- tokens get revoked by setting revoked_at, not DELETE. The recipe
-- for hard-deleting a wms_tokens row with inbound history lives at
-- docs/runbooks/wms-tokens-hard-delete.md (R14).
--
-- sales_orders.latest_inbound_id is added unindexed and without an
-- FK -- not an FK because of the chicken-and-egg on first-time-receipt
-- (canonical row must exist before inbound row's canonical_id is
-- valid; latest_inbound_id would point forward to an inbound row that
-- doesn't yet exist). Forensic integrity is tracked in the
-- inbound -> canonical direction via inbound.canonical_id.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS inbound_sales_orders (
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

-- Idempotency UNIQUE: a re-POST of the exact same
-- (source_system, external_id, external_version) is detected here and
-- short-circuits to 200 OK at the API layer.
CREATE UNIQUE INDEX IF NOT EXISTS inbound_sales_orders_idempotency
    ON inbound_sales_orders (source_system, external_id, external_version);

-- Partial index supports the "current version" lookup at upsert time
-- without scanning superseded rows.
CREATE INDEX IF NOT EXISTS inbound_sales_orders_current
    ON inbound_sales_orders (source_system, external_id, received_at DESC)
    WHERE status = 'applied';

-- Forensic / canonical-side join.
CREATE INDEX IF NOT EXISTS inbound_sales_orders_canonical
    ON inbound_sales_orders (canonical_id);

-- Pointer from the canonical row back to the most-recent applied
-- inbound row. No FK, no index by default (the inbound -> canonical
-- direction is the forensic one; this is a convenience pointer).
ALTER TABLE sales_orders
    ADD COLUMN IF NOT EXISTS latest_inbound_id BIGINT;

COMMIT;
