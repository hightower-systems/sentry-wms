"""
Packing endpoints: order lookup for packing, scan-to-verify, and pack completion.
"""

from flask import Blueprint, g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_auth
from models.database import get_db
from services.audit_service import write_audit_log

packing_bp = Blueprint("packing", __name__)


@packing_bp.route("/order/<barcode>")
@require_auth
def get_order(barcode):
    db = next(get_db())
    try:
        so = db.execute(
            text(
                """
                SELECT so_id, so_number, so_barcode, customer_name, status,
                       ship_method, ship_address, warehouse_id
                FROM sales_orders
                WHERE so_barcode = :barcode OR so_number = :barcode
                LIMIT 1
                """
            ),
            {"barcode": barcode},
        ).fetchone()

        if not so:
            return jsonify({"error": "Order not found"}), 404

        if so.status != "PICKING":
            return jsonify({"error": f"Order is not ready for packing. Current status: {so.status}"}), 400

        lines = db.execute(
            text(
                """
                SELECT sol.so_line_id, sol.line_number, sol.item_id,
                       i.sku, i.item_name, i.upc, i.weight_lbs,
                       sol.quantity_ordered, sol.quantity_picked, sol.quantity_packed
                FROM sales_order_lines sol
                JOIN items i ON i.item_id = sol.item_id
                WHERE sol.so_id = :so_id
                ORDER BY sol.line_number
                """
            ),
            {"so_id": so.so_id},
        ).fetchall()

        calculated_weight = 0.0
        total_items = 0
        items_verified = 0
        line_list = []

        for l in lines:
            weight = float(l.weight_lbs) if l.weight_lbs else 0.0
            calculated_weight += weight * l.quantity_picked
            total_items += l.quantity_picked
            verified = l.quantity_packed >= l.quantity_picked
            if verified:
                items_verified += l.quantity_picked

            line_list.append({
                "so_line_id": l.so_line_id,
                "line_number": l.line_number,
                "item_id": l.item_id,
                "sku": l.sku,
                "item_name": l.item_name,
                "upc": l.upc,
                "weight_lbs": weight,
                "quantity_ordered": l.quantity_ordered,
                "quantity_picked": l.quantity_picked,
                "quantity_packed": l.quantity_packed,
                "pack_verified": verified,
            })

        return jsonify({
            "sales_order": {
                "so_id": so.so_id,
                "so_number": so.so_number,
                "so_barcode": so.so_barcode,
                "customer_name": so.customer_name,
                "status": so.status,
                "ship_method": so.ship_method,
                "ship_address": so.ship_address,
                "warehouse_id": so.warehouse_id,
            },
            "lines": line_list,
            "calculated_weight_lbs": round(calculated_weight, 2),
            "total_items": total_items,
            "items_verified": items_verified,
        })
    finally:
        db.close()


@packing_bp.route("/verify", methods=["POST"])
@require_auth
def verify_item():
    data = request.get_json()
    if not data or not data.get("so_id") or not data.get("scanned_barcode"):
        return jsonify({"error": "so_id and scanned_barcode are required"}), 400

    so_id = data["so_id"]
    scanned_barcode = data["scanned_barcode"]
    quantity = data.get("quantity", 1)

    db = next(get_db())
    try:
        # Validate SO
        so = db.execute(
            text("SELECT so_id, status FROM sales_orders WHERE so_id = :so_id"),
            {"so_id": so_id},
        ).fetchone()

        if not so:
            return jsonify({"error": "Order not found"}), 404
        if so.status != "PICKING":
            return jsonify({"error": f"Order is not ready for packing. Current status: {so.status}"}), 400

        # Find matching item on this SO by barcode
        lines = db.execute(
            text(
                """
                SELECT sol.so_line_id, sol.item_id, sol.quantity_picked, sol.quantity_packed,
                       i.sku, i.item_name, i.upc, i.barcode_aliases
                FROM sales_order_lines sol
                JOIN items i ON i.item_id = sol.item_id
                WHERE sol.so_id = :so_id
                """
            ),
            {"so_id": so_id},
        ).fetchall()

        matched_line = None
        for line in lines:
            if _barcode_matches(scanned_barcode, line.upc, line.barcode_aliases):
                if line.quantity_picked > line.quantity_packed:
                    matched_line = line
                    break

        if not matched_line:
            # Check if barcode matches any item on the order at all
            any_match = any(_barcode_matches(scanned_barcode, l.upc, l.barcode_aliases) for l in lines)
            if any_match:
                return jsonify({"error": "Item already fully verified on this order"}), 400
            return jsonify({"error": "Item not found on this order"}), 400

        # Validate quantity
        remaining = matched_line.quantity_picked - matched_line.quantity_packed
        if quantity > remaining:
            return jsonify({"error": f"Over-pack: only {remaining} items remaining to verify"}), 400

        # Update quantity_packed
        new_packed = matched_line.quantity_packed + quantity
        line_complete = new_packed >= matched_line.quantity_picked

        db.execute(
            text(
                """
                UPDATE sales_order_lines
                SET quantity_packed = :qty, status = CASE WHEN :qty >= quantity_picked THEN 'PACKED' ELSE status END
                WHERE so_line_id = :sol_id
                """
            ),
            {"qty": new_packed, "sol_id": matched_line.so_line_id},
        )

        db.commit()

        # Calculate order progress
        updated_lines = db.execute(
            text(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE quantity_packed >= quantity_picked) AS verified
                FROM sales_order_lines WHERE so_id = :so_id
                """
            ),
            {"so_id": so_id},
        ).fetchone()

        return jsonify({
            "message": "Item verified",
            "item": {
                "sku": matched_line.sku,
                "item_name": matched_line.item_name,
                "quantity_verified": quantity,
                "line_complete": line_complete,
            },
            "order_progress": {
                "total_lines": updated_lines.total,
                "lines_verified": updated_lines.verified,
                "all_verified": updated_lines.verified == updated_lines.total,
            },
        })
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@packing_bp.route("/complete", methods=["POST"])
@require_auth
def complete_packing():
    data = request.get_json()
    if not data or not data.get("so_id"):
        return jsonify({"error": "so_id is required"}), 400

    so_id = data["so_id"]
    db = next(get_db())
    try:
        so = db.execute(
            text("SELECT so_id, so_number, status, warehouse_id FROM sales_orders WHERE so_id = :so_id"),
            {"so_id": so_id},
        ).fetchone()

        if not so:
            return jsonify({"error": "Order not found"}), 404
        if so.status != "PICKING":
            return jsonify({"error": f"Order is not ready for packing. Current status: {so.status}"}), 400

        # Check all lines verified
        unverified = db.execute(
            text(
                """
                SELECT COUNT(*) FROM sales_order_lines
                WHERE so_id = :so_id AND quantity_packed < quantity_picked
                """
            ),
            {"so_id": so_id},
        ).scalar()

        if unverified > 0:
            return jsonify({"error": f"Cannot complete packing - {unverified} items not yet verified"}), 400

        # Update SO
        db.execute(
            text("UPDATE sales_orders SET status = 'PACKED', packed_at = NOW() WHERE so_id = :so_id"),
            {"so_id": so_id},
        )

        # Calculate weight and total
        stats = db.execute(
            text(
                """
                SELECT COALESCE(SUM(i.weight_lbs * sol.quantity_picked), 0) AS total_weight,
                       COALESCE(SUM(sol.quantity_picked), 0) AS total_items
                FROM sales_order_lines sol
                JOIN items i ON i.item_id = sol.item_id
                WHERE sol.so_id = :so_id
                """
            ),
            {"so_id": so_id},
        ).fetchone()

        write_audit_log(
            db,
            action_type="PACK",
            entity_type="SO",
            entity_id=so_id,
            user_id=g.current_user["username"],
            warehouse_id=so.warehouse_id,
            details={"so_number": so.so_number, "total_items": int(stats.total_items)},
        )

        db.commit()

        return jsonify({
            "message": "Order packed successfully",
            "so_number": so.so_number,
            "status": "PACKED",
            "total_items": int(stats.total_items),
            "calculated_weight_lbs": round(float(stats.total_weight), 2),
        })
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _barcode_matches(scanned, upc, barcode_aliases):
    if scanned == upc:
        return True
    if barcode_aliases and isinstance(barcode_aliases, list):
        return scanned in barcode_aliases
    return False
