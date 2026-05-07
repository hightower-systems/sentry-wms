-- ============================================================
-- Migration 049: transfer orders (v1.8.0 #281)
-- ============================================================
-- Warehouse-to-warehouse inventory transfers. Hard-reserved at
-- import via inventory.quantity_allocated (#1 SO ATP already excludes
-- allocated quantity, so TO commits land in the same column and SO
-- pickers see them excluded with no SO-side code change). Picked
-- through the existing picking module via a new pick_tasks
-- discriminator (so_id XOR to_id). Admin-approved per submission
-- batch with a self-approval gate. Emits transfer.completed/1 on
-- each approval (existing schema reused; aggregate_id is the
-- approval row id so multi-batch TOs get distinct events).
--
-- v1.8.0 migration discipline: SET lock_timeout / statement_timeout
-- at the top so a bad migration fails fast instead of holding
-- ACCESS EXCLUSIVE during a long rewrite. The XOR CHECK on
-- pick_tasks lands NOT VALID then VALIDATE outside the BEGIN/COMMIT
-- so the validation lock is SHARE UPDATE EXCLUSIVE rather than
-- ACCESS EXCLUSIVE.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

CREATE TABLE IF NOT EXISTS transfer_orders (
    to_id                     SERIAL PRIMARY KEY,
    to_number                 VARCHAR(32) NOT NULL UNIQUE,
    source_warehouse_id       INT NOT NULL REFERENCES warehouses(warehouse_id),
    destination_warehouse_id  INT NOT NULL REFERENCES warehouses(warehouse_id),
    status                    VARCHAR(24) NOT NULL DEFAULT 'OPEN'
                                CHECK (status IN ('OPEN','PARTIALLY_PICKED','AWAITING_APPROVAL','APPROVED','CLOSED','CANCELLED')),
    created_by                VARCHAR(100) NOT NULL,
    notes                     TEXT,
    external_id               UUID NOT NULL UNIQUE,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT transfer_orders_warehouses_distinct
        CHECK (source_warehouse_id <> destination_warehouse_id)
);

CREATE INDEX IF NOT EXISTS ix_transfer_orders_source ON transfer_orders(source_warehouse_id, status);
CREATE INDEX IF NOT EXISTS ix_transfer_orders_dest   ON transfer_orders(destination_warehouse_id, status);
CREATE INDEX IF NOT EXISTS ix_transfer_orders_status ON transfer_orders(status, created_at);

CREATE TABLE IF NOT EXISTS transfer_order_lines (
    to_line_id      SERIAL PRIMARY KEY,
    to_id           INT NOT NULL REFERENCES transfer_orders(to_id) ON DELETE CASCADE,
    item_id         INT NOT NULL REFERENCES items(item_id),
    line_number     INT NOT NULL,
    requested_qty   INT NOT NULL CHECK (requested_qty > 0),
    committed_qty   INT NOT NULL DEFAULT 0 CHECK (committed_qty >= 0),
    picked_qty      INT NOT NULL DEFAULT 0 CHECK (picked_qty >= 0),
    approved_qty    INT NOT NULL DEFAULT 0 CHECK (approved_qty >= 0),
    status          VARCHAR(24) NOT NULL DEFAULT 'PENDING'
                      CHECK (status IN ('PENDING','PARTIALLY_PICKED','PICKED','APPROVED','SHORT_CLOSED')),
    UNIQUE (to_id, line_number),
    CHECK (committed_qty <= requested_qty),
    CHECK (picked_qty <= committed_qty),
    CHECK (approved_qty <= picked_qty)
);

CREATE INDEX IF NOT EXISTS ix_transfer_order_lines_to ON transfer_order_lines(to_id);
CREATE INDEX IF NOT EXISTS ix_transfer_order_lines_item ON transfer_order_lines(item_id);

CREATE TABLE IF NOT EXISTS transfer_order_approvals (
    to_approval_id    SERIAL PRIMARY KEY,
    to_id             INT NOT NULL REFERENCES transfer_orders(to_id) ON DELETE CASCADE,
    submitted_by      VARCHAR(100) NOT NULL,
    submitted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_by       VARCHAR(100),
    approved_at       TIMESTAMPTZ,
    rejected_at       TIMESTAMPTZ,
    rejection_reason  TEXT,
    lines_snapshot    JSONB NOT NULL,
    status            VARCHAR(16) NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING','APPROVED','REJECTED')),
    external_id       UUID NOT NULL UNIQUE,
    CHECK ((status = 'APPROVED' AND approved_at IS NOT NULL)
        OR (status = 'REJECTED' AND rejected_at IS NOT NULL)
        OR (status = 'PENDING'))
);

CREATE INDEX IF NOT EXISTS ix_to_approvals_to ON transfer_order_approvals(to_id);
CREATE INDEX IF NOT EXISTS ix_to_approvals_pending ON transfer_order_approvals(status, submitted_at)
    WHERE status = 'PENDING';

-- pick_tasks gains TO discriminator. so_id / so_line_id drop NOT NULL
-- so a TO pick row carries to_id + to_line_id with so_id NULL. The
-- XOR CHECK below enforces exactly one of the two is set.
ALTER TABLE pick_tasks
    ALTER COLUMN so_id DROP NOT NULL,
    ALTER COLUMN so_line_id DROP NOT NULL;

ALTER TABLE pick_tasks
    ADD COLUMN IF NOT EXISTS to_id        INT REFERENCES transfer_orders(to_id),
    ADD COLUMN IF NOT EXISTS to_line_id   INT REFERENCES transfer_order_lines(to_line_id);

CREATE INDEX IF NOT EXISTS ix_pick_tasks_to      ON pick_tasks(to_id);
CREATE INDEX IF NOT EXISTS ix_pick_tasks_to_line ON pick_tasks(to_line_id);

ALTER TABLE pick_tasks
    DROP CONSTRAINT IF EXISTS pick_tasks_target_xor;

ALTER TABLE pick_tasks
    ADD CONSTRAINT pick_tasks_target_xor
    CHECK ((so_id IS NOT NULL AND to_id IS NULL)
        OR (so_id IS NULL     AND to_id IS NOT NULL))
    NOT VALID;

-- Self-approval gate setting. Mirrors the cycle count adjustment
-- self-approval pattern. Default TRUE so out-of-the-box behaviour
-- requires a second admin to approve another admin's submission.
INSERT INTO app_settings (key, value)
VALUES ('transfer_order_block_self_approval', 'true')
ON CONFLICT (key) DO NOTHING;

COMMIT;

-- VALIDATE outside the BEGIN/COMMIT so the validation lock is
-- SHARE UPDATE EXCLUSIVE rather than ACCESS EXCLUSIVE. Existing
-- pick_tasks rows all have so_id NOT NULL so the existing data
-- satisfies the XOR; this is just the post-validation flip.
ALTER TABLE pick_tasks VALIDATE CONSTRAINT pick_tasks_target_xor;
