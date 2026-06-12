"""Shared cancel-sales-order service contract.

The service is the single transition point for SO cancellation across
both the admin operator path and the inbound (ERP-driven) path. The
existing per-route admin tests in test_admin.py cover the admin entry
point; this file focuses on the service-level invariants:

- Idempotent on already-CANCELLED.
- SHIPPED rejection raises CancelNotAllowed.
- OPEN-with-allocation unwind: inventory.quantity_allocated released,
  pending pick_tasks deleted, pick_batch_orders dropped. (PICKING was
  retired in mig 060; the "in batch" signal is now allocation state
  on the OPEN SO rather than a distinct status.)
- PICKED / PACKED unwind: inventory.quantity_on_hand on the default
  receiving bin increments by quantity_picked per line; sales_order_lines
  reset quantity_picked / quantity_packed = 0 and status = 'PENDING';
  PICKED pick_tasks rows STAY (audit trail).
- One audit_log row per real cancel; idempotent re-cancel writes none.
- audit_log.details carries pre_status + source.
- audit_log hash chain stays intact.
- source must be one of the allowed values.
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from sqlalchemy import text as sa_text

from db_test_context import get_raw_connection


def _ensure_user(username="op"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE username = %s", (username,))
    row = cur.fetchone()
    if row:
        cur.close()
        return row[0]
    cur.execute(
        "INSERT INTO users (username, password_hash, full_name, role, external_id) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING user_id",
        (username,
         "$2b$12$placeholderHashForTests000000000000000000000000000000",
         username.title(), "USER", str(uuid.uuid4())),
    )
    user_id = cur.fetchone()[0]
    cur.close()
    return user_id


def _insert_so(status="OPEN", warehouse_id=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders (so_number, customer_name, status, "
        "warehouse_id, external_id) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING so_id",
        (f"SO-CANCEL-{uuid.uuid4().hex[:8]}", "Cust", status, warehouse_id,
         str(uuid.uuid4())),
    )
    so_id = cur.fetchone()[0]
    cur.close()
    return so_id


def _insert_item():
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO items (sku, item_name, upc, external_id) "
        "VALUES (%s, %s, %s, %s) RETURNING item_id",
        (f"SKU-{uuid.uuid4().hex[:8]}", "Widget", "0123456789012",
         str(uuid.uuid4())),
    )
    item_id = cur.fetchone()[0]
    cur.close()
    return item_id


def _insert_so_line(so_id, item_id, *, qty_ordered=2, qty_allocated=0,
                     qty_picked=0, qty_packed=0, status="PENDING"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_order_lines "
        "(so_id, item_id, quantity_ordered, quantity_allocated, "
        " quantity_picked, quantity_packed, line_number, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING so_line_id",
        (so_id, item_id, qty_ordered, qty_allocated, qty_picked,
         qty_packed, 1, status),
    )
    sol_id = cur.fetchone()[0]
    cur.close()
    return sol_id


def _set_inv(item_id, bin_id, *, qty_on_hand, qty_allocated=0, warehouse_id=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO inventory (item_id, bin_id, warehouse_id, "
        "quantity_on_hand, quantity_allocated) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (item_id, bin_id, lot_number) DO UPDATE "
        "SET quantity_on_hand = EXCLUDED.quantity_on_hand, "
        "    quantity_allocated = EXCLUDED.quantity_allocated",
        (item_id, bin_id, warehouse_id, qty_on_hand, qty_allocated),
    )
    cur.close()


def _get_inv(item_id, bin_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT quantity_on_hand, quantity_allocated FROM inventory "
        "WHERE item_id = %s AND bin_id = %s",
        (item_id, bin_id),
    )
    row = cur.fetchone()
    cur.close()
    return row  # (on_hand, allocated) or None


def _insert_pick_task(so_id, sol_id, item_id, bin_id, *, qty=2,
                       status="PENDING"):
    conn = get_raw_connection()
    cur = conn.cursor()
    # Need a pick_batch first; the FK to pick_batches is non-null.
    cur.execute(
        "INSERT INTO pick_batches (batch_number, warehouse_id, status) "
        "VALUES (%s, 1, 'OPEN') RETURNING batch_id",
        (f"BATCH-{uuid.uuid4().hex[:8]}",),
    )
    batch_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO pick_tasks (batch_id, so_id, so_line_id, item_id, "
        "bin_id, quantity_to_pick, status, pick_sequence) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING pick_task_id",
        (batch_id, so_id, sol_id, item_id, bin_id, qty, status, 1),
    )
    task_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO pick_batch_orders (batch_id, so_id, tote_number) "
        "VALUES (%s, %s, %s)",
        (batch_id, so_id, f"TOTE-{uuid.uuid4().hex[:4]}"),
    )
    cur.close()
    return task_id, batch_id


# ----------------------------------------------------------------------
# Per-status unwind
# ----------------------------------------------------------------------


class TestOpenStatus:
    def test_open_so_status_flips_only(self, _db_transaction):
        from services.sales_order_service import cancel_sales_order
        db = _db_transaction
        _ensure_user("op")
        so_id = _insert_so(status="OPEN")
        result = cancel_sales_order(
            db, so_id=so_id, source="admin", username="op",
        )
        assert result["pre_status"] == "OPEN"
        status = db.execute(
            sa_text("SELECT status FROM sales_orders WHERE so_id = :s"),
            {"s": so_id},
        ).fetchone().status
        assert status == "CANCELLED"
        assert result["audit_log_id"] is not None


class TestOpenWithAllocation:
    def test_open_with_allocation_releases_and_drops_pick_tasks(self, _db_transaction):
        # Post mig 060 the PICKING status no longer exists. An OPEN SO
        # with quantity_allocated > 0 (sitting inside an active batch)
        # is the new "release allocation on cancel" path.
        from services.sales_order_service import cancel_sales_order
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        so_id = _insert_so(status="OPEN")
        _set_inv(item_id, bin_id=3, qty_on_hand=10, qty_allocated=2)
        sol_id = _insert_so_line(so_id, item_id, qty_allocated=2,
                                  status="PENDING")
        _insert_pick_task(so_id, sol_id, item_id, bin_id=3, qty=2,
                          status="PENDING")

        cancel_sales_order(db, so_id=so_id, source="admin", username="op")

        on_hand, allocated = db.execute(
            sa_text("SELECT quantity_on_hand, quantity_allocated FROM inventory "
                    "WHERE item_id = :i AND bin_id = :b"),
            {"i": item_id, "b": 3},
        ).fetchone()
        assert on_hand == 10  # untouched
        assert allocated == 0  # released

        sol_alloc = db.execute(
            sa_text("SELECT quantity_allocated FROM sales_order_lines "
                    "WHERE so_line_id = :s"),
            {"s": sol_id},
        ).fetchone().quantity_allocated
        assert sol_alloc == 0

        pick_tasks = db.execute(
            sa_text("SELECT COUNT(*) FROM pick_tasks WHERE so_id = :s"),
            {"s": so_id},
        ).scalar()
        assert pick_tasks == 0
        batch_orders = db.execute(
            sa_text("SELECT COUNT(*) FROM pick_batch_orders WHERE so_id = :s"),
            {"s": so_id},
        ).scalar()
        assert batch_orders == 0


class TestPickedStatus:
    def test_picked_restores_to_default_receiving_bin(self, _db_transaction):
        from services.sales_order_service import cancel_sales_order
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        so_id = _insert_so(status="PICKED")
        # Default receiving bin from the seed is bin_id=1.
        _set_inv(item_id, bin_id=1, qty_on_hand=0, qty_allocated=0)
        sol_id = _insert_so_line(so_id, item_id, qty_picked=2,
                                  status="PICKED")
        _insert_pick_task(so_id, sol_id, item_id, bin_id=3, qty=2,
                          status="PICKED")

        cancel_sales_order(db, so_id=so_id, source="admin", username="op")

        # quantity_on_hand at receiving bin should now be 2.
        on_hand, allocated = db.execute(
            sa_text("SELECT quantity_on_hand, quantity_allocated FROM inventory "
                    "WHERE item_id = :i AND bin_id = 1"),
            {"i": item_id},
        ).fetchone()
        assert on_hand == 2
        assert allocated == 0

        # sales_order_lines reset.
        line = db.execute(
            sa_text("SELECT quantity_picked, quantity_packed, status "
                    "FROM sales_order_lines WHERE so_line_id = :s"),
            {"s": sol_id},
        ).fetchone()
        assert line.quantity_picked == 0
        assert line.quantity_packed == 0
        assert line.status == "PENDING"

        # PICKED pick_tasks rows STAY for audit; only pick_batch_orders dropped.
        task_status = db.execute(
            sa_text("SELECT status FROM pick_tasks WHERE so_id = :s"),
            {"s": so_id},
        ).fetchone().status
        assert task_status == "PICKED"
        batch_orders = db.execute(
            sa_text("SELECT COUNT(*) FROM pick_batch_orders WHERE so_id = :s"),
            {"s": so_id},
        ).scalar()
        assert batch_orders == 0


class TestPackedStatus:
    def test_packed_restores_and_resets_packed_qty(self, _db_transaction):
        from services.sales_order_service import cancel_sales_order
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        so_id = _insert_so(status="PACKED")
        _set_inv(item_id, bin_id=1, qty_on_hand=5, qty_allocated=0)
        sol_id = _insert_so_line(so_id, item_id, qty_picked=3,
                                  qty_packed=3, status="PACKED")
        _insert_pick_task(so_id, sol_id, item_id, bin_id=3, qty=3,
                          status="PICKED")

        cancel_sales_order(db, so_id=so_id, source="admin", username="op")

        on_hand, _ = db.execute(
            sa_text("SELECT quantity_on_hand, quantity_allocated FROM inventory "
                    "WHERE item_id = :i AND bin_id = 1"),
            {"i": item_id},
        ).fetchone()
        # Receiving bin was 5 + 3 = 8 after cancel.
        assert on_hand == 8
        line = db.execute(
            sa_text("SELECT quantity_picked, quantity_packed, status "
                    "FROM sales_order_lines WHERE so_line_id = :s"),
            {"s": sol_id},
        ).fetchone()
        assert line.quantity_picked == 0
        assert line.quantity_packed == 0
        assert line.status == "PENDING"


class TestShippedStatus:
    def test_shipped_raises_cancel_not_allowed(self, _db_transaction):
        from services.sales_order_service import (
            CancelNotAllowed, cancel_sales_order,
        )
        db = _db_transaction
        _ensure_user("op")
        so_id = _insert_so(status="SHIPPED")
        with pytest.raises(CancelNotAllowed) as exc:
            cancel_sales_order(
                db, so_id=so_id, source="admin", username="op",
            )
        assert exc.value.current_status == "SHIPPED"
        # SO status not flipped.
        status = db.execute(
            sa_text("SELECT status FROM sales_orders WHERE so_id = :s"),
            {"s": so_id},
        ).fetchone().status
        assert status == "SHIPPED"


class TestNotFound:
    def test_unknown_so_id_raises_cancel_not_allowed(self, _db_transaction):
        from services.sales_order_service import (
            CancelNotAllowed, cancel_sales_order,
        )
        db = _db_transaction
        _ensure_user("op")
        with pytest.raises(CancelNotAllowed) as exc:
            cancel_sales_order(
                db, so_id=999_999_999, source="admin", username="op",
            )
        assert exc.value.current_status == "UNKNOWN"


# ----------------------------------------------------------------------
# Idempotency
# ----------------------------------------------------------------------


class TestAlreadyCancelled:
    def test_re_cancel_is_idempotent(self, _db_transaction):
        from services.sales_order_service import cancel_sales_order
        db = _db_transaction
        _ensure_user("op")
        so_id = _insert_so(status="OPEN")
        first = cancel_sales_order(
            db, so_id=so_id, source="admin", username="op",
        )
        second = cancel_sales_order(
            db, so_id=so_id, source="admin", username="op",
        )
        assert first["audit_log_id"] is not None
        assert second["audit_log_id"] is None
        assert second["pre_status"] == "CANCELLED"
        # Only one CANCEL audit row total.
        count = db.execute(
            sa_text(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE entity_type = 'SO' AND entity_id = :s "
                "  AND action_type = 'CANCEL'"
            ),
            {"s": so_id},
        ).scalar()
        assert count == 1


# ----------------------------------------------------------------------
# Audit log
# ----------------------------------------------------------------------


class TestAuditLogShape:
    def test_audit_row_carries_pre_status_and_source(self, _db_transaction):
        from services.sales_order_service import cancel_sales_order
        db = _db_transaction
        _ensure_user("op")
        so_id = _insert_so(status="PICKED")
        row_count = db.execute(
            sa_text("SELECT COUNT(*) FROM sales_orders WHERE so_id = :s"),
            {"s": so_id},
        ).scalar()
        assert row_count == 1
        cancel_sales_order(
            db, so_id=so_id, source="admin", username="op",
        )
        row = db.execute(
            sa_text(
                "SELECT user_id, details FROM audit_log "
                "WHERE entity_type = 'SO' AND entity_id = :s "
                "  AND action_type = 'CANCEL'"
            ),
            {"s": so_id},
        ).fetchone()
        assert row.user_id == "op"
        details = row.details if isinstance(row.details, dict) else None
        # details is JSONB; psycopg2 returns dict
        assert details is not None
        assert details["pre_status"] == "PICKED"
        assert details["source"] == "admin"

    def test_chain_intact_after_cancel(self, _db_transaction):
        from services.sales_order_service import cancel_sales_order
        db = _db_transaction
        _ensure_user("op")
        so_id = _insert_so(status="OPEN")
        cancel_sales_order(
            db, so_id=so_id, source="admin", username="op",
        )
        broken = db.execute(sa_text("SELECT verify_audit_log_chain()")).scalar()
        assert broken is None


class TestSourceValidation:
    def test_invalid_source_raises_value_error(self, _db_transaction):
        from services.sales_order_service import cancel_sales_order
        db = _db_transaction
        _ensure_user("op")
        so_id = _insert_so(status="OPEN")
        with pytest.raises(ValueError, match="source must be"):
            cancel_sales_order(
                db, so_id=so_id, source="other", username="op",
            )
