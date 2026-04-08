-- ============================================================
-- Migration 011: Bin Type Simplification
-- ============================================================
-- Replace 6 old bin types with 3 new ones:
--   Staging         — Inbound/QC, pick algorithm NEVER pulls from here
--   PickableStaging — Staging area pickers CAN pull from
--   Pickable        — Standard shelf/bulk/shipping bins
-- ============================================================

-- RECV bins: inbound staging → Staging
UPDATE bins SET bin_type = 'Staging' WHERE bin_type IN ('RECEIVING', 'INBOUND_STAGING');

-- QC bins: hold area → Staging
UPDATE bins SET bin_type = 'Staging' WHERE bin_type = 'QC';
UPDATE bins SET bin_type = 'Staging' WHERE bin_type = 'STANDARD' AND bin_code LIKE 'QC%';

-- SHIP bins: outbound staging → Pickable (order status tracks shipment)
UPDATE bins SET bin_type = 'Pickable' WHERE bin_type IN ('OUTBOUND_STAGING', 'SHIPPING');
UPDATE bins SET bin_type = 'Pickable' WHERE bin_type = 'STAGING' AND bin_code LIKE 'SHIP%';

-- Shelf and bulk bins → Pickable
UPDATE bins SET bin_type = 'Pickable' WHERE bin_type IN ('PICKING', 'BULK', 'STANDARD');

-- Catch any remaining old types
UPDATE bins SET bin_type = 'Pickable' WHERE bin_type NOT IN ('Staging', 'PickableStaging', 'Pickable');

-- Add CHECK constraint
ALTER TABLE bins DROP CONSTRAINT IF EXISTS bins_bin_type_check;
ALTER TABLE bins ADD CONSTRAINT bins_bin_type_check
  CHECK (bin_type IN ('Staging', 'PickableStaging', 'Pickable'));
