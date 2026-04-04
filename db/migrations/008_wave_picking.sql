-- ============================================================
-- Migration 008: Wave Picking
-- Adds wave_pick_orders and wave_pick_breakdown tables
-- ============================================================

CREATE TABLE wave_pick_orders (
    id SERIAL PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES pick_batches(batch_id),
    so_id INTEGER NOT NULL REFERENCES sales_orders(so_id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(batch_id, so_id)
);

CREATE TABLE wave_pick_breakdown (
    id SERIAL PRIMARY KEY,
    pick_task_id INTEGER NOT NULL REFERENCES pick_tasks(pick_task_id),
    so_id INTEGER NOT NULL REFERENCES sales_orders(so_id),
    so_line_id INTEGER NOT NULL REFERENCES sales_order_lines(so_line_id),
    quantity INTEGER NOT NULL,
    quantity_picked INTEGER DEFAULT 0,
    short_quantity INTEGER DEFAULT 0
);

CREATE INDEX ix_wave_pick_breakdown_task ON wave_pick_breakdown(pick_task_id);
CREATE INDEX ix_wave_pick_orders_batch ON wave_pick_orders(batch_id);
