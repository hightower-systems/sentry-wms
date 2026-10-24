"""
Receiving endpoints: PO lookup and item receipt submission.
"""

import uuid
from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, request
from sqlalchemy import text

from constants import (
    PO_OPEN, PO_PARTIAL, PO_RECEIVED, PO_CLOSED,
    POL_PENDING, POL_PARTIAL, POL_RECEIVED,
    ACTION_RECEIVE, ACTION_RECEIVE_CANCEL,
)
from middleware.auth_middleware import require_auth, warehouse_scope_clause
from middleware.db import with_db
from schemas.receiving import CancelReceivingRequest, ReceiveItemsRequest
from services.audit_service import write_audit_log
from services.events_service import emit_event, get_user_external_id, resolve_source_external_id
from services.inventory_service import (
    add_inventory,
    release_satisfiable_backorders,
    RELEASE_SOURCE_RECEIPT,
)
from services.serial_history import latest_native_serial_position, lock_native_serials
from services.shipping_allocations import normalise_serials
from services.webhook_dispatcher.backorder_notifier import (
    dispatch_backorder_notification,
)
from utils.validation import validate_body

receiving_bp = Blueprint("receiving", __name__)


@receiving_bp.route("/po/<barcode>")
@require_auth
@with_db
def lookup_po(barcode):
    # V-026: filter warehouse at SELECT time so non-admins cannot use
    # this endpoint as an existence oracle for POs in other warehouses.
    # A PO belonging to a warehouse the user can't see returns the same
    # 404 as a PO that doesn't exist at all.
    scope_clause, scope_params = warehouse_scope_clause("warehouse_id")
    po = g.db.execute(
        text(
            f"""
            SELECT po_id, po_number, po_barcode, vendor_name, vendor_id,
                   status, expected_date, warehouse_id, notes, created_at,
                   received_at, created_by
            FROM purchase_orders
            WHERE (po_barcode = :barcode OR po_number = :barcode)
              {scope_clause}
            LIMIT 1
            """
        ),
        {"barcode": barcode, **scope_params},
    ).fetchone()

    if not po:
        return jsonify({"error": "Purchase order not found"}), 404

    if po.status == PO_CLOSED:
        return jsonify({"error": "Purchase order is closed"}), 400

    lines = g.db.execute(
        text(
            """
            SELECT pol.po_line_id, pol.line_number, pol.item_id,
                   i.sku, i.item_name, i.upc, i.mpn,
                   pol.quantity_ordered, pol.quantity_received,
                   (pol.quantity_ordered - pol.quantity_received) AS quantity_remaining,
                   pol.status
            FROM purchase_order_lines pol
            JOIN items i ON i.item_id = pol.item_id
            WHERE pol.po_id = :po_id
            ORDER BY pol.line_number
            """
        ),
        {"po_id": po.po_id},
    ).fetchall()

    return jsonify({
        "purchase_order": {
            "po_id": po.po_id,
            "po_number": po.po_number,
            "po_barcode": po.po_barcode,
            "vendor_name": po.vendor_name,
            "status": po.status,
            "expected_date": po.expected_date.isoformat() if po.expected_date else None,
            "warehouse_id": po.warehouse_id,
            "notes": po.notes,
            "created_at": po.created_at.isoformat() if po.created_at else None,
        },
        "lines": [
            {
                "po_line_id": l.po_line_id,
                "line_number": l.line_number,
                "item_id": l.item_id,
                "sku": l.sku,
                "item_name": l.item_name,
                "upc": l.upc,
                "mpn": l.mpn,
                "quantity_ordered": l.quantity_ordered,
                "quantity_received": l.quantity_received,
                "quantity_remaining": l.quantity_remaining,
                "status": l.status,
            }
            for l in lines
        ],
    })


@receiving_bp.route("/receive", methods=["POST"])
@require_auth
@validate_body(ReceiveItemsRequest)
@with_db
def receive_items(validated):
    po_id = validated.po_id
    items = validated.items

    # Validate PO with warehouse scope at SELECT time (V-026).
    # v1.5.0 #119: FOR UPDATE holds a row lock on the purchase_orders
    # aggregate for the rest of this transaction so two concurrent
    # receives against the same PO produce per-aggregate FIFO on the
    # integration_events outbox. The PO line lock (V-029) below is
    # stricter than this one on its own axis; both hold.
    scope_clause, scope_params = warehouse_scope_clause("warehouse_id")
    po = g.db.execute(
        text(
            f"""
            SELECT po_id, status, warehouse_id, external_id FROM purchase_orders
            WHERE po_id = :po_id {scope_clause}
            FOR UPDATE
            """
        ),
        {"po_id": po_id, **scope_params},
    ).fetchone()

    if not po:
        return jsonify({"error": "Purchase order not found"}), 404

    if po.status not in (PO_OPEN, PO_PARTIAL):
        return jsonify({"error": f"Purchase order status is {po.status}, cannot receive"}), 400

    warehouse_id = po.warehouse_id
    username = g.current_user["username"]
    receipt_ids = []
    warnings = []
    # Collected by release_satisfiable_backorders per
    # flipped BO, drained after g.db.commit() to fire Teams cards.
    _deferred_notifications: list = []

    # Hoist request-constant external-id resolution out of the per-item
    # loop. The PO and the user are the same for every item in this
    # request, so resolve each once and reuse the cached value in every
    # item's emit_event payload. (Same values as before, fewer round
    # trips.)
    po_external_id = (
        resolve_source_external_id(g.db, "purchase_order", po.external_id)
        or str(po.external_id)
    )
    completed_by_user_external_id = get_user_external_id(g.db, username)

    # Per-request memo for item external-id resolution so a repeated
    # item_id resolves once.
    _item_external_id_cache: dict = {}

    # Per-request memo for bin validation. The turbo receive client sends
    # the same receiving bin for every item in a batch, so without this the
    # same (existence + warehouse-match) lookup runs once per item. Only
    # valid bins are cached; an invalid bin short-circuits below with the
    # same error as the unmemoized path, so behavior is unchanged.
    _validated_bin_warehouse: dict = {}

    # v1.5.0 #112 audit window shrink: instead of writing each item's
    # audit_log row mid-loop (which fires the chain-hash trigger and
    # takes LOCK TABLE audit_log_chain_head IN EXCLUSIVE MODE, held to
    # COMMIT), collect each item's audit args here and write them all
    # near the end, just before the final PO-status update + commit.
    # quantity_received_before is captured at loop time (pre-call value)
    # so the row stays self-contained and identical to the old behavior.
    _pending_audit_rows: list = []

    try:
        request_serials = [serial for entry in items for serial in normalise_serials(entry.serial_number)]
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 422
    if len(request_serials) != len(set(request_serials)):
        return jsonify({"error": "A serial number cannot occur twice in this receipt"}), 422
    lock_native_serials(g.db, request_serials)

    for item_entry in items:
        item_id = item_entry.item_id
        quantity = item_entry.quantity
        bin_id = item_entry.bin_id
        lot_number = item_entry.lot_number
        serials = normalise_serials(item_entry.serial_number)
        serial_number = "\n".join(serials) or None
        notes = item_entry.notes
        for serial in serials:
            position = latest_native_serial_position(g.db, item_id, serial)
            if position is not None and position["warehouse_id"] is not None:
                return jsonify({"error": "A received serial number is already present in stock"}), 409

        # Validate bin exists and belongs to PO warehouse. Memoized per
        # request (_validated_bin_warehouse): a repeated bin_id -- the turbo
        # client's single receiving bin -- skips the round trip. Invalid
        # bins are never cached; they return here with the same error as
        # the unmemoized path.
        if bin_id not in _validated_bin_warehouse:
            bin_row = g.db.execute(
                text("SELECT bin_id, warehouse_id FROM bins WHERE bin_id = :bin_id"),
                {"bin_id": bin_id},
            ).fetchone()
            if not bin_row:
                return jsonify({"error": f"Bin {bin_id} not found"}), 404
            if bin_row.warehouse_id != warehouse_id:
                return jsonify({"error": f"Bin {bin_id} does not belong to this PO's warehouse"}), 400
            _validated_bin_warehouse[bin_id] = bin_row.warehouse_id

        # Find matching PO line. V-029: SELECT ... FOR UPDATE holds a
        # row lock for the remainder of this transaction so two
        # concurrent receives against the same line cannot both pass
        # the remaining-quantity check. The second waits until the
        # first commits (seeing updated quantity_received) or rolls
        # back (seeing the original).
        line_params = {"po_id": po_id, "item_id": item_id}
        line_clause = ""
        if item_entry.po_line_id is not None:
            line_clause = " AND po_line_id = :po_line_id"
            line_params["po_line_id"] = item_entry.po_line_id
        po_lines = g.db.execute(
            text(
                f"""
                SELECT po_line_id, line_number, quantity_ordered, quantity_received
                FROM purchase_order_lines
                WHERE po_id = :po_id AND item_id = :item_id {line_clause}
                LIMIT 2
                FOR UPDATE
                """
            ),
            line_params,
        ).fetchall()

        if not po_lines:
            return jsonify({"error": f"Item {item_id} is not on PO {po_id}"}), 400
        if len(po_lines) != 1:
            return jsonify({
                "error": "This item occurs on multiple PO lines; supply po_line_id.",
                "error_kind": "ambiguous_po_line",
            }), 409
        po_line = po_lines[0]
        if (item_entry.expected_quantity_received is not None
                and po_line.quantity_received != item_entry.expected_quantity_received):
            return jsonify({
                "error": "The PO line received quantity changed; reload before receiving.",
                "error_kind": "po_line_changed",
                "quantity_received": po_line.quantity_received,
            }), 409

        # Check for over-receipt
        remaining = po_line.quantity_ordered - po_line.quantity_received
        if quantity > remaining:
            over_receipt_row = g.db.execute(
                text("SELECT value FROM app_settings WHERE key = 'allow_over_receipt'")
            ).fetchone()
            if not over_receipt_row or over_receipt_row.value != "true":
                return jsonify({
                    "error": f"Over-receipt not allowed on line {po_line.line_number}: "
                             f"requested {quantity} but only {remaining} remaining. "
                             f"Enable 'allow_over_receipt' in Settings to permit this."
                }), 400
            warnings.append(
                f"Over-receipt on line {po_line.line_number}: received {quantity} but only {remaining} remaining"
            )

        # 1. Create item_receipts record
        result = g.db.execute(
            text(
                """
                INSERT INTO item_receipts (po_id, po_line_id, item_id, quantity_received, bin_id,
                                           warehouse_id, lot_number, serial_number, received_by, notes, external_id)
                VALUES (:po_id, :po_line_id, :item_id, :quantity, :bin_id,
                        :warehouse_id, :lot_number, :serial_number, :received_by, :notes, :ext_id)
                RETURNING receipt_id, external_id, received_at
                """
            ),
            {
                "po_id": po_id,
                "po_line_id": po_line.po_line_id,
                "item_id": item_id,
                "quantity": quantity,
                "bin_id": bin_id,
                "warehouse_id": warehouse_id,
                "lot_number": lot_number,
                "serial_number": serial_number,
                "received_by": username,
                "notes": notes,
                "ext_id": str(uuid.uuid4()),
            },
        )
        receipt_row = result.fetchone()
        receipt_id = receipt_row.receipt_id
        receipt_external_id = receipt_row.external_id
        receipt_at = receipt_row.received_at
        receipt_ids.append(receipt_id)

        # Look up the item's canonical UUID, then resolve it to the
        # source-system external_id (the SKU the upstream pusher used at
        # Pipe B time) for the wire payload. Falls back to canonical UUID
        # when no upstream mapping exists. Memoized per request so a
        # repeated item_id resolves once.
        if item_id in _item_external_id_cache:
            item_external_id = _item_external_id_cache[item_id]
        else:
            item_row = g.db.execute(
                text("SELECT external_id FROM items WHERE item_id = :iid"),
                {"iid": item_id},
            ).fetchone()
            item_canonical = item_row.external_id if item_row else None
            item_external_id = (
                resolve_source_external_id(g.db, "item", item_canonical)
                or (str(item_canonical) if item_canonical else None)
            )
            _item_external_id_cache[item_id] = item_external_id

        # 2 & 3. Update PO line quantity and status
        new_qty_received = po_line.quantity_received + quantity
        new_line_status = POL_RECEIVED if new_qty_received >= po_line.quantity_ordered else POL_PARTIAL
        g.db.execute(
            text(
                """
                UPDATE purchase_order_lines
                SET quantity_received = :qty, status = :status
                WHERE po_line_id = :po_line_id
                """
            ),
            {"qty": new_qty_received, "status": new_line_status, "po_line_id": po_line.po_line_id},
        )

        # 4. Create or update inventory
        add_inventory(g.db, item_id, bin_id, warehouse_id, quantity, lot_number)

        # 5. Audit log (deferred)
        # quantity_ordered + quantity_received_before make the row
        # self-contained: ordered total / cumulative-before-this-call /
        # this transaction. Investigators can reconstruct PO progress
        # from one row without joining purchase_order_lines.
        # quantity_received_before is captured HERE (the pre-call value
        # of po_line.quantity_received) so the deferred write below
        # records the same number it would have mid-loop. The write
        # itself is deferred to the end of the handler so the chain-hash
        # trigger's EXCLUSIVE table lock is held for the shortest window
        # rather than across the rest of the loop + backorder matcher.
        _pending_audit_rows.append(
            {
                "quantity_ordered": po_line.quantity_ordered,
                "quantity_received_before": po_line.quantity_received,
                "quantity": quantity,
                "item_id": item_id,
                "bin_id": bin_id,
                "receipt_id": receipt_id,
            }
        )

        # 6. v1.5.0 #112: emit receipt.completed on the integration_events
        # outbox. One event per item_receipts row; lines[] is a
        # single-element array in v1.5.0 (Sentry does not batch items
        # into one receipt). Visible_at is set at COMMIT by the deferred
        # trigger so readers see events in commit order.
        emit_event(
            g.db,
            event_type="receipt.completed",
            event_version=1,
            aggregate_type="item_receipt",
            aggregate_id=receipt_id,
            aggregate_external_id=receipt_external_id,
            warehouse_id=warehouse_id,
            source_txn_id=g.source_txn_id,
            payload={
                # receipt_external_id stays canonical (no upstream system
                # creates receipts; they originate inside Sentry).
                "receipt_external_id": str(receipt_external_id),
                # po_external_id resolves to the source-system PO identifier
                # (e.g. our PO number) when one was Pipe-B'd in. Resolved
                # once before the loop; reused here.
                "po_external_id": po_external_id,
                "lines": [
                    {
                        "item_external_id": item_external_id,
                        "quantity_received": quantity,
                        "lot_number": lot_number,
                        "serial_number": serial_number,
                    }
                ],
                "completed_by_user_external_id": completed_by_user_external_id,
                "completed_at": receipt_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        )

        # Flip any WAITING_STOCK backorders in this warehouse
        # that this receipt makes fully satisfiable. Same transaction;
        # the partial index from mig 067 covers the candidate lookup.
        # Notifications are collected here and fired AFTER the commit
        # below so a fire-and-forget Teams send does not block the
        # receipt's HTTP response.
        release_satisfiable_backorders(
            g.db,
            warehouse_id=warehouse_id,
            item_id=item_id,
            source_txn_id=g.source_txn_id,
            deferred_notifications=_deferred_notifications,
            source=RELEASE_SOURCE_RECEIPT,
        )

    # Deferred audit writes. Done here -- after the per-item loop and
    # its backorder-matcher work -- so the chain-hash trigger's
    # LOCK TABLE audit_log_chain_head IN EXCLUSIVE MODE (held until
    # COMMIT) is acquired as late as possible, shrinking the window the
    # global lock is held. Written in per-item order so the chain mirrors
    # receipt order, identical to the previous mid-loop behavior.
    for audit_details in _pending_audit_rows:
        write_audit_log(
            g.db,
            action_type=ACTION_RECEIVE,
            entity_type="PO",
            entity_id=po_id,
            user_id=username,
            warehouse_id=warehouse_id,
            details=audit_details,
        )

    # Update PO status based on all lines
    all_lines = g.db.execute(
        text("SELECT status FROM purchase_order_lines WHERE po_id = :po_id"),
        {"po_id": po_id},
    ).fetchall()

    if all(l.status == POL_RECEIVED for l in all_lines):
        g.db.execute(
            text("UPDATE purchase_orders SET status = :status, received_at = NOW() WHERE po_id = :po_id"),
            {"po_id": po_id, "status": PO_RECEIVED},
        )
        po_status = PO_RECEIVED
    else:
        g.db.execute(
            text("UPDATE purchase_orders SET status = :status WHERE po_id = :po_id"),
            {"po_id": po_id, "status": PO_PARTIAL},
        )
        po_status = PO_PARTIAL

    g.db.commit()

    # Drain backorder.fulfillable notifications collected by
    # release_satisfiable_backorders. Fire-and-forget Teams cards
    # spawned in daemon threads.
    for event_type, payload, wid in _deferred_notifications:
        dispatch_backorder_notification(
            event_type=event_type,
            payload=payload,
            warehouse_id=wid,
        )

    return jsonify({
        "message": "Receipt submitted successfully",
        "receipt_ids": receipt_ids,
        "po_status": po_status,
        "warnings": warnings,
    })


@receiving_bp.route("/cancel", methods=["POST"])
@require_auth
@validate_body(CancelReceivingRequest)
@with_db
def cancel_receiving(validated):
    """Undo all receipts from a session by receipt_ids.

    Reverses inventory additions, PO line quantities, and deletes receipt records.
    Used when user cancels a receiving session.
    """
    receipt_ids = validated.receipt_ids
    if not receipt_ids:
        return jsonify({"message": "Nothing to cancel"}), 200

    username = g.current_user["username"]
    reversed_count = 0

    # Collect PO IDs before deleting receipts
    po_ids = set()
    po_ids.add(validated.po_id)

    scope_clause, scope_params = warehouse_scope_clause("warehouse_id")

    # Pre-flight cross-PO guard. The mobile receive screen accumulates
    # receipt_ids across every PO loaded in a session (sessionReceiptIds
    # is never reset between POs), so a single Cancel tap can sweep
    # receipts belonging to POs the operator never intended to touch.
    # Reject the whole call if any visible receipt belongs to a different
    # po_id; refuse before any state mutation so a partial reversal is
    # not possible. Receipts in another warehouse are filtered out by
    # scope_clause (V-026) and are not considered mismatched here, which
    # preserves the existing silent-skip behaviour for cross-warehouse
    # IDs in the loop below.
    mismatched = g.db.execute(
        text(
            f"""
            SELECT receipt_id, po_id FROM item_receipts
             WHERE receipt_id = ANY(:rids)
               AND po_id != :po_id
               {scope_clause}
             LIMIT 20
            """
        ),
        {"rids": receipt_ids, "po_id": validated.po_id, **scope_params},
    ).fetchall()
    if mismatched:
        return jsonify({
            "error": (
                "receipt_ids span multiple purchase orders; refusing to "
                "cross-cancel. Cancel one PO at a time."
            ),
            "po_id": validated.po_id,
            "mismatched_receipt_ids": [r.receipt_id for r in mismatched],
        }), 400

    # V-025: derive audit warehouse_id from the receipt rows themselves,
    # never from the attacker-controlled request body. Track per-
    # warehouse reversals so each audit row has a trustworthy
    # warehouse_id + reversed-receipt list.
    reversed_by_warehouse: dict[int, list[int]] = {}
    for rid in receipt_ids:
        # V-026: scoped SELECT means a receipt in another warehouse is
        # indistinguishable from a receipt that doesn't exist (both
        # silently skipped, not 403).
        receipt = g.db.execute(
            text(
                f"""
                SELECT receipt_id, po_id, po_line_id, item_id, quantity_received,
                       bin_id, warehouse_id
                FROM item_receipts
                WHERE receipt_id = :rid {scope_clause}
                """
            ),
            {"rid": rid, **scope_params},
        ).fetchone()
        if not receipt:
            continue

        po_ids.add(receipt.po_id)
        reversed_by_warehouse.setdefault(receipt.warehouse_id, []).append(rid)

        # 1. Reverse inventory
        g.db.execute(
            text("""
                UPDATE inventory SET quantity_on_hand = GREATEST(0, quantity_on_hand - :qty)
                WHERE item_id = :iid AND bin_id = :bid AND warehouse_id = :wid
            """),
            {"qty": receipt.quantity_received, "iid": receipt.item_id, "bid": receipt.bin_id, "wid": receipt.warehouse_id},
        )

        # 2. Reverse PO line quantity
        g.db.execute(
            text("""
                UPDATE purchase_order_lines
                SET quantity_received = GREATEST(0, quantity_received - :qty),
                    status = CASE WHEN GREATEST(0, quantity_received - :qty) = 0 THEN :pol_pending
                                  WHEN GREATEST(0, quantity_received - :qty) >= quantity_ordered THEN :pol_received
                                  ELSE :pol_partial END
                WHERE po_line_id = :plid
            """),
            {"qty": receipt.quantity_received, "plid": receipt.po_line_id,
             "pol_pending": POL_PENDING, "pol_received": POL_RECEIVED, "pol_partial": POL_PARTIAL},
        )

        # 3. Delete receipt record
        g.db.execute(text("DELETE FROM item_receipts WHERE receipt_id = :rid"), {"rid": rid})

        reversed_count += 1

    for pid in po_ids:
        all_lines = g.db.execute(
            text("SELECT quantity_received, quantity_ordered FROM purchase_order_lines WHERE po_id = :pid"),
            {"pid": pid},
        ).fetchall()
        if all(l.quantity_received >= l.quantity_ordered for l in all_lines):
            new_status = PO_RECEIVED
        elif any(l.quantity_received > 0 for l in all_lines):
            new_status = PO_PARTIAL
        else:
            new_status = PO_OPEN
        g.db.execute(
            text("UPDATE purchase_orders SET status = :status WHERE po_id = :pid"),
            {"status": new_status, "pid": pid},
        )

    # V-025: one audit row per warehouse involved, each with the
    # authoritative warehouse_id from the receipt rows rather than the
    # request body. Never fall back to validated.warehouse_id.
    for wh_id, rids in reversed_by_warehouse.items():
        write_audit_log(
            g.db,
            action_type=ACTION_RECEIVE_CANCEL,
            entity_type="PO",
            entity_id=validated.po_id or 0,
            user_id=username,
            warehouse_id=wh_id,
            details={"reversed_receipts": len(rids), "receipt_ids": rids},
        )

    g.db.commit()
    return jsonify({"message": f"Cancelled {reversed_count} receipt(s)", "reversed": reversed_count})
