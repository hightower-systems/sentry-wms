"""Sales-order shared service.

v1.9.0 introduces one shared cancel handler. Two callers converge here:

- Admin operator path: POST /api/admin/sales-orders/<id>/cancel
  (and /cancel-backorder, which delegates here after stamping
  cancellation_reason).
- Inbound path: an ERP-pushed update on an existing SO whose
  canonical status field has flipped to CANCELLED.

The cancel transition is the only state-changing SO operation that
historically did NOT write audit_log; routing both paths through this
service closes that hole and gives the inventory unwind one source of
truth.

Backorders add two things on top:

- Parent-cancel cascade: after the per-status unwind, any non-terminal
  child backorder (order_type='backorder') is recursively cancelled
  with cancellation_reason='parent_cancelled'. Keeps Sentry from
  stranding a BO when the parent is cancelled.
- backorder.cancelled emit: when the SO being cancelled is itself a
  BO (order_type='backorder'), emit on the integration_events outbox
  so the Teams adapter / future marketplace adapter see the event.
  The operator-initiated path and the cascade both emit; no consumer
  needs to know which.

The emit relies on the Flask request context for source_txn_id /
user_external_id, mirroring complete_batch's pattern. Outside a
request (unit tests that call cancel_sales_order directly) the emit
is skipped.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from flask import g, has_request_context
from sqlalchemy import text

from constants import (
    ACTION_CANCEL,
    ACTION_PICK,
    ACTION_SO_PICK_RELEASED,
    ACTION_SO_STATUS_REVERTED,
    ACTION_SO_UNPACKED,
    ACTION_SO_UNSHIPPED,
    BATCH_COMPLETED,
    CANCEL_REASON_PARENT_CANCELLED,
    ORDER_TYPE_BACKORDER,
    ORDER_TYPE_RETURN,
    ORDER_TYPE_SO_SUFFIX,
    order_type_allows_fulfillment_ops,
    ACTION_RETURN_RECEIVE,
    ACTION_RETURN_VOID,
    RMA_STATUS_PARTIALLY_RECEIVED,
    RMA_STATUS_RECEIVED,
    SO_CANCELLED,
    SO_OPEN,
    SO_PACKED,
    SO_PICKED,
    SO_SHIPPED,
    TASK_PENDING,
    TASK_PICKED,
    TASK_RELEASED,
)
from services.audit_service import write_audit_log
from services.events_service import emit_event, get_user_external_id
from services.inventory_service import add_inventory


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


def mint_child_so_number(
    db, *, parent_so_id: int, parent_so_number: str, order_type: str
) -> str:
    """Build a readable child so_number off the ORIGINAL's number.

    Post-fulfillment children (replacement / exchange / return / refund) carry a
    human-readable suffix on the original's so_number rather than their own
    POS-<id>: the first child of a given (parent, order_type) is
    "<parent>-<SUFFIX>"; a subsequent one (a rare partial replacement or second
    refund) is "<parent>-<SUFFIX>-N", where N is the existing-child count + 1.

    The sales_orders.so_number UNIQUE constraint is the integrity backstop: a
    rare concurrent mint that races the COUNT collides on the so_number UNIQUE
    at INSERT and surfaces as an IntegrityError, rather than silently issuing a
    duplicate number. The refund path serializes on a FOR UPDATE lock of the
    original; the checkout / create_rma paths do not, so a same-parent race
    there surfaces the IntegrityError to the operator (rare at a single-register
    POS; a true retry loop is the durable fix -- tracked separately).
    """
    try:
        suffix = ORDER_TYPE_SO_SUFFIX[order_type]
    except KeyError:
        raise ValueError(
            f"order_type {order_type!r} has no so_number suffix"
        ) from None
    existing = db.execute(
        text(
            "SELECT COUNT(*) FROM sales_orders "
            "WHERE parent_so_id = :pid AND order_type = :ot"
        ),
        {"pid": parent_so_id, "ot": order_type},
    ).scalar()
    if not existing:
        return f"{parent_so_number}-{suffix}"
    return f"{parent_so_number}-{suffix}-{existing + 1}"


def create_rma(db, *, original_so_id: int, lines, created_by: str, memo=None) -> dict:
    """Create the goods-in RMA SO (order_type='return') for a set of return
    lines, inheriting the warehouse + order_source from the original order.

    lines: an iterable of dicts {item_id, quantity, original_so_line_id}. The
    RMA's so_number is "<original>-RMA" (via mint_child_so_number); each return
    line records the expected quantity and points back to the original line.
    Returns {"so_id": ..., "so_number": ...}.

    Operational only: creating an RMA moves no money and no goods (goods move
    at receiving), so no financial event is emitted here.
    """
    orig = db.execute(
        text(
            "SELECT so_number, warehouse_id, order_source "
            "FROM sales_orders WHERE so_id = :sid"
        ),
        {"sid": original_so_id},
    ).fetchone()
    if orig is None:
        raise ValueError(f"original SO {original_so_id} not found")

    so_number = mint_child_so_number(
        db,
        parent_so_id=original_so_id,
        parent_so_number=orig.so_number,
        order_type=ORDER_TYPE_RETURN,
    )
    # Empty / whitespace-only memo stores as NULL, matching the SO memo PATCH.
    clean_memo = (memo or "").strip() or None
    row = db.execute(
        text(
            """
            INSERT INTO sales_orders (
                so_number, so_barcode, status, warehouse_id,
                order_source, order_type, parent_so_id, created_by, external_id,
                memo
            ) VALUES (
                :so_number, :so_number, 'OPEN', :wh_id,
                :order_source, :order_type, :parent_so_id, :created_by,
                :external_id, :memo
            )
            RETURNING so_id
            """
        ),
        {
            "so_number":    so_number,
            "wh_id":        orig.warehouse_id,
            "order_source": orig.order_source,
            "order_type":   ORDER_TYPE_RETURN,
            "parent_so_id": original_so_id,
            "created_by":   created_by,
            "external_id":  str(uuid.uuid4()),
            "memo":         clean_memo,
        },
    ).fetchone()
    rma_so_id = row.so_id

    for idx, ln in enumerate(lines):
        db.execute(
            text(
                """
                INSERT INTO sales_order_lines (
                    so_id, item_id, quantity_ordered, quantity_allocated,
                    quantity_picked, quantity_packed, quantity_shipped,
                    line_number, status, original_so_line_id
                ) VALUES (
                    :so_id, :item_id, :qty, 0,
                    0, 0, 0,
                    :line_number, 'OPEN', :original_so_line_id
                )
                """
            ),
            {
                "so_id":               rma_so_id,
                "item_id":             ln["item_id"],
                "qty":                 ln["quantity"],
                "line_number":         idx + 1,
                "original_so_line_id": ln.get("original_so_line_id"),
            },
        )
    return {"so_id": rma_so_id, "so_number": so_number}


def receive_rma(
    db,
    *,
    rma_so_id: int,
    item_id: int,
    quantity: int,
    warehouse_id: int,
    bin_id: int,
    received_by: str,
    received_by_external_id: str,
    source_txn_id,
    notes: str | None = None,
    idempotency_key: str | None = None,
    so_line_id: int | None = None,
    expected_quantity_received: int | None = None,
    lot_number: str | None = None,
    serial_number: str | None = None,
) -> dict:
    """Receive goods back against a return SO (the <orig>-RMA): one item into
    one bin, mirroring PO receive. Records an item_receipts row, restocks
    inventory to the destination bin, advances the return line's
    quantity_received and the RMA status (OPEN -> PARTIALLY_RECEIVED ->
    RECEIVED), writes an audit row, and emits return.received. The destination
    bin/warehouse is the disposition signal a downstream ledger maps to its GL.

    The caller (the admin route) resolves received_by_external_id +
    source_txn_id from the request context and passes them in, so this stays
    free of Flask globals and unit-testable. Returns {"receipt_id", "status"}.
    """
    # Local imports keep these lower-level services off the module import graph
    # (avoids any import cycle through the service layer).
    from services.audit_service import write_audit_log
    from services.events_service import emit_event, resolve_source_external_id
    from services.inventory_service import add_inventory
    from services.shipping_allocations import normalise_serials, validate_return_tracking

    rma = db.execute(
        text(
            "SELECT so_number, external_id, parent_so_id "
            "FROM sales_orders WHERE so_id = :sid AND order_type = :ot FOR UPDATE"
        ),
        {"sid": rma_so_id, "ot": ORDER_TYPE_RETURN},
    ).fetchone()
    if rma is None:
        raise ValueError(f"RMA {rma_so_id} not found or not a return SO")
    matching_lines = db.execute(
        text(
            "SELECT line.so_line_id, line.quantity_ordered, line.quantity_received, line.original_so_line_id, "
            " item.is_lot_tracked, item.is_serial_tracked "
            "FROM sales_order_lines line JOIN items item ON item.item_id = line.item_id "
            "WHERE line.so_id = :sid AND line.item_id = :iid "
            "AND (:line_id IS NULL OR line.so_line_id = :line_id) FOR UPDATE OF line"
        ),
        {"sid": rma_so_id, "iid": item_id, "line_id": so_line_id},
    ).fetchall()
    if len(matching_lines) != 1:
        raise ValueError("Choose the exact unique return order line")
    line = matching_lines[0]
    lot_number = (lot_number or "").strip() or None
    serials = normalise_serials(serial_number)
    serial_number = "\n".join(serials) or None
    if bool(line.is_lot_tracked) != bool(lot_number):
        raise ValueError("Provide the actual batch identity for this return item")
    if (line.is_serial_tracked and len(serials) != quantity) or (not line.is_serial_tracked and serials):
        raise ValueError("Provide one actual serial number per serialized return unit")

    # Idempotency: a stable idempotency_key (reused as the receipt's external_id,
    # which is UNIQUE on item_receipts) lets a double-tap / retry no-op instead
    # of double-restocking. Return the prior result without re-writing anything.
    if idempotency_key:
        prior = db.execute(
            text("SELECT receipt_id, so_id, so_line_id, item_id, quantity_received, warehouse_id, bin_id, lot_number, serial_number FROM item_receipts WHERE external_id = :ext"),
            {"ext": idempotency_key},
        ).fetchone()
        if prior:
            if (prior.so_id != rma_so_id or prior.so_line_id != line.so_line_id or prior.item_id != item_id
                or prior.quantity_received != quantity or prior.warehouse_id != warehouse_id or prior.bin_id != bin_id
                or prior.lot_number != lot_number or prior.serial_number != serial_number):
                raise ValueError("The return confirmation identity was already used for different goods")
            status_row = db.execute(
                text("SELECT status FROM sales_orders WHERE so_id = :sid"),
                {"sid": rma_so_id},
            ).fetchone()
            return {
                "receipt_id": prior.receipt_id,
                "status": status_row.status if status_row else None,
                "replayed": True,
            }

    # Over-receipt guard: a return line can only take back what it ordered.
    # Without this, a fat-fingered quantity restocks more inventory than ever
    # shipped and pushes quantity_received past quantity_ordered.
    if expected_quantity_received is not None and line.quantity_received != expected_quantity_received:
        raise ValueError("The return line has changed; reload its received quantity")
    remaining = int(line.quantity_ordered) - int(line.quantity_received)
    if quantity > remaining:
        raise ValueError(
            f"cannot receive {quantity} of item {item_id}: only {remaining} of "
            f"{line.quantity_ordered} remain on RMA {rma_so_id}"
        )

    # Validate the destination bin exists and belongs to warehouse_id. A bad
    # bin_id otherwise 500s on the item_receipts FK; a bin in a DIFFERENT
    # warehouse silently misfiles the restock into a mismatched (wh, bin).
    binrow = db.execute(
        text("SELECT warehouse_id FROM bins WHERE bin_id = :bid"),
        {"bid": bin_id},
    ).fetchone()
    if binrow is None:
        raise ValueError(f"bin {bin_id} not found")
    if int(binrow.warehouse_id) != int(warehouse_id):
        raise ValueError(
            f"bin {bin_id} belongs to warehouse {binrow.warehouse_id}, not {warehouse_id}"
        )

    if line.is_lot_tracked or line.is_serial_tracked:
        validate_return_tracking(db, rma.parent_so_id, line.original_so_line_id, item_id, quantity, lot_number, serials)

    receipt = db.execute(
        text(
            """
            INSERT INTO item_receipts (
                so_id, so_line_id, item_id, quantity_received,
                bin_id, warehouse_id, received_by, notes, external_id, lot_number, serial_number
            ) VALUES (
                :so_id, :so_line_id, :item_id, :qty,
                :bin_id, :wh_id, :rcv, :notes, :ext, :lot_number, :serial_number
            )
            RETURNING receipt_id, external_id, received_at
            """
        ),
        {
            "so_id": rma_so_id, "so_line_id": line.so_line_id, "item_id": item_id,
            "qty": quantity, "bin_id": bin_id, "wh_id": warehouse_id,
            "rcv": received_by, "notes": notes,
            "ext": idempotency_key or str(uuid.uuid4()),
            "lot_number": lot_number,
            "serial_number": serial_number,
        },
    ).fetchone()

    add_inventory(db, item_id, bin_id, warehouse_id, quantity, lot_number)

    # quantity_received is denormalized (not re-derived from item_receipts at
    # read time). Return receipts are deliberately append-only: there is no void
    # path, because a void would not decrement this counter and would silently
    # desync it from the receipts. To correct an over-receive, create a
    # correcting RMA -- never void a return receipt.
    db.execute(
        text(
            "UPDATE sales_order_lines "
            "SET quantity_received = quantity_received + :q WHERE so_line_id = :lid"
        ),
        {"q": quantity, "lid": line.so_line_id},
    )

    open_lines = db.execute(
        text(
            "SELECT COUNT(*) FROM sales_order_lines "
            "WHERE so_id = :sid AND quantity_received < quantity_ordered"
        ),
        {"sid": rma_so_id},
    ).scalar()
    new_status = (
        RMA_STATUS_RECEIVED if open_lines == 0 else RMA_STATUS_PARTIALLY_RECEIVED
    )
    db.execute(
        text("UPDATE sales_orders SET status = :st WHERE so_id = :sid"),
        {"st": new_status, "sid": rma_so_id},
    )

    write_audit_log(
        db,
        action_type=ACTION_RETURN_RECEIVE,
        entity_type="SO",
        entity_id=rma_so_id,
        user_id=received_by,
        warehouse_id=warehouse_id,
        details={
            "item_id": item_id,
            "quantity": quantity,
            "bin_id": bin_id,
            "receipt_id": receipt.receipt_id,
            "so_line_id": line.so_line_id,
            "rma_status": new_status,
        },
    )

    wh = db.execute(
        text("SELECT warehouse_code FROM warehouses WHERE warehouse_id = :w"),
        {"w": warehouse_id},
    ).fetchone()
    bn = db.execute(
        text("SELECT bin_code FROM bins WHERE bin_id = :b"),
        {"b": bin_id},
    ).fetchone()
    item_row = db.execute(
        text("SELECT external_id FROM items WHERE item_id = :i"),
        {"i": item_id},
    ).fetchone()
    item_external_id = (
        resolve_source_external_id(db, "item", item_row.external_id)
        or (str(item_row.external_id) if item_row else None)
    )
    parent_external_id = None
    if rma.parent_so_id:
        parent = db.execute(
            text("SELECT external_id FROM sales_orders WHERE so_id = :p"),
            {"p": rma.parent_so_id},
        ).fetchone()
        parent_external_id = str(parent.external_id) if parent else None

    emit_event(
        db,
        event_type="return.received",
        event_version=1,
        aggregate_type="item_receipt",
        aggregate_id=receipt.receipt_id,
        aggregate_external_id=receipt.external_id,
        warehouse_id=warehouse_id,
        source_txn_id=source_txn_id,
        payload={
            "receipt_external_id": str(receipt.external_id),
            "so_external_id": str(rma.external_id),
            "parent_so_external_id": parent_external_id,
            "warehouse_code": wh.warehouse_code,
            "bin_code": bn.bin_code,
            "lines": [
                {"item_external_id": item_external_id, "quantity_received": quantity,
                 "so_line_id": line.so_line_id, "lot_number": lot_number, "serial_number": serial_number}
            ],
            "received_by_user_external_id": received_by_external_id,
            "received_at": receipt.received_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        },
    )
    return {"receipt_id": receipt.receipt_id, "status": new_status}


def _get_default_receiving_bin(db, warehouse_id: int, current_status: str) -> int:
    """Lock and verify the configured restoration location before changing stock."""
    receiving_bin = db.execute(
        text(
            "SELECT receiving_bin.bin_id "
            "FROM app_settings settings "
            "JOIN bins receiving_bin ON receiving_bin.bin_id::text = settings.value "
            "JOIN warehouses warehouse ON warehouse.warehouse_id = receiving_bin.warehouse_id "
            "WHERE settings.key = 'default_receiving_bin' "
            "AND receiving_bin.warehouse_id = :warehouse_id "
            "AND receiving_bin.is_active IS TRUE "
            "AND receiving_bin.bin_type = 'Staging' "
            "AND warehouse.is_active IS TRUE "
            "FOR SHARE OF settings, receiving_bin, warehouse"
        ),
        {"warehouse_id": warehouse_id},
    ).fetchone()
    if receiving_bin is None:
        raise CancelNotAllowed(
            "Configure an active staging receiving location in this order's warehouse "
            "before cancelling picked goods.",
            current_status=current_status,
        )
    return receiving_bin.bin_id


class ReturnVoidNotAllowed(Exception):
    """Raised when a return SO (RMA) cannot be voided. The caller surfaces
    this as a 4xx whose `reason` code the UI can message on, optionally with
    the current_status for context."""

    def __init__(self, message: str, reason: str, current_status: Optional[str] = None):
        super().__init__(message)
        self.reason = reason
        self.current_status = current_status


def void_return_order(db, *, so_id: int, username: str) -> Dict[str, Any]:
    """Soft-delete (void) a return SO an operator created by mistake.

    Reversible and inventory-safe. Stamps sales_orders.voided_at /
    voided_by and writes a RETURN_VOID audit_log entry; the row and its
    history persist, and list_sales_orders hides voided rows so the RMA
    drops off the page. Touches no inventory, no item_receipts, and
    neither the parent sale nor any refund link.

    Distinct from cancel_sales_order on purpose: the generic cancel
    unwinds outbound allocation / picking, which is wrong for a goods-in
    return (see the standing note in admin/src/pages/RMA.jsx).

    Guards (each raises ReturnVoidNotAllowed with a distinct reason):
      - not_found      : so_id does not exist
      - not_a_return   : order_type != 'return' (no universal SO delete)
      - not_open       : status != OPEN (a received RMA advances past OPEN)
      - has_receipts   : an item_receipts row books against this so_id
                         (goods already came back; voiding would strand them)
      - has_refund     : refund_so_id is set (a credit memo links to it)
    An already-voided return is an idempotent no-op (no new audit row).

    Locks the row FOR UPDATE so a concurrent return-receive cannot land
    mid-void. Does NOT commit; the caller owns the transaction. Returns a
    dict with so_number, audit_log_id (None on the idempotent path), and
    already_voided.

    Raises:
        ReturnVoidNotAllowed on any guard violation.
    """
    so = db.execute(
        text(
            "SELECT so_id, so_number, status, order_type, warehouse_id, "
            "       refund_so_id, voided_at "
            "  FROM sales_orders "
            " WHERE so_id = :sid "
            " FOR UPDATE"
        ),
        {"sid": so_id},
    ).fetchone()
    if so is None:
        raise ReturnVoidNotAllowed(
            "sales order not found", reason="not_found",
        )
    if so.voided_at is not None:
        # Idempotent: already voided (only returns ever are). No new audit row.
        return {
            "so_number": so.so_number,
            "audit_log_id": None,
            "already_voided": True,
        }
    if so.order_type != ORDER_TYPE_RETURN:
        raise ReturnVoidNotAllowed(
            "only return orders (RMAs) can be voided here",
            reason="not_a_return", current_status=so.status,
        )
    if so.status != SO_OPEN:
        raise ReturnVoidNotAllowed(
            "only an un-received (OPEN) return can be voided; this one has "
            "advanced past OPEN",
            reason="not_open", current_status=so.status,
        )
    received = db.execute(
        text("SELECT 1 FROM item_receipts WHERE so_id = :sid LIMIT 1"),
        {"sid": so_id},
    ).fetchone()
    if received is not None:
        raise ReturnVoidNotAllowed(
            "this return has received goods; voiding would strand the "
            "received inventory",
            reason="has_receipts", current_status=so.status,
        )
    if so.refund_so_id is not None:
        raise ReturnVoidNotAllowed(
            "this return is linked to a refund; resolve the refund first",
            reason="has_refund", current_status=so.status,
        )

    db.execute(
        text(
            "UPDATE sales_orders "
            "   SET voided_at = :ts, voided_by = :user "
            " WHERE so_id = :sid"
        ),
        {"ts": datetime.now(timezone.utc), "user": username, "sid": so_id},
    )

    audit_log_id = write_audit_log(
        db,
        action_type=ACTION_RETURN_VOID,
        entity_type="SO",
        entity_id=so_id,
        user_id=username,
        warehouse_id=so.warehouse_id,
        details={
            "so_number": so.so_number,
            "order_type": so.order_type,
            "pre_status": so.status,
        },
    )

    return {
        "so_number": so.so_number,
        "audit_log_id": audit_log_id,
        "already_voided": False,
    }


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

    - OPEN: if the SO has been allocated to an active pick batch
      (sales_order_lines.quantity_allocated > 0), release the
      inventory.quantity_allocated, delete pending pick_tasks +
      pick_batch_orders; otherwise status flip only. PICKING was
      retired in mig 060, so allocation state replaces it as the
      "is this inside a batch?" signal.
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
    idempotent already-cancelled path), and deferred_notifications: a
    list of (event_type, payload, warehouse_id) tuples the caller must
    fire AFTER its own commit (each entry typically maps to a
    fire-and-forget Teams card). The list aggregates across the
    cascade so a single top-level cancel returns every BO-side
    notification the recursive walk produced.

    Raises:
        CancelNotAllowed when the SO is SHIPPED or not found.
    """
    if source not in ALLOWED_SOURCES:
        raise ValueError(
            f"source must be one of {ALLOWED_SOURCES}; got {source!r}"
        )

    so = db.execute(
        text(
            "SELECT so_id, so_number, external_id, status, warehouse_id, "
            "       parent_so_id, order_type, cancellation_reason "
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
            "deferred_notifications": [],
        }

    pre_status = so.status
    warehouse_id = so.warehouse_id

    if pre_status == SO_OPEN:
        # OPEN with no allocation: _unwind_allocated's SELECT returns
        # nothing and the DELETEs are no-ops. OPEN inside an active
        # batch (pre-mig 060 this would have been PICKING): release
        # allocation, drop pending tasks and pick_batch_orders.
        _unwind_allocated(db, so_id)
    elif pre_status in (SO_PICKED, SO_PACKED):
        _unwind_picked_or_packed(db, so_id, warehouse_id, pre_status)
    # WAITING_STOCK (mig 067) is BO-only and inventory-free; no
    # per-status unwind beyond the status flip below.

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

    # If this SO is a backorder, emit backorder.cancelled on the
    # integration_events outbox and collect the payload so the caller
    # can fire-and-forget a Teams card after commit. Both the operator
    # path (/cancel-backorder) and the parent-cancel cascade reach this
    # branch, so the consumer always sees the event regardless of
    # initiator. Emit is skipped outside a Flask request context (unit
    # tests that call cancel_sales_order directly); the
    # deferred-notifications list then stays empty for those callers.
    deferred_notifications = []
    if so.order_type == ORDER_TYPE_BACKORDER and has_request_context():
        parent_so_number = db.execute(
            text("SELECT so_number FROM sales_orders WHERE so_id = :sid"),
            {"sid": so.parent_so_id},
        ).scalar() if so.parent_so_id is not None else None

        # cancellation_reason may have been stamped by the caller
        # (/cancel-backorder) or by the cascade below. Re-read so the
        # emit reflects the latest value.
        reason_row = db.execute(
            text(
                "SELECT cancellation_reason FROM sales_orders "
                " WHERE so_id = :sid"
            ),
            {"sid": so_id},
        ).fetchone()
        reason = (reason_row.cancellation_reason if reason_row else None) or "other"

        cancelled_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        user_external_id = get_user_external_id(db, username)
        cancelled_payload = {
            "backorder_so_external_id": str(so.external_id),
            "backorder_so_number": so.so_number,
            "parent_so_number": parent_so_number or "",
            "warehouse_id": warehouse_id,
            "cancellation_reason": reason,
            "cancelled_by_user_external_id": user_external_id,
            "cancelled_at": cancelled_at,
        }
        emit_event(
            db,
            event_type="backorder.cancelled",
            event_version=1,
            aggregate_type="sales_order",
            aggregate_id=so_id,
            aggregate_external_id=so.external_id,
            warehouse_id=warehouse_id,
            source_txn_id=g.source_txn_id,
            payload=cancelled_payload,
        )
        deferred_notifications.append(
            ("backorder.cancelled", cancelled_payload, warehouse_id)
        )

    # Parent-cancel cascade. Walk any non-terminal child
    # backorder, stamp 'parent_cancelled' as its reason, and
    # recursively cancel. The recursive call re-enters this function
    # on the child's so_id (a different row, so FOR UPDATE does not
    # deadlock); the child path's own emit handles backorder.cancelled.
    # cancel_sales_order is idempotent on already-CANCELLED so a child
    # that is already CANCELLED is a no-op.
    if so.order_type != ORDER_TYPE_BACKORDER:
        children = db.execute(
            text(
                "SELECT so_id FROM sales_orders "
                " WHERE parent_so_id = :sid "
                "   AND order_type = :backorder "
                "   AND status NOT IN (:cancelled, :shipped)"
            ),
            {
                "sid": so_id,
                "backorder": ORDER_TYPE_BACKORDER,
                "cancelled": SO_CANCELLED,
                "shipped": SO_SHIPPED,
            },
        ).fetchall()
        for child in children:
            db.execute(
                text(
                    "UPDATE sales_orders "
                    "   SET cancellation_reason = :reason "
                    " WHERE so_id = :cid"
                ),
                {"reason": CANCEL_REASON_PARENT_CANCELLED, "cid": child.so_id},
            )
            child_result = cancel_sales_order(
                db, so_id=child.so_id, source=source, username=username,
            )
            deferred_notifications.extend(
                child_result.get("deferred_notifications", [])
            )

    return {
        "pre_status": pre_status,
        "so_number": so.so_number,
        "audit_log_id": audit_log_id,
        "deferred_notifications": deferred_notifications,
    }


def _unwind_allocated(db, so_id: int) -> None:
    """Pre-pick state unwind: release inventory.quantity_allocated for
    each line's pending pick_tasks, zero out
    sales_order_lines.quantity_allocated, then delete pending
    pick_tasks + pick_batch_orders. Safe to call on an OPEN SO with
    no allocation: the SELECT returns nothing and the DELETEs are
    no-ops."""
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


def _unwind_picked_or_packed(
    db, so_id: int, warehouse_id: int, current_status: str
) -> None:
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
    receiving_bin_id = _get_default_receiving_bin(db, warehouse_id, current_status)

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


# so-refinement: forward-flow ordering used by the revert-status path.
# PICKING / PACKING / ALLOCATED were retired in v1.13.0 (mig 058); the
# live flow is OPEN -> PICKED -> PACKED -> SHIPPED. Any target status
# with a strictly-lower index than current is a "backward" transition
# and must go through revert_sales_order_status() so the operator
# decides what happens to picked / packed / shipped state.
_STATUS_ORDER = {
    SO_OPEN: 0,
    SO_PICKED: 1,
    SO_PACKED: 2,
    SO_SHIPPED: 3,
}


class RevertNotAllowed(Exception):
    """The revert request is invalid for a structural reason (target
    status not lower than current, partial release would leave the SO
    in an inconsistent state, etc.). Caller maps to a 4xx with the
    `kind` discriminator so the frontend can show the right error."""

    def __init__(self, message: str, kind: str, **context):
        super().__init__(message)
        self.kind = kind
        self.context = context


def revert_sales_order_status(
    db,
    *,
    so_id: int,
    new_status: str,
    release_pick_task_ids: list,
    username: str,
) -> Dict[str, Any]:
    """Demote an SO from PICKED/PACKED/SHIPPED back to an earlier
    status, unwinding the side effects the operator selected.

    Effects, computed from (current, target):
      * unship: SHIPPED to anything. Clears tracking_number, carrier,
        shipped_at on the header. No physical inventory move (the
        goods left the building); operators reconcile externally.
      * unpack: current >= PACKED and target < PACKED. Zeros
        quantity_packed on every line. No inventory move (pack does
        not touch inventory; only labels units as packed).
      * release: each pick_task_id in release_pick_task_ids restores
        its quantity_picked to the bin it came from, decrements the
        line's quantity_picked by the same amount, and marks the
        pick_task as RELEASED so a subsequent revert prompt does not
        re-offer it.

    Guards:
      * Target must be strictly lower than current (forward only by
        the normal status flow).
      * SO must exist and not be CANCELLED.
      * Every release_pick_task_id must belong to this SO and still
        be in TASK_PICKED state.
      * If target < PICKED and any line ends with quantity_picked > 0
        after releases, raises with kind='picked_qty_remaining' so the
        operator can either release more or pick a higher target.

    Audit: one ACTION_SO_STATUS_REVERTED row per request (from/to),
    plus one ACTION_SO_PICK_RELEASED per released pick_task, plus a
    single ACTION_SO_UNPACKED row when unpack runs and a single
    ACTION_SO_UNSHIPPED row when unship runs.
    """
    if new_status not in _STATUS_ORDER:
        raise RevertNotAllowed(
            f"unknown target status: {new_status!r}",
            kind="invalid_status",
        )

    so = db.execute(
        text(
            "SELECT so_id, so_number, status, warehouse_id, order_type, "
            "       tracking_number, carrier, shipped_at "
            "  FROM sales_orders WHERE so_id = :sid FOR UPDATE"
        ),
        {"sid": so_id},
    ).fetchone()
    if so is None:
        raise RevertNotAllowed(
            "sales order not found", kind="not_found",
        )
    # A return SO runs the inbound RMA lifecycle, not the outbound
    # OPEN/PICKED/PACKED/SHIPPED ladder this revert unwinds; releasing
    # picked qty against it is never valid. Belt-and-suspenders (returns
    # never reach PICKED/PACKED/SHIPPED), but keeps the rule server-side.
    if not order_type_allows_fulfillment_ops(so.order_type):
        raise RevertNotAllowed(
            "cannot release picked quantity on a return SO",
            kind="not_eligible",
            order_type=so.order_type,
        )
    if so.status == SO_CANCELLED:
        raise RevertNotAllowed(
            "cannot revert a cancelled order; reopen via the cancel-undo workflow",
            kind="cancelled",
            current_status=so.status,
        )

    cur_idx = _STATUS_ORDER.get(so.status)
    new_idx = _STATUS_ORDER[new_status]
    if cur_idx is None or new_idx > cur_idx:
        raise RevertNotAllowed(
            "target status must not be higher than current status",
            kind="not_backward",
            current_status=so.status,
            target_status=new_status,
        )

    # so-refinement: new_status == current_status is the "release-only"
    # path. The operator wants to release picks without flipping status,
    # which is valid when releasing a subset that does not zero out
    # quantity_picked. need_unship/need_unpack guard against firing on
    # a same-status request so a release-only call on a SHIPPED order
    # does not also clear its shipment fields.
    need_unship = so.status == SO_SHIPPED and new_status != SO_SHIPPED
    need_unpack = cur_idx >= _STATUS_ORDER[SO_PACKED] and new_idx < _STATUS_ORDER[SO_PACKED]
    target_below_picked = new_idx < _STATUS_ORDER[SO_PICKED]

    pick_ids = list({int(x) for x in release_pick_task_ids or []})
    released_details = []

    if pick_ids:
        tasks = db.execute(
            text(
                "SELECT pick_task_id, so_line_id, item_id, bin_id, "
                "       quantity_to_pick, quantity_picked, status "
                "  FROM pick_tasks "
                " WHERE pick_task_id = ANY(:ids) AND so_id = :sid "
                " FOR UPDATE"
            ),
            {"ids": pick_ids, "sid": so_id},
        ).fetchall()
        found_ids = {t.pick_task_id for t in tasks}
        missing = [pid for pid in pick_ids if pid not in found_ids]
        if missing:
            raise RevertNotAllowed(
                f"pick_task(s) not found on this SO: {missing}",
                kind="pick_task_missing",
                missing_ids=missing,
            )
        wrong_state = [t.pick_task_id for t in tasks if t.status != TASK_PICKED]
        if wrong_state:
            raise RevertNotAllowed(
                f"pick_task(s) not in PICKED state: {wrong_state}",
                kind="pick_task_wrong_state",
                wrong_state_ids=wrong_state,
            )
        for t in tasks:
            qty_picked = int(t.quantity_picked or 0)
            # quantity_to_pick is what create_pick_batch / wave_create
            # bumped sol.quantity_allocated by; releasing the task must
            # undo that allocation so a re-scan sees the line as still
            # needing coverage. quantity_picked is what physically moved
            # out of the bin and goes back via add_inventory; for a
            # normal PICKED task the two values are equal, but the split
            # keeps the partial-pick path (qty_picked < qty_to_pick)
            # correct rather than under-restoring the allocation.
            qty_allocated = int(t.quantity_to_pick or 0)
            if qty_picked > 0:
                add_inventory(
                    db,
                    item_id=t.item_id,
                    bin_id=t.bin_id,
                    warehouse_id=so.warehouse_id,
                    quantity=qty_picked,
                    lot_number=None,
                )
            db.execute(
                text(
                    "UPDATE sales_order_lines "
                    "   SET quantity_picked = GREATEST(quantity_picked - :picked_qty, 0), "
                    "       quantity_allocated = GREATEST(quantity_allocated - :alloc_qty, 0) "
                    " WHERE so_line_id = :sol_id"
                ),
                {"picked_qty": qty_picked, "alloc_qty": qty_allocated, "sol_id": t.so_line_id},
            )
            db.execute(
                text(
                    "UPDATE pick_tasks SET status = :released "
                    " WHERE pick_task_id = :ptid"
                ),
                {"released": TASK_RELEASED, "ptid": t.pick_task_id},
            )
            # Audit row only when units actually moved. A qty_picked=0
            # release still flips the task to RELEASED and undoes the
            # allocation, but there is no physical inventory event to
            # narrate so SO_PICK_RELEASED is skipped.
            if qty_picked > 0:
                released_details.append({
                    "pick_task_id": t.pick_task_id,
                    "so_line_id": t.so_line_id,
                    "item_id": t.item_id,
                    "bin_id": t.bin_id,
                    "quantity": qty_picked,
                })

    if target_below_picked:
        # Reject mid-flight: if releases were partial, the SO would
        # have picked qty on lines but a status that claims no picks.
        # Operator must either release more pick_tasks or pick a
        # target that still permits picks (PICKED or higher).
        remaining = db.execute(
            text(
                "SELECT COALESCE(SUM(quantity_picked), 0) AS total "
                "  FROM sales_order_lines WHERE so_id = :sid"
            ),
            {"sid": so_id},
        ).scalar()
        if remaining and int(remaining) > 0:
            raise RevertNotAllowed(
                "cannot demote below PICKED while quantity_picked remains; "
                "release the remaining pick_tasks or pick a higher target status",
                kind="picked_qty_remaining",
                remaining_picked=int(remaining),
                target_status=new_status,
            )

    if need_unpack:
        db.execute(
            text(
                "UPDATE sales_order_lines SET quantity_packed = 0 "
                " WHERE so_id = :sid AND quantity_packed > 0"
            ),
            {"sid": so_id},
        )
        db.execute(
            text("UPDATE sales_orders SET packed_at = NULL WHERE so_id = :sid"),
            {"sid": so_id},
        )
        write_audit_log(
            db,
            action_type=ACTION_SO_UNPACKED,
            entity_type="SO",
            entity_id=so_id,
            user_id=username,
            warehouse_id=so.warehouse_id,
            details={"from_status": so.status, "to_status": new_status},
        )

    if need_unship:
        db.execute(
            text(
                "UPDATE sales_orders "
                "   SET tracking_number = NULL, carrier = NULL, shipped_at = NULL "
                " WHERE so_id = :sid"
            ),
            {"sid": so_id},
        )
        write_audit_log(
            db,
            action_type=ACTION_SO_UNSHIPPED,
            entity_type="SO",
            entity_id=so_id,
            user_id=username,
            warehouse_id=so.warehouse_id,
            details={
                "prev_tracking_number": so.tracking_number,
                "prev_carrier": so.carrier,
                "prev_shipped_at": so.shipped_at.isoformat() if so.shipped_at else None,
            },
        )

    for d in released_details:
        write_audit_log(
            db,
            action_type=ACTION_SO_PICK_RELEASED,
            entity_type="SO",
            entity_id=so_id,
            user_id=username,
            warehouse_id=so.warehouse_id,
            details=d,
        )

    status_changed = new_status != so.status
    if status_changed:
        db.execute(
            text("UPDATE sales_orders SET status = :status WHERE so_id = :sid"),
            {"status": new_status, "sid": so_id},
        )
        write_audit_log(
            db,
            action_type=ACTION_SO_STATUS_REVERTED,
            entity_type="SO",
            entity_id=so_id,
            user_id=username,
            warehouse_id=so.warehouse_id,
            details={
                "from_status": so.status,
                "to_status": new_status,
                "released_pick_tasks": [d["pick_task_id"] for d in released_details],
                "unpacked": bool(need_unpack),
                "unshipped": bool(need_unship),
            },
        )

    return {
        "so_id": so_id,
        "so_number": so.so_number,
        "from_status": so.status,
        "to_status": new_status,
        "released_pick_tasks": released_details,
        "unpacked": bool(need_unpack),
        "unshipped": bool(need_unship),
    }


# ---------------------------------------------------------------------------
# Admin virtual pick
# ---------------------------------------------------------------------------
#
# Operator-driven shortcut: an admin marks a sales order picked through the
# admin UI without the handheld going out on the floor. Used when the floor
# work already happened but the digital pick never landed -- e.g. legacy
# ShipRush bridge, a stuck batch, or an SO that arrived already-fulfilled.
#
# The end state must be indistinguishable from a real pick:
#   * sales_order_lines.quantity_picked carries the picked qty per line
#   * inventory.quantity_on_hand decrements at the chosen bin per pick
#   * audit log carries one ACTION_PICK row per pick (entity_type='SO')
#   * pick.confirmed integration event fires when the SO flips to PICKED
#
# Symmetry with handheld picks comes from synthesising a one-shot
# pick_batch (status COMPLETED at insert) and one pick_task per pick
# entry. The existing _revert_so_status path takes pick_task_ids as input
# and walks the same release SQL regardless of how the task was created,
# so a virtual pick can be undone through the same "Release Picked
# Quantities" UI as a real pick. No second undo path needed.


class AdminPickError(Exception):
    """Operator submitted an admin-pick request that cannot be applied.

    kind distinguishes the four shapes the route layer maps to HTTP
    status codes:

      * 'so_not_open'           422 -- SO must be OPEN to virtual-pick
      * 'line_not_on_so'        422 -- so_line_id does not belong to so_id
      * 'bin_wrong_warehouse'   422 -- bin is in a different warehouse
      * 'over_pick'             422 -- summed pick qty > unpicked remaining
      * 'insufficient_available' 409 -- bin has less available than asked
    """

    def __init__(self, message: str, kind: str, **context):
        super().__init__(message)
        self.kind = kind
        self.context = context


def record_admin_pick(db, *, so_id: int, picks, username: str) -> dict:
    """Apply a batch of admin virtual picks atomically.

    picks: iterable of {so_line_id, bin_id, quantity} entries. Multiple
    entries with the same so_line_id are allowed (split-bin: line N
    drawn from bin A and bin B in one submit) and sum into the line's
    quantity_picked.

    Caller owns the transaction boundary. This function performs no
    commit; the route handler commits after also running
    maybe_promote_so_to_picked. Raises AdminPickError on validation
    failure; the caller does not need to roll back because no writes
    happen ahead of the validation pass.

    Implementation notes:
      * One synthetic pick_batch row per call (status COMPLETED, marked
        ADMIN-PICK-<so_id>-<microsecond timestamp>) so subsequent
        release flows see one batch per admin action.
      * One pick_task row per pick entry (status TASK_PICKED,
        scan_confirmed=true, picked_by=username) so the existing
        release-pick-tasks endpoint can undo individual entries.
      * Inventory side: decrement quantity_on_hand at the bin by the
        picked qty. quantity_allocated is NOT changed because the
        bin was never pre-allocated against this SO -- the available
        check (on_hand - allocated >= qty) is the safety against
        stealing inventory promised to a different SO.
      * Line side: bump quantity_picked. Bump quantity_allocated to
        max(current, quantity_picked) to preserve the picked-floor
        invariant that the handheld path's _normalize_so_reservations
        enforces.
    """
    so = db.execute(
        text(
            "SELECT so_id, so_number, external_id, status, warehouse_id, "
            "       order_type "
            "  FROM sales_orders WHERE so_id = :sid FOR UPDATE"
        ),
        {"sid": so_id},
    ).fetchone()
    if so is None:
        raise AdminPickError(
            f"sales order {so_id} not found",
            kind="so_not_found",
        )
    # A return SO is inbound RMA goods-in (separate status lifecycle); the
    # outbound admin pick never applies to it. Defense-in-depth: returns
    # are also held out of the sales-orders ledger the button lives on.
    if not order_type_allows_fulfillment_ops(so.order_type):
        raise AdminPickError(
            "cannot admin-pick a return SO",
            kind="not_eligible",
            order_type=so.order_type,
        )
    if so.status != SO_OPEN:
        raise AdminPickError(
            f"sales order must be OPEN to admin-pick (current: {so.status})",
            kind="so_not_open",
            current_status=so.status,
        )

    pick_entries = list(picks)
    if not pick_entries:
        raise AdminPickError("no picks supplied", kind="empty")

    # Group by so_line_id so we can validate the over-pick guard against
    # the line's remaining capacity in a single sum check per line.
    by_line: dict[int, int] = {}
    for p in pick_entries:
        by_line[p["so_line_id"]] = by_line.get(p["so_line_id"], 0) + p["quantity"]

    # Lock + load every involved line. ANY(:ids) keeps it one round trip
    # whether the operator picks 1 or 50 lines.
    line_rows = db.execute(
        text(
            "SELECT so_line_id, item_id, quantity_ordered, quantity_picked, "
            "       quantity_allocated "
            "  FROM sales_order_lines "
            " WHERE so_line_id = ANY(:ids) AND so_id = :sid "
            " FOR UPDATE"
        ),
        {"ids": list(by_line.keys()), "sid": so_id},
    ).fetchall()
    line_by_id = {ln.so_line_id: ln for ln in line_rows}
    missing = [lid for lid in by_line if lid not in line_by_id]
    if missing:
        raise AdminPickError(
            f"so_line_id(s) not on SO {so_id}: {missing}",
            kind="line_not_on_so",
            missing_line_ids=missing,
        )
    for lid, total in by_line.items():
        ln = line_by_id[lid]
        remaining = ln.quantity_ordered - ln.quantity_picked
        if total > remaining:
            raise AdminPickError(
                f"line {lid} over-pick: requested {total}, remaining {remaining}",
                kind="over_pick",
                so_line_id=lid,
                requested=total,
                remaining=remaining,
            )

    # Bin-warehouse sanity. Loads every distinct bin in one query.
    bin_ids = sorted({p["bin_id"] for p in pick_entries})
    bin_rows = db.execute(
        text("SELECT bin_id, warehouse_id FROM bins WHERE bin_id = ANY(:ids)"),
        {"ids": bin_ids},
    ).fetchall()
    bin_warehouse = {b.bin_id: b.warehouse_id for b in bin_rows}
    for bid in bin_ids:
        if bid not in bin_warehouse:
            raise AdminPickError(
                f"bin {bid} not found", kind="bin_not_found", bin_id=bid,
            )
        if bin_warehouse[bid] != so.warehouse_id:
            raise AdminPickError(
                f"bin {bid} is in warehouse {bin_warehouse[bid]}, "
                f"SO {so_id} is in warehouse {so.warehouse_id}",
                kind="bin_wrong_warehouse",
                bin_id=bid,
                bin_warehouse=bin_warehouse[bid],
                so_warehouse=so.warehouse_id,
            )

    # Synthesise the wrapper batch. Microsecond timestamp keeps the
    # batch_number UNIQUE clean even when an operator double-submits.
    batch_number = (
        f"ADMIN-PICK-{so_id}-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    )
    batch_id = db.execute(
        text(
            "INSERT INTO pick_batches "
            "  (batch_number, warehouse_id, assigned_to, status, "
            "   started_at, completed_at) "
            "VALUES (:bn, :wh, :user, :st, NOW(), NOW()) "
            "RETURNING batch_id"
        ),
        {
            "bn": batch_number,
            "wh": so.warehouse_id,
            "user": username,
            "st": BATCH_COMPLETED,
        },
    ).scalar()
    db.execute(
        text(
            "INSERT INTO pick_batch_orders (batch_id, so_id) "
            "VALUES (:bid, :sid)"
        ),
        {"bid": batch_id, "sid": so_id},
    )

    created_task_ids: list[int] = []
    for seq, pick in enumerate(pick_entries, start=1):
        line = line_by_id[pick["so_line_id"]]

        # Atomic available check + on_hand decrement. The WHERE clause
        # is the safety: if available drops below the requested qty
        # between the validation read and the write, the UPDATE
        # matches zero rows and RETURNING is empty. Treat that as
        # insufficient_available and raise -- the caller's transaction
        # rolls back any prior writes (batch row, prior picks).
        updated = db.execute(
            text(
                "UPDATE inventory "
                "   SET quantity_on_hand = quantity_on_hand - :qty, "
                "       updated_at = NOW() "
                " WHERE item_id = :iid AND bin_id = :bid "
                "   AND quantity_on_hand - quantity_allocated >= :qty "
                "RETURNING inventory_id"
            ),
            {
                "qty": pick["quantity"],
                "iid": line.item_id,
                "bid": pick["bin_id"],
            },
        ).fetchone()
        if updated is None:
            raise AdminPickError(
                f"bin {pick['bin_id']} has insufficient available stock "
                f"for line {pick['so_line_id']} (requested {pick['quantity']})",
                kind="insufficient_available",
                so_line_id=pick["so_line_id"],
                bin_id=pick["bin_id"],
                requested=pick["quantity"],
            )

        # Bump line counters. GREATEST keeps quantity_allocated >= the
        # new picked floor without overriding a higher standing
        # reservation (matching the handheld path's invariant).
        db.execute(
            text(
                "UPDATE sales_order_lines "
                "   SET quantity_picked = quantity_picked + :qty, "
                "       quantity_allocated = GREATEST( "
                "           quantity_allocated, quantity_picked + :qty "
                "       ) "
                " WHERE so_line_id = :sol_id"
            ),
            {"qty": pick["quantity"], "sol_id": pick["so_line_id"]},
        )

        # Synthetic pick_task. status=PICKED, scan_confirmed=true,
        # tote_number NULL (admin picks have no tote). pick_sequence
        # 1..N within the batch keeps the (batch_id, pick_sequence)
        # index dense.
        task_id = db.execute(
            text(
                "INSERT INTO pick_tasks "
                "  (batch_id, so_id, so_line_id, item_id, bin_id, "
                "   quantity_to_pick, quantity_picked, pick_sequence, "
                "   status, picked_by, picked_at, scan_confirmed) "
                "VALUES (:bid, :sid, :sol, :iid, :bin, "
                "        :qty, :qty, :seq, "
                "        :st, :user, NOW(), TRUE) "
                "RETURNING pick_task_id"
            ),
            {
                "bid": batch_id,
                "sid": so_id,
                "sol": pick["so_line_id"],
                "iid": line.item_id,
                "bin": pick["bin_id"],
                "qty": pick["quantity"],
                "seq": seq,
                "st": TASK_PICKED,
                "user": username,
            },
        ).scalar()
        created_task_ids.append(task_id)

        # Audit row per pick. details.source='admin_virtual' is the
        # discriminator that lets dashboards and post-incident readers
        # separate operator shortcuts from handheld floor work.
        item_sku = db.execute(
            text("SELECT sku FROM items WHERE item_id = :iid"),
            {"iid": line.item_id},
        ).scalar()
        write_audit_log(
            db,
            action_type=ACTION_PICK,
            entity_type="SO",
            entity_id=so_id,
            user_id=username,
            warehouse_id=so.warehouse_id,
            details={
                "source": "admin_virtual",
                "sku": item_sku,
                "quantity_to_pick": pick["quantity"],
                "quantity_picked": pick["quantity"],
                "pick_task_id": task_id,
                "item_id": line.item_id,
                "bin_id": pick["bin_id"],
                "batch_id": batch_id,
                "so_line_id": pick["so_line_id"],
            },
        )

    return {
        "batch_id": batch_id,
        "batch_number": batch_number,
        "pick_task_ids": created_task_ids,
        "picks_applied": len(created_task_ids),
    }


def maybe_promote_so_to_picked(db, *, so_id: int, username: str) -> bool:
    """If every line on the SO is fully picked, flip status to PICKED.

    Mirrors complete_batch's status flip + pick.confirmed emit, but
    only fires when the SO is the one being completed. Returns True
    when the flip happened, False when at least one line is still
    under-picked.

    Caller owns the transaction boundary; this function performs no
    commit. Skipped emission outside a Flask request context matches
    complete_batch's pattern -- unit tests can call this directly and
    get the status flip without the event side effect.
    """
    short = db.execute(
        text(
            "SELECT 1 FROM sales_order_lines "
            " WHERE so_id = :sid AND quantity_picked < quantity_ordered "
            " LIMIT 1"
        ),
        {"sid": so_id},
    ).fetchone()
    if short is not None:
        return False

    so = db.execute(
        text(
            "SELECT so_id, so_number, external_id, status, warehouse_id "
            "  FROM sales_orders WHERE so_id = :sid FOR UPDATE"
        ),
        {"sid": so_id},
    ).fetchone()
    if so is None:
        return False
    if so.status != SO_OPEN:
        # Some other request already promoted it; nothing to do.
        return False

    db.execute(
        text(
            "UPDATE sales_orders SET status = :st, picked_at = NOW() "
            " WHERE so_id = :sid"
        ),
        {"sid": so_id, "st": SO_PICKED},
    )

    if not has_request_context():
        return True

    line_rows = db.execute(
        text(
            """
            SELECT i.external_id AS item_external_id, sol.quantity_picked
              FROM sales_order_lines sol
              JOIN items i ON i.item_id = sol.item_id
             WHERE sol.so_id = :sid
             ORDER BY sol.line_number
            """
        ),
        {"sid": so_id},
    ).fetchall()
    completed_at = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    emit_event(
        db,
        event_type="pick.confirmed",
        event_version=1,
        aggregate_type="sales_order",
        aggregate_id=so.so_id,
        aggregate_external_id=so.external_id,
        warehouse_id=so.warehouse_id,
        source_txn_id=g.source_txn_id,
        payload={
            "sales_order_external_id": str(so.external_id),
            "lines": [
                {
                    "item_external_id": str(line.item_external_id),
                    "quantity_picked": line.quantity_picked,
                    "lot_number": None,
                    "serial_number": None,
                }
                for line in line_rows
            ],
            "completed_by_user_external_id": get_user_external_id(db, username),
            "completed_at": completed_at,
        },
    )
    return True
