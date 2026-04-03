"""
Shipping / fulfillment endpoint: records tracking info and creates fulfillment records.
"""

from flask import Blueprint, g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_auth
from models.database import get_db
from services.audit_service import write_audit_log

shipping_bp = Blueprint("shipping", __name__)


@shipping_bp.route("/fulfill", methods=["POST"])
@require_auth
def fulfill():
    data = request.get_json()
    if not data or not data.get("so_id"):
        return jsonify({"error": "so_id is required"}), 400
    if not data.get("tracking_number"):
        return jsonify({"error": "tracking_number is required"}), 400
    if not data.get("carrier"):
        return jsonify({"error": "carrier is required"}), 400

    so_id = data["so_id"]
    tracking_number = data["tracking_number"]
    carrier = data["carrier"]
    ship_method = data.get("ship_method")
    username = g.current_user["username"]

    db = next(get_db())
    try:
        # Validate SO
        so = db.execute(
            text(
                "SELECT so_id, so_number, status, warehouse_id FROM sales_orders WHERE so_id = :so_id"
            ),
            {"so_id": so_id},
        ).fetchone()

        if not so:
            return jsonify({"error": "Order not found"}), 404
        if so.status != "PACKED":
            return jsonify({"error": f"Order must be packed before shipping. Current status: {so.status}"}), 400

        # 1. Create item_fulfillments record
        result = db.execute(
            text(
                """
                INSERT INTO item_fulfillments (so_id, warehouse_id, tracking_number, carrier, ship_method, shipped_by, status)
                VALUES (:so_id, :wh, :tracking, :carrier, :ship_method, :shipped_by, 'SHIPPED')
                RETURNING fulfillment_id
                """
            ),
            {
                "so_id": so_id,
                "wh": so.warehouse_id,
                "tracking": tracking_number,
                "carrier": carrier,
                "ship_method": ship_method,
                "shipped_by": username,
            },
        )
        fulfillment_id = result.fetchone()[0]

        # 2. Create fulfillment lines for each SO line with quantity_picked > 0
        so_lines = db.execute(
            text(
                """
                SELECT sol.so_line_id, sol.item_id, sol.quantity_picked
                FROM sales_order_lines sol
                WHERE sol.so_id = :so_id AND sol.quantity_picked > 0
                """
            ),
            {"so_id": so_id},
        ).fetchall()

        lines_shipped = 0
        total_quantity = 0

        for line in so_lines:
            # Find bin_id from pick_tasks
            pick_task = db.execute(
                text(
                    """
                    SELECT bin_id FROM pick_tasks
                    WHERE so_id = :so_id AND item_id = :item_id AND status IN ('PICKED', 'SHORT')
                    ORDER BY pick_task_id ASC
                    LIMIT 1
                    """
                ),
                {"so_id": so_id, "item_id": line.item_id},
            ).fetchone()

            bin_id = pick_task.bin_id if pick_task else 1  # fallback shouldn't happen

            db.execute(
                text(
                    """
                    INSERT INTO item_fulfillment_lines (fulfillment_id, so_line_id, item_id, quantity_shipped, bin_id)
                    VALUES (:fid, :sol_id, :item_id, :qty, :bin_id)
                    """
                ),
                {
                    "fid": fulfillment_id,
                    "sol_id": line.so_line_id,
                    "item_id": line.item_id,
                    "qty": line.quantity_picked,
                    "bin_id": bin_id,
                },
            )

            # 3. Update SO line
            db.execute(
                text(
                    "UPDATE sales_order_lines SET quantity_shipped = quantity_picked, status = 'SHIPPED' WHERE so_line_id = :sol_id"
                ),
                {"sol_id": line.so_line_id},
            )

            lines_shipped += 1
            total_quantity += line.quantity_picked

        # 4. Update SO status
        db.execute(
            text("UPDATE sales_orders SET status = 'SHIPPED', shipped_at = NOW() WHERE so_id = :so_id"),
            {"so_id": so_id},
        )

        # 5. Audit log
        write_audit_log(
            db,
            action_type="SHIP",
            entity_type="SO",
            entity_id=so_id,
            user_id=username,
            warehouse_id=so.warehouse_id,
            details={
                "so_number": so.so_number,
                "tracking_number": tracking_number,
                "carrier": carrier,
                "fulfillment_id": fulfillment_id,
            },
        )

        db.commit()

        return jsonify({
            "message": "Shipment fulfilled",
            "fulfillment_id": fulfillment_id,
            "so_number": so.so_number,
            "tracking_number": tracking_number,
            "carrier": carrier,
            "ship_method": ship_method,
            "lines_shipped": lines_shipped,
            "total_quantity": total_quantity,
        })
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
