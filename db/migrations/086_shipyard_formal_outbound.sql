-- 086: formal shipyard outbound manifests, whole-ticket turnover and loading.
--
-- A shipyard outbound manifest is an external demand document. Confirmation
-- turns it into an internal outbound order. Each task always represents one
-- complete cargo ticket; multi-pallet tickets cannot be split across tasks.
-- Inventory is not changed when a task is allocated. It is depleted once when
-- every pallet label and photo in the ticket has been validated.

SET lock_timeout = '5s';
SET statement_timeout = '120s';

BEGIN;

ALTER TABLE shipyard_inbound_lines
    ADD COLUMN IF NOT EXISTS material_code VARCHAR(128);

CREATE INDEX IF NOT EXISTS ix_shipyard_inbound_line_material
    ON shipyard_inbound_lines (
        material_code,
        item_name,
        item_spec
    );

CREATE TABLE IF NOT EXISTS shipyard_outbound_manifests (
    manifest_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    warehouse_id            INT NOT NULL REFERENCES warehouses(warehouse_id),
    manifest_no             VARCHAR(128) NOT NULL,
    factory_shipment_no     VARCHAR(128) NOT NULL,
    source_file_name        VARCHAR(255) NOT NULL,
    source_file_sha256      CHAR(64) NOT NULL,
    source_template_version VARCHAR(32) NOT NULL
                                DEFAULT 'shipyard-outbound-v1',
    status                  VARCHAR(16) NOT NULL DEFAULT 'PARSED',
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by              VARCHAR(100) NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT shipyard_outbound_manifest_status
        CHECK (status IN ('PARSED', 'CONFIRMED', 'VOID')),
    CONSTRAINT shipyard_outbound_manifest_sha256
        CHECK (length(source_file_sha256) = 64)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_shipyard_outbound_manifest_no
    ON shipyard_outbound_manifests (warehouse_id, manifest_no)
    WHERE status <> 'VOID';

CREATE UNIQUE INDEX IF NOT EXISTS uq_shipyard_outbound_manifest_file
    ON shipyard_outbound_manifests (warehouse_id, source_file_sha256)
    WHERE status <> 'VOID';

CREATE INDEX IF NOT EXISTS ix_shipyard_outbound_manifest_status
    ON shipyard_outbound_manifests (warehouse_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS shipyard_outbound_manifest_files (
    manifest_id     UUID PRIMARY KEY
                        REFERENCES shipyard_outbound_manifests(manifest_id)
                        ON DELETE CASCADE,
    file_name       VARCHAR(255) NOT NULL,
    content_type    VARCHAR(128) NOT NULL,
    file_size       BIGINT NOT NULL,
    file_sha256     CHAR(64) NOT NULL,
    file_bytes      BYTEA NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT shipyard_outbound_manifest_file_size
        CHECK (file_size > 0 AND file_size <= 10485760),
    CONSTRAINT shipyard_outbound_manifest_file_sha256
        CHECK (length(file_sha256) = 64)
);

CREATE TABLE IF NOT EXISTS shipyard_outbound_manifest_lines (
    line_id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    manifest_id              UUID NOT NULL
                                 REFERENCES shipyard_outbound_manifests(manifest_id)
                                 ON DELETE CASCADE,
    source_row_no            VARCHAR(64) NOT NULL,
    ticket_no                VARCHAR(96),
    ship_no                  VARCHAR(64) NOT NULL,
    engineering_no           VARCHAR(64) NOT NULL,
    production_drawing_no    VARCHAR(128) NOT NULL,
    manufacturer_name        VARCHAR(200) NOT NULL DEFAULT '',
    material_code            VARCHAR(128) NOT NULL,
    item_name                VARCHAR(255) NOT NULL,
    item_spec                TEXT,
    demand_quantity          NUMERIC(18,3) NOT NULL,
    unit                     VARCHAR(32) NOT NULL DEFAULT '件',
    demand_date              DATE,
    expected_pallet_count    INT,
    expected_tag_suffixes    TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    project_binding_id       BIGINT
                                 REFERENCES shipyard_project_bindings(binding_id),
    status                   VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    raw_data                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shipyard_outbound_manifest_line_row
        UNIQUE (manifest_id, source_row_no),
    CONSTRAINT shipyard_outbound_manifest_line_quantity
        CHECK (demand_quantity > 0),
    CONSTRAINT shipyard_outbound_manifest_line_pallet_count
        CHECK (expected_pallet_count IS NULL OR expected_pallet_count > 0),
    CONSTRAINT shipyard_outbound_manifest_line_material_code
        CHECK (length(BTRIM(material_code)) > 0),
    CONSTRAINT shipyard_outbound_manifest_line_item_name
        CHECK (length(BTRIM(item_name)) > 0),
    CONSTRAINT shipyard_outbound_manifest_line_status
        CHECK (status IN (
            'PENDING', 'ALLOCATED', 'SHORTAGE', 'OUTBOUND', 'VOID'
        ))
);

CREATE INDEX IF NOT EXISTS ix_shipyard_outbound_manifest_line_match
    ON shipyard_outbound_manifest_lines (
        ship_no,
        engineering_no,
        production_drawing_no,
        material_code,
        item_name
    );

CREATE TABLE IF NOT EXISTS shipyard_outbound_orders (
    outbound_order_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    warehouse_id            INT NOT NULL REFERENCES warehouses(warehouse_id),
    outbound_order_no       VARCHAR(128) NOT NULL,
    manifest_id             UUID NOT NULL
                                REFERENCES shipyard_outbound_manifests(manifest_id)
                                ON DELETE RESTRICT,
    factory_shipment_no     VARCHAR(128) NOT NULL,
    status                  VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    confirmed_by            VARCHAR(100) NOT NULL,
    confirmed_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shipyard_outbound_order_no
        UNIQUE (warehouse_id, outbound_order_no),
    CONSTRAINT uq_shipyard_outbound_order_manifest
        UNIQUE (manifest_id),
    CONSTRAINT shipyard_outbound_order_status
        CHECK (status IN (
            'PENDING', 'IN_PROGRESS', 'PARTIAL', 'OUTBOUND', 'LOADED'
        ))
);

CREATE INDEX IF NOT EXISTS ix_shipyard_outbound_order_status
    ON shipyard_outbound_orders (warehouse_id, status, confirmed_at DESC);

CREATE TABLE IF NOT EXISTS shipyard_outbound_order_lines (
    order_line_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    outbound_order_id       UUID NOT NULL
                                REFERENCES shipyard_outbound_orders(outbound_order_id)
                                ON DELETE RESTRICT,
    manifest_line_id        UUID NOT NULL
                                REFERENCES shipyard_outbound_manifest_lines(line_id)
                                ON DELETE RESTRICT,
    source_row_no           VARCHAR(64) NOT NULL,
    ticket_no               VARCHAR(96),
    ship_no                 VARCHAR(64) NOT NULL,
    engineering_no          VARCHAR(64) NOT NULL,
    production_drawing_no   VARCHAR(128) NOT NULL,
    manufacturer_name       VARCHAR(200) NOT NULL DEFAULT '',
    material_code           VARCHAR(128) NOT NULL,
    item_name               VARCHAR(255) NOT NULL,
    item_spec               TEXT,
    demand_quantity         NUMERIC(18,3) NOT NULL,
    unit                    VARCHAR(32) NOT NULL,
    allocated_quantity      NUMERIC(18,3) NOT NULL DEFAULT 0,
    fulfilled_quantity      NUMERIC(18,3) NOT NULL DEFAULT 0,
    status                  VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    demand_date             DATE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shipyard_outbound_order_line_source
        UNIQUE (outbound_order_id, source_row_no),
    CONSTRAINT uq_shipyard_outbound_order_manifest_line
        UNIQUE (manifest_line_id),
    CONSTRAINT shipyard_outbound_order_line_quantity
        CHECK (
            demand_quantity > 0
            AND allocated_quantity >= 0
            AND fulfilled_quantity >= 0
            AND fulfilled_quantity <= allocated_quantity
        ),
    CONSTRAINT shipyard_outbound_order_line_status
        CHECK (status IN ('PENDING', 'ALLOCATED', 'SHORTAGE', 'OUTBOUND'))
);

CREATE INDEX IF NOT EXISTS ix_shipyard_outbound_order_line_status
    ON shipyard_outbound_order_lines (outbound_order_id, status);

CREATE TABLE IF NOT EXISTS shipyard_outbound_tasks (
    task_id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_no                  VARCHAR(160) NOT NULL,
    outbound_order_id        UUID NOT NULL
                                 REFERENCES shipyard_outbound_orders(outbound_order_id)
                                 ON DELETE RESTRICT,
    order_line_id            UUID NOT NULL
                                 REFERENCES shipyard_outbound_order_lines(order_line_id)
                                 ON DELETE RESTRICT,
    ticket_id                UUID NOT NULL
                                 REFERENCES shipyard_cargo_tickets(ticket_id)
                                 ON DELETE RESTRICT,
    planned_quantity         NUMERIC(18,3) NOT NULL,
    expected_pallet_count    INT NOT NULL,
    expected_tag_suffixes    TEXT[] NOT NULL,
    location_path_snapshot   VARCHAR(255) NOT NULL,
    material_snapshot        JSONB NOT NULL DEFAULT '{}'::jsonb,
    status                   VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shipyard_outbound_task_no UNIQUE (task_no),
    CONSTRAINT uq_shipyard_outbound_task_ticket UNIQUE (ticket_id),
    CONSTRAINT shipyard_outbound_task_quantity
        CHECK (planned_quantity > 0),
    CONSTRAINT shipyard_outbound_task_pallets
        CHECK (
            expected_pallet_count > 0
            AND cardinality(expected_tag_suffixes) = expected_pallet_count
        ),
    CONSTRAINT shipyard_outbound_task_status
        CHECK (status IN ('PENDING', 'OUTBOUND', 'EXCEPTION'))
);

CREATE INDEX IF NOT EXISTS ix_shipyard_outbound_task_status
    ON shipyard_outbound_tasks (outbound_order_id, status);

CREATE TABLE IF NOT EXISTS shipyard_outbound_confirmations (
    confirmation_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id                  UUID NOT NULL
                                 REFERENCES shipyard_outbound_tasks(task_id)
                                 ON DELETE RESTRICT,
    outbound_order_id        UUID NOT NULL
                                 REFERENCES shipyard_outbound_orders(outbound_order_id)
                                 ON DELETE RESTRICT,
    ticket_id                UUID NOT NULL
                                 REFERENCES shipyard_cargo_tickets(ticket_id)
                                 ON DELETE RESTRICT,
    idempotency_key          VARCHAR(128) NOT NULL,
    quantity                 NUMERIC(18,3) NOT NULL,
    previous_on_hand         NUMERIC(18,3) NOT NULL,
    balance_after            NUMERIC(18,3) NOT NULL,
    bin_id                   INT NOT NULL REFERENCES bins(bin_id),
    location_path_snapshot   VARCHAR(255) NOT NULL,
    scan_payloads            JSONB NOT NULL DEFAULT '[]'::jsonb,
    client_device_id         VARCHAR(128),
    outbound_by              VARCHAR(100) NOT NULL,
    outbound_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shipyard_outbound_confirmation_task UNIQUE (task_id),
    CONSTRAINT uq_shipyard_outbound_confirmation_idempotency
        UNIQUE (task_id, idempotency_key),
    CONSTRAINT shipyard_outbound_confirmation_quantity
        CHECK (
            quantity > 0
            AND previous_on_hand >= quantity
            AND balance_after >= 0
        )
);

CREATE INDEX IF NOT EXISTS ix_shipyard_outbound_confirmation_order
    ON shipyard_outbound_confirmations (outbound_order_id, outbound_at DESC);

CREATE TABLE IF NOT EXISTS shipyard_outbound_confirmation_pallets (
    confirmation_id     UUID NOT NULL
                            REFERENCES shipyard_outbound_confirmations(confirmation_id)
                            ON DELETE CASCADE,
    pallet_id           UUID NOT NULL
                            REFERENCES shipyard_pallet_units(pallet_id)
                            ON DELETE RESTRICT,
    tag_suffix          VARCHAR(32) NOT NULL,
    qr_payload          TEXT NOT NULL,
    qr_payload_sha256   CHAR(64) NOT NULL,
    photo_id            UUID NOT NULL
                            REFERENCES shipyard_photos(photo_id)
                            ON DELETE RESTRICT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (confirmation_id, pallet_id),
    CONSTRAINT uq_shipyard_outbound_confirmation_pallet
        UNIQUE (pallet_id),
    CONSTRAINT shipyard_outbound_confirmation_pallet_suffix
        CHECK (tag_suffix ~ '^[0-9]+-[0-9]+$'),
    CONSTRAINT shipyard_outbound_confirmation_pallet_sha256
        CHECK (length(qr_payload_sha256) = 64)
);

ALTER TABLE shipyard_photos
    ADD COLUMN IF NOT EXISTS outbound_task_id UUID
        REFERENCES shipyard_outbound_tasks(task_id)
        ON DELETE RESTRICT,
    ADD COLUMN IF NOT EXISTS outbound_confirmation_id UUID
        REFERENCES shipyard_outbound_confirmations(confirmation_id)
        ON DELETE RESTRICT;

DO $$
BEGIN
    ALTER TABLE shipyard_photos
        DROP CONSTRAINT IF EXISTS shipyard_photo_kind;
    ALTER TABLE shipyard_photos
        ADD CONSTRAINT shipyard_photo_kind
        CHECK (photo_kind IN (
            'PALLET', 'GROUP', 'AREA', 'DIFFERENCE', 'LOADING'
        ));
END $$;

CREATE INDEX IF NOT EXISTS ix_shipyard_photo_outbound_task
    ON shipyard_photos (outbound_task_id, tag_suffix)
    WHERE outbound_task_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_shipyard_photo_outbound_pallet
    ON shipyard_photos (outbound_confirmation_id, pallet_id)
    WHERE outbound_confirmation_id IS NOT NULL
      AND photo_kind = 'PALLET';

CREATE TABLE IF NOT EXISTS shipyard_outbound_loading_confirmations (
    loading_confirmation_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    outbound_order_id        UUID NOT NULL
                                 REFERENCES shipyard_outbound_orders(outbound_order_id)
                                 ON DELETE RESTRICT,
    idempotency_key          VARCHAR(128) NOT NULL,
    notes                    VARCHAR(500),
    client_device_id         VARCHAR(128),
    loaded_by                VARCHAR(100) NOT NULL,
    loaded_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shipyard_outbound_loading_order UNIQUE (outbound_order_id),
    CONSTRAINT uq_shipyard_outbound_loading_idempotency
        UNIQUE (outbound_order_id, idempotency_key)
);

COMMIT;
