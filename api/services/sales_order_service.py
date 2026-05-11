"""Sales-order shared service.

v1.9.0 introduces one shared cancel handler. Two callers converge here:

- Admin operator path: POST /api/admin/sales-orders/<id>/cancel.
- Inbound path: an ERP-pushed update on an existing SO whose
  canonical status field has flipped to CANCELLED.

The cancel transition is the only state-changing SO operation that
historically did NOT write audit_log; routing both paths through this
service closes that hole and gives the inventory unwind one source of
truth. Cancellation does not emit an outbox event (Sentry follows the
ERP for cancel; downstream consumers learn through their own ERP
integration).
"""

from typing import Any, Dict, Optional

from sqlalchemy import text

from constants import (
    ACTION_CANCEL,
    SO_CANCELLED,
    SO_OPEN,
    SO_PACKED,
    SO_PICKED,
    SO_PICKING,
    SO_SHIPPED,
    TASK_PENDING,
)
from services.audit_service import write_audit_log
from services.inventory_service import add_inventory


# "ALLOCATED" appears as a SO state in the existing codebase but has no
# named constant in api/constants.py. Mirroring the literal here so the
# helper does not introduce its own unnamed magic value.
_SO_ALLOCATED = "ALLOCATED"

# Allowed source values for the audit_log.details.source field. Both
# admin and inbound flows must pass one of these so an audit reader can
# distinguish operator-initiated cancels from ERP-initiated cancels.
ALLOWED_SOURCES = ("admin", "inbound")


class CancelNotAllowed(Exception):
    """Raised when the SO cannot be cancelled (typically because it is
    already SHIPPED). The caller surfaces this as a 4xx response with
    an error_kind that maps to the current_status."""

    def __init__(self, message: str, current_status: str):
        super().__init__(message)
        self.current_status = current_status


def _get_default_receiving_bin(db) -> int:
    """Read the default_receiving_bin app_setting. Raises RuntimeError
    if the setting is missing; the seed always provisions it. A
    misconfigured deployment surfaces as a 500 by design rather than
    silently dropping inventory restoration."""
    row = db.execute(
        text(
            "SELECT value FROM app_settings WHERE key = 'default_receiving_bin'"
        )
    ).fetchone()
    if not row or not row.value:
        raise RuntimeError(
            "default_receiving_bin app_setting is missing; cannot unwind "
            "PICKED/PACKED cancellation"
        )
    return int(row.value)


def cancel_sales_order(
    db,
    *,
    so_id: int,
    source: str,
    username: str,
) -> Dict[str, Any]:
    """Cancel a sales order. Idempotent on already-cancelled.

    Locks the sales_orders row with FOR UPDATE so a concurrent ship /
    pick cannot transition past us mid-cancel. Per-status unwind:

    - OPEN: status flip only.
    - ALLOCATED / PICKING: release inventory.quantity_allocated, delete
      pending pick_tasks + pick_batch_orders.
    - PICKED / PACKED: increment inventory.quantity_on_hand at the
      default receiving bin by each line's quantity_picked, reset
      sales_order_lines.quantity_picked / quantity_packed = 0 and
      status = 'PENDING'. Pre-existing PICKED pick_tasks rows stay in
      place as the audit trail of what happened. Operators move items
      physically; the inventory record reflects the ERP-mandated state.
    - SHIPPED: raises CancelNotAllowed; caller returns 4xx. The dockd
      void-ship route is the path for SHIPPED reversal.

    Args:
        so_id: sales_orders.so_id.
        source: "admin" or "inbound" (lands in audit_log.details.source).
        username: actor for audit_log.user_id.

    Returns dict with pre_status, so_number, audit_log_id (None on the
    idempotent already-cancelled path).

    Raises:
        CancelNotAllowed when the SO is SHIPPED or not found.
    """
    if source not in ALLOWED_SOURCES:
        raise ValueError(
            f"source must be one of {ALLOWED_SOURCES}; got {source!r}"
        )

    so = db.execute(
        text(
            "SELECT so_id, so_number, status, warehouse_id "
            "  FROM sales_orders "
            " WHERE so_id = :sid "
            " FOR UPDATE"
        ),
        {"sid": so_id},
    ).fetchone()
    if so is None:
        raise CancelNotAllowed(
            "sales order not found", current_status="UNKNOWN"
        )
    if so.status == SO_SHIPPED:
        raise CancelNotAllowed(
            "cannot cancel a SHIPPED order; void the ship via "
            "/api/v1/dockd/orders/<so>/void-ship first",
            current_status=so.status,
        )
    if so.status == SO_CANCELLED:
        # Idempotent no-op. Audit was already written at original cancel.
        return {
            "pre_status": SO_CANCELLED,
            "so_number": so.so_number,
            "audit_log_id": None,
        }

    pre_status = so.status
    warehouse_id = so.warehouse_id

    if pre_status in (_SO_ALLOCATED, SO_PICKING):
        _unwind_allocated(db, so_id)
    elif pre_status in (SO_PICKED, SO_PACKED):
        _unwind_picked_or_packed(db, so_id, warehouse_id)
    # SO_OPEN: no inventory unwind; only the status flip below.

    db.execute(
        text(
            "UPDATE sales_orders SET status = :status WHERE so_id = :sid"
        ),
        {"status": SO_CANCELLED, "sid": so_id},
    )

    audit_log_id = write_audit_log(
        db,
        action_type=ACTION_CANCEL,
        entity_type="SO",
        entity_id=so_id,
        user_id=username,
        warehouse_id=warehouse_id,
        details={
            "so_number": so.so_number,
            "pre_status": pre_status,
            "source": source,
        },
    )

    return {
        "pre_status": pre_status,
        "so_number": so.so_number,
        "audit_log_id": audit_log_id,
    }


def _unwind_allocated(db, so_id: int) -> None:
    """Pre-pick state unwind: release inventory.quantity_allocated for
    each line's pending pick_tasks, zero out
    sales_order_lines.quantity_allocated, then delete pending
    pick_tasks + pick_batch_orders. Mirrors the pre-existing
    admin_orders cancel path so behavior is unchanged for the OPEN /
    ALLOCATED / PICKING transitions."""
    lines = db.execute(
        text(
            "SELECT so_line_id, item_id, quantity_allocated "
            "  FROM sales_order_lines "
            " WHERE so_id = :sid AND quantity_allocated > 0"
        ),
        {"sid": so_id},
    ).fetchall()

    for line in lines:
        tasks = db.execute(
            text(
                "SELECT bin_id, quantity_to_pick FROM pick_tasks "
                " WHERE so_line_id = :sol_id AND status = :task_status"
            ),
            {"sol_id": line.so_line_id, "task_status": TASK_PENDING},
        ).fetchall()
        for task in tasks:
            db.execute(
                text(
                    "UPDATE inventory "
                    "   SET quantity_allocated = quantity_allocated - :qty "
                    " WHERE item_id = :iid AND bin_id = :bid"
                ),
                {
                    "qty": task.quantity_to_pick,
                    "iid": line.item_id,
                    "bid": task.bin_id,
                },
            )
        db.execute(
            text(
                "UPDATE sales_order_lines SET quantity_allocated = 0 "
                " WHERE so_line_id = :sol_id"
            ),
            {"sol_id": line.so_line_id},
        )

    db.execute(
        text("DELETE FROM pick_tasks WHERE so_id = :sid"),
        {"sid": so_id},
    )
    db.execute(
        text("DELETE FROM pick_batch_orders WHERE so_id = :sid"),
        {"sid": so_id},
    )


def _unwind_picked_or_packed(db, so_id: int, warehouse_id: int) -> None:
    """Post-pick state unwind. Items have already left their source
    bins (decremented at pick-confirm time). Restore them to the
    default receiving bin so an operator can physically move them back
    or redirect the inventory however the ERP-mandated cancel
    workflow requires.

    PICKED pick_tasks rows stay in place: they are the audit trail of
    what physically happened. Only the SO-line state resets so a future
    re-pick attempt (rare; cancellation is terminal in v1.9) would
    re-allocate cleanly. pick_batch_orders is dropped so the SO does
    not show in batch listings.
    """
    receiving_bin_id = _get_default_receiving_bin(db)

    lines = db.execute(
        text(
            "SELECT so_line_id, item_id, quantity_picked "
            "  FROM sales_order_lines "
            " WHERE so_id = :sid AND quantity_picked > 0"
        ),
        {"sid": so_id},
    ).fetchall()

    for line in lines:
        # add_inventory handles both new-row and existing-row cases via
        # the V-030 advisory-lock + SELECT-then-INSERT-or-UPDATE pattern.
        # lot_number stays NULL; per-lot tracking is not part of the
        # cancel-restore semantic.
        add_inventory(
            db,
            item_id=line.item_id,
            bin_id=receiving_bin_id,
            warehouse_id=warehouse_id,
            quantity=line.quantity_picked,
            lot_number=None,
        )
        db.execute(
            text(
                "UPDATE sales_order_lines "
                "   SET quantity_picked = 0, "
                "       quantity_packed = 0, "
                "       status          = 'PENDING' "
                " WHERE so_line_id = :sol_id"
            ),
            {"sol_id": line.so_line_id},
        )

    db.execute(
        text("DELETE FROM pick_batch_orders WHERE so_id = :sid"),
        {"sid": so_id},
    )
