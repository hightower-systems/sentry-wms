-- avid-overhaul-mk1: track when a picking ticket has been rendered for
-- a sales order so the Picking Tickets queue can hide already-printed
-- orders without dropping them from the SO history entirely.
--
-- The column is NULL until a successful client-side render confirms
-- the ticket made it to the operator. POST /admin/sales-orders/mark-printed
-- writes the timestamp; the GET picking-ticket endpoint itself does
-- NOT set it, because Print All can fetch ticket data for SOs that
-- subsequently fail to render client-side, and we do not want those
-- orders silently disappearing from the queue.

ALTER TABLE sales_orders ADD COLUMN IF NOT EXISTS printed_at TIMESTAMPTZ;

-- Partial index for the "unprinted in queue" predicate path. The
-- picking-tickets list filters with status IN (...) AND printed_at
-- IS NULL; the partial index keeps the index small (only rows that
-- still need to print) and serves the common query directly.
CREATE INDEX IF NOT EXISTS ix_sales_orders_unprinted
    ON sales_orders (status, ship_by_date)
    WHERE printed_at IS NULL;
