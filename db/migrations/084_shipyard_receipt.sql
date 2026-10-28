-- 084: shipyard pallet scan, photo evidence, actual receipt and shortage tracking.
--
-- A source manifest row is one cargo ticket/ticket group. A receipt may add one
-- or more pallet units to that ticket, but quantity is always booked once at
-- ticket level. Photos are uploaded first as unbound evidence and then attached
-- to the receipt/pallet in the same transaction as the inventory ledger row.

SET lock_timeout = '5s';
SET statement_timeout = '120s';

BEGIN;

ALTER TABLE shipyard_inbound_lines
    ADD COLUMN IF NOT EXISTS project_binding_id BIGINT
        REFERENCES shipyard_project_bindings(binding_id);

ALTER TABLE shipyard_cargo_tickets
    ADD COLUMN IF NOT EXISTS on_hand_quantity NUMERIC(18,3)
        NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS shortage_status VARCHAR(32)
        NOT NULL DEFAULT 'NONE',
    ADD COLUMN IF NOT EXISTS shortage_reported_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS shortage_reported_by VARCHAR(100),
    ADD COLUMN IF NOT EXISTS shortage_note VARCHAR(500);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'shipyard_cargo_ticket_on_hand_quantity'
    ) THEN
        ALTER TABLE shipyard_cargo_tickets
            ADD CONSTRAINT shipyard_cargo_ticket_on_hand_quantity
            CHECK (
                on_hand_quantity >= 0
                AND on_hand_quantity <= received_quantity
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'shipyard_cargo_ticket_received_not_over_declared'
    ) THEN
        ALTER TABLE shipyard_cargo_tickets
            ADD CONSTRAINT shipyard_cargo_ticket_received_not_over_declared
            CHECK (received_quantity <= declared_quantity);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'shipyard_cargo_ticket_shortage_status'
    ) THEN
        ALTER TABLE shipyard_cargo_tickets
            ADD CONSTRAINT shipyard_cargo_ticket_shortage_status
            CHECK (shortage_status IN (
                'NONE', 'PENDING_REPORT', 'REPORTED', 'IN_TRANSIT',
                'PARTIAL_REPLENISHMENT', 'REPLENISHED'
            ));
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_shipyard_pallet_ticket_suffix_normalized
    ON shipyard_pallet_units (ticket_id, COALESCE(tag_suffix, ''));

CREATE TABLE IF NOT EXISTS shipyard_receipts (
    receipt_id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id                   UUID NOT NULL
                                    REFERENCES shipyard_cargo_tickets(ticket_id)
                                    ON DELETE RESTRICT,
    idempotency_key             VARCHAR(128) NOT NULL,
    previous_received_quantity  NUMERIC(18,3) NOT NULL,
    received_quantity           NUMERIC(18,3) NOT NULL,
    balance_after               NUMERIC(18,3) NOT NULL,
    resulting_ticket_status     VARCHAR(24) NOT NULL,
    bin_id                      INT NOT NULL REFERENCES bins(bin_id),
    location_path_snapshot      VARCHAR(255) NOT NULL,
    difference_reason           VARCHAR(500),
    client_device_id            VARCHAR(128),
    received_by                 VARCHAR(100) NOT NULL,
    received_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shipyard_receipt_idempotency
        UNIQUE (ticket_id, idempotency_key),
    CONSTRAINT shipyard_receipt_quantity
        CHECK (received_quantity > 0),
    CONSTRAINT shipyard_receipt_previous_quantity
        CHECK (previous_received_quantity >= 0),
    CONSTRAINT shipyard_receipt_balance
        CHECK (balance_after >= 0),
    CONSTRAINT shipyard_receipt_status
        CHECK (resulting_ticket_status IN ('PARTIAL', 'IN_STOCK'))
);

CREATE INDEX IF NOT EXISTS ix_shipyard_receipt_ticket
    ON shipyard_receipts (ticket_id, received_at DESC);

CREATE TABLE IF NOT EXISTS shipyard_receipt_pallets (
    receipt_id      UUID NOT NULL
                        REFERENCES shipyard_receipts(receipt_id)
                        ON DELETE CASCADE,
    pallet_id       UUID NOT NULL
                        REFERENCES shipyard_pallet_units(pallet_id)
                        ON DELETE RESTRICT,
    tag_suffix      VARCHAR(32),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (receipt_id, pallet_id),
    CONSTRAINT uq_shipyard_receipt_pallet UNIQUE (pallet_id)
);

CREATE TABLE IF NOT EXISTS shipyard_stock_ledger (
    ledger_id               BIGSERIAL PRIMARY KEY,
    ticket_id               UUID NOT NULL
                                REFERENCES shipyard_cargo_tickets(ticket_id)
                                ON DELETE RESTRICT,
    source_type             VARCHAR(24) NOT NULL,
    source_id               UUID NOT NULL,
    quantity_delta          NUMERIC(18,3) NOT NULL,
    balance_after           NUMERIC(18,3) NOT NULL,
    warehouse_id            INT NOT NULL REFERENCES warehouses(warehouse_id),
    bin_id                  INT NOT NULL REFERENCES bins(bin_id),
    location_path_snapshot  VARCHAR(255) NOT NULL,
    actor                   VARCHAR(100) NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shipyard_stock_ledger_source
        UNIQUE (source_type, source_id),
    CONSTRAINT shipyard_stock_ledger_source_type
        CHECK (source_type IN ('RECEIPT', 'OUTBOUND', 'ADJUSTMENT')),
    CONSTRAINT shipyard_stock_ledger_nonzero_delta
        CHECK (quantity_delta <> 0),
    CONSTRAINT shipyard_stock_ledger_balance
        CHECK (balance_after >= 0)
);

CREATE INDEX IF NOT EXISTS ix_shipyard_stock_ledger_ticket
    ON shipyard_stock_ledger (ticket_id, created_at);

CREATE TABLE IF NOT EXISTS shipyard_photos (
    photo_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id       UUID NOT NULL
                        REFERENCES shipyard_cargo_tickets(ticket_id)
                        ON DELETE RESTRICT,
    warehouse_id    INT NOT NULL REFERENCES warehouses(warehouse_id),
    receipt_id      UUID REFERENCES shipyard_receipts(receipt_id)
                        ON DELETE SET NULL,
    pallet_id       UUID REFERENCES shipyard_pallet_units(pallet_id)
                        ON DELETE SET NULL,
    tag_suffix      VARCHAR(32),
    photo_kind      VARCHAR(16) NOT NULL,
    file_name       VARCHAR(255) NOT NULL,
    content_type    VARCHAR(64) NOT NULL,
    file_size       BIGINT NOT NULL,
    file_sha256     CHAR(64) NOT NULL,
    file_bytes      BYTEA NOT NULL,
    uploaded_by     VARCHAR(100) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT shipyard_photo_kind
        CHECK (photo_kind IN ('PALLET', 'GROUP', 'AREA', 'DIFFERENCE')),
    CONSTRAINT shipyard_photo_suffix
        CHECK (tag_suffix IS NULL OR tag_suffix ~ '^[0-9]+-[0-9]+$'),
    CONSTRAINT shipyard_photo_content_type
        CHECK (content_type IN ('image/jpeg', 'image/png', 'image/webp')),
    CONSTRAINT shipyard_photo_size
        CHECK (file_size > 0 AND file_size <= 10485760),
    CONSTRAINT shipyard_photo_sha256
        CHECK (length(file_sha256) = 64)
);

CREATE INDEX IF NOT EXISTS ix_shipyard_photo_ticket
    ON shipyard_photos (ticket_id, created_at);

CREATE INDEX IF NOT EXISTS ix_shipyard_photo_unbound
    ON shipyard_photos (ticket_id, photo_kind)
    WHERE receipt_id IS NULL;

COMMIT;
