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
    external_id UUID UNIQUE NOT NULL
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
    external_id UUID UNIQUE NOT NULL
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
    order_date TIMESTAMPTZ,
    ship_by_date DATE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    picked_at TIMESTAMPTZ,
    packed_at TIMESTAMPTZ,
    shipped_at TIMESTAMPTZ,
    carrier VARCHAR(100),
    tracking_number VARCHAR(255),
    created_by VARCHAR(100),
    external_id UUID UNIQUE NOT NULL
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

CREATE TABLE audit_log (
    log_id BIGSERIAL PRIMARY KEY,
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

-- V-025 tamper resistance: hash-chain trigger + append-only guards.
-- The identical DDL lives in db/migrations/016_audit_log_tamper_resistance.sql
-- for deployments that were created before V-025 shipped.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE OR REPLACE FUNCTION audit_log_chain_hash() RETURNS TRIGGER AS $$
DECLARE
    prev BYTEA;
    payload TEXT;
BEGIN
    SELECT row_hash INTO prev FROM audit_log ORDER BY log_id DESC LIMIT 1;
    NEW.prev_hash := COALESCE(prev, '\x00'::bytea);
    payload := COALESCE(NEW.action_type, '') || '|' ||
               COALESCE(NEW.entity_type, '') || '|' ||
               COALESCE(NEW.entity_id::text, '') || '|' ||
               COALESCE(NEW.user_id, '') || '|' ||
               COALESCE(NEW.warehouse_id::text, '') || '|' ||
               COALESCE(NEW.details::text, '') || '|' ||
               COALESCE(NEW.created_at::text, NOW()::text);
    NEW.row_hash := digest(NEW.prev_hash || payload::bytea, 'sha256');
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

-- ============================================================
-- WMS TOKENS (v1.5.0 inbound API tokens for X-WMS-Token auth)
-- ============================================================
-- Hash-only storage per Decision P. token_hash is
-- SHA256(SENTRY_TOKEN_PEPPER || plaintext).hexdigest() per Decision Q.
-- Scope columns are typed arrays per Decision S. Default expiry is
-- one year per Decision R.
--
-- The identical DDL lives in db/migrations/023_wms_tokens.sql for
-- deployments created before v1.5.0.
-- ============================================================

CREATE TABLE wms_tokens (
    token_id       BIGSERIAL     PRIMARY KEY,
    token_name     VARCHAR(128)  NOT NULL,
    token_hash     CHAR(64)      UNIQUE NOT NULL,
    warehouse_ids  BIGINT[]      NOT NULL DEFAULT '{}',
    event_types    TEXT[]        NOT NULL DEFAULT '{}',
    endpoints      TEXT[]        NOT NULL DEFAULT '{}',
    connector_id   VARCHAR(64)   REFERENCES connectors(connector_id),
    status         VARCHAR(16)   NOT NULL DEFAULT 'active',
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    rotated_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    expires_at     TIMESTAMPTZ   NOT NULL DEFAULT (NOW() + INTERVAL '1 year'),
    revoked_at     TIMESTAMPTZ,
    last_used_at   TIMESTAMPTZ
);

CREATE INDEX wms_tokens_status_rotated ON wms_tokens (status, rotated_at);

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
