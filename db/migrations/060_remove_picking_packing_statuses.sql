-- 060: Retire PICKING, PACKING, and ALLOCATED as SO/SO-line statuses.
--
-- The sales_orders lifecycle previously listed OPEN, PICKING, PICKED,
-- PACKING, PACKED, SHIPPED, CANCELLED. In practice PACKING was never
-- set by application code (packing flipped PICKED -> PACKED directly),
-- and PICKING was only a derived indicator of "this SO sits inside an
-- active pick batch." Concurrency for picking is enforced by row locks
-- (FOR UPDATE OF inv, FOR UPDATE OF so) and the pick_batches
-- aggregate, not by the SO row's status. The sales_order_lines
-- comment also listed ALLOCATED, which the app never writes.
--
-- After this migration the lifecycle is:
--     OPEN -> PICKED -> PACKED -> SHIPPED
-- with CANCELLED as the off-ramp. "In picking" is derived from
-- pick_batch_orders joined to pick_batches.status; see the dashboard
-- query in api/routes/admin/admin_users.py.
--
-- sales_order_service.cancel_sales_order folds the former PICKING
-- branch into the OPEN branch: an OPEN SO with allocated lines is
-- the new "in batch" signal that drives the allocation unwind.
--
-- The backfill below RUNS: live rows still in the retired statuses
-- fold back to their surviving equivalents (no-op on databases that
-- never held them). If any folded sales_orders rows belonged to an
-- open pick batch, cancel or complete those batches before
-- re-enabling pick traffic; create_pick_batch now refuses an OPEN SO
-- that already has an active pick_batch_orders row.
--
-- v1.8.0 migration discipline (V-213): SET lock_timeout /
-- statement_timeout, BEGIN/COMMIT-wrapped. There is no DB CHECK on
-- sales_orders.status; the column comment is the source of truth.

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

UPDATE sales_orders
   SET status = 'OPEN'
 WHERE status IN ('PICKING', 'PACKING', 'ALLOCATED');

UPDATE sales_order_lines
   SET status = 'PENDING'
 WHERE status = 'ALLOCATED';

COMMENT ON COLUMN sales_orders.status IS
    'Lifecycle: OPEN, PICKED, PACKED, SHIPPED, CANCELLED. Enforced by api/constants.py, not by a DB CHECK. PICKING/PACKING retired in mig 060; the picking state is derived from pick_batch_orders + pick_batches.status.';

COMMENT ON COLUMN sales_order_lines.status IS
    'Lifecycle: PENDING, PICKED, PACKED, SHIPPED. ALLOCATED retired in mig 060 (never written by app code).';

COMMIT;
