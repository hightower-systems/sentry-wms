-- ============================================================
-- SENTRY WMS - Apartment Test Lab Seed Data
-- ============================================================
-- Matches the 61 printed Zebra barcode labels exactly.
-- Cross-referenced against HANDOFF-SESSION5-CLEAN-SLATE.md
-- ============================================================

-- ============================================================
-- WAREHOUSES (2)
-- ============================================================
INSERT INTO warehouses (warehouse_code, warehouse_name, address) VALUES
('APT-LAB', 'Apartment Test Lab', '123 Dev Street, Denver, CO'),
('VIRTUAL', 'Virtual Warehouse', 'N/A');

-- ============================================================
-- ZONES (6 zones in APT-LAB, warehouse_id=1)
-- ============================================================
INSERT INTO zones (warehouse_id, zone_code, zone_name, zone_type) VALUES
(1, 'RCV',   'Receiving Area',  'RECEIVING'),
(1, 'PICK',  'Pick Zone',       'PICKING'),
(1, 'BULK',  'Bulk Storage',    'STORAGE'),
(1, 'STAGE', 'Staging Area',    'STAGING'),
(1, 'SHIP',  'Shipping Desk',   'SHIPPING'),
(1, 'QC',    'Quality Control', 'STORAGE');

-- ============================================================
-- BINS (16 total)
-- zone_id mapping: 1=RCV, 2=PICK, 3=BULK, 4=STAGE, 5=SHIP, 6=QC
-- ============================================================
INSERT INTO bins (zone_id, warehouse_id, bin_code, bin_barcode, bin_type, aisle, row_num, level_num, pick_sequence, putaway_sequence, description) VALUES
-- Receiving (zone 1)
(1, 1, 'RECV-01',  'RECV-01',  'Staging', NULL, NULL, NULL, 0,   0,   'Front Door Left'),
(1, 1, 'RECV-02',  'RECV-02',  'Staging', NULL, NULL, NULL, 0,   0,   'Front Door Right'),
-- Picking shelves (zone 2) - Shelf Unit A
(2, 1, 'A-01-01',  'A-01-01',  'Pickable', 'A', '01', '01', 100, 100, 'Shelf 1, Left'),
(2, 1, 'A-01-02',  'A-01-02',  'Pickable', 'A', '01', '02', 200, 200, 'Shelf 1, Center'),
(2, 1, 'A-01-03',  'A-01-03',  'Pickable', 'A', '01', '03', 300, 300, 'Shelf 1, Right'),
(2, 1, 'A-02-01',  'A-02-01',  'Pickable', 'A', '02', '01', 400, 400, 'Shelf 2, Right'),
(2, 1, 'A-02-02',  'A-02-02',  'Pickable', 'A', '02', '02', 500, 500, 'Shelf 2, Center'),
(2, 1, 'A-02-03',  'A-02-03',  'Pickable', 'A', '02', '03', 600, 600, 'Shelf 2, Left'),
-- Picking shelves (zone 2) - Shelf Unit B
(2, 1, 'B-01-01',  'B-01-01',  'Pickable', 'B', '01', '01', 700, 700, 'Shelf 3, Left'),
(2, 1, 'B-01-02',  'B-01-02',  'Pickable', 'B', '01', '02', 800, 800, 'Shelf 3, Center'),
(2, 1, 'B-01-03',  'B-01-03',  'Pickable', 'B', '01', '03', 900, 900, 'Shelf 3, Right'),
-- Bulk storage (zone 3)
(3, 1, 'BULK-01',  'BULK-01',  'Pickable', NULL, NULL, NULL, 0, 0, 'Closet / Floor'),
(3, 1, 'BULK-02',  'BULK-02',  'Pickable', NULL, NULL, NULL, 0, 0, 'Closet / Floor'),
-- Staging (zone 4)
(4, 1, 'SHIP-01',  'SHIP-01',  'Pickable', NULL, NULL, NULL, 0, 0, 'Desk, Left'),
(4, 1, 'SHIP-02',  'SHIP-02',  'Pickable', NULL, NULL, NULL, 0, 0, 'Desk, Right'),
-- QC (zone 6)
(6, 1, 'QC-01',    'QC-01',    'Staging', NULL, NULL, NULL, 0, 0, 'Small Box on Desk');

-- ============================================================
-- ITEMS (20 fly fishing products)
-- default_bin_id references: RECV-01=1, RECV-02=2, A-01-01=3, A-01-02=4,
--   A-01-03=5, A-02-01=6, A-02-02=7, A-02-03=8, B-01-01=9, B-01-02=10,
--   B-01-03=11, BULK-01=12, BULK-02=13, SHIP-01=14, SHIP-02=15, QC-01=16
-- ============================================================
INSERT INTO items (sku, item_name, description, upc, category, weight_lbs, default_bin_id) VALUES
('TST-001', 'Elk Hair Caddis (Sz 14)',       'Classic dry fly pattern',              '100000000001', 'Flies',       0.01, 3),
('TST-002', 'Woolly Bugger Black (Sz 8)',     'Versatile streamer pattern',           '100000000002', 'Flies',       0.01, 4),
('TST-003', 'Adams Dry Fly (Sz 16)',          'All-purpose dry fly',                  '100000000003', 'Flies',       0.01, 5),
('TST-004', 'Pheasant Tail Nymph (Sz 12)',    'Classic nymph pattern',                '100000000004', 'Flies',       0.01, 6),
('TST-005', 'Weight Forward Fly Line 5wt',    'Premium weight-forward fly line',      '100000000005', 'Lines',       0.25, 7),
('TST-006', 'Felt Sole Wading Boot',         'Premium felt-sole wading boot',        '100000000006', 'Footwear',    2.50, 8),
('TST-007', 'Tapered Leader 9ft 5X',         'Knotless tapered leader',              '100000000007', 'Terminal',    0.02, 9),
('TST-008', '9ft 5wt Fly Rod',               '5-weight 4-piece fly rod',             '100000000008', 'Rods',        3.00, 10),
('TST-009', 'Large Slim Fly Box',            'Waterproof fly storage box',           '100000000009', 'Accessories', 0.30, 11),
('TST-010', 'UV Wader Repair Kit',           'UV-cure wader patch kit',              '100000000010', 'Repair',      0.15, 11),
('TST-011', '4pc Trout Rod 9ft',             '4-weight 4-piece rod',                 '100000000011', 'Rods',        2.80, 3),
('TST-012', 'Premium Fly Line WF5F',         'Weight-forward floating line',         '100000000012', 'Lines',       0.25, 4),
('TST-013', 'Stockingfoot Chest Waders',     'Breathable stockingfoot waders',       '100000000013', 'Waders',      3.50, 5),
('TST-014', 'Large Arbor Fly Reel',          'Large arbor fly reel',                 '100000000014', 'Reels',       0.45, 6),
('TST-015', 'Waterproof Sling Pack',         'Waterproof sling pack',                '100000000015', 'Packs',       1.20, 7),
('TST-016', 'Hare''s Ear Nymph (Sz 14)',     'Classic bead-head nymph',              '100000000016', 'Flies',       0.01, 8),
('TST-017', 'Copper John (Sz 16)',           'Weighted nymph pattern',               '100000000017', 'Flies',       0.01, 9),
('TST-018', 'Parachute Adams (Sz 18)',       'High-visibility dry fly',              '100000000018', 'Flies',       0.01, 10),
('TST-019', 'Stimulator Orange (Sz 10)',     'Attractor dry fly pattern',            '100000000019', 'Flies',       0.01, 11),
('TST-020', 'San Juan Worm Red (Sz 12)',     'Simple worm pattern',                  '100000000020', 'Flies',       0.01, 12);

-- ============================================================
-- INITIAL INVENTORY (all items stocked in their default bins)
-- ============================================================
INSERT INTO inventory (item_id, bin_id, warehouse_id, quantity_on_hand) VALUES
(1,  3,  1, 50),   -- TST-001 in A-01-01
(2,  4,  1, 50),   -- TST-002 in A-01-02
(3,  5,  1, 50),   -- TST-003 in A-01-03
(4,  6,  1, 50),   -- TST-004 in A-02-01
(5,  7,  1, 25),   -- TST-005 in A-02-02
(6,  8,  1, 10),   -- TST-006 in A-02-03
(7,  9,  1, 100),  -- TST-007 in B-01-01
(8,  10, 1, 15),   -- TST-008 in B-01-02
(9,  11, 1, 20),   -- TST-009 in B-01-03
(10, 11, 1, 30),   -- TST-010 in B-01-03 (shared bin)
(11, 3,  1, 12),   -- TST-011 in A-01-01 (shared bin)
(12, 4,  1, 20),   -- TST-012 in A-01-02 (shared bin)
(13, 5,  1, 8),    -- TST-013 in A-01-03 (shared bin)
(14, 6,  1, 10),   -- TST-014 in A-02-01 (shared bin)
(15, 7,  1, 15),   -- TST-015 in A-02-02 (shared bin)
(16, 8,  1, 40),   -- TST-016 in A-02-03 (shared bin)
(17, 9,  1, 60),   -- TST-017 in B-01-01 (shared bin)
(18, 10, 1, 45),   -- TST-018 in B-01-02 (shared bin)
(19, 11, 1, 35),   -- TST-019 in B-01-03 (shared bin)
(20, 12, 1, 200);  -- TST-020 in BULK-01

-- ============================================================
-- USERS
-- ============================================================
-- Password hash is bcrypt of 'admin'
INSERT INTO users (username, password_hash, full_name, role, warehouse_id, allowed_functions)
VALUES ('admin', '$2b$12$zDGRKFLmc6v/A4mVhxOzb.7uoW1ulnXn0AisK5uJ5iWk33vC2EpSK', 'Admin User', 'ADMIN', 1, '{}');

-- ============================================================
-- APP SETTINGS
-- ============================================================
INSERT INTO app_settings (key, value) VALUES ('session_timeout_hours', '8');
INSERT INTO app_settings (key, value) VALUES ('require_packing_before_shipping', 'true');
INSERT INTO app_settings (key, value) VALUES ('default_receiving_bin', '1');
INSERT INTO app_settings (key, value) VALUES ('allow_over_receiving', 'true');

-- ============================================================
-- PURCHASE ORDERS (5 total)
-- ============================================================

-- PO-2026-001: Large initial stock, 10 lines (Test Vendor A)
INSERT INTO purchase_orders (po_number, po_barcode, vendor_name, status, expected_date, warehouse_id, created_by)
VALUES ('PO-2026-001', 'PO-2026-001', 'Test Vendor A', 'OPEN', CURRENT_DATE + INTERVAL '3 days', 1, 'admin');
INSERT INTO purchase_order_lines (po_id, item_id, quantity_ordered, line_number) VALUES
(1, 1,  100, 1),
(1, 2,  100, 2),
(1, 3,  100, 3),
(1, 4,  100, 4),
(1, 5,  50,  5),
(1, 6,  20,  6),
(1, 7,  200, 7),
(1, 8,  30,  8),
(1, 9,  40,  9),
(1, 10, 60,  10);

-- PO-2026-002: Small reorder, 3 lines (Test Vendor B)
INSERT INTO purchase_orders (po_number, po_barcode, vendor_name, status, expected_date, warehouse_id, created_by)
VALUES ('PO-2026-002', 'PO-2026-002', 'Test Vendor B', 'OPEN', CURRENT_DATE + INTERVAL '5 days', 1, 'admin');
INSERT INTO purchase_order_lines (po_id, item_id, quantity_ordered, line_number) VALUES
(2, 11, 25, 1),
(2, 12, 25, 2),
(2, 13, 15, 3);

-- PO-2026-003: Overlapping items with PO-001, 8 lines (Test Vendor A)
INSERT INTO purchase_orders (po_number, po_barcode, vendor_name, status, expected_date, warehouse_id, created_by)
VALUES ('PO-2026-003', 'PO-2026-003', 'Test Vendor A', 'OPEN', CURRENT_DATE + INTERVAL '7 days', 1, 'admin');
INSERT INTO purchase_order_lines (po_id, item_id, quantity_ordered, line_number) VALUES
(3, 1,  50,  1),
(3, 2,  50,  2),
(3, 5,  25,  3),
(3, 14, 20,  4),
(3, 15, 30,  5),
(3, 16, 80,  6),
(3, 17, 100, 7),
(3, 18, 75,  8);

-- PO-2026-004: New items only, 5 lines (Test Vendor C)
INSERT INTO purchase_orders (po_number, po_barcode, vendor_name, status, expected_date, warehouse_id, created_by)
VALUES ('PO-2026-004', 'PO-2026-004', 'Test Vendor C', 'OPEN', CURRENT_DATE + INTERVAL '10 days', 1, 'admin');
INSERT INTO purchase_order_lines (po_id, item_id, quantity_ordered, line_number) VALUES
(4, 16, 50, 1),
(4, 17, 50, 2),
(4, 18, 50, 3),
(4, 19, 50, 4),
(4, 20, 50, 5);

-- PO-2026-005: Bulk single-item order, 1 line qty 100 (Test Vendor B)
INSERT INTO purchase_orders (po_number, po_barcode, vendor_name, status, expected_date, warehouse_id, created_by)
VALUES ('PO-2026-005', 'PO-2026-005', 'Test Vendor B', 'OPEN', CURRENT_DATE + INTERVAL '2 days', 1, 'admin');
INSERT INTO purchase_order_lines (po_id, item_id, quantity_ordered, line_number) VALUES
(5, 20, 100, 1);

-- ============================================================
-- SALES ORDERS (20 total)
-- ============================================================

-- SO-2026-001 through 005: Single item orders
INSERT INTO sales_orders (so_number, so_barcode, customer_name, status, warehouse_id, ship_method, order_date, created_by) VALUES
('SO-2026-001', 'SO-2026-001', 'Test Customer 1', 'OPEN', 1, 'GROUND',  NOW(), 'admin'),
('SO-2026-002', 'SO-2026-002', 'Test Customer 2', 'OPEN', 1, 'GROUND',  NOW(), 'admin'),
('SO-2026-003', 'SO-2026-003', 'Test Customer 3', 'OPEN', 1, 'EXPRESS', NOW(), 'admin'),
('SO-2026-004', 'SO-2026-004', 'Test Customer 4', 'OPEN', 1, 'GROUND',  NOW(), 'admin'),
('SO-2026-005', 'SO-2026-005', 'Test Customer 5', 'OPEN', 1, 'EXPRESS', NOW(), 'admin');

INSERT INTO sales_order_lines (so_id, item_id, quantity_ordered, line_number) VALUES
(1, 1, 2, 1),   -- SO-001: 2x Elk Hair Caddis
(2, 5, 1, 1),   -- SO-002: 1x Fly Line
(3, 8, 1, 1),   -- SO-003: 1x Fly Rod
(4, 9, 1, 1),   -- SO-004: 1x Fly Box
(5, 20, 5, 1);  -- SO-005: 5x San Juan Worm

-- SO-2026-006 through 010: Multi-item orders
INSERT INTO sales_orders (so_number, so_barcode, customer_name, status, warehouse_id, ship_method, order_date, created_by) VALUES
('SO-2026-006', 'SO-2026-006', 'Test Customer 1', 'OPEN', 1, 'GROUND',  NOW(), 'admin'),
('SO-2026-007', 'SO-2026-007', 'Test Customer 2', 'OPEN', 1, 'EXPRESS', NOW(), 'admin'),
('SO-2026-008', 'SO-2026-008', 'Test Customer 3', 'OPEN', 1, 'GROUND',  NOW(), 'admin'),
('SO-2026-009', 'SO-2026-009', 'Test Customer 4', 'OPEN', 1, 'EXPRESS', NOW(), 'admin'),
('SO-2026-010', 'SO-2026-010', 'Test Customer 5', 'OPEN', 1, 'GROUND',  NOW(), 'admin');

INSERT INTO sales_order_lines (so_id, item_id, quantity_ordered, line_number) VALUES
-- SO-006: 3 lines
(6, 1, 3, 1),   (6, 2, 2, 2),   (6, 3, 1, 3),
-- SO-007: 4 lines
(7, 4, 2, 1),   (7, 5, 1, 2),   (7, 6, 1, 3),   (7, 7, 5, 4),
-- SO-008: 5 lines
(8, 8, 1, 1),   (8, 9, 2, 2),   (8, 10, 3, 3),  (8, 11, 1, 4),  (8, 12, 2, 5),
-- SO-009: 3 lines
(9, 13, 1, 1),  (9, 14, 1, 2),  (9, 15, 1, 3),
-- SO-010: 4 lines
(10, 16, 5, 1), (10, 17, 5, 2), (10, 18, 5, 3), (10, 19, 5, 4);

-- SO-2026-011 through 015: Shared items (same items across multiple SOs to test contention)
INSERT INTO sales_orders (so_number, so_barcode, customer_name, status, warehouse_id, ship_method, order_date, created_by) VALUES
('SO-2026-011', 'SO-2026-011', 'Test Customer 1', 'OPEN', 1, 'GROUND',  NOW(), 'admin'),
('SO-2026-012', 'SO-2026-012', 'Test Customer 2', 'OPEN', 1, 'GROUND',  NOW(), 'admin'),
('SO-2026-013', 'SO-2026-013', 'Test Customer 3', 'OPEN', 1, 'EXPRESS', NOW(), 'admin'),
('SO-2026-014', 'SO-2026-014', 'Test Customer 4', 'OPEN', 1, 'GROUND',  NOW(), 'admin'),
('SO-2026-015', 'SO-2026-015', 'Test Customer 5', 'OPEN', 1, 'EXPRESS', NOW(), 'admin');

INSERT INTO sales_order_lines (so_id, item_id, quantity_ordered, line_number) VALUES
-- All 5 SOs want the same popular items
(11, 1, 2, 1),  (11, 7, 3, 2),
(12, 1, 2, 1),  (12, 7, 3, 2),
(13, 1, 2, 1),  (13, 7, 3, 2),
(14, 1, 2, 1),  (14, 7, 3, 2),
(15, 1, 2, 1),  (15, 7, 3, 2);

-- SO-2026-016 through 018: Serpentine walk (items scattered across all aisles)
INSERT INTO sales_orders (so_number, so_barcode, customer_name, status, warehouse_id, ship_method, order_date, created_by) VALUES
('SO-2026-016', 'SO-2026-016', 'Test Customer 1', 'OPEN', 1, 'GROUND',  NOW(), 'admin'),
('SO-2026-017', 'SO-2026-017', 'Test Customer 2', 'OPEN', 1, 'EXPRESS', NOW(), 'admin'),
('SO-2026-018', 'SO-2026-018', 'Test Customer 3', 'OPEN', 1, 'GROUND',  NOW(), 'admin');

INSERT INTO sales_order_lines (so_id, item_id, quantity_ordered, line_number) VALUES
-- SO-016: hits A-01-01, A-02-01, B-01-01, B-01-03
(16, 1, 1, 1),  (16, 4, 1, 2),  (16, 17, 1, 3), (16, 19, 1, 4),
-- SO-017: hits A-01-02, A-02-02, B-01-02, BULK-01
(17, 2, 1, 1),  (17, 5, 1, 2),  (17, 18, 1, 3), (17, 20, 2, 4),
-- SO-018: hits A-01-03, A-02-03, B-01-01, B-01-03
(18, 3, 1, 1),  (18, 6, 1, 2),  (18, 7, 1, 3),  (18, 9, 1, 4);

-- SO-2026-019 through 020: Short pick test (order more than available)
INSERT INTO sales_orders (so_number, so_barcode, customer_name, status, warehouse_id, ship_method, order_date, created_by) VALUES
('SO-2026-019', 'SO-2026-019', 'Test Customer 4', 'OPEN', 1, 'GROUND', NOW(), 'admin'),
('SO-2026-020', 'SO-2026-020', 'Test Customer 5', 'OPEN', 1, 'GROUND', NOW(), 'admin');

INSERT INTO sales_order_lines (so_id, item_id, quantity_ordered, line_number) VALUES
-- SO-019: wants 99 of item with only 10 on hand (TST-006 wading boots)
(19, 6, 99, 1),
-- SO-020: wants 999 of item with only 200 on hand (TST-020 San Juan Worms)
(20, 20, 999, 1);
