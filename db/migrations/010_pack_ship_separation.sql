-- Migration 010: Pack/Ship Separation
-- Adds require_packing_before_shipping setting, carrier/tracking on sales_orders,
-- and migrates allowed_functions from pack_ship to separate pack + ship.

-- 1. Add the packing toggle setting
INSERT INTO app_settings (key, value) VALUES ('require_packing_before_shipping', 'true')
ON CONFLICT (key) DO NOTHING;

-- 2. Add carrier and tracking_number directly on sales_orders for quick access
ALTER TABLE sales_orders ADD COLUMN IF NOT EXISTS carrier VARCHAR(100);
ALTER TABLE sales_orders ADD COLUMN IF NOT EXISTS tracking_number VARCHAR(255);

-- 3. Migrate allowed_functions: replace pack_ship with pack and ship
UPDATE users
SET allowed_functions = array_remove(allowed_functions, 'pack_ship') || ARRAY['pack', 'ship']
WHERE 'pack_ship' = ANY(allowed_functions);
