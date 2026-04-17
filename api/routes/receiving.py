"""
Receiving endpoints: PO lookup and item receipt submission.
"""

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
from services.inventory_service import add_inventory
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
                   i.sku, i.item_name, i.upc,
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
    scope_clause, scope_params = warehouse_scope_clause("warehouse_id")
    po = g.db.execute(
        text(
            f"""
            SELECT po_id, status, warehouse_id FROM purchase_orders
            WHERE po_id = :po_id {scope_clause}
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

    for item_entry in items:
        item_id = item_entry.item_id
        quantity = item_entry.quantity
        bin_id = item_entry.bin_id
        lot_number = item_entry.lot_number
        serial_number = item_entry.serial_number
        notes = item_entry.notes

        # Validate bin exists and belongs to PO warehouse
        bin_row = g.db.execute(
            text("SELECT bin_id, warehouse_id FROM bins WHERE bin_id = :bin_id"),
            {"bin_id": bin_id},
        ).fetchone()
        if not bin_row:
            return jsonify({"error": f"Bin {bin_id} not found"}), 404
        if bin_row.warehouse_id != warehouse_id:
            return jsonify({"error": f"Bin {bin_id} does not belong to this PO's warehouse"}), 400

        # Find matching PO line. V-029: SELECT ... FOR UPDATE holds a
        # row lock for the remainder of this transaction so two
        # concurrent receives against the same line cannot both pass
        # the remaining-quantity check. The second waits until the
        # first commits (seeing updated quantity_received) or rolls
        # back (seeing the original).
        po_line = g.db.execute(
            text(
                """
                SELECT po_line_id, line_number, quantity_ordered, quantity_received
                FROM purchase_order_lines
                WHERE po_id = :po_id AND item_id = :item_id
                LIMIT 1
                FOR UPDATE
                """
            ),
            {"po_id": po_id, "item_id": item_id},
        ).fetchone()

        if not po_line:
            return jsonify({"error": f"Item {item_id} is not on PO {po_id}"}), 400

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
                                           warehouse_id, lot_number, serial_number, received_by, notes)
                VALUES (:po_id, :po_line_id, :item_id, :quantity, :bin_id,
                        :warehouse_id, :lot_number, :serial_number, :received_by, :notes)
                RETURNING receipt_id
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
            },
        )
        receipt_id = result.fetchone()[0]
        receipt_ids.append(receipt_id)

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

        # 5. Audit log
        write_audit_log(
            g.db,
            action_type=ACTION_RECEIVE,
            entity_type="PO",
            entity_id=po_id,
            user_id=username,
            warehouse_id=warehouse_id,
            details={"item_id": item_id, "quantity": quantity, "bin_id": bin_id, "receipt_id": receipt_id},
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
    if validated.po_id:
        po_ids.add(validated.po_id)

    scope_clause, scope_params = warehouse_scope_clause("warehouse_id")
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

    # Audit log
    write_audit_log(
        g.db,
        action_type=ACTION_RECEIVE_CANCEL,
        entity_type="PO",
        entity_id=validated.po_id or 0,
        user_id=username,
        warehouse_id=validated.warehouse_id,
        details={"reversed_receipts": reversed_count, "receipt_ids": receipt_ids},
    )

    g.db.commit()
    return jsonify({"message": f"Cancelled {reversed_count} receipt(s)", "reversed": reversed_count})
