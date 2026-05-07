-- ============================================================
-- SENTRY WMS - PostgreSQL Schema
-- ============================================================
-- Development: PostgreSQL (local Docker)
-- Production:  PostgreSQL Cloud or Fabric SQL Database
-- ============================================================

-- gen_random_uuid() backs the external_id DEFAULT on every aggregate /
-- actor table below. The extension is idempotent and also required by
-- the audit_log hash-chain trigger further down.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================
-- LOCATIONS & WAREHOUSES
-- ============================================================

CREATE TABLE warehouses (
    warehouse_id SERIAL PRIMARY KEY,
    warehouse_code VARCHAR(20) NOT NULL UNIQUE,
    warehouse_name VARCHAR(100) NOT NULL,
    address VARCHAR(500),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    -- v1.8.0 (#283): productivity dashboard "Today" / "Yesterday"
    -- range maps to warehouse-local, not UTC. Default matches the
    -- AvidMax baseline; per-warehouse override is operator-managed
    -- in v1.8 (no admin UI in this release).
    timezone VARCHAR(64) NOT NULL DEFAULT 'America/Denver'
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
    external_id UUID UNIQUE NOT NULL,
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
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    external_id UUID UNIQUE NOT NULL,
    -- v1.7.0 Pipe B: pointer back to the most-recent applied inbound row.
    -- Unindexed, no FK; see db/migrations/040_inbound_items.sql.
    latest_inbound_id BIGINT
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
    created_by VARCHAR(100),
    external_id UUID UNIQUE NOT NULL,
    -- v1.7.0 Pipe B: pointer back to the most-recent applied inbound row.
    -- Unindexed, no FK; see db/migrations/043_inbound_purchase_orders.sql.
    latest_inbound_id BIGINT
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
    notes VARCHAR(500),
    external_id UUID UNIQUE NOT NULL
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
    -- v1.7.0 (#266): per-order ecommerce ship-to / bill-to. ship_address
    -- above stays the warehouse-floor field used by pick/pack/ship;
    -- shipping_address + billing_address are the canonical values
    -- inbound consumers populate (per-order, not per-customer).
    billing_address TEXT,
    shipping_address TEXT,
    -- v1.8.0 (#282): values from the source ERP. Currency implied per
    -- Sentry instance (no per-order currency in v1.8). Wire-level
    -- range / precision validation lives in the Pydantic inbound
    -- schema; the column itself is permissive.
    order_total            NUMERIC(12,2),
    customer_shipping_paid NUMERIC(12,2),
    order_date TIMESTAMPTZ,
    ship_by_date DATE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    picked_at TIMESTAMPTZ,
    packed_at TIMESTAMPTZ,
    shipped_at TIMESTAMPTZ,
    carrier VARCHAR(100),
    tracking_number VARCHAR(255),
    created_by VARCHAR(100),
    external_id UUID UNIQUE NOT NULL,
    -- v1.7.0 Pipe B: pointer back to the most-recent applied inbound row.
    -- Unindexed, no FK; see db/migrations/039_inbound_sales_orders.sql.
    latest_inbound_id BIGINT
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
    transferred_at TIMESTAMPTZ DEFAULT NOW(),
    external_id UUID UNIQUE NOT NULL
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
    completed_at TIMESTAMPTZ,
    external_id UUID UNIQUE NOT NULL
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
    shipped_at TIMESTAMPTZ DEFAULT NOW(),
    external_id UUID UNIQUE NOT NULL
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
    cycle_count_id INT REFERENCES cycle_counts(count_id),
    external_id UUID UNIQUE NOT NULL
);

-- ============================================================
-- AUDIT LOG (Every action tracked)
-- ============================================================

-- v1.7.0 #271: log_id sequence is owned by audit_log.log_id but the
-- column has no DEFAULT, so concurrent transactions cannot pre-allocate
-- log_ids out of trigger-execution order. The chain trigger assigns
-- NEW.log_id := nextval(...) inside its lock-protected critical section.
-- See db/migrations/047_audit_log_chain_serialization.sql.
CREATE SEQUENCE audit_log_log_id_seq;

CREATE TABLE audit_log (
    log_id BIGINT PRIMARY KEY NOT NULL,
    action_type VARCHAR(50) NOT NULL,      -- 'RECEIVE', 'PUTAWAY', 'PICK', 'PACK', 'SHIP', 'TRANSFER', 'ADJUST', 'COUNT'
    entity_type VARCHAR(50) NOT NULL,      -- 'PO', 'SO', 'ITEM', 'BIN', 'INVENTORY'
    entity_id INT NOT NULL,
    user_id VARCHAR(100) NOT NULL,
    device_id VARCHAR(100),                -- Chainway C6000 device identifier
    warehouse_id INT REFERENCES warehouses(warehouse_id),
    details JSONB,                         -- JSON blob of action details
    created_at TIMESTAMPTZ DEFAULT NOW(),
    -- V-025: tamper-resistance hash chain. Populated by the
    -- audit_log_chain_before_insert trigger defined below. UPDATE and
    -- DELETE are rejected by triggers; any retroactive change to a
    -- row breaks downstream row_hash values, detectable via
    -- verify_audit_log_chain().
    prev_hash BYTEA,
    row_hash BYTEA
);

CREATE INDEX ix_audit_log_action ON audit_log(action_type, created_at);
CREATE INDEX ix_audit_log_entity ON audit_log(entity_type, entity_id);
-- v1.8.0 (#283): productivity dashboard aggregation. Pattern is
-- WHERE created_at BETWEEN :s AND :e AND action_type = ANY(:actions)
-- AND warehouse_id = :w GROUP BY user_id, action_type. INCLUDE clause
-- covers the projection for index-only scans.
CREATE INDEX ix_audit_log_dashboard
    ON audit_log(action_type, created_at, user_id, warehouse_id)
    INCLUDE (entity_id, details);

-- V-025 tamper resistance: hash-chain trigger + append-only guards.
-- The identical DDL lives in db/migrations/016_audit_log_tamper_resistance.sql
-- for deployments that were created before V-025 shipped.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- v1.7.0 #271: chain serialization sentinel + LOCK TABLE EXCLUSIVE on
-- the chain trigger's critical section. The chain trigger:
--   1. Acquires LOCK TABLE EXCLUSIVE on audit_log_chain_head.
--   2. Calls nextval('audit_log_log_id_seq') for NEW.log_id (so log_id
--      ordering matches trigger-execution ordering -- otherwise concurrent
--      transactions pre-allocate log_ids out of trigger-execution order
--      and the strict-by-log_id chain forks).
--   3. Reads sentinel.row_hash, computes NEW.row_hash, updates sentinel.
-- Under READ COMMITTED + EXCLUSIVE table lock, the next waiter sees the
-- prior holder's committed UPDATE on unblock; the lock + the explicit
-- log_id allocation together yield strict-by-log_id chain integrity.
-- See db/migrations/047_audit_log_chain_serialization.sql for the
-- iteration history (advisory lock + FOR UPDATE were tried first, both
-- insufficient).
CREATE TABLE audit_log_chain_head (
    singleton  BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    row_hash   BYTEA NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO audit_log_chain_head (singleton, row_hash)
VALUES (TRUE, '\x00'::bytea);

CREATE OR REPLACE FUNCTION audit_log_chain_hash() RETURNS TRIGGER AS $$
DECLARE
    prev BYTEA;
    payload TEXT;
BEGIN
    -- v1.7.0 #271: serialize the entire critical section (log_id
    -- allocation + prev_hash read + row_hash compute + sentinel
    -- update). EXCLUSIVE table lock blocks other writers; nextval
    -- inside the lock guarantees log_id-order matches trigger-
    -- execution-order so the strict-by-log_id chain holds.
    LOCK TABLE audit_log_chain_head IN EXCLUSIVE MODE;
    NEW.log_id := nextval('audit_log_log_id_seq');
    SELECT row_hash INTO prev FROM audit_log_chain_head
     WHERE singleton = TRUE;
    NEW.prev_hash := COALESCE(prev, '\x00'::bytea);
    payload := COALESCE(NEW.action_type, '') || '|' ||
               COALESCE(NEW.entity_type, '') || '|' ||
               COALESCE(NEW.entity_id::text, '') || '|' ||
               COALESCE(NEW.user_id, '') || '|' ||
               COALESCE(NEW.warehouse_id::text, '') || '|' ||
               COALESCE(NEW.details::text, '') || '|' ||
               COALESCE(NEW.created_at::text, NOW()::text);
    NEW.row_hash := digest(NEW.prev_hash || payload::bytea, 'sha256');
    UPDATE audit_log_chain_head
       SET row_hash = NEW.row_hash, updated_at = NOW()
     WHERE singleton = TRUE;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_chain_before_insert
    BEFORE INSERT ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_chain_hash();

CREATE OR REPLACE FUNCTION audit_log_reject_mutation() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_log rows are append-only (V-025 tamper resistance)';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_no_update
    BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_reject_mutation();

CREATE TRIGGER audit_log_no_delete
    BEFORE DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_reject_mutation();

CREATE OR REPLACE FUNCTION verify_audit_log_chain() RETURNS BIGINT AS $$
DECLARE
    prev BYTEA := '\x00'::bytea;
    r RECORD;
    computed BYTEA;
    payload TEXT;
BEGIN
    FOR r IN SELECT * FROM audit_log ORDER BY log_id ASC LOOP
        IF r.prev_hash IS DISTINCT FROM prev THEN
            RETURN r.log_id;
        END IF;
        payload := COALESCE(r.action_type, '') || '|' ||
                   COALESCE(r.entity_type, '') || '|' ||
                   COALESCE(r.entity_id::text, '') || '|' ||
                   COALESCE(r.user_id, '') || '|' ||
                   COALESCE(r.warehouse_id::text, '') || '|' ||
                   COALESCE(r.details::text, '') || '|' ||
                   COALESCE(r.created_at::text, '');
        computed := digest(r.prev_hash || payload::bytea, 'sha256');
        IF computed IS DISTINCT FROM r.row_hash THEN
            RETURN r.log_id;
        END IF;
        prev := r.row_hash;
    END LOOP;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

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
    password_changed_at TIMESTAMPTZ,
    must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
    external_id UUID UNIQUE NOT NULL
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
    -- v1.5.0 #127: credential_type discriminates v1.3's connector_api_key
    -- rows from future v2+ outbound flavours (outbound_oauth,
    -- outbound_api_key, outbound_bearer). Inbound tokens live in
    -- wms_tokens, not here.
    credential_type VARCHAR(32) NOT NULL DEFAULT 'connector_api_key',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(connector_name, warehouse_id, credential_key)
);

-- ============================================================
-- SYNC STATE (Per-connector health and activity tracking)
-- ============================================================

CREATE TABLE sync_state (
    id SERIAL PRIMARY KEY,
    connector_name VARCHAR(64) NOT NULL,
    warehouse_id INT NOT NULL REFERENCES warehouses(warehouse_id),
    sync_type VARCHAR(32) NOT NULL,              -- 'orders', 'items', 'inventory', 'fulfillment'
    sync_status VARCHAR(16) DEFAULT 'idle',      -- 'idle', 'running', 'error'
    running_since TIMESTAMPTZ,                    -- V-012: stale 'running' recovery timestamp
    run_id UUID,                                  -- V-102: generation id; transitions match on this
    last_synced_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    last_error_at TIMESTAMPTZ,
    last_error_message TEXT,
    consecutive_errors INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(connector_name, warehouse_id, sync_type)
);

CREATE INDEX ix_sync_state_connector ON sync_state(connector_name, warehouse_id);

-- ============================================================
-- CONNECTORS + CONSUMER GROUPS (v1.5.0 polling substrate)
-- ============================================================
-- connectors is deliberately minimal in v1.5.0. v1.9 expands it to
-- the full framework-doc shape; landing the PK now lets consumer_groups
-- (below), wms_tokens (migration 023), and webhook_deliveries (v1.6)
-- all carry the same FK without a later rename.
--
-- consumer_groups tracks per-group cursor state for GET /api/v1/events
-- polling. Decision T throttles last_heartbeat writes to once per 30s
-- inside the handler to cut hot-path write amplification.
--
-- The identical DDL lives in db/migrations/021_consumer_groups.sql
-- for deployments that were created before v1.5.0.
-- ============================================================

CREATE TABLE connectors (
    connector_id VARCHAR(64) PRIMARY KEY,
    display_name VARCHAR(128) NOT NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE consumer_groups (
    consumer_group_id VARCHAR(64)  PRIMARY KEY,
    connector_id      VARCHAR(64)  NOT NULL REFERENCES connectors(connector_id),
    last_cursor       BIGINT       NOT NULL DEFAULT 0,
    last_heartbeat    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    subscription      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_consumer_groups_connector ON consumer_groups (connector_id);

-- v1.5.1 V-207 (#148): tombstones so recreating a consumer_group
-- under an id that was previously deleted forces explicit
-- acknowledgement of the cursor=0 replay. See
-- db/migrations/027_consumer_groups_tombstones.sql.
CREATE TABLE consumer_groups_tombstones (
    consumer_group_id      VARCHAR(64)  PRIMARY KEY,
    last_cursor_at_delete  BIGINT       NOT NULL DEFAULT 0,
    connector_id           VARCHAR(64),
    deleted_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    deleted_by             VARCHAR(100)
);

-- ============================================================
-- INBOUND SOURCE SYSTEMS ALLOWLIST (v1.7.0 Pipe B gate)
-- ============================================================
-- Privilege table: a row here is what gates a source_system from
-- writing inbound. Operator-managed via SQL only in v1.7 (no admin
-- endpoint; documented in docs/runbooks/inbound-source-systems.md).
-- Defined ahead of wms_tokens because wms_tokens.source_system FKs
-- into it. The identical DDL lives in
-- db/migrations/037_wms_tokens_inbound_columns.sql for deployments
-- created before v1.7.0.
-- ============================================================
CREATE TABLE inbound_source_systems_allowlist (
    source_system  VARCHAR(64)  PRIMARY KEY,
    kind           VARCHAR(16)  NOT NULL CHECK (kind IN ('connector','internal_tool','manual_import')),
    notes          TEXT,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- v1.7.0 forensic audit (V-157 pattern, mirrors wms_tokens_audit).
CREATE TABLE inbound_source_systems_allowlist_audit (
    audit_id         BIGSERIAL    PRIMARY KEY,
    event_type       VARCHAR(16)  NOT NULL,
    rows_affected    INTEGER,
    sess_user        TEXT         NOT NULL,
    curr_user        TEXT         NOT NULL,
    backend_pid      INTEGER      NOT NULL,
    application_name TEXT,
    event_at         TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX inbound_source_systems_allowlist_audit_event_at
    ON inbound_source_systems_allowlist_audit (event_at DESC);

CREATE OR REPLACE FUNCTION inbound_source_systems_allowlist_audit_delete()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    _count INTEGER;
BEGIN
    SELECT COUNT(*) INTO _count FROM deleted_rows;
    INSERT INTO inbound_source_systems_allowlist_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'DELETE', _count, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

CREATE TRIGGER tr_inbound_source_systems_allowlist_audit_delete
    AFTER DELETE ON inbound_source_systems_allowlist
    REFERENCING OLD TABLE AS deleted_rows
    FOR EACH STATEMENT EXECUTE FUNCTION inbound_source_systems_allowlist_audit_delete();

CREATE OR REPLACE FUNCTION inbound_source_systems_allowlist_audit_truncate()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO inbound_source_systems_allowlist_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'TRUNCATE', NULL, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

-- v1.7.0 #275: this AFTER TRUNCATE trigger fires only on
-- `TRUNCATE inbound_source_systems_allowlist CASCADE`. A plain
-- `TRUNCATE inbound_source_systems_allowlist` raises ForeignKeyViolation
-- before the trigger fires because six v1.7 tables (inbound_sales_orders,
-- inbound_items, inbound_customers, inbound_purchase_orders,
-- inbound_vendors, cross_system_mappings) and one nullable referencer
-- (wms_tokens) declare FKs into source_system. The CASCADE form is
-- therefore the sole path that writes a forensic audit row; a direct
-- plain-TRUNCATE attempt leaves a Postgres error in the logs but no
-- audit_log entry. See docs/audit-log.md for the operator-facing shape.
CREATE TRIGGER tr_inbound_source_systems_allowlist_audit_truncate
    AFTER TRUNCATE ON inbound_source_systems_allowlist
    FOR EACH STATEMENT EXECUTE FUNCTION inbound_source_systems_allowlist_audit_truncate();

-- ============================================================
-- WMS TOKENS (v1.5.0 inbound API tokens for X-WMS-Token auth)
-- ============================================================
-- Hash-only storage per Decision P. token_hash is
-- SHA256(SENTRY_TOKEN_PEPPER || plaintext).hexdigest() per Decision Q.
-- Scope columns are typed arrays per Decision S. Default expiry is
-- one year per Decision R.
--
-- v1.7.0 adds three columns for Pipe B (inbound):
--   source_system     -- nullable FK to inbound_source_systems_allowlist
--                        (PostgreSQL forbids subqueries in CHECK
--                        constraints; nullable FK is the correct shape
--                        and naturally exempts outbound-only tokens).
--   inbound_resources -- TEXT[] scope dimension for inbound resources
--                        (sales_orders / items / customers / vendors /
--                        purchase_orders).
--   mapping_override  -- BOOLEAN capability flag for per-request mapping
--                        overrides; default false.
--   mapping_overrides -- JSONB per-token static override map (v1.8.0
--                        #284, mig 052). Consulted only when
--                        mapping_override is TRUE.
--
-- The identical DDL lives in db/migrations/023_wms_tokens.sql for
-- deployments created before v1.5.0; the v1.7 columns are added by
-- db/migrations/037_wms_tokens_inbound_columns.sql; the v1.8
-- mapping_overrides JSONB is added by
-- db/migrations/052_mapping_override_per_token.sql.
-- ============================================================

CREATE TABLE wms_tokens (
    token_id          BIGSERIAL     PRIMARY KEY,
    token_name        VARCHAR(128)  NOT NULL,
    token_hash        CHAR(64)      UNIQUE NOT NULL,
    warehouse_ids     BIGINT[]      NOT NULL DEFAULT '{}',
    event_types       TEXT[]        NOT NULL DEFAULT '{}',
    endpoints         TEXT[]        NOT NULL DEFAULT '{}',
    connector_id      VARCHAR(64)   REFERENCES connectors(connector_id),
    source_system     VARCHAR(64)   REFERENCES inbound_source_systems_allowlist(source_system),
    inbound_resources TEXT[]        NOT NULL DEFAULT '{}',
    mapping_override  BOOLEAN       NOT NULL DEFAULT FALSE,
    mapping_overrides JSONB         NOT NULL DEFAULT '{}'::jsonb,
    status            VARCHAR(16)   NOT NULL DEFAULT 'active',
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    rotated_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    expires_at        TIMESTAMPTZ   NOT NULL DEFAULT (NOW() + INTERVAL '1 year'),
    revoked_at        TIMESTAMPTZ,
    last_used_at      TIMESTAMPTZ
);

CREATE INDEX wms_tokens_status_rotated ON wms_tokens (status, rotated_at);

-- v1.5.1 #157: forensic instrumentation for wms_tokens deletions.
-- Every DELETE + TRUNCATE fires a trigger that writes who / when /
-- how-many to wms_tokens_audit. Fresh installs get the same shape
-- migration 028 adds to upgrade paths. See the migration file for
-- the background on the Gate 11 / 12 incident this is mitigating.
CREATE TABLE wms_tokens_audit (
    audit_id        BIGSERIAL    PRIMARY KEY,
    event_type      VARCHAR(16)  NOT NULL,  -- 'DELETE' | 'TRUNCATE'
    rows_affected   INTEGER,
    sess_user       TEXT         NOT NULL,
    curr_user       TEXT         NOT NULL,
    backend_pid     INTEGER      NOT NULL,
    application_name TEXT,
    event_at        TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX wms_tokens_audit_event_at ON wms_tokens_audit (event_at DESC);

CREATE OR REPLACE FUNCTION wms_tokens_audit_delete()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    _count INTEGER;
BEGIN
    SELECT COUNT(*) INTO _count FROM deleted_rows;
    INSERT INTO wms_tokens_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'DELETE', _count, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

CREATE TRIGGER tr_wms_tokens_audit_delete
    AFTER DELETE ON wms_tokens
    REFERENCING OLD TABLE AS deleted_rows
    FOR EACH STATEMENT EXECUTE FUNCTION wms_tokens_audit_delete();

CREATE OR REPLACE FUNCTION wms_tokens_audit_truncate()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO wms_tokens_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'TRUNCATE', NULL, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

CREATE TRIGGER tr_wms_tokens_audit_truncate
    AFTER TRUNCATE ON wms_tokens
    FOR EACH STATEMENT EXECUTE FUNCTION wms_tokens_audit_truncate();

-- v1.7.0 #274: defense-in-depth for token revocation cache invalidation.
-- Identical DDL lives in db/migrations/048_wms_tokens_revocation_notify.sql
-- for upgrade paths. AFTER UPDATE OF revoked_at fires
-- pg_notify('wms_token_revocations', token_id) on NULL -> NOT NULL
-- transitions so a direct-DB revoke triggers the same cross-worker
-- cache invalidation as the Flask admin path. See migration file.
CREATE OR REPLACE FUNCTION wms_tokens_revocation_notify()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.revoked_at IS NOT NULL
       AND (OLD.revoked_at IS NULL OR OLD.revoked_at <> NEW.revoked_at)
    THEN
        -- v1.7.0 #278: keep `status` in lock-step with revoked_at on
        -- direct-DB writes. Idempotent; the inner UPDATE doesn't
        -- re-enter this function (AFTER UPDATE OF revoked_at column
        -- filter). See db/migrations/048_wms_tokens_revocation_notify.sql.
        IF NEW.status IS DISTINCT FROM 'revoked' THEN
            UPDATE wms_tokens
               SET status = 'revoked'
             WHERE token_id = NEW.token_id;
        END IF;
        PERFORM pg_notify(
            'wms_token_revocations',
            NEW.token_id::text
        );
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER tr_wms_tokens_revocation_notify
    AFTER UPDATE OF revoked_at ON wms_tokens
    FOR EACH ROW
    EXECUTE FUNCTION wms_tokens_revocation_notify();

-- ============================================================
-- CROSS-SYSTEM MAPPINGS (v1.7.0 Pipe B canonical bridge)
-- ============================================================
-- Bidirectional table binding (source_system, source_type, source_id)
-- to (canonical_type, canonical_id). Each external ID maps to exactly
-- one canonical entity (UNIQUE on the source side); a single canonical
-- entity may carry mappings in many source systems (canonical-side
-- index, not constraint).
--
-- The identical DDL lives in db/migrations/038_cross_system_mappings.sql
-- for deployments created before v1.7.0.
-- ============================================================

CREATE TABLE cross_system_mappings (
    mapping_id       BIGSERIAL    PRIMARY KEY,
    source_system    VARCHAR(64)  NOT NULL REFERENCES inbound_source_systems_allowlist(source_system),
    source_type      VARCHAR(32)  NOT NULL,
    source_id        VARCHAR(128) NOT NULL,
    canonical_type   VARCHAR(32)  NOT NULL,
    canonical_id     UUID         NOT NULL,
    first_seen_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CHECK (source_type    IN ('sales_order','item','customer','vendor','purchase_order')),
    CHECK (canonical_type IN ('sales_order','item','customer','vendor','purchase_order'))
);

CREATE UNIQUE INDEX cross_system_mappings_source_unique
    ON cross_system_mappings (source_system, source_type, source_id);

CREATE INDEX cross_system_mappings_canonical
    ON cross_system_mappings (canonical_type, canonical_id);

-- v1.7.0 forensic audit (V-157 pattern, mirrors wms_tokens_audit).
CREATE TABLE cross_system_mappings_audit (
    audit_id         BIGSERIAL    PRIMARY KEY,
    event_type       VARCHAR(16)  NOT NULL,
    rows_affected    INTEGER,
    sess_user        TEXT         NOT NULL,
    curr_user        TEXT         NOT NULL,
    backend_pid      INTEGER      NOT NULL,
    application_name TEXT,
    event_at         TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX cross_system_mappings_audit_event_at
    ON cross_system_mappings_audit (event_at DESC);

CREATE OR REPLACE FUNCTION cross_system_mappings_audit_delete()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    _count INTEGER;
BEGIN
    SELECT COUNT(*) INTO _count FROM deleted_rows;
    INSERT INTO cross_system_mappings_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'DELETE', _count, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

CREATE TRIGGER tr_cross_system_mappings_audit_delete
    AFTER DELETE ON cross_system_mappings
    REFERENCING OLD TABLE AS deleted_rows
    FOR EACH STATEMENT EXECUTE FUNCTION cross_system_mappings_audit_delete();

CREATE OR REPLACE FUNCTION cross_system_mappings_audit_truncate()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO cross_system_mappings_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'TRUNCATE', NULL, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

CREATE TRIGGER tr_cross_system_mappings_audit_truncate
    AFTER TRUNCATE ON cross_system_mappings
    FOR EACH STATEMENT EXECUTE FUNCTION cross_system_mappings_audit_truncate();

-- ============================================================
-- INBOUND STAGING TABLES (v1.7.0 Pipe B per-resource history)
-- ============================================================
-- Append-only with status flag. Each accepted inbound POST inserts a
-- fresh row; older rows for the same (source_system, external_id)
-- flip to 'superseded'. canonical_id resolves to the canonical
-- table's external_id UUID per the V-216 retrofit. ingested_via_token_id
-- is BIGINT (wms_tokens.token_id is BIGSERIAL) ON DELETE RESTRICT;
-- tokens are revoked via revoked_at, not DELETE. Per-resource files
-- are: 039 sales_orders, 040 items, 041 customers, 042 vendors,
-- 043 purchase_orders.
-- ============================================================

CREATE TABLE inbound_sales_orders (
    inbound_id            BIGSERIAL    PRIMARY KEY,
    source_system         VARCHAR(64)  NOT NULL REFERENCES inbound_source_systems_allowlist(source_system),
    external_id           VARCHAR(128) NOT NULL,
    external_version      VARCHAR(64)  NOT NULL,
    canonical_id          UUID         NOT NULL,
    canonical_payload     JSONB        NOT NULL,
    source_payload        JSONB,
    received_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    status                VARCHAR(16)  NOT NULL DEFAULT 'applied',
    superseded_at         TIMESTAMPTZ,
    ingested_via_token_id BIGINT       NOT NULL REFERENCES wms_tokens(token_id) ON DELETE RESTRICT,
    CHECK (status IN ('applied','superseded'))
);

CREATE UNIQUE INDEX inbound_sales_orders_idempotency
    ON inbound_sales_orders (source_system, external_id, external_version);

CREATE INDEX inbound_sales_orders_current
    ON inbound_sales_orders (source_system, external_id, received_at DESC)
    WHERE status = 'applied';

CREATE INDEX inbound_sales_orders_canonical
    ON inbound_sales_orders (canonical_id);

CREATE TABLE inbound_items (
    inbound_id            BIGSERIAL    PRIMARY KEY,
    source_system         VARCHAR(64)  NOT NULL REFERENCES inbound_source_systems_allowlist(source_system),
    external_id           VARCHAR(128) NOT NULL,
    external_version      VARCHAR(64)  NOT NULL,
    canonical_id          UUID         NOT NULL,
    canonical_payload     JSONB        NOT NULL,
    source_payload        JSONB,
    received_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    status                VARCHAR(16)  NOT NULL DEFAULT 'applied',
    superseded_at         TIMESTAMPTZ,
    ingested_via_token_id BIGINT       NOT NULL REFERENCES wms_tokens(token_id) ON DELETE RESTRICT,
    CHECK (status IN ('applied','superseded'))
);

CREATE UNIQUE INDEX inbound_items_idempotency
    ON inbound_items (source_system, external_id, external_version);

CREATE INDEX inbound_items_current
    ON inbound_items (source_system, external_id, received_at DESC)
    WHERE status = 'applied';

CREATE INDEX inbound_items_canonical
    ON inbound_items (canonical_id);

-- v1.7.0 customers (new canonical table; conservative NOT NULL posture
-- per plan §1.4 -- only canonical_id, created_at, updated_at,
-- latest_inbound_id NOT NULL until v2.0 has signal). DDL identical
-- to db/migrations/041_inbound_customers.sql.
CREATE TABLE customers (
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

CREATE TABLE inbound_customers (
    inbound_id            BIGSERIAL    PRIMARY KEY,
    source_system         VARCHAR(64)  NOT NULL REFERENCES inbound_source_systems_allowlist(source_system),
    external_id           VARCHAR(128) NOT NULL,
    external_version      VARCHAR(64)  NOT NULL,
    canonical_id          UUID         NOT NULL,
    canonical_payload     JSONB        NOT NULL,
    source_payload        JSONB,
    received_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    status                VARCHAR(16)  NOT NULL DEFAULT 'applied',
    superseded_at         TIMESTAMPTZ,
    ingested_via_token_id BIGINT       NOT NULL REFERENCES wms_tokens(token_id) ON DELETE RESTRICT,
    CHECK (status IN ('applied','superseded'))
);

CREATE UNIQUE INDEX inbound_customers_idempotency
    ON inbound_customers (source_system, external_id, external_version);

CREATE INDEX inbound_customers_current
    ON inbound_customers (source_system, external_id, received_at DESC)
    WHERE status = 'applied';

CREATE INDEX inbound_customers_canonical
    ON inbound_customers (canonical_id);

-- v1.7.0 vendors (new canonical table; same conservative NOT NULL
-- posture as customers). DDL identical to
-- db/migrations/042_inbound_vendors.sql.
CREATE TABLE vendors (
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

CREATE TABLE inbound_vendors (
    inbound_id            BIGSERIAL    PRIMARY KEY,
    source_system         VARCHAR(64)  NOT NULL REFERENCES inbound_source_systems_allowlist(source_system),
    external_id           VARCHAR(128) NOT NULL,
    external_version      VARCHAR(64)  NOT NULL,
    canonical_id          UUID         NOT NULL,
    canonical_payload     JSONB        NOT NULL,
    source_payload        JSONB,
    received_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    status                VARCHAR(16)  NOT NULL DEFAULT 'applied',
    superseded_at         TIMESTAMPTZ,
    ingested_via_token_id BIGINT       NOT NULL REFERENCES wms_tokens(token_id) ON DELETE RESTRICT,
    CHECK (status IN ('applied','superseded'))
);

CREATE UNIQUE INDEX inbound_vendors_idempotency
    ON inbound_vendors (source_system, external_id, external_version);

CREATE INDEX inbound_vendors_current
    ON inbound_vendors (source_system, external_id, received_at DESC)
    WHERE status = 'applied';

CREATE INDEX inbound_vendors_canonical
    ON inbound_vendors (canonical_id);

CREATE TABLE inbound_purchase_orders (
    inbound_id            BIGSERIAL    PRIMARY KEY,
    source_system         VARCHAR(64)  NOT NULL REFERENCES inbound_source_systems_allowlist(source_system),
    external_id           VARCHAR(128) NOT NULL,
    external_version      VARCHAR(64)  NOT NULL,
    canonical_id          UUID         NOT NULL,
    canonical_payload     JSONB        NOT NULL,
    source_payload        JSONB,
    received_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    status                VARCHAR(16)  NOT NULL DEFAULT 'applied',
    superseded_at         TIMESTAMPTZ,
    ingested_via_token_id BIGINT       NOT NULL REFERENCES wms_tokens(token_id) ON DELETE RESTRICT,
    CHECK (status IN ('applied','superseded'))
);

CREATE UNIQUE INDEX inbound_purchase_orders_idempotency
    ON inbound_purchase_orders (source_system, external_id, external_version);

CREATE INDEX inbound_purchase_orders_current
    ON inbound_purchase_orders (source_system, external_id, received_at DESC)
    WHERE status = 'applied';

CREATE INDEX inbound_purchase_orders_canonical
    ON inbound_purchase_orders (canonical_id);

-- v1.7.0 inbound retention beat log. One row per (resource, run);
-- the Celery beat task itself is Python code and lives outside
-- migrations. DDL identical to db/migrations/044_inbound_retention_beat.sql.
CREATE TABLE inbound_cleanup_runs (
    run_id          BIGSERIAL    PRIMARY KEY,
    resource        VARCHAR(32)  NOT NULL,
    started_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    rows_nullified  INTEGER      NOT NULL DEFAULT 0,
    retention_days  INTEGER      NOT NULL,
    status          VARCHAR(16)  NOT NULL DEFAULT 'running',
    error_message   TEXT,
    CHECK (resource IN ('sales_orders','items','customers','vendors','purchase_orders')),
    CHECK (status   IN ('running','succeeded','failed'))
);

CREATE INDEX inbound_cleanup_runs_resource_started
    ON inbound_cleanup_runs (resource, started_at DESC);

CREATE INDEX inbound_cleanup_runs_status_started
    ON inbound_cleanup_runs (status, started_at DESC)
    WHERE status = 'failed';

-- ============================================================
-- SNAPSHOT SCANS (v1.5.0 bulk-snapshot keeper coordination)
-- ============================================================
-- Per-scan metadata for GET /api/v1/snapshot/inventory. The API tier
-- INSERTs a 'pending' row; the snapshot-keeper daemon (#132) opens a
-- REPEATABLE READ transaction, exports a pg_snapshot_id via
-- pg_export_snapshot(), writes it back, and holds the transaction
-- idle until the scan completes. Keeper wake-up is NOTIFY-driven
-- (LISTEN on 'snapshot_scans_pending') with a 1s fallback poll.
--
-- The identical DDL lives in db/migrations/024_snapshot_scans.sql
-- for deployments created before v1.5.0.
-- ============================================================

CREATE TABLE snapshot_scans (
    scan_id              UUID          PRIMARY KEY,
    pg_snapshot_id       TEXT,
    snapshot_event_id    BIGINT,
    warehouse_id         INTEGER       NOT NULL,
    started_at           TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    last_accessed_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    status               VARCHAR(16)   NOT NULL DEFAULT 'pending',
    created_by_token_id  BIGINT        REFERENCES wms_tokens(token_id)
);

CREATE INDEX snapshot_scans_status_started ON snapshot_scans (status, started_at);

CREATE OR REPLACE FUNCTION notify_snapshot_scans_pending()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'pending' THEN
        PERFORM pg_notify('snapshot_scans_pending', NEW.scan_id::text);
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER tr_snapshot_scans_notify
    AFTER INSERT ON snapshot_scans
    FOR EACH ROW EXECUTE FUNCTION notify_snapshot_scans_pending();

-- ============================================================
-- INTEGRATION EVENTS (v1.5.0 transactional outbox)
-- ============================================================
-- Every inventory-changing handler writes one row here inside its own
-- transaction. External connectors poll /api/v1/events with a cursor
-- over event_id. The visible_at deferred-constraint trigger sets
-- visible_at at COMMIT time so readers see events in commit order even
-- though BIGSERIAL may have assigned event_ids out of commit order.
-- Readers filter "visible_at <= NOW() - INTERVAL '2 seconds'
-- AND event_id > cursor"; the 2-second buffer tolerates the gap
-- between a trigger firing and the COMMIT becoming visible to a
-- separate session.
--
-- The identical DDL lives in db/migrations/020_integration_events.sql
-- for deployments that were created before v1.5.0.
-- ============================================================

CREATE TABLE integration_events (
    event_id              BIGSERIAL    PRIMARY KEY,
    event_type            VARCHAR(64)  NOT NULL,
    event_version         SMALLINT     NOT NULL DEFAULT 1,
    event_timestamp       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    aggregate_type        VARCHAR(32)  NOT NULL,
    aggregate_id          BIGINT       NOT NULL,
    aggregate_external_id UUID         NOT NULL,
    warehouse_id          INT          NOT NULL REFERENCES warehouses(warehouse_id),
    source_txn_id         UUID         NOT NULL,
    visible_at            TIMESTAMPTZ,
    payload               JSONB        NOT NULL,
    CONSTRAINT integration_events_idempotency_key
        UNIQUE (aggregate_type, aggregate_id, event_type, source_txn_id)
);

CREATE INDEX ix_integration_events_warehouse_event
    ON integration_events (warehouse_id, event_id);
CREATE INDEX ix_integration_events_type_event
    ON integration_events (event_type, event_id);
CREATE INDEX ix_integration_events_visible_at
    ON integration_events (visible_at)
    WHERE visible_at IS NOT NULL;

CREATE OR REPLACE FUNCTION set_integration_event_visible_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    UPDATE integration_events
       SET visible_at = clock_timestamp()
     WHERE event_id = NEW.event_id;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER tr_integration_events_visible_at
    AFTER INSERT ON integration_events
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION set_integration_event_visible_at();

-- v1.6.0 #164 / migration 031: visibility NOTIFY trigger. Webhook
-- dispatcher LISTENs on 'integration_events_visible'; the deferred
-- visible_at trigger above UPDATEs visible_at at COMMIT, this
-- AFTER-UPDATE trigger then pg_notify's the new event_id. The
-- function gates on NULL -> NOT NULL so an idempotent re-stamp
-- does not emit a duplicate NOTIFY. Correctness lives on the
-- per-subscription cursor; NOTIFY is latency reduction only.
-- The identical DDL lives in db/migrations/031_integration_events_notify.sql.
CREATE OR REPLACE FUNCTION notify_integration_event_visible()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.visible_at IS NOT NULL AND OLD.visible_at IS NULL THEN
        PERFORM pg_notify('integration_events_visible', NEW.event_id::text);
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER tr_integration_events_notify
    AFTER UPDATE OF visible_at ON integration_events
    FOR EACH ROW
    EXECUTE FUNCTION notify_integration_event_visible();

-- ============================================================
-- WEBHOOK SUBSCRIPTIONS + SECRETS (v1.6.0 outbound push)
-- ============================================================
-- Subscription state for the v1.6.0 webhook dispatcher.
-- subscription_id is UUID for opaque admin URLs; status is one
-- of 'active' | 'paused' | 'revoked' (auto-pause writes 'paused'
-- with pause_reason populated; soft delete writes 'revoked').
-- Rate-limit and ceiling CHECK ranges are the bottom rung that
-- catches bypass paths around the admin layer (which separately
-- enforces upper bounds against env-var hard caps).
--
-- delivery_url CHECK is permissive ('^https?://') so dev/CI can
-- use http; production HTTPS-only enforcement lives in the admin
-- endpoint behind SENTRY_ALLOW_HTTP_WEBHOOKS opt-out.
--
-- webhook_secrets stores Fernet-encrypted HMAC signing material
-- in two slots: generation=1 primary, generation=2 previous.
-- The dispatcher signs with generation=1 only; consumers accept
-- either until expires_at (24h dual-accept rotation window).
-- ON DELETE CASCADE on subscription_id keeps secret rows from
-- outliving their subscription.
--
-- The identical DDL lives in db/migrations/029_webhook_subscriptions_and_secrets.sql.
CREATE TABLE webhook_subscriptions (
    subscription_id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    connector_id             VARCHAR(64)  NOT NULL REFERENCES connectors(connector_id),
    display_name             VARCHAR(128) NOT NULL,
    delivery_url             TEXT         NOT NULL,
    subscription_filter      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    last_delivered_event_id  BIGINT       NOT NULL DEFAULT 0,
    status                   VARCHAR(16)  NOT NULL DEFAULT 'active',
    pause_reason             VARCHAR(32),
    rate_limit_per_second    INTEGER      NOT NULL DEFAULT 50,
    pending_ceiling          INTEGER      NOT NULL DEFAULT 10000,
    dlq_ceiling              INTEGER      NOT NULL DEFAULT 1000,
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT webhook_subscriptions_delivery_url_scheme
        CHECK (delivery_url ~ '^https?://'),
    CONSTRAINT webhook_subscriptions_rate_limit_range
        CHECK (rate_limit_per_second BETWEEN 1 AND 100),
    CONSTRAINT webhook_subscriptions_pending_ceiling_range
        CHECK (pending_ceiling BETWEEN 100 AND 100000),
    CONSTRAINT webhook_subscriptions_dlq_ceiling_range
        CHECK (dlq_ceiling BETWEEN 10 AND 10000),
    -- #236: bottom-rung enforcement on status + pause_reason.
    -- Migration 029 left validation to the application layer
    -- ("Status validation is application side"); migration 036
    -- adds the column-level CHECKs so a privileged-role error
    -- or malicious migration cannot write an out-of-band value.
    -- The malformed_filter value lands here because the
    -- dispatcher's V-314 auto-pause path writes it.
    CONSTRAINT webhook_subscriptions_status_enum
        CHECK (status IN ('active', 'paused', 'revoked')),
    CONSTRAINT webhook_subscriptions_pause_reason_enum
        CHECK (
            pause_reason IS NULL
            OR pause_reason IN (
                'manual',
                'pending_ceiling',
                'dlq_ceiling',
                'malformed_filter'
            )
        )
);

CREATE INDEX webhook_subscriptions_status
    ON webhook_subscriptions (status)
    WHERE status = 'active';

CREATE TABLE webhook_secrets (
    subscription_id     UUID         NOT NULL REFERENCES webhook_subscriptions(subscription_id) ON DELETE CASCADE,
    generation          SMALLINT     NOT NULL,
    secret_ciphertext   BYTEA        NOT NULL,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at          TIMESTAMPTZ,
    PRIMARY KEY (subscription_id, generation),
    CONSTRAINT webhook_secrets_generation_range
        CHECK (generation IN (1, 2))
);

-- ============================================================
-- WEBHOOK DELIVERIES (v1.6.0 outbound push, per-attempt log)
-- ============================================================
-- Append-only with one exception: the terminal `dlq` transition
-- flips the row that was last `in_flight` rather than inserting
-- a fresh row, so an event that retried N times before
-- terminating leaves N+1 rows. Cursor advances strictly on
-- terminal state (`succeeded` or `dlq`), never on in-progress.
--
-- subscription_id FK is ON DELETE RESTRICT (not CASCADE) so
-- delivery history outlives soft-delete and is retained for
-- forensics; hard delete requires `?purge=true` plus no
-- pending/in_flight rows. event_id has no FK because
-- integration_events partitions in v2.1 and logical integrity
-- is sufficient given the cursor-based contract.
--
-- attempt_number BETWEEN 1 AND 8 matches the hard-coded retry
-- schedule [1s, 4s, 15s, 60s, 5m, 30m, 2h, 12h]. status enum
-- is CHECKed; error_kind is not (enum grows with consumer
-- feedback in v1.6.x).
--
-- Four indexes are each pinned to a specific dispatcher or
-- admin query path; storing response_body_hash (sha256 hex)
-- instead of full bodies keeps the table small under fan-out.
--
-- The identical DDL lives in db/migrations/030_webhook_deliveries.sql.
CREATE TABLE webhook_deliveries (
    delivery_id          BIGSERIAL    PRIMARY KEY,
    subscription_id      UUID         NOT NULL REFERENCES webhook_subscriptions(subscription_id) ON DELETE RESTRICT,
    event_id             BIGINT       NOT NULL,
    attempt_number       SMALLINT     NOT NULL,
    status               VARCHAR(16)  NOT NULL,
    scheduled_at         TIMESTAMPTZ  NOT NULL,
    attempted_at         TIMESTAMPTZ,
    completed_at         TIMESTAMPTZ,
    http_status          SMALLINT,
    response_body_hash   CHAR(64),
    response_time_ms     INTEGER,
    error_kind           VARCHAR(32),
    error_detail         VARCHAR(512),
    secret_generation    SMALLINT     NOT NULL,
    CONSTRAINT webhook_deliveries_attempt_number_range
        CHECK (attempt_number BETWEEN 1 AND 8),
    CONSTRAINT webhook_deliveries_status_enum
        CHECK (status IN ('pending', 'in_flight', 'succeeded', 'failed', 'dlq'))
);

CREATE INDEX webhook_deliveries_dispatch
    ON webhook_deliveries (subscription_id, scheduled_at)
    WHERE status = 'pending';

CREATE INDEX webhook_deliveries_latest
    ON webhook_deliveries (subscription_id, event_id, delivery_id DESC);

CREATE INDEX webhook_deliveries_dlq
    ON webhook_deliveries (subscription_id, completed_at)
    WHERE status = 'dlq';

CREATE INDEX webhook_deliveries_pending_count
    ON webhook_deliveries (subscription_id)
    WHERE status IN ('pending', 'in_flight');

-- ============================================================
-- WEBHOOK AUDIT TRIGGERS (v1.6.0 forensic instrumentation;
-- mirrors V-157 wms_tokens_audit shape)
-- ============================================================
-- DELETE / TRUNCATE forensic shadows for webhook_subscriptions
-- and webhook_secrets, landing at the same time as the source
-- tables (not after an incident, as wms_tokens_audit did).
--
-- Triggers are statement-level so a wipe-the-world DELETE
-- produces exactly one audit row. The transition-tables feature
-- (REFERENCING OLD TABLE AS deleted_rows) lets the DELETE
-- trigger COUNT affected rows. TRUNCATE does not expose a
-- transition table, so rows_affected is NULL on TRUNCATE events.
-- Each row captures session_user / current_user / backend_pid /
-- application_name / clock_timestamp so an incident is bindable
-- to a specific role + backend.
--
-- The two source tables get separate audit tables (not one
-- shared table) so a future column addition on either does not
-- couple the schemas.
--
-- The identical DDL lives in db/migrations/032_webhook_audit_triggers.sql.

CREATE TABLE webhook_subscriptions_audit (
    audit_id          BIGSERIAL    PRIMARY KEY,
    event_type        VARCHAR(16)  NOT NULL,
    rows_affected     INTEGER,
    sess_user         TEXT         NOT NULL,
    curr_user         TEXT         NOT NULL,
    backend_pid       INTEGER      NOT NULL,
    application_name  TEXT,
    event_at          TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX webhook_subscriptions_audit_event_at
    ON webhook_subscriptions_audit (event_at DESC);

CREATE OR REPLACE FUNCTION webhook_subscriptions_audit_delete()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    _count INTEGER;
BEGIN
    SELECT COUNT(*) INTO _count FROM deleted_rows;
    INSERT INTO webhook_subscriptions_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'DELETE', _count, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

CREATE TRIGGER tr_webhook_subscriptions_audit_delete
    AFTER DELETE ON webhook_subscriptions
    REFERENCING OLD TABLE AS deleted_rows
    FOR EACH STATEMENT EXECUTE FUNCTION webhook_subscriptions_audit_delete();

CREATE OR REPLACE FUNCTION webhook_subscriptions_audit_truncate()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO webhook_subscriptions_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'TRUNCATE', NULL, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

CREATE TRIGGER tr_webhook_subscriptions_audit_truncate
    AFTER TRUNCATE ON webhook_subscriptions
    FOR EACH STATEMENT EXECUTE FUNCTION webhook_subscriptions_audit_truncate();

CREATE TABLE webhook_secrets_audit (
    audit_id          BIGSERIAL    PRIMARY KEY,
    event_type        VARCHAR(16)  NOT NULL,
    rows_affected     INTEGER,
    sess_user         TEXT         NOT NULL,
    curr_user         TEXT         NOT NULL,
    backend_pid       INTEGER      NOT NULL,
    application_name  TEXT,
    event_at          TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX webhook_secrets_audit_event_at
    ON webhook_secrets_audit (event_at DESC);

CREATE OR REPLACE FUNCTION webhook_secrets_audit_delete()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    _count INTEGER;
BEGIN
    SELECT COUNT(*) INTO _count FROM deleted_rows;
    INSERT INTO webhook_secrets_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'DELETE', _count, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

CREATE TRIGGER tr_webhook_secrets_audit_delete
    AFTER DELETE ON webhook_secrets
    REFERENCING OLD TABLE AS deleted_rows
    FOR EACH STATEMENT EXECUTE FUNCTION webhook_secrets_audit_delete();

CREATE OR REPLACE FUNCTION webhook_secrets_audit_truncate()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO webhook_secrets_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'TRUNCATE', NULL, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

CREATE TRIGGER tr_webhook_secrets_audit_truncate
    AFTER TRUNCATE ON webhook_secrets
    FOR EACH STATEMENT EXECUTE FUNCTION webhook_secrets_audit_truncate();

-- ============================================================
-- WEBHOOK DELIVERIES audit (#235; v1.6.1)
-- ============================================================
-- Statement-level DELETE / TRUNCATE forensic instrumentation for
-- webhook_deliveries, mirroring the migration 032 shape on
-- webhook_subscriptions / webhook_secrets. cleanup_webhook_deliveries
-- (#228 chunked beat task) and the cascade in the hard-delete
-- admin path both DELETE rows here; without these triggers a
-- compromised cleanup-task role could mass-DELETE the per-attempt
-- history with no forensic surface. The DDL lives in
-- db/migrations/035_webhook_deliveries_audit.sql.
CREATE TABLE webhook_deliveries_audit (
    audit_id          BIGSERIAL    PRIMARY KEY,
    event_type        VARCHAR(16)  NOT NULL,
    rows_affected     INTEGER,
    sess_user         TEXT         NOT NULL,
    curr_user         TEXT         NOT NULL,
    backend_pid       INTEGER      NOT NULL,
    application_name  TEXT,
    event_at          TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX webhook_deliveries_audit_event_at
    ON webhook_deliveries_audit (event_at DESC);

CREATE OR REPLACE FUNCTION webhook_deliveries_audit_delete()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    _count INTEGER;
BEGIN
    SELECT COUNT(*) INTO _count FROM deleted_rows;
    INSERT INTO webhook_deliveries_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'DELETE', _count, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

CREATE TRIGGER tr_webhook_deliveries_audit_delete
    AFTER DELETE ON webhook_deliveries
    REFERENCING OLD TABLE AS deleted_rows
    FOR EACH STATEMENT EXECUTE FUNCTION webhook_deliveries_audit_delete();

CREATE OR REPLACE FUNCTION webhook_deliveries_audit_truncate()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO webhook_deliveries_audit (
        event_type, rows_affected, sess_user, curr_user,
        backend_pid, application_name
    ) VALUES (
        'TRUNCATE', NULL, SESSION_USER, CURRENT_USER,
        pg_backend_pid(), current_setting('application_name', true)
    );
    RETURN NULL;
END;
$$;

CREATE TRIGGER tr_webhook_deliveries_audit_truncate
    AFTER TRUNCATE ON webhook_deliveries
    FOR EACH STATEMENT EXECUTE FUNCTION webhook_deliveries_audit_truncate();

-- ============================================================
-- WEBHOOK SUBSCRIPTIONS TOMBSTONES (v1.6.0 URL-reuse gate)
-- ============================================================
-- Per-deletion history that backs the URL-reuse acknowledgement
-- gate on the admin webhook create endpoint. Hard-delete writes
-- a tombstone; a subsequent CREATE under the same delivery_url
-- with an unacknowledged tombstone is refused 409
-- url_reuse_unacknowledged unless the request body carries
-- acknowledge_url_reuse: true (which stamps acknowledged_at +
-- acknowledged_by on every matching tombstone).
--
-- Tombstones are forensic history and are never deleted. The
-- partial index covers only unacknowledged tombstones so the
-- URL-reuse query stays fast as the table accumulates
-- acknowledged rows.
--
-- subscription_id has no FK because the source row no longer
-- exists by the time the tombstone is written. deleted_by is
-- NOT NULL because the admin endpoint always runs under cookie
-- auth; acknowledged_by is nullable until the gate is cleared.
--
-- The identical DDL lives in db/migrations/033_webhook_subscriptions_tombstones.sql
-- plus db/migrations/034_webhook_tombstones_canonical.sql (#218: the gate
-- compares delivery_url_canonical so a case / port / fragment / trailing-
-- slash mutation cannot bypass URL-reuse acknowledgement).
CREATE TABLE webhook_subscriptions_tombstones (
    tombstone_id            BIGSERIAL    PRIMARY KEY,
    subscription_id         UUID         NOT NULL,
    delivery_url_at_delete  TEXT         NOT NULL,
    delivery_url_canonical  TEXT         NOT NULL,
    connector_id            VARCHAR(64)  NOT NULL,
    deleted_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    deleted_by              INTEGER      NOT NULL REFERENCES users(user_id),
    acknowledged_at         TIMESTAMPTZ,
    acknowledged_by         INTEGER      REFERENCES users(user_id)
);

CREATE INDEX webhook_subscriptions_tombstones_canonical_unack
    ON webhook_subscriptions_tombstones (delivery_url_canonical)
    WHERE acknowledged_at IS NULL;

-- ============================================================
-- TRANSFER ORDERS (v1.8.0 #281)
-- ============================================================
-- Warehouse-to-warehouse inventory transfers. Identical DDL lives
-- in db/migrations/049_transfer_orders.sql. pick_tasks gains a
-- to_id / to_line_id discriminator with an XOR CHECK so the
-- existing picking module dispatches on whether a row points at
-- a SO line or a TO line.

CREATE TABLE transfer_orders (
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

CREATE INDEX ix_transfer_orders_source ON transfer_orders(source_warehouse_id, status);
CREATE INDEX ix_transfer_orders_dest   ON transfer_orders(destination_warehouse_id, status);
CREATE INDEX ix_transfer_orders_status ON transfer_orders(status, created_at);

CREATE TABLE transfer_order_lines (
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

CREATE INDEX ix_transfer_order_lines_to   ON transfer_order_lines(to_id);
CREATE INDEX ix_transfer_order_lines_item ON transfer_order_lines(item_id);

CREATE TABLE transfer_order_approvals (
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

CREATE INDEX ix_to_approvals_to ON transfer_order_approvals(to_id);
CREATE INDEX ix_to_approvals_pending ON transfer_order_approvals(status, submitted_at)
    WHERE status = 'PENDING';

ALTER TABLE pick_tasks
    ALTER COLUMN so_id DROP NOT NULL,
    ALTER COLUMN so_line_id DROP NOT NULL;

ALTER TABLE pick_tasks
    ADD COLUMN to_id      INT REFERENCES transfer_orders(to_id),
    ADD COLUMN to_line_id INT REFERENCES transfer_order_lines(to_line_id);

CREATE INDEX ix_pick_tasks_to      ON pick_tasks(to_id);
CREATE INDEX ix_pick_tasks_to_line ON pick_tasks(to_line_id);

ALTER TABLE pick_tasks
    ADD CONSTRAINT pick_tasks_target_xor
    CHECK ((so_id IS NOT NULL AND to_id IS NULL)
        OR (so_id IS NULL     AND to_id IS NOT NULL));

INSERT INTO app_settings (key, value)
VALUES ('transfer_order_block_self_approval', 'true')
ON CONFLICT (key) DO NOTHING;

-- ============================================================
-- USER DASHBOARD PREFERENCES (v1.8.0 #283)
-- ============================================================
-- Per-user override storage for the productivity dashboard. Defaults
-- are applied at read time when no row exists; this table is override
-- storage, not a settings record. Identical DDL lives in
-- db/migrations/051_user_dashboard_preferences.sql.

CREATE TABLE user_dashboard_preferences (
    user_id          INT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    chart_order      JSONB NOT NULL DEFAULT '["picking","packing","shipped","received_skus","putaway_skus"]'::jsonb,
    default_range    VARCHAR(16) NOT NULL DEFAULT 'today'
                       CHECK (default_range IN ('today','yesterday','last_7d','last_30d','custom')),
    default_view     VARCHAR(8) NOT NULL DEFAULT 'charts'
                       CHECK (default_view IN ('charts','table')),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
