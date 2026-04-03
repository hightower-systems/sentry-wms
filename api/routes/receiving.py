"""
Receiving endpoints: PO lookup and item receipt submission.
"""

from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_auth
from models.database import get_db
from services.audit_service import write_audit_log

receiving_bp = Blueprint("receiving", __name__)


@receiving_bp.route("/po/<barcode>")
@require_auth
def lookup_po(barcode):
    db = next(get_db())
    try:
        po = db.execute(
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

        lines = db.execute(
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
    finally:
        db.close()


@receiving_bp.route("/receive", methods=["POST"])
@require_auth
def receive_items():
    data = request.get_json()
    if not data or not data.get("po_id") or not data.get("items"):
        return jsonify({"error": "po_id and items are required"}), 400

    po_id = data["po_id"]
    items = data["items"]

    db = next(get_db())
    try:
        # Validate PO
        po = db.execute(
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
            bin_row = db.execute(
                text("SELECT bin_id FROM bins WHERE bin_id = :bin_id"),
                {"bin_id": bin_id},
            ).fetchone()
            if not bin_row:
                return jsonify({"error": f"Bin {bin_id} not found"}), 404

            # Find matching PO line
            po_line = db.execute(
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
            result = db.execute(
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
            db.execute(
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
            existing_inv = db.execute(
                text(
                    """
                    SELECT inventory_id, quantity_on_hand
                    FROM inventory
                    WHERE item_id = :item_id AND bin_id = :bin_id
                      AND lot_number IS NOT DISTINCT FROM :lot_number
                    """
                ),
                {"item_id": item_id, "bin_id": bin_id, "lot_number": lot_number},
            ).fetchone()

            if existing_inv:
                db.execute(
                    text(
                        """
                        UPDATE inventory
                        SET quantity_on_hand = quantity_on_hand + :qty, updated_at = NOW()
                        WHERE inventory_id = :inv_id
                        """
                    ),
                    {"qty": quantity, "inv_id": existing_inv.inventory_id},
                )
            else:
                db.execute(
                    text(
                        """
                        INSERT INTO inventory (item_id, bin_id, warehouse_id, quantity_on_hand, lot_number)
                        VALUES (:item_id, :bin_id, :warehouse_id, :qty, :lot_number)
                        """
                    ),
                    {"item_id": item_id, "bin_id": bin_id, "warehouse_id": warehouse_id, "qty": quantity, "lot_number": lot_number},
                )

            # 5. Audit log
            write_audit_log(
                db,
                action_type="RECEIVE",
                entity_type="PO",
                entity_id=po_id,
                user_id=username,
                warehouse_id=warehouse_id,
                details={"item_id": item_id, "quantity": quantity, "bin_id": bin_id, "receipt_id": receipt_id},
            )

        # Update PO status based on all lines
        all_lines = db.execute(
            text("SELECT status FROM purchase_order_lines WHERE po_id = :po_id"),
            {"po_id": po_id},
        ).fetchall()

        if all(l.status == "RECEIVED" for l in all_lines):
            db.execute(
                text("UPDATE purchase_orders SET status = 'RECEIVED', received_at = NOW() WHERE po_id = :po_id"),
                {"po_id": po_id},
            )
            po_status = "RECEIVED"
        else:
            db.execute(
                text("UPDATE purchase_orders SET status = 'PARTIAL' WHERE po_id = :po_id"),
                {"po_id": po_id},
            )
            po_status = "PARTIAL"

        db.commit()

        return jsonify({
            "message": "Receipt submitted successfully",
            "receipt_ids": receipt_ids,
            "po_status": po_status,
            "warnings": warnings,
        })
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
