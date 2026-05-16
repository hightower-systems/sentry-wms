-- avid-overhaul-mk1 P5.1: link items to the canonical vendors table.
-- The upstream vendors table (mig 042 / schema.sql) exists but no
-- code reads or writes it from the admin surface, and items have no
-- vendor association. This migration adds the FK so the Items page
-- can filter / display vendor and so the eventual PO flow can pick
-- a canonical vendor instead of typing a free-text vendor name.
--
-- Single FK (not a join table) per the locked decision in the
-- overhaul discovery: the catalog assumes one primary vendor per
-- SKU. If a SKU genuinely sources from multiple vendors, that is a
-- future schema change (item_vendors many-to-many) - not blocking
-- the immediate need.
--
-- ON DELETE RESTRICT mirrors how items.default_bin_id and other
-- canonical relationships behave. The application surfaces a clean
-- 400 "vendor in use" error before reaching this constraint.

ALTER TABLE items
    ADD COLUMN IF NOT EXISTS vendor_id UUID REFERENCES vendors(canonical_id) ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS ix_items_vendor_id ON items(vendor_id);
