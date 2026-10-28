-- 085: shipyard distribution demand, dispatch orders and delivery notes.
--
-- The shipyard Excel export is a demand snapshot, not an inbound manifest and
-- not an outbound stock transaction. Dispatch orders may arrive before a
-- matching demand row; unmatched/ambiguous links stay explicit until manually
-- resolved. Delivery notes snapshot the dispatch data used for printing.

SET lock_timeout = '5s';
SET statement_timeout = '120s';

BEGIN;

CREATE TABLE IF NOT EXISTS shipyard_distribution_imports (
    import_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    warehouse_id            INT NOT NULL REFERENCES warehouses(warehouse_id),
    import_no               VARCHAR(128) NOT NULL,
    source_file_name        VARCHAR(255) NOT NULL,
    source_file_sha256      CHAR(64) NOT NULL,
    source_template_version VARCHAR(32) NOT NULL,
    status                  VARCHAR(16) NOT NULL DEFAULT 'PARSED',
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by              VARCHAR(100) NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shipyard_distribution_import_no
        UNIQUE (warehouse_id, import_no),
    CONSTRAINT shipyard_distribution_import_status
        CHECK (status IN ('PARSED', 'CONFIRMED', 'VOID')),
    CONSTRAINT shipyard_distribution_import_sha256
        CHECK (length(source_file_sha256) = 64)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_shipyard_distribution_import_file
    ON shipyard_distribution_imports (warehouse_id, source_file_sha256)
    WHERE status <> 'VOID';

CREATE INDEX IF NOT EXISTS ix_shipyard_distribution_import_status
    ON shipyard_distribution_imports (warehouse_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS shipyard_distribution_files (
    import_id       UUID PRIMARY KEY
                        REFERENCES shipyard_distribution_imports(import_id)
                        ON DELETE CASCADE,
    file_name       VARCHAR(255) NOT NULL,
    content_type    VARCHAR(128) NOT NULL,
    file_size       BIGINT NOT NULL,
    file_sha256     CHAR(64) NOT NULL,
    file_bytes      BYTEA NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT shipyard_distribution_file_size
        CHECK (file_size > 0 AND file_size <= 10485760),
    CONSTRAINT shipyard_distribution_file_sha256
        CHECK (length(file_sha256) = 64)
);

CREATE TABLE IF NOT EXISTS shipyard_distribution_lines (
    line_id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    import_id               UUID NOT NULL
                                REFERENCES shipyard_distribution_imports(import_id)
                                ON DELETE CASCADE,
    source_row_no           VARCHAR(32) NOT NULL,
    source_row_hash         CHAR(64),
    engineering_no          VARCHAR(64) NOT NULL,
    section_no              VARCHAR(64) NOT NULL,
    pallet_no               VARCHAR(128) NOT NULL,
    pallet_name             VARCHAR(255) NOT NULL,
    work_order_status       VARCHAR(64),
    delivery_status         VARCHAR(64),
    applicant_raw           VARCHAR(255),
    department              VARCHAR(255),
    delivery_address        VARCHAR(255),
    contact_name            VARCHAR(100),
    contact_phone           VARCHAR(50),
    demand_window_raw       VARCHAR(255),
    demand_start_at         TIMESTAMPTZ,
    demand_end_at           TIMESTAMPTZ,
    resource_1              VARCHAR(100),
    driver_1                VARCHAR(100),
    resource_2              VARCHAR(100),
    driver_2                VARCHAR(100),
    resource_3              VARCHAR(100),
    driver_3                VARCHAR(100),
    confirmer               VARCHAR(100),
    completed_at_raw        VARCHAR(100),
    completed_at            TIMESTAMPTZ,
    confirmation_status     VARCHAR(64),
    raw_data                JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shipyard_distribution_line_row
        UNIQUE (import_id, source_row_no),
    CONSTRAINT uq_shipyard_distribution_line_pallet
        UNIQUE (import_id, engineering_no, section_no, pallet_no),
    CONSTRAINT shipyard_distribution_line_hash
        CHECK (source_row_hash IS NULL OR length(source_row_hash) = 64),
    CONSTRAINT shipyard_distribution_demand_window
        CHECK (
            demand_end_at IS NULL
            OR demand_start_at IS NULL
            OR demand_end_at >= demand_start_at
        )
);

-- 085 was applied to development databases before source_row_hash was added
-- to the CREATE TABLE definition. Keep the migration safe to rerun there.
ALTER TABLE shipyard_distribution_lines
    ADD COLUMN IF NOT EXISTS source_row_hash CHAR(64);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'shipyard_distribution_line_hash'
    ) THEN
        ALTER TABLE shipyard_distribution_lines
            ADD CONSTRAINT shipyard_distribution_line_hash
            CHECK (
                source_row_hash IS NULL
                OR length(source_row_hash) = 64
            );
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_shipyard_distribution_line_match
    ON shipyard_distribution_lines (engineering_no, pallet_no);

CREATE TABLE IF NOT EXISTS shipyard_dispatch_orders (
    dispatch_order_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    warehouse_id            INT NOT NULL REFERENCES warehouses(warehouse_id),
    dispatch_no             VARCHAR(128) NOT NULL,
    dispatch_date           DATE NOT NULL,
    source_type             VARCHAR(16) NOT NULL DEFAULT 'MANUAL',
    source_file_name        VARCHAR(255),
    status                  VARCHAR(16) NOT NULL DEFAULT 'CONFIRMED',
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key         VARCHAR(128) NOT NULL,
    created_by              VARCHAR(100) NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shipyard_dispatch_no
        UNIQUE (warehouse_id, dispatch_no),
    CONSTRAINT uq_shipyard_dispatch_idempotency
        UNIQUE (warehouse_id, idempotency_key),
    CONSTRAINT shipyard_dispatch_source_type
        CHECK (source_type IN ('MANUAL', 'EXCEL', 'IMAGE_OCR')),
    CONSTRAINT shipyard_dispatch_status
        CHECK (status IN ('CONFIRMED', 'CANCELLED'))
);

CREATE INDEX IF NOT EXISTS ix_shipyard_dispatch_date
    ON shipyard_dispatch_orders (warehouse_id, dispatch_date DESC);

CREATE TABLE IF NOT EXISTS shipyard_dispatch_lines (
    dispatch_line_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dispatch_order_id       UUID NOT NULL
                                REFERENCES shipyard_dispatch_orders(dispatch_order_id)
                                ON DELETE CASCADE,
    line_no                 VARCHAR(32) NOT NULL,
    engineering_no          VARCHAR(64) NOT NULL,
    pallet_no               VARCHAR(128) NOT NULL,
    material_spec_raw       TEXT NOT NULL,
    material_code           VARCHAR(128),
    material_name           VARCHAR(255),
    specification           TEXT,
    quantity                NUMERIC(18,3) NOT NULL,
    unit                    VARCHAR(32) NOT NULL,
    expected_arrival_date   DATE,
    keeper_name             VARCHAR(100),
    keeper_phone            VARCHAR(50),
    remarks                 VARCHAR(500),
    demand_line_id          UUID
                                REFERENCES shipyard_distribution_lines(line_id)
                                ON DELETE SET NULL,
    match_status            VARCHAR(16) NOT NULL,
    match_details           JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_text                TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shipyard_dispatch_line_no
        UNIQUE (dispatch_order_id, line_no),
    CONSTRAINT shipyard_dispatch_line_quantity
        CHECK (quantity > 0),
    CONSTRAINT shipyard_dispatch_match_status
        CHECK (match_status IN ('MATCHED', 'MANUAL', 'UNMATCHED', 'AMBIGUOUS'))
);

CREATE INDEX IF NOT EXISTS ix_shipyard_dispatch_line_match
    ON shipyard_dispatch_lines (engineering_no, pallet_no, match_status);

CREATE UNIQUE INDEX IF NOT EXISTS uq_shipyard_dispatch_line_demand
    ON shipyard_dispatch_lines (demand_line_id)
    WHERE demand_line_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS shipyard_delivery_notes (
    delivery_note_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    warehouse_id            INT NOT NULL REFERENCES warehouses(warehouse_id),
    delivery_note_no        VARCHAR(128) NOT NULL,
    dispatch_order_id       UUID NOT NULL
                                REFERENCES shipyard_dispatch_orders(dispatch_order_id)
                                ON DELETE RESTRICT,
    issue_date              DATE NOT NULL,
    status                  VARCHAR(16) NOT NULL DEFAULT 'ISSUED',
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key         VARCHAR(128) NOT NULL,
    receiver_name           VARCHAR(100),
    received_at             TIMESTAMPTZ,
    created_by              VARCHAR(100) NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shipyard_delivery_note_no
        UNIQUE (warehouse_id, delivery_note_no),
    CONSTRAINT uq_shipyard_delivery_note_dispatch
        UNIQUE (dispatch_order_id),
    CONSTRAINT uq_shipyard_delivery_note_idempotency
        UNIQUE (warehouse_id, idempotency_key),
    CONSTRAINT shipyard_delivery_note_status
        CHECK (status IN ('ISSUED', 'CANCELLED'))
);

CREATE INDEX IF NOT EXISTS ix_shipyard_delivery_note_date
    ON shipyard_delivery_notes (warehouse_id, issue_date DESC);

CREATE TABLE IF NOT EXISTS shipyard_delivery_lines (
    delivery_line_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    delivery_note_id        UUID NOT NULL
                                REFERENCES shipyard_delivery_notes(delivery_note_id)
                                ON DELETE CASCADE,
    dispatch_line_id        UUID NOT NULL
                                REFERENCES shipyard_dispatch_lines(dispatch_line_id)
                                ON DELETE RESTRICT,
    line_no                 VARCHAR(32) NOT NULL,
    engineering_no          VARCHAR(64) NOT NULL,
    section_no              VARCHAR(64),
    pallet_no               VARCHAR(128) NOT NULL,
    material_code           VARCHAR(128),
    material_name           VARCHAR(255),
    specification           TEXT,
    material_description    TEXT NOT NULL,
    quantity                NUMERIC(18,3) NOT NULL,
    unit                    VARCHAR(32) NOT NULL,
    keeper_name             VARCHAR(100),
    remarks                 VARCHAR(500),
    demand_line_id          UUID
                                REFERENCES shipyard_distribution_lines(line_id)
                                ON DELETE SET NULL,
    demand_import_no        VARCHAR(128),
    demand_source_row_no    VARCHAR(32),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shipyard_delivery_line_no
        UNIQUE (delivery_note_id, line_no),
    CONSTRAINT uq_shipyard_delivery_line_dispatch_line
        UNIQUE (dispatch_line_id),
    CONSTRAINT shipyard_delivery_line_quantity
        CHECK (quantity > 0)
);

ALTER TABLE shipyard_delivery_notes
    ADD COLUMN IF NOT EXISTS receiver_name VARCHAR(100),
    ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ;

ALTER TABLE shipyard_delivery_lines
    ADD COLUMN IF NOT EXISTS section_no VARCHAR(64),
    ADD COLUMN IF NOT EXISTS material_code VARCHAR(128),
    ADD COLUMN IF NOT EXISTS material_name VARCHAR(255),
    ADD COLUMN IF NOT EXISTS specification TEXT,
    ADD COLUMN IF NOT EXISTS demand_import_no VARCHAR(128),
    ADD COLUMN IF NOT EXISTS demand_source_row_no VARCHAR(32);

COMMIT;
