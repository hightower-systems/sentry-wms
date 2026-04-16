-- ============================================================
-- SENTRY WMS - PostgreSQL Schema
-- ============================================================
-- Development: PostgreSQL (local Docker)
-- Production:  PostgreSQL Cloud or Fabric SQL Database
-- ============================================================

-- ============================================================
-- LOCATIONS & WAREHOUSES
-- ============================================================

CREATE TABLE warehouses (
    warehouse_id SERIAL PRIMARY KEY,
    warehouse_code VARCHAR(20) NOT NULL UNIQUE,
    warehouse_name VARCHAR(100) NOT NULL,
    address VARCHAR(500),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE zones (
    zone_id SERIAL PRIMARY KEY,
    warehouse_id INT NOT NULL REFERENCES warehouses(warehouse_id),
    zone_code VARCHAR(20) NOT NULL,
    zone_name VARCHAR(100) NOT NULL,
    zone_type VARCHAR(50) NOT NULL,  -- 'RECEIVING', 'STORAGE', 'PICKING', 'STAGING', 'SHIPPING'
    is_active BOOLEAN DEFAULT TRUE,
    UNIQUE(warehouse_id, zone_code)
);

CREATE TABLE bins (
    bin_id SERIAL PRIMARY KEY,
    zone_id INT NOT NULL REFERENCES zones(zone_id),
    warehouse_id INT NOT NULL REFERENCES warehouses(warehouse_id),
    bin_code VARCHAR(50) NOT NULL,         -- e.g. 'A-01-03-02' (Aisle-Row-Level-Position)
    bin_barcode VARCHAR(100) NOT NULL,     -- scannable barcode value
    bin_type VARCHAR(50) NOT NULL DEFAULT 'Pickable',  -- 'Staging', 'PickableStaging', 'Pickable'
    aisle VARCHAR(10),
    row_num VARCHAR(10),
    level_num VARCHAR(10),
    position_num VARCHAR(10),
    pick_sequence INT NOT NULL DEFAULT 0,  -- CRITICAL: drives pick path optimization
    putaway_sequence INT NOT NULL DEFAULT 0,
    max_weight_lbs DECIMAL(10,2),
    max_volume_cuft DECIMAL(10,2),
    description VARCHAR(200),
    is_active BOOLEAN DEFAULT TRUE,
    UNIQUE(warehouse_id, bin_code)
);

CREATE INDEX ix_bins_pick_sequence ON bins(warehouse_id, pick_sequence);
CREATE INDEX ix_bins_barcode ON bins(bin_barcode);

-- ============================================================
-- ITEMS (SKU MASTER)
-- ============================================================

CREATE TABLE items (
    item_id SERIAL PRIMARY KEY,
    sku VARCHAR(50) NOT NULL UNIQUE,
    item_name VARCHAR(200) NOT NULL,
    description VARCHAR(1000),
    upc VARCHAR(50),                       -- primary barcode
    barcode_aliases JSONB,                 -- array of alternate barcodes
    category VARCHAR(100),
    weight_lbs DECIMAL(10,4),
    length_in DECIMAL(10,2),
    width_in DECIMAL(10,2),
    height_in DECIMAL(10,2),
    default_bin_id INT REFERENCES bins(bin_id),
    reorder_point INT DEFAULT 0,
    reorder_qty INT DEFAULT 0,
    is_lot_tracked BOOLEAN DEFAULT FALSE,
    is_serial_tracked BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ix_items_upc ON items(upc);
CREATE INDEX ix_items_sku ON items(sku);

-- ============================================================
-- INVENTORY (Current stock by bin)
-- ============================================================

CREATE TABLE inventory (
    inventory_id SERIAL PRIMARY KEY,
    item_id INT NOT NULL REFERENCES items(item_id),
    bin_id INT NOT NULL REFERENCES bins(bin_id),
    warehouse_id INT NOT NULL REFERENCES warehouses(warehouse_id),
    quantity_on_hand INT NOT NULL DEFAULT 0,
    quantity_allocated INT NOT NULL DEFAULT 0,  -- reserved for open orders
    -- quantity_available is computed in queries: (quantity_on_hand - quantity_allocated)
    lot_number VARCHAR(50),
    expiry_date DATE,
    last_counted_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(item_id, bin_id, lot_number)
);

CREATE INDEX ix_inventory_item ON inventory(item_id);
CREATE INDEX ix_inventory_bin ON inventory(bin_id);
CREATE INDEX ix_inventory_warehouse ON inventory(warehouse_id);

-- ============================================================
-- PURCHASE ORDERS (Inbound / Receiving)
-- ============================================================

CREATE TABLE purchase_orders (
    po_id SERIAL PRIMARY KEY,
    po_number VARCHAR(50) NOT NULL UNIQUE,
    po_barcode VARCHAR(100),               -- scannable PO barcode
    vendor_name VARCHAR(200),
    vendor_id VARCHAR(50),
    status VARCHAR(20) NOT NULL DEFAULT 'OPEN',  -- 'OPEN', 'PARTIAL', 'RECEIVED', 'CLOSED'
    expected_date DATE,
    warehouse_id INT NOT NULL REFERENCES warehouses(warehouse_id),
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    received_at TIMESTAMPTZ,
    created_by VARCHAR(100)
);

CREATE TABLE purchase_order_lines (
    po_line_id SERIAL PRIMARY KEY,
    po_id INT NOT NULL REFERENCES purchase_orders(po_id),
    item_id INT NOT NULL REFERENCES items(item_id),
    quantity_ordered INT NOT NULL,
    quantity_received INT NOT NULL DEFAULT 0,
    -- quantity_remaining computed in queries: (quantity_ordered - quantity_received)
    unit_cost DECIMAL(10,4),
    line_number INT NOT NULL,
    status VARCHAR(20) DEFAULT 'PENDING'   -- 'PENDING', 'PARTIAL', 'RECEIVED'
);

-- ============================================================
-- ITEM RECEIPTS (Created when PO items are scanned in)
-- ============================================================

CREATE TABLE item_receipts (
    receipt_id SERIAL PRIMARY KEY,
    po_id INT REFERENCES purchase_orders(po_id),
    po_line_id INT REFERENCES purchase_order_lines(po_line_id),
    item_id INT NOT NULL REFERENCES items(item_id),
    quantity_received INT NOT NULL,
    bin_id INT NOT NULL REFERENCES bins(bin_id),  -- staging bin on receipt
    warehouse_id INT NOT NULL REFERENCES warehouses(warehouse_id),
    lot_number VARCHAR(50),
    serial_number VARCHAR(100),
    received_by VARCHAR(100) NOT NULL,
    received_at TIMESTAMPTZ DEFAULT NOW(),
    notes VARCHAR(500)
);

-- ============================================================
-- SALES ORDERS (Outbound / Picking)
-- ============================================================

CREATE TABLE sales_orders (
    so_id SERIAL PRIMARY KEY,
    so_number VARCHAR(50) NOT NULL UNIQUE,
    so_barcode VARCHAR(100),               -- scannable pick ticket barcode
    customer_name VARCHAR(200),
    customer_id VARCHAR(50),
    customer_phone VARCHAR(50),
    customer_address TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'OPEN',  -- 'OPEN', 'PICKING', 'PICKED', 'PACKING', 'PACKED', 'SHIPPED', 'CANCELLED'
    priority INT DEFAULT 0,                -- higher = pick first
    warehouse_id INT NOT NULL REFERENCES warehouses(warehouse_id),
    ship_method VARCHAR(50),
    ship_address VARCHAR(500),
    order_date TIMESTAMPTZ,
    ship_by_date DATE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    picked_at TIMESTAMPTZ,
    packed_at TIMESTAMPTZ,
    shipped_at TIMESTAMPTZ,
    carrier VARCHAR(100),
    tracking_number VARCHAR(255),
    created_by VARCHAR(100)
);

CREATE TABLE sales_order_lines (
    so_line_id SERIAL PRIMARY KEY,
    so_id INT NOT NULL REFERENCES sales_orders(so_id),
    item_id INT NOT NULL REFERENCES items(item_id),
    quantity_ordered INT NOT NULL,
    quantity_allocated INT NOT NULL DEFAULT 0,
    quantity_picked INT NOT NULL DEFAULT 0,
    quantity_packed INT NOT NULL DEFAULT 0,
    quantity_shipped INT NOT NULL DEFAULT 0,
    line_number INT NOT NULL,
    status VARCHAR(20) DEFAULT 'PENDING'   -- 'PENDING', 'ALLOCATED', 'PICKED', 'PACKED', 'SHIPPED'
);

-- ============================================================
-- PICK BATCHES (Groups multiple orders for efficient walking)
-- ============================================================

CREATE TABLE pick_batches (
    batch_id SERIAL PRIMARY KEY,
    batch_number VARCHAR(50) NOT NULL UNIQUE,
    warehouse_id INT NOT NULL REFERENCES warehouses(warehouse_id),
    status VARCHAR(20) NOT NULL DEFAULT 'OPEN',  -- 'OPEN', 'IN_PROGRESS', 'COMPLETED'
    assigned_to VARCHAR(100),
    total_orders INT DEFAULT 0,
    total_items INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE TABLE pick_batch_orders (
    batch_order_id SERIAL PRIMARY KEY,
    batch_id INT NOT NULL REFERENCES pick_batches(batch_id),
    so_id INT NOT NULL REFERENCES sales_orders(so_id),
    tote_number VARCHAR(50),               -- physical tote label for this order in the batch
    UNIQUE(batch_id, so_id)
);

CREATE TABLE pick_tasks (
    pick_task_id SERIAL PRIMARY KEY,
    batch_id INT NOT NULL REFERENCES pick_batches(batch_id),
    so_id INT NOT NULL REFERENCES sales_orders(so_id),
    so_line_id INT NOT NULL REFERENCES sales_order_lines(so_line_id),
    item_id INT NOT NULL REFERENCES items(item_id),
    bin_id INT NOT NULL REFERENCES bins(bin_id),
    quantity_to_pick INT NOT NULL,
    quantity_picked INT NOT NULL DEFAULT 0,
    pick_sequence INT NOT NULL,            -- ORDER BY this for optimized walk path
    tote_number VARCHAR(50),
    status VARCHAR(20) DEFAULT 'PENDING',  -- 'PENDING', 'PICKED', 'SHORT', 'SKIPPED'
    picked_by VARCHAR(100),
    picked_at TIMESTAMPTZ,
    scan_confirmed BOOLEAN DEFAULT FALSE   -- item barcode was scanned to verify
);

CREATE INDEX ix_pick_tasks_batch_sequence ON pick_tasks(batch_id, pick_sequence);

-- Wave picking: links SOs to wave batches
CREATE TABLE wave_pick_orders (
    id SERIAL PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES pick_batches(batch_id),
    so_id INTEGER NOT NULL REFERENCES sales_orders(so_id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(batch_id, so_id)
);

CREATE INDEX ix_wave_pick_orders_batch ON wave_pick_orders(batch_id);

-- Wave picking: per-SO breakdown for combined pick tasks
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

-- ============================================================
-- BIN TRANSFERS (Put-away + general moves)
-- ============================================================

CREATE TABLE bin_transfers (
    transfer_id SERIAL PRIMARY KEY,
    item_id INT NOT NULL REFERENCES items(item_id),
    from_bin_id INT NOT NULL REFERENCES bins(bin_id),
    to_bin_id INT NOT NULL REFERENCES bins(bin_id),
    warehouse_id INT NOT NULL REFERENCES warehouses(warehouse_id),
    quantity INT NOT NULL,
    transfer_type VARCHAR(20) NOT NULL,    -- 'PUTAWAY', 'MOVE', 'REPLENISH'
    lot_number VARCHAR(50),
    reason VARCHAR(200),
    transferred_by VARCHAR(100) NOT NULL,
    transferred_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- CYCLE COUNTS
-- ============================================================

CREATE TABLE cycle_counts (
    count_id SERIAL PRIMARY KEY,
    warehouse_id INT NOT NULL REFERENCES warehouses(warehouse_id),
    bin_id INT NOT NULL REFERENCES bins(bin_id),
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',  -- 'PENDING', 'IN_PROGRESS', 'COMPLETED', 'VARIANCE'
    assigned_to VARCHAR(100),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE cycle_count_lines (
    count_line_id SERIAL PRIMARY KEY,
    count_id INT NOT NULL REFERENCES cycle_counts(count_id),
    item_id INT NOT NULL REFERENCES items(item_id),
    expected_quantity INT NOT NULL,
    counted_quantity INT,
    -- variance computed in queries: (counted_quantity - expected_quantity)
    scanned BOOLEAN DEFAULT FALSE,
    unexpected BOOLEAN DEFAULT FALSE,
    counted_by VARCHAR(100),
    counted_at TIMESTAMPTZ
);

-- ============================================================
-- ITEM FULFILLMENTS (Ship confirmations)
-- ============================================================

CREATE TABLE item_fulfillments (
    fulfillment_id SERIAL PRIMARY KEY,
    so_id INT NOT NULL REFERENCES sales_orders(so_id),
    warehouse_id INT NOT NULL REFERENCES warehouses(warehouse_id),
    tracking_number VARCHAR(100),
    carrier VARCHAR(50),
    ship_method VARCHAR(50),
    status VARCHAR(20) DEFAULT 'SHIPPED',
    shipped_by VARCHAR(100),
    shipped_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE item_fulfillment_lines (
    fulfillment_line_id SERIAL PRIMARY KEY,
    fulfillment_id INT NOT NULL REFERENCES item_fulfillments(fulfillment_id),
    so_line_id INT NOT NULL REFERENCES sales_order_lines(so_line_id),
    item_id INT NOT NULL REFERENCES items(item_id),
    quantity_shipped INT NOT NULL,
    bin_id INT NOT NULL REFERENCES bins(bin_id),  -- where it was picked from
    lot_number VARCHAR(50),
    serial_number VARCHAR(100)
);

-- ============================================================
-- INVENTORY ADJUSTMENTS (Variance corrections, damages, etc.)
-- ============================================================

CREATE TABLE inventory_adjustments (
    adjustment_id SERIAL PRIMARY KEY,
    item_id INT NOT NULL REFERENCES items(item_id),
    bin_id INT NOT NULL REFERENCES bins(bin_id),
    warehouse_id INT NOT NULL REFERENCES warehouses(warehouse_id),
    quantity_change INT NOT NULL,           -- positive = add, negative = remove
    reason_code VARCHAR(50) NOT NULL,      -- 'CYCLE_COUNT', 'DAMAGE', 'FOUND', 'LOST', 'CORRECTION'
    reason_detail VARCHAR(500),
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',  -- 'PENDING', 'APPROVED', 'REJECTED'
    adjusted_by VARCHAR(100) NOT NULL,
    adjusted_at TIMESTAMPTZ DEFAULT NOW(),
    cycle_count_id INT REFERENCES cycle_counts(count_id)
);

-- ============================================================
-- AUDIT LOG (Every action tracked)
-- ============================================================

CREATE TABLE audit_log (
    log_id BIGSERIAL PRIMARY KEY,
    action_type VARCHAR(50) NOT NULL,      -- 'RECEIVE', 'PUTAWAY', 'PICK', 'PACK', 'SHIP', 'TRANSFER', 'ADJUST', 'COUNT'
    entity_type VARCHAR(50) NOT NULL,      -- 'PO', 'SO', 'ITEM', 'BIN', 'INVENTORY'
    entity_id INT NOT NULL,
    user_id VARCHAR(100) NOT NULL,
    device_id VARCHAR(100),                -- Chainway C6000 device identifier
    warehouse_id INT REFERENCES warehouses(warehouse_id),
    details JSONB,                         -- JSON blob of action details
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ix_audit_log_action ON audit_log(action_type, created_at);
CREATE INDEX ix_audit_log_entity ON audit_log(entity_type, entity_id);

-- ============================================================
-- USERS (Authentication)
-- ============================================================

CREATE TABLE users (
    user_id SERIAL PRIMARY KEY,
    username VARCHAR(50) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(100) NOT NULL,
    role VARCHAR(20) NOT NULL DEFAULT 'USER',  -- 'ADMIN', 'USER'
    warehouse_id INT REFERENCES warehouses(warehouse_id),
    warehouse_ids INT[] DEFAULT '{}',          -- multi-warehouse assignment
    allowed_functions TEXT[] DEFAULT '{}',      -- mobile module access: receive, putaway, pick, pack, ship, count, transfer
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_login TIMESTAMPTZ,
    password_changed_at TIMESTAMPTZ
);

-- ============================================================
-- LOGIN ATTEMPTS (Persistent rate limiting)
-- ============================================================

CREATE TABLE login_attempts (
    id SERIAL PRIMARY KEY,
    key VARCHAR(255) NOT NULL UNIQUE,
    attempts INT NOT NULL DEFAULT 0,
    locked_until TIMESTAMPTZ,
    last_attempt TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_login_attempts_key ON login_attempts (key);

-- ============================================================
-- APP SETTINGS (Configurable system settings)
-- ============================================================

CREATE TABLE app_settings (
    id SERIAL PRIMARY KEY,
    key VARCHAR(100) UNIQUE NOT NULL,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- PREFERRED BINS (Priority-ranked bin assignments per item)
-- ============================================================

CREATE TABLE preferred_bins (
    preferred_bin_id SERIAL PRIMARY KEY,
    item_id INT NOT NULL REFERENCES items(item_id),
    bin_id INT NOT NULL REFERENCES bins(bin_id),
    priority INT NOT NULL DEFAULT 1,
    notes VARCHAR(500),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(item_id, bin_id)
);

CREATE INDEX ix_preferred_bins_item_priority ON preferred_bins(item_id, priority);

-- ============================================================
-- FOREIGN KEY INDEXES
-- PostgreSQL does not auto-index FK columns. These are needed
-- for JOIN performance and cascading delete efficiency.
-- ============================================================

-- Locations
CREATE INDEX ix_zones_warehouse ON zones(warehouse_id);

-- Orders
CREATE INDEX ix_purchase_orders_warehouse ON purchase_orders(warehouse_id);
CREATE INDEX ix_purchase_order_lines_po ON purchase_order_lines(po_id);
CREATE INDEX ix_sales_orders_warehouse ON sales_orders(warehouse_id);
CREATE INDEX ix_sales_order_lines_so ON sales_order_lines(so_id);

-- Receiving
CREATE INDEX ix_item_receipts_po ON item_receipts(po_id);
CREATE INDEX ix_item_receipts_po_line ON item_receipts(po_line_id);

-- Picking
CREATE INDEX ix_pick_batches_warehouse ON pick_batches(warehouse_id);
CREATE INDEX ix_pick_batch_orders_so ON pick_batch_orders(so_id);
CREATE INDEX ix_pick_tasks_so ON pick_tasks(so_id);
CREATE INDEX ix_pick_tasks_so_line ON pick_tasks(so_line_id);

-- Shipping
CREATE INDEX ix_item_fulfillments_so ON item_fulfillments(so_id);
CREATE INDEX ix_fulfillment_lines_fulfillment ON item_fulfillment_lines(fulfillment_id);

-- Inventory operations
CREATE INDEX ix_transfers_warehouse ON bin_transfers(warehouse_id);
CREATE INDEX ix_cycle_counts_warehouse ON cycle_counts(warehouse_id);
CREATE INDEX ix_cycle_count_lines_count ON cycle_count_lines(count_id);
CREATE INDEX ix_inventory_adjustments_warehouse ON inventory_adjustments(warehouse_id);

-- Audit
CREATE INDEX ix_audit_log_warehouse ON audit_log(warehouse_id);

-- ============================================================
-- CONNECTOR CREDENTIALS (Encrypted ERP/commerce API secrets)
-- ============================================================

CREATE TABLE connector_credentials (
    id SERIAL PRIMARY KEY,
    connector_name VARCHAR(64) NOT NULL,
    warehouse_id INT NOT NULL REFERENCES warehouses(warehouse_id),
    credential_key VARCHAR(128) NOT NULL,
    encrypted_value TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(connector_name, warehouse_id, credential_key)
);
