-- avid-overhaul-mk1 P7: denormalise the SO's originating source_system
-- onto the sales_orders row so the admin SO edit surface can edit it
-- without touching the inbound history. Pre-P7 the only path to the
-- source_system was sales_orders.latest_inbound_id ->
-- inbound_sales_orders.source_system, which is read-mostly and not
-- always populated (admin-created and POS-created SOs have no inbound
-- row at all).
--
-- Adding the column directly:
--   * lets a CSR repoint an SO that ingested under the wrong system
--     tag (e.g. shopify-us when it should have been shopify-ca) via
--     PUT /admin/sales-orders/<id> without rewriting inbound history.
--   * gives POS / admin-created SOs a real source_system value (NULL
--     pre-P7 because there was no inbound row to derive from).
--   * stays FK-referenced to inbound_source_systems_allowlist so the
--     value is always one of the canonical tags.
--
-- Backfill walks the latest applied inbound row per SO. SOs without an
-- inbound history (admin-created, POS-created) stay NULL and become
-- editable from the admin SO page.

ALTER TABLE sales_orders
    ADD COLUMN IF NOT EXISTS source_system VARCHAR(64)
        REFERENCES inbound_source_systems_allowlist(source_system);

-- Backfill from the most-recent applied inbound row. The
-- inbound_sales_orders_current index already covers (source_system,
-- external_id, received_at DESC) WHERE status='applied' so the
-- correlated subquery is index-only.
UPDATE sales_orders so
   SET source_system = sub.source_system
  FROM (
      SELECT iso.source_system, sales.so_id
        FROM sales_orders sales
        JOIN inbound_sales_orders iso
          ON iso.inbound_id = sales.latest_inbound_id
       WHERE sales.latest_inbound_id IS NOT NULL
         AND iso.status = 'applied'
  ) sub
 WHERE so.so_id = sub.so_id
   AND so.source_system IS NULL;

-- Lookups by source_system from the admin SO filter dropdown.
CREATE INDEX IF NOT EXISTS ix_sales_orders_source_system
    ON sales_orders (source_system)
    WHERE source_system IS NOT NULL;
