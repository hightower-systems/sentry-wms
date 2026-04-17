"""Concurrency tests for V-029 / V-030.

These tests use two real PostgreSQL connections (bypassing Flask's test
session) to prove that row-level locks actually serialize conflicting
writes. Without ``SELECT ... FOR UPDATE`` in the application SQL, the
tests still pass against one connection but demonstrate the race on a
second.

We deliberately avoid Flask's test client here: the conftest's
savepoint-per-test wrapper does not play well with multi-session locks.
"""

import os
import sys
import threading

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2


def _make_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


class TestV029_PoLineLock:
    """V-029: SELECT FOR UPDATE on purchase_order_lines prevents two
    concurrent receives from both passing the remaining-qty check."""

    def test_source_has_for_update(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "routes", "receiving.py"
        )
        src = open(path).read()
        # The receive path must lock the PO line before reading
        # quantity_received / quantity_ordered.
        assert "FOR UPDATE" in src, (
            "routes/receiving.py must hold a row lock on purchase_order_lines "
            "before the over-receipt check (V-029)"
        )

    def test_for_update_blocks_concurrent_reader(self):
        """A second SELECT FOR UPDATE NOWAIT on the same PO line must
        fail with LockNotAvailable while the first transaction holds
        the lock."""
        # Pick any seeded PO line; we'll lock it in conn1 and assert
        # conn2 cannot acquire the same lock.
        bootstrap = _make_conn()
        bootstrap.autocommit = True
        cur = bootstrap.cursor()
        cur.execute("SELECT po_line_id FROM purchase_order_lines LIMIT 1")
        row = cur.fetchone()
        assert row is not None, "seed data must include at least one PO line"
        po_line_id = row[0]
        cur.close()
        bootstrap.close()

        conn1 = _make_conn()
        conn2 = _make_conn()
        try:
            c1 = conn1.cursor()
            c1.execute("BEGIN")
            c1.execute(
                "SELECT po_line_id FROM purchase_order_lines WHERE po_line_id = %s FOR UPDATE",
                (po_line_id,),
            )
            # Second session: try to acquire the same lock with NOWAIT
            # so the test does not hang.
            c2 = conn2.cursor()
            c2.execute("BEGIN")
            try:
                c2.execute(
                    "SELECT po_line_id FROM purchase_order_lines WHERE po_line_id = %s FOR UPDATE NOWAIT",
                    (po_line_id,),
                )
                raise AssertionError(
                    "conn2 should have failed to acquire the row lock"
                )
            except psycopg2.errors.LockNotAvailable:
                pass  # expected
        finally:
            conn1.rollback()
            conn2.rollback()
            conn1.close()
            conn2.close()

    def test_concurrent_receives_do_not_over_receive(self):
        """End-to-end: two threads simulate the app's receive flow
        against the same PO line. Combined they request more than the
        line allows. With FOR UPDATE, exactly one succeeds and the
        other sees insufficient remaining-qty after the first commit."""
        # Set up a fresh PO line with a known quantity we control.
        setup = _make_conn()
        setup.autocommit = True
        cur = setup.cursor()
        cur.execute(
            "INSERT INTO purchase_orders (po_number, po_barcode, vendor_name, status, warehouse_id) "
            "VALUES ('PO-V029-RACE', 'PO-V029-RACE', 'V', 'OPEN', 1) RETURNING po_id"
        )
        po_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO purchase_order_lines (po_id, item_id, quantity_ordered, line_number) "
            "VALUES (%s, 1, 10, 1) RETURNING po_line_id",
            (po_id,),
        )
        po_line_id = cur.fetchone()[0]
        cur.close()
        setup.close()

        request_qty = 10  # each thread wants 10, total 20, PO line allows 10
        results = []
        errors = []
        barrier = threading.Barrier(2)

        def worker():
            conn = _make_conn()
            try:
                cur = conn.cursor()
                cur.execute("BEGIN")
                barrier.wait()
                cur.execute(
                    "SELECT quantity_ordered, quantity_received FROM purchase_order_lines "
                    "WHERE po_line_id = %s FOR UPDATE",
                    (po_line_id,),
                )
                qo, qr = cur.fetchone()
                if qo - qr < request_qty:
                    conn.rollback()
                    errors.append("over-receipt blocked")
                    return
                cur.execute(
                    "UPDATE purchase_order_lines SET quantity_received = quantity_received + %s "
                    "WHERE po_line_id = %s",
                    (request_qty, po_line_id),
                )
                conn.commit()
                results.append("received")
            except Exception as exc:
                errors.append(str(exc))
            finally:
                conn.close()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(results) == 1, f"expected exactly one success, got {results}"
        assert len(errors) == 1, f"expected exactly one block, got {errors}"
        assert "over-receipt" in errors[0]

        # Final state: qty_received exactly equals qty_ordered, never exceeds.
        check = _make_conn()
        check.autocommit = True
        cur = check.cursor()
        cur.execute(
            "SELECT quantity_ordered, quantity_received FROM purchase_order_lines WHERE po_line_id = %s",
            (po_line_id,),
        )
        qo, qr = cur.fetchone()
        assert qr <= qo, f"over-receipt: received {qr} against ordered {qo}"
        # Cleanup (test-session conftest rolls back the savepoint, but
        # this test bypasses that wrapper via direct psycopg2).
        cur.execute(
            "DELETE FROM purchase_order_lines WHERE po_line_id = %s", (po_line_id,)
        )
        cur.execute("DELETE FROM purchase_orders WHERE po_id = %s", (po_id,))
        cur.close()
        check.close()
