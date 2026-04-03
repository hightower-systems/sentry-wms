"""
Bin transfer endpoint: general-purpose bin-to-bin inventory moves.
"""

from flask import Blueprint, g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_auth
from models.database import get_db
from services.audit_service import write_audit_log

transfers_bp = Blueprint("transfers", __name__)


@transfers_bp.route("/move", methods=["POST"])
@require_auth
def move():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    item_id = data.get("item_id")
    from_bin_id = data.get("from_bin_id")
    to_bin_id = data.get("to_bin_id")
    quantity = data.get("quantity", 0)
    reason = data.get("reason")
    lot_number = data.get("lot_number")

    if not item_id or not from_bin_id or not to_bin_id:
        return jsonify({"error": "item_id, from_bin_id, and to_bin_id are required"}), 400
    if quantity <= 0:
        return jsonify({"error": "Quantity must be greater than 0"}), 400
    if from_bin_id == to_bin_id:
        return jsonify({"error": "from_bin_id and to_bin_id must be different"}), 400

    db = next(get_db())
    try:
        # Validate item
        item = db.execute(
            text("SELECT item_id, sku, item_name FROM items WHERE item_id = :iid"),
            {"iid": item_id},
        ).fetchone()
        if not item:
            return jsonify({"error": "Item not found"}), 404

        # Validate bins
        from_bin = db.execute(
            text("SELECT bin_id, bin_code, warehouse_id FROM bins WHERE bin_id = :bid"),
            {"bid": from_bin_id},
        ).fetchone()
        if not from_bin:
            return jsonify({"error": "Source bin not found"}), 404

        to_bin = db.execute(
            text("SELECT bin_id, bin_code FROM bins WHERE bin_id = :bid"),
            {"bid": to_bin_id},
        ).fetchone()
        if not to_bin:
            return jsonify({"error": "Destination bin not found"}), 404

        # Check source inventory
        source_inv = db.execute(
            text(
                """
                SELECT inventory_id, quantity_on_hand
                FROM inventory
                WHERE item_id = :iid AND bin_id = :bid
                  AND lot_number IS NOT DISTINCT FROM :lot
                """
            ),
            {"iid": item_id, "bid": from_bin_id, "lot": lot_number},
        ).fetchone()

        if not source_inv or source_inv.quantity_on_hand < quantity:
            available = source_inv.quantity_on_hand if source_inv else 0
            return jsonify({"error": f"Insufficient quantity. Available in bin: {available}"}), 400

        username = g.current_user["username"]
        warehouse_id = from_bin.warehouse_id

        # 1. Decrement source
        new_source_qty = source_inv.quantity_on_hand - quantity
        if new_source_qty == 0:
            db.execute(
                text("DELETE FROM inventory WHERE inventory_id = :inv_id"),
                {"inv_id": source_inv.inventory_id},
            )
        else:
            db.execute(
                text("UPDATE inventory SET quantity_on_hand = :qty, updated_at = NOW() WHERE inventory_id = :inv_id"),
                {"qty": new_source_qty, "inv_id": source_inv.inventory_id},
            )

        # 2. Create or update destination
        dest_inv = db.execute(
            text(
                """
                SELECT inventory_id, quantity_on_hand
                FROM inventory
                WHERE item_id = :iid AND bin_id = :bid
                  AND lot_number IS NOT DISTINCT FROM :lot
                """
            ),
            {"iid": item_id, "bid": to_bin_id, "lot": lot_number},
        ).fetchone()

        if dest_inv:
            new_dest_qty = dest_inv.quantity_on_hand + quantity
            db.execute(
                text("UPDATE inventory SET quantity_on_hand = :qty, updated_at = NOW() WHERE inventory_id = :inv_id"),
                {"qty": new_dest_qty, "inv_id": dest_inv.inventory_id},
            )
        else:
            new_dest_qty = quantity
            db.execute(
                text(
                    """
                    INSERT INTO inventory (item_id, bin_id, warehouse_id, quantity_on_hand, lot_number)
                    VALUES (:iid, :bid, :wh, :qty, :lot)
                    """
                ),
                {"iid": item_id, "bid": to_bin_id, "wh": warehouse_id, "qty": quantity, "lot": lot_number},
            )

        # 3. Create bin_transfers record
        result = db.execute(
            text(
                """
                INSERT INTO bin_transfers (item_id, from_bin_id, to_bin_id, warehouse_id, quantity,
                                           transfer_type, lot_number, reason, transferred_by)
                VALUES (:iid, :from_bid, :to_bid, :wh, :qty, 'MOVE', :lot, :reason, :user)
                RETURNING transfer_id
                """
            ),
            {
                "iid": item_id,
                "from_bid": from_bin_id,
                "to_bid": to_bin_id,
                "wh": warehouse_id,
                "qty": quantity,
                "lot": lot_number,
                "reason": reason,
                "user": username,
            },
        )
        transfer_id = result.fetchone()[0]

        # 4. Audit log
        write_audit_log(
            db,
            action_type="TRANSFER",
            entity_type="ITEM",
            entity_id=item_id,
            user_id=username,
            warehouse_id=warehouse_id,
            details={
                "from_bin_id": from_bin_id,
                "from_bin_code": from_bin.bin_code,
                "to_bin_id": to_bin_id,
                "to_bin_code": to_bin.bin_code,
                "quantity": quantity,
                "reason": reason,
                "transfer_id": transfer_id,
            },
        )

        # 5. Commit
        db.commit()

        return jsonify({
            "message": "Transfer completed",
            "transfer_id": transfer_id,
            "item": {
                "sku": item.sku,
                "item_name": item.item_name,
            },
            "from_bin": {
                "bin_code": from_bin.bin_code,
                "remaining_quantity": new_source_qty,
            },
            "to_bin": {
                "bin_code": to_bin.bin_code,
                "new_quantity": new_dest_qty,
            },
            "quantity_moved": quantity,
        })
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
