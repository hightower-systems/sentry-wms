"""
Put-away endpoints: pending items, bin suggestion, and confirm transfer.
"""

from flask import Blueprint, g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_auth
from models.database import get_db
from services.audit_service import write_audit_log

putaway_bp = Blueprint("putaway", __name__)


@putaway_bp.route("/pending/<int:warehouse_id>")
@require_auth
def pending_putaway(warehouse_id):
    db = next(get_db())
    try:
        rows = db.execute(
            text(
                """
                SELECT inv.inventory_id, inv.item_id, i.sku, i.item_name, i.upc,
                       inv.quantity_on_hand AS quantity, inv.bin_id, b.bin_code,
                       inv.lot_number
                FROM inventory inv
                JOIN items i ON i.item_id = inv.item_id
                JOIN bins b ON b.bin_id = inv.bin_id
                WHERE b.bin_type = 'INBOUND_STAGING'
                  AND inv.quantity_on_hand > 0
                  AND inv.warehouse_id = :warehouse_id
                """
            ),
            {"warehouse_id": warehouse_id},
        ).fetchall()

        return jsonify({
            "pending_items": [
                {
                    "inventory_id": r.inventory_id,
                    "item_id": r.item_id,
                    "sku": r.sku,
                    "item_name": r.item_name,
                    "upc": r.upc,
                    "quantity": r.quantity,
                    "bin_id": r.bin_id,
                    "bin_code": r.bin_code,
                    "lot_number": r.lot_number,
                }
                for r in rows
            ]
        })
    finally:
        db.close()


@putaway_bp.route("/suggest/<int:item_id>")
@require_auth
def suggest_bin(item_id):
    db = next(get_db())
    try:
        # Get item info
        item = db.execute(
            text("SELECT item_id, sku, item_name, default_bin_id FROM items WHERE item_id = :item_id"),
            {"item_id": item_id},
        ).fetchone()

        if not item:
            return jsonify({"error": "Item not found"}), 404

        suggested_bin = None
        alternative_bins = []

        # Check for existing stock in non-staging bins
        stock_bins = db.execute(
            text(
                """
                SELECT inv.bin_id, b.bin_code, b.bin_barcode, z.zone_name,
                       inv.quantity_on_hand AS current_quantity
                FROM inventory inv
                JOIN bins b ON b.bin_id = inv.bin_id
                JOIN zones z ON z.zone_id = b.zone_id
                WHERE inv.item_id = :item_id
                  AND b.bin_type NOT IN ('INBOUND_STAGING', 'OUTBOUND_STAGING')
                  AND inv.quantity_on_hand > 0
                ORDER BY inv.quantity_on_hand DESC
                """
            ),
            {"item_id": item_id},
        ).fetchall()

        alternative_bins = [
            {
                "bin_id": r.bin_id,
                "bin_code": r.bin_code,
                "bin_barcode": r.bin_barcode,
                "zone_name": r.zone_name,
                "current_quantity": r.current_quantity,
                "reason": "Existing stock",
            }
            for r in stock_bins
        ]

        # Priority 1: default bin
        if item.default_bin_id:
            default = db.execute(
                text(
                    """
                    SELECT b.bin_id, b.bin_code, b.bin_barcode, z.zone_name
                    FROM bins b
                    JOIN zones z ON z.zone_id = b.zone_id
                    WHERE b.bin_id = :bin_id
                    """
                ),
                {"bin_id": item.default_bin_id},
            ).fetchone()

            if default:
                suggested_bin = {
                    "bin_id": default.bin_id,
                    "bin_code": default.bin_code,
                    "bin_barcode": default.bin_barcode,
                    "zone_name": default.zone_name,
                    "reason": "Default bin assignment",
                }
        # Priority 2: bin with most existing stock
        elif stock_bins:
            top = stock_bins[0]
            suggested_bin = {
                "bin_id": top.bin_id,
                "bin_code": top.bin_code,
                "bin_barcode": top.bin_barcode,
                "zone_name": top.zone_name,
                "reason": "Existing stock - consolidate",
            }

        return jsonify({
            "item_id": item.item_id,
            "sku": item.sku,
            "item_name": item.item_name,
            "suggested_bin": suggested_bin,
            "alternative_bins": alternative_bins,
        })
    finally:
        db.close()


@putaway_bp.route("/confirm", methods=["POST"])
@require_auth
def confirm_putaway():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    item_id = data.get("item_id")
    from_bin_id = data.get("from_bin_id")
    to_bin_id = data.get("to_bin_id")
    quantity = data.get("quantity", 0)
    lot_number = data.get("lot_number")

    if not item_id or not from_bin_id or not to_bin_id:
        return jsonify({"error": "item_id, from_bin_id, and to_bin_id are required"}), 400

    if quantity <= 0:
        return jsonify({"error": "Quantity must be greater than 0"}), 400

    if from_bin_id == to_bin_id:
        return jsonify({"error": "from_bin_id and to_bin_id must be different"}), 400

    db = next(get_db())
    try:
        # Validate item exists
        item = db.execute(
            text("SELECT item_id, sku FROM items WHERE item_id = :item_id"),
            {"item_id": item_id},
        ).fetchone()
        if not item:
            return jsonify({"error": "Item not found"}), 404

        # Validate bins exist
        from_bin = db.execute(
            text("SELECT bin_id, bin_code, warehouse_id FROM bins WHERE bin_id = :bin_id"),
            {"bin_id": from_bin_id},
        ).fetchone()
        if not from_bin:
            return jsonify({"error": "Source bin not found"}), 404

        to_bin = db.execute(
            text("SELECT bin_id, bin_code FROM bins WHERE bin_id = :bin_id"),
            {"bin_id": to_bin_id},
        ).fetchone()
        if not to_bin:
            return jsonify({"error": "Destination bin not found"}), 404

        # Validate source inventory
        source_inv = db.execute(
            text(
                """
                SELECT inventory_id, quantity_on_hand
                FROM inventory
                WHERE item_id = :item_id AND bin_id = :bin_id
                  AND lot_number IS NOT DISTINCT FROM :lot_number
                """
            ),
            {"item_id": item_id, "bin_id": from_bin_id, "lot_number": lot_number},
        ).fetchone()

        if not source_inv or source_inv.quantity_on_hand < quantity:
            available = source_inv.quantity_on_hand if source_inv else 0
            return jsonify({"error": f"Insufficient inventory in source bin. Available: {available}"}), 400

        username = g.current_user["username"]
        warehouse_id = from_bin.warehouse_id

        # 1. Decrement source inventory
        new_qty = source_inv.quantity_on_hand - quantity
        if new_qty == 0:
            db.execute(
                text("DELETE FROM inventory WHERE inventory_id = :inv_id"),
                {"inv_id": source_inv.inventory_id},
            )
        else:
            db.execute(
                text("UPDATE inventory SET quantity_on_hand = :qty, updated_at = NOW() WHERE inventory_id = :inv_id"),
                {"qty": new_qty, "inv_id": source_inv.inventory_id},
            )

        # 2. Create or update destination inventory
        dest_inv = db.execute(
            text(
                """
                SELECT inventory_id
                FROM inventory
                WHERE item_id = :item_id AND bin_id = :bin_id
                  AND lot_number IS NOT DISTINCT FROM :lot_number
                """
            ),
            {"item_id": item_id, "bin_id": to_bin_id, "lot_number": lot_number},
        ).fetchone()

        if dest_inv:
            db.execute(
                text("UPDATE inventory SET quantity_on_hand = quantity_on_hand + :qty, updated_at = NOW() WHERE inventory_id = :inv_id"),
                {"qty": quantity, "inv_id": dest_inv.inventory_id},
            )
        else:
            db.execute(
                text(
                    """
                    INSERT INTO inventory (item_id, bin_id, warehouse_id, quantity_on_hand, lot_number)
                    VALUES (:item_id, :bin_id, :warehouse_id, :qty, :lot_number)
                    """
                ),
                {"item_id": item_id, "bin_id": to_bin_id, "warehouse_id": warehouse_id, "qty": quantity, "lot_number": lot_number},
            )

        # 3. Create bin_transfers record
        result = db.execute(
            text(
                """
                INSERT INTO bin_transfers (item_id, from_bin_id, to_bin_id, warehouse_id, quantity,
                                           transfer_type, lot_number, transferred_by)
                VALUES (:item_id, :from_bin_id, :to_bin_id, :warehouse_id, :quantity,
                        'PUTAWAY', :lot_number, :transferred_by)
                RETURNING transfer_id
                """
            ),
            {
                "item_id": item_id,
                "from_bin_id": from_bin_id,
                "to_bin_id": to_bin_id,
                "warehouse_id": warehouse_id,
                "quantity": quantity,
                "lot_number": lot_number,
                "transferred_by": username,
            },
        )
        transfer_id = result.fetchone()[0]

        # 4. Audit log
        write_audit_log(
            db,
            action_type="PUTAWAY",
            entity_type="ITEM",
            entity_id=item_id,
            user_id=username,
            warehouse_id=warehouse_id,
            details={
                "from_bin_id": from_bin_id,
                "to_bin_id": to_bin_id,
                "quantity": quantity,
                "transfer_id": transfer_id,
            },
        )

        # 5. Commit
        db.commit()

        return jsonify({
            "message": "Put-away confirmed",
            "transfer_id": transfer_id,
            "item": item.sku,
            "from_bin": from_bin.bin_code,
            "to_bin": to_bin.bin_code,
            "quantity": quantity,
        })
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
