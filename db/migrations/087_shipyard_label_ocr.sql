-- 087: label-photo OCR is the normal workflow; QR payloads become legacy-optional.

SET lock_timeout = '5s';
SET statement_timeout = '120s';

BEGIN;

ALTER TABLE shipyard_pallet_units
    ALTER COLUMN qr_payload DROP NOT NULL,
    ALTER COLUMN qr_payload_sha256 DROP NOT NULL;

ALTER TABLE shipyard_pallet_units
    ADD COLUMN IF NOT EXISTS label_fields JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS shipyard_label_recognitions (
    recognition_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    photo_id        UUID NOT NULL UNIQUE
                        REFERENCES shipyard_photos(photo_id)
                        ON DELETE CASCADE,
    ticket_id       UUID NOT NULL
                        REFERENCES shipyard_cargo_tickets(ticket_id)
                        ON DELETE CASCADE,
    task_id         UUID
                        REFERENCES shipyard_outbound_tasks(task_id)
                        ON DELETE SET NULL,
    warehouse_id    INT NOT NULL
                        REFERENCES warehouses(warehouse_id)
                        ON DELETE RESTRICT,
    provider        VARCHAR(32) NOT NULL,
    status          VARCHAR(24) NOT NULL,
    raw_text        TEXT NOT NULL DEFAULT '',
    fields          JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence      NUMERIC(5,4),
    error_code      VARCHAR(64),
    error_message   VARCHAR(500),
    created_by      VARCHAR(100) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT shipyard_label_recognition_status
        CHECK (status IN ('MATCHED', 'NEEDS_REVIEW', 'FAILED')),
    CONSTRAINT shipyard_label_recognition_confidence
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);

CREATE INDEX IF NOT EXISTS ix_shipyard_label_recognition_ticket
    ON shipyard_label_recognitions (ticket_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_shipyard_label_recognition_task
    ON shipyard_label_recognitions (task_id, created_at DESC)
    WHERE task_id IS NOT NULL;

ALTER TABLE shipyard_outbound_confirmation_pallets
    ALTER COLUMN qr_payload DROP NOT NULL,
    ALTER COLUMN qr_payload_sha256 DROP NOT NULL;

ALTER TABLE shipyard_outbound_confirmation_pallets
    ADD COLUMN IF NOT EXISTS label_recognition_id UUID
        REFERENCES shipyard_label_recognitions(recognition_id)
        ON DELETE RESTRICT;

COMMIT;
