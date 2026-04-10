"""
Receiving endpoints: PO lookup and item receipt submission.
"""

from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_auth
from middleware.db import with_db
from services.audit_service import write_audit_log
from services.inventory_service import add_inventory

receiving_bp = Blueprint("receiving", __name__)


@receiving_bp.route("/po/<barcode>")
@require_auth
@with_db
def lookup_po(barcode):
    po = g.db.execute(
        text(
            """
            SELECT po_id, po_number, po_barcode, vendor_name, vendor_id,
                   status, expected_date, warehouse_id, notes, created_at,
                   received_at, created_by
            FROM purchase_orders
            WHERE po_barcode = :barcode OR po_number = :barcode
            LIMIT 1
            """
        ),
        {"barcode": barcode},
    ).fetchone()

    if not po:
        return jsonify({"error": "Purchase order not found"}), 404

    if po.status == "CLOSED":
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
@with_db
def receive_items():
    data = request.get_json()
    if not data or not data.get("po_id") or not data.get("items"):
        return jsonify({"error": "po_id and items are required"}), 400

    po_id = data["po_id"]
    items = data["items"]

    # Validate PO
    po = g.db.execute(
        text("SELECT po_id, status, warehouse_id FROM purchase_orders WHERE po_id = :po_id"),
        {"po_id": po_id},
    ).fetchone()

    if not po:
        return jsonify({"error": "Purchase order not found"}), 404

    if po.status not in ("OPEN", "PARTIAL"):
        return jsonify({"error": f"Purchase order status is {po.status}, cannot receive"}), 400

    warehouse_id = po.warehouse_id
    username = g.current_user["username"]
    receipt_ids = []
    warnings = []

    for item_entry in items:
        item_id = item_entry.get("item_id")
        quantity = item_entry.get("quantity", 0)
        bin_id = item_entry.get("bin_id")
        lot_number = item_entry.get("lot_number")
        serial_number = item_entry.get("serial_number")
        notes = item_entry.get("notes")

        if not item_id or quantity <= 0:
            return jsonify({"error": "Each item must have item_id and quantity > 0"}), 400

        # Validate bin exists
        bin_row = g.db.execute(
            text("SELECT bin_id FROM bins WHERE bin_id = :bin_id"),
            {"bin_id": bin_id},
        ).fetchone()
        if not bin_row:
            return jsonify({"error": f"Bin {bin_id} not found"}), 404

        # Find matching PO line
        po_line = g.db.execute(
            text(
                """
                SELECT po_line_id, line_number, quantity_ordered, quantity_received
                FROM purchase_order_lines
                WHERE po_id = :po_id AND item_id = :item_id
                LIMIT 1
                """
            ),
            {"po_id": po_id, "item_id": item_id},
        ).fetchone()

        if not po_line:
            return jsonify({"error": f"Item {item_id} is not on PO {po_id}"}), 400

        # Check for over-receipt
        remaining = po_line.quantity_ordered - po_line.quantity_received
        if quantity > remaining:
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
        new_line_status = "RECEIVED" if new_qty_received >= po_line.quantity_ordered else "PARTIAL"
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
            action_type="RECEIVE",
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

    if all(l.status == "RECEIVED" for l in all_lines):
        g.db.execute(
            text("UPDATE purchase_orders SET status = 'RECEIVED', received_at = NOW() WHERE po_id = :po_id"),
            {"po_id": po_id},
        )
        po_status = "RECEIVED"
    else:
        g.db.execute(
            text("UPDATE purchase_orders SET status = 'PARTIAL' WHERE po_id = :po_id"),
            {"po_id": po_id},
        )
        po_status = "PARTIAL"

    g.db.commit()

    return jsonify({
        "message": "Receipt submitted successfully",
        "receipt_ids": receipt_ids,
        "po_status": po_status,
        "warnings": warnings,
    })


@receiving_bp.route("/cancel", methods=["POST"])
@require_auth
@with_db
def cancel_receiving():
    """Undo all receipts from a session by receipt_ids.

    Reverses inventory additions, PO line quantities, and deletes receipt records.
    Used when user cancels a receiving session.
    """
    data = request.get_json()
    receipt_ids = data.get("receipt_ids", [])
    if not receipt_ids:
        return jsonify({"message": "Nothing to cancel"}), 200

    username = g.current_user["username"]
    reversed_count = 0

    for rid in receipt_ids:
        receipt = g.db.execute(
            text("SELECT receipt_id, po_id, po_line_id, item_id, quantity_received, bin_id, warehouse_id FROM item_receipts WHERE receipt_id = :rid"),
            {"rid": rid},
        ).fetchone()
        if not receipt:
            continue

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
                    status = CASE WHEN GREATEST(0, quantity_received - :qty) = 0 THEN 'OPEN'
                                  WHEN GREATEST(0, quantity_received - :qty) >= quantity_ordered THEN 'RECEIVED'
                                  ELSE 'PARTIAL' END
                WHERE po_line_id = :plid
            """),
            {"qty": receipt.quantity_received, "plid": receipt.po_line_id},
        )

        # 3. Delete receipt record
        g.db.execute(text("DELETE FROM item_receipts WHERE receipt_id = :rid"), {"rid": rid})

        reversed_count += 1

    # Recalculate PO status for affected POs
    affected_pos = set()
    for rid in receipt_ids:
        # We already deleted the receipts, so get PO ID from data
        pass

    # Recalculate PO status from the receipt_ids we were given
    po_ids = set()
    for rid in receipt_ids:
        po_row = g.db.execute(
            text("SELECT DISTINCT po_id FROM purchase_order_lines WHERE po_line_id IN (SELECT po_line_id FROM item_receipts WHERE receipt_id = :rid)"),
            {"rid": rid},
        ).fetchone()
        if po_row:
            po_ids.add(po_row.po_id)

    # Also get PO IDs from the data if provided
    if data.get("po_id"):
        po_ids.add(data["po_id"])

    for pid in po_ids:
        all_lines = g.db.execute(
            text("SELECT quantity_received, quantity_ordered FROM purchase_order_lines WHERE po_id = :pid"),
            {"pid": pid},
        ).fetchall()
        if all(l.quantity_received >= l.quantity_ordered for l in all_lines):
            new_status = "RECEIVED"
        elif any(l.quantity_received > 0 for l in all_lines):
            new_status = "PARTIAL"
        else:
            new_status = "OPEN"
        g.db.execute(
            text("UPDATE purchase_orders SET status = :status WHERE po_id = :pid"),
            {"status": new_status, "pid": pid},
        )

    # Audit log
    write_audit_log(
        g.db,
        action_type="RECEIVE_CANCEL",
        entity_type="PO",
        entity_id=data.get("po_id", 0),
        user_id=username,
        warehouse_id=data.get("warehouse_id"),
        details={"reversed_receipts": reversed_count, "receipt_ids": receipt_ids},
    )

    g.db.commit()
    return jsonify({"message": f"Cancelled {reversed_count} receipt(s)", "reversed": reversed_count})
