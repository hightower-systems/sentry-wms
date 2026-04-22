#!/bin/bash
# ============================================================
# SENTRY WMS - Database Seed Wrapper
# ============================================================
# When SKIP_SEED=true: only creates admin user + default warehouse + bin types
# When SKIP_SEED is unset/false: runs full demo seed (items, POs, SOs, etc.)
#
# Default fresh-install behavior: admin/admin with must_change_password=true
# so the first login is forced into the change-password flow. Operators
# who set ADMIN_PASSWORD explicitly (CI, automation, deterministic dev
# environments) keep their existing password and skip the forced flow.
# ============================================================

set -e

# Resolve admin password and whether to require a forced first-login change.
if [ -n "${ADMIN_PASSWORD:-}" ]; then
  ADMIN_PW="$ADMIN_PASSWORD"
  MUST_CHANGE="false"
else
  ADMIN_PW="admin"
  MUST_CHANGE="true"
fi

if [ "$SKIP_SEED" = "true" ]; then
  echo "SKIP_SEED=true  -  creating minimal setup (admin user + default warehouse only)"
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v admin_pw="$ADMIN_PW" -v must_change="$MUST_CHANGE" <<'MINIMAL'
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Default warehouse
INSERT INTO warehouses (warehouse_code, warehouse_name, address) VALUES
('WH-01', 'My Warehouse', '');

-- Default zones (bin type placeholders)
INSERT INTO zones (warehouse_id, zone_code, zone_name, zone_type) VALUES
(1, 'RCV',   'Receiving',    'RECEIVING'),
(1, 'PICK',  'Picking',      'PICKING'),
(1, 'STAGE', 'Staging',      'STAGING');

-- Default bins (one per type)
INSERT INTO bins (zone_id, warehouse_id, bin_code, bin_barcode, bin_type, pick_sequence, putaway_sequence, description, external_id) VALUES
(1, 1, 'RECV-01', 'RECV-01', 'Staging',  0, 0, 'Default receiving bin', gen_random_uuid()),
(2, 1, 'PICK-01', 'PICK-01', 'Pickable', 100, 100, 'Default pick bin', gen_random_uuid()),
(3, 1, 'BULK-01', 'BULK-01', 'Pickable', 0, 0, 'Default bulk bin', gen_random_uuid());

-- Admin user
INSERT INTO users (username, password_hash, full_name, role, warehouse_id, allowed_functions, must_change_password, external_id)
VALUES ('admin', crypt(:'admin_pw', gen_salt('bf')), 'Admin User', 'ADMIN', 1, '{}', :'must_change'::boolean, gen_random_uuid());

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
  # Overwrite placeholder admin password with the resolved one + set forced-change flag
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v admin_pw="$ADMIN_PW" -v must_change="$MUST_CHANGE" <<'UPDATE_PW'
CREATE EXTENSION IF NOT EXISTS pgcrypto;
UPDATE users SET password_hash = crypt(:'admin_pw', gen_salt('bf')),
                 must_change_password = :'must_change'::boolean
 WHERE username = 'admin';
UPDATE_PW
  echo "Full seed complete."
fi

echo ""
if [ "$MUST_CHANGE" = "true" ]; then
  echo "================================================="
  echo "  SENTRY WMS INITIAL SETUP COMPLETE"
  echo "  Fresh install: admin credentials are admin/admin."
  echo "  Forced password change required on first login."
  echo "================================================="
else
  echo "================================================="
  echo "  SENTRY WMS INITIAL SETUP COMPLETE"
  echo "  Admin username: admin"
  echo "  Admin password: $ADMIN_PW"
  echo "  >>> CHANGE THIS PASSWORD AFTER FIRST LOGIN <<<"
  echo "================================================="
fi
echo ""
