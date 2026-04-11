#!/bin/bash
# ============================================================
# SENTRY WMS - Database Seed Wrapper
# ============================================================
# When SKIP_SEED=true: only creates admin user + default warehouse + bin types
# When SKIP_SEED is unset/false: runs full demo seed (items, POs, SOs, etc.)
# ============================================================

set -e

if [ "$SKIP_SEED" = "true" ]; then
  echo "SKIP_SEED=true  -  creating minimal setup (admin user + default warehouse only)"
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<'MINIMAL'
-- Default warehouse
INSERT INTO warehouses (warehouse_code, warehouse_name, address) VALUES
('WH-01', 'My Warehouse', '');

-- Default zones (bin type placeholders)
INSERT INTO zones (warehouse_id, zone_code, zone_name, zone_type) VALUES
(1, 'RCV',   'Receiving',    'RECEIVING'),
(1, 'PICK',  'Picking',      'PICKING'),
(1, 'STAGE', 'Staging',      'STAGING');

-- Default bins (one per type)
INSERT INTO bins (zone_id, warehouse_id, bin_code, bin_barcode, bin_type, pick_sequence, putaway_sequence, description) VALUES
(1, 1, 'RECV-01', 'RECV-01', 'Staging',  0, 0, 'Default receiving bin'),
(2, 1, 'PICK-01', 'PICK-01', 'Pickable', 100, 100, 'Default pick bin'),
(3, 1, 'BULK-01', 'BULK-01', 'Pickable', 0, 0, 'Default bulk bin');

-- Admin user (password: admin)
INSERT INTO users (username, password_hash, full_name, role, warehouse_id, allowed_functions)
VALUES ('admin', '$2b$12$zDGRKFLmc6v/A4mVhxOzb.7uoW1ulnXn0AisK5uJ5iWk33vC2EpSK', 'Admin User', 'ADMIN', 1, '{}');

-- Default settings
INSERT INTO app_settings (key, value) VALUES ('session_timeout_hours', '8');
INSERT INTO app_settings (key, value) VALUES ('require_packing_before_shipping', 'true');
INSERT INTO app_settings (key, value) VALUES ('default_receiving_bin', '1');
INSERT INTO app_settings (key, value) VALUES ('allow_over_receiving', 'true');
MINIMAL
  echo "Minimal seed complete."
else
  echo "Running full demo seed..."
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /seed-data/seed-apartment-lab.sql
  echo "Full seed complete."
fi
