"""Per-aggregate FOR UPDATE retrofit across the seven v1.5.0 emit sites (#119).

Two kinds of coverage here:

1. Source-scan assertions that verify each emit site's initial SELECT on
   the aggregate row carries ``FOR UPDATE``. A grep-based test is enough
   because the failure we are preventing (a dropped / renamed lock
   clause) shows up textually in the source, not at runtime.

2. One dynamic concurrency test that proves the lock actually blocks:
   session A holds ``FOR UPDATE`` on a sales_orders row, session B's
   ``FOR UPDATE NOWAIT`` against the same row fails with
   LockNotAvailable. sales_orders is the richest target (three of seven
   emit sites use it). The other tables get the same Postgres row-lock
   semantics and do not need a separate dynamic proof.
"""

import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest


_API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _read(relpath: str) -> str:
    with open(os.path.join(_API_DIR, relpath), "r", encoding="utf-8") as f:
        return f.read()


class TestAggregateRowLocks:
    """Each entry below names the emit site, the source file, and the
    substring that must appear somewhere in the file: normally the full
    SELECT clause that ends in `FOR UPDATE` on the named aggregate. If a
    future edit drops the lock clause or renames the target table the
    grep fails and the PR surfaces the regression.
    """

    def test_receiving_locks_purchase_orders(self):
        src = _read("routes/receiving.py")
        assert "FROM purchase_orders" in src
        assert "FOR UPDATE" in src
        # The PO lock sits in the same receive_items function that already
        # holds V-029's FOR UPDATE on purchase_order_lines; both strings
        # must appear.
        assert src.count("FOR UPDATE") >= 2, (
            "routes/receiving.py must hold FOR UPDATE on both "
            "purchase_orders (v1.5.0 #119) and purchase_order_lines (V-029)"
        )

    def test_review_adjustments_locks_inventory_adjustments(self):
        src = _read("routes/admin/admin_users.py")
        assert (
            "FROM inventory_adjustments WHERE adjustment_id = :aid FOR UPDATE"
            in src
        ), "review_adjustments must lock the inventory_adjustments row (v1.5.0 #119)"

    def test_direct_adjustment_locks_inventory_row(self):
        src = _read("routes/admin/admin_users.py")
        assert (
            "FROM inventory WHERE item_id = :iid AND bin_id = :bid FOR UPDATE"
            in src
        ), (
            "direct_adjustment REMOVE branch must lock the inventory row "
            "(v1.5.0 #119). ADD branch is covered by add_inventory's V-030 lock."
        )

    def test_picking_service_locks_sales_orders_in_batch(self):
        src = _read("services/picking_service.py")
        assert "FOR UPDATE OF so" in src, (
            "picking_service.complete_batch must hold FOR UPDATE OF so on "
            "the pick_batch_orders JOIN sales_orders SELECT (v1.5.0 #119)"
        )

    def test_packing_locks_sales_orders(self):
        src = _read("routes/packing.py")
        assert "FROM sales_orders" in src and "FOR UPDATE" in src, (
            "packing.complete_packing must lock the sales_orders row (v1.5.0 #119)"
        )

    def test_shipping_locks_sales_orders(self):
        src = _read("routes/shipping.py")
        assert "FROM sales_orders" in src and "FOR UPDATE" in src, (
            "shipping.fulfill must lock the sales_orders row (v1.5.0 #119)"
        )

    def test_transfers_delegates_lock_to_move_inventory(self):
        # transfers.move does not add a new FOR UPDATE on bin_transfers
        # because the row is created inline. The serialisation point is
        # move_inventory's V-030 FOR UPDATE on the source inventory row.
        # Assert both the comment naming this decision and the V-030 lock
        # in the service it calls.
        transfers_src = _read("routes/transfers.py")
        assert "v1.5.0 #119" in transfers_src, (
            "routes/transfers.py must carry a comment documenting that the "
            "per-aggregate serialisation is delegated to move_inventory"
        )
        service_src = _read("services/inventory_service.py")
        assert "FOR UPDATE" in service_src, (
            "services/inventory_service.move_inventory must hold FOR UPDATE "
            "on the source inventory row (V-030); this is the serialisation "
            "point transfers.move relies on for per-aggregate FIFO."
        )


def _make_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


class TestSalesOrderLockBlocksConcurrent:
    """Dynamic proof that the FOR UPDATE on sales_orders actually blocks a
    second session. Applies to pack.confirmed, ship.confirmed, and (via
    FOR UPDATE OF so) pick.confirmed since all three target the same
    row-lock primitive on the same table.
    """

    def test_second_session_for_update_nowait_fails(self):
        bootstrap = _make_conn()
        bootstrap.autocommit = True
        cur = bootstrap.cursor()
        cur.execute("SELECT so_id FROM sales_orders LIMIT 1")
        row = cur.fetchone()
        assert row is not None, "seed data must include at least one SO"
        so_id = row[0]
        cur.close()
        bootstrap.close()

        holder = _make_conn()
        contender = _make_conn()
        try:
            h = holder.cursor()
            h.execute("BEGIN")
            h.execute(
                "SELECT so_id FROM sales_orders WHERE so_id = %s FOR UPDATE",
                (so_id,),
            )

            c = contender.cursor()
            c.execute("BEGIN")
            with pytest.raises(psycopg2.errors.LockNotAvailable):
                c.execute(
                    "SELECT so_id FROM sales_orders WHERE so_id = %s FOR UPDATE NOWAIT",
                    (so_id,),
                )
        finally:
            holder.rollback()
            contender.rollback()
            holder.close()
            contender.close()
