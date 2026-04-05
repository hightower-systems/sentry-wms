-- ============================================================
-- SENTRY WMS - Apartment Test Lab Seed Data
-- ============================================================
-- Sets up a small warehouse environment for development testing
-- Based on the apartment-test-lab setup guide
-- ============================================================

-- Warehouse
INSERT INTO warehouses (warehouse_code, warehouse_name, address)
VALUES ('APT-LAB', 'Apartment Test Lab', '123 Dev Street, Denver, CO');

-- Zones
INSERT INTO zones (warehouse_id, zone_code, zone_name, zone_type) VALUES
(1, 'RCV', 'Receiving Area', 'RECEIVING'),
(1, 'STOR', 'Storage Shelves', 'STORAGE'),
(1, 'PICK', 'Pick Zone', 'PICKING'),
(1, 'STAGE', 'Staging Table', 'STAGING'),
(1, 'SHIP', 'Shipping Desk', 'SHIPPING');

-- Bins (small apartment layout - 2 shelving units, 3 shelves each)
INSERT INTO bins (zone_id, warehouse_id, bin_code, bin_barcode, bin_type, aisle, row_num, level_num, pick_sequence, putaway_sequence) VALUES
-- Receiving staging
(1, 1, 'RCV-01', 'BIN-RCV-01', 'INBOUND_STAGING', NULL, NULL, NULL, 0, 0),
-- Storage shelves - Unit A
(2, 1, 'A-01-01', 'BIN-A-01-01', 'STANDARD', 'A', '01', '01', 100, 100),
(2, 1, 'A-01-02', 'BIN-A-01-02', 'STANDARD', 'A', '01', '02', 200, 200),
(2, 1, 'A-01-03', 'BIN-A-01-03', 'STANDARD', 'A', '01', '03', 300, 300),
-- Storage shelves - Unit B
(2, 1, 'B-01-01', 'BIN-B-01-01', 'STANDARD', 'B', '01', '01', 400, 400),
(2, 1, 'B-01-02', 'BIN-B-01-02', 'STANDARD', 'B', '01', '02', 500, 500),
(2, 1, 'B-01-03', 'BIN-B-01-03', 'STANDARD', 'B', '01', '03', 600, 600),
-- Outbound staging
(4, 1, 'STG-01', 'BIN-STG-01', 'OUTBOUND_STAGING', NULL, NULL, NULL, 900, 0),
-- Shipping
(5, 1, 'SHIP-01', 'BIN-SHIP-01', 'OUTBOUND_STAGING', NULL, NULL, NULL, 999, 0);

-- Sample Items (10 test products)
INSERT INTO items (sku, item_name, description, upc, category, weight_lbs, default_bin_id) VALUES
('WIDGET-BLU', 'Blue Widget', 'Standard blue widget', '100000000001', 'Widgets', 0.5, 2),
('WIDGET-RED', 'Red Widget', 'Standard red widget', '100000000002', 'Widgets', 0.5, 3),
('WIDGET-GRN', 'Green Widget', 'Standard green widget', '100000000003', 'Widgets', 0.5, 4),
('GADGET-SM', 'Small Gadget', 'Compact gadget', '100000000004', 'Gadgets', 1.2, 5),
('GADGET-LG', 'Large Gadget', 'Full-size gadget', '100000000005', 'Gadgets', 2.8, 6),
('CABLE-USB', 'USB-C Cable 6ft', 'USB-C charging cable', '100000000006', 'Cables', 0.2, 7),
('CABLE-HDMI', 'HDMI Cable 3ft', 'HDMI 2.1 cable', '100000000007', 'Cables', 0.3, 2),
('CASE-PHN', 'Phone Case - Black', 'Universal phone case', '100000000008', 'Cases', 0.1, 3),
('SCREEN-PRO', 'Screen Protector', 'Tempered glass screen protector', '100000000009', 'Accessories', 0.05, 4),
('CHARGER-W', 'Wireless Charger', '15W wireless charging pad', '100000000010', 'Chargers', 0.4, 5);

-- Initial Inventory
INSERT INTO inventory (item_id, bin_id, warehouse_id, quantity_on_hand) VALUES
(1, 2, 1, 25),
(2, 3, 1, 30),
(3, 4, 1, 20),
(4, 5, 1, 15),
(5, 6, 1, 10),
(6, 7, 1, 50),
(7, 2, 1, 40),
(8, 3, 1, 35),
(9, 4, 1, 100),
(10, 5, 1, 18);

-- Default admin user (password: 'admin' - change in production)
-- Password hash is bcrypt of 'admin'
INSERT INTO users (username, password_hash, full_name, role, warehouse_id, allowed_functions)
VALUES ('admin', '$2b$12$zDGRKFLmc6v/A4mVhxOzb.7uoW1ulnXn0AisK5uJ5iWk33vC2EpSK', 'Admin User', 'ADMIN', 1, '{}');

-- App settings defaults
INSERT INTO app_settings (key, value) VALUES ('session_timeout_hours', '8');

-- Sample Purchase Order
INSERT INTO purchase_orders (po_number, po_barcode, vendor_name, status, expected_date, warehouse_id, created_by) VALUES
('PO-001', 'PO-001', 'Test Vendor Inc', 'OPEN', CURRENT_DATE + INTERVAL '3 days', 1, 'admin');

INSERT INTO purchase_order_lines (po_id, item_id, quantity_ordered, line_number) VALUES
(1, 1, 50, 1),
(1, 4, 20, 2),
(1, 6, 100, 3);

-- Sample Sales Orders
INSERT INTO sales_orders (so_number, so_barcode, customer_name, status, warehouse_id, ship_method, order_date, created_by) VALUES
('SO-001', 'SO-001', 'Test Customer A', 'OPEN', 1, 'GROUND', NOW(), 'admin'),
('SO-002', 'SO-002', 'Test Customer B', 'OPEN', 1, 'EXPRESS', NOW(), 'admin');

INSERT INTO sales_order_lines (so_id, item_id, quantity_ordered, line_number) VALUES
(1, 1, 2, 1),
(1, 6, 1, 2),
(2, 3, 3, 1),
(2, 10, 1, 2);
