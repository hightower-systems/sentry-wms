"""
Shipping / fulfillment endpoint: records tracking info and creates fulfillment records.
"""

from flask import Blueprint, g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_auth, warehouse_scope_clause
from middleware.db import with_db
from schemas.shipping import FulfillRequest
from services.audit_service import write_audit_log
from constants import SO_PICKED, SO_PACKED, SO_SHIPPED, ACTION_SHIP, TASK_PICKED, TASK_SHORT
from utils.validation import validate_body

shipping_bp = Blueprint("shipping", __name__)


def _require_packing(db):
    """Check if packing is required before shipping."""
    row = db.execute(
        text("SELECT value FROM app_settings WHERE key = 'require_packing_before_shipping'")
    ).fetchone()
    return not row or row.value != "false"


@shipping_bp.route("/order/<barcode>")
@require_auth
@with_db
def get_order(barcode):
    """Look up an order for shipping. Respects the require_packing setting."""
    if not barcode or not barcode.strip():
        return jsonify({"error": "Barcode is required"}), 400
    if len(barcode) > 100:
        return jsonify({"error": "Barcode too long (max 100 characters)"}), 400

    # V-026: scope at SELECT time so wrong-warehouse looks like not-found.
    scope_clause, scope_params = warehouse_scope_clause("warehouse_id")
    so = g.db.execute(
        text(
            f"""
            SELECT so_id, so_number, so_barcode, customer_name, status,
                   ship_method, ship_address, warehouse_id
            FROM sales_orders
            WHERE (so_barcode = :barcode OR so_number = :barcode)
              {scope_clause}
            LIMIT 1
            """
        ),
        {"barcode": barcode, **scope_params},
    ).fetchone()

    if not so:
        return jsonify({"error": "Order not found"}), 404

    packing_required = _require_packing(g.db)
    allowed_statuses = [SO_PACKED] if packing_required else [SO_PICKED, SO_PACKED]

    if so.status not in allowed_statuses:
        if packing_required and so.status == SO_PICKED:
            return jsonify({"error": "Order must be packed before shipping"}), 400
        return jsonify({"error": f"Order is not ready for shipping. Current status: {so.status}"}), 400

    # Get item summary
    lines = g.db.execute(
        text(
            """
            SELECT sol.so_line_id, sol.line_number, sol.item_id,
                   i.sku, i.item_name,
                   sol.quantity_ordered, sol.quantity_picked, sol.quantity_packed
            FROM sales_order_lines sol
            JOIN items i ON i.item_id = sol.item_id
            WHERE sol.so_id = :so_id
            ORDER BY sol.line_number
            """
        ),
        {"so_id": so.so_id},
    ).fetchall()

    total_items = sum(l.quantity_picked for l in lines)

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
        "lines": [
            {
                "so_line_id": l.so_line_id,
                "line_number": l.line_number,
                "item_id": l.item_id,
                "sku": l.sku,
                "item_name": l.item_name,
                "quantity_ordered": l.quantity_ordered,
                "quantity_picked": l.quantity_picked,
            }
            for l in lines
        ],
        "total_items": total_items,
        "total_lines": len(lines),
    })


@shipping_bp.route("/fulfill", methods=["POST"])
@require_auth
@validate_body(FulfillRequest)
@with_db
def fulfill(validated):
    so_id = validated.so_id
    carrier = validated.carrier
    tracking_number = validated.tracking_number
    ship_method = validated.ship_method
    username = g.current_user["username"]

    # Validate SO with warehouse scope at SELECT time (V-026).
    scope_clause, scope_params = warehouse_scope_clause("warehouse_id")
    so = g.db.execute(
        text(
            f"""
            SELECT so_id, so_number, status, warehouse_id FROM sales_orders
            WHERE so_id = :so_id {scope_clause}
            """
        ),
        {"so_id": so_id, **scope_params},
    ).fetchone()

    if not so:
        return jsonify({"error": "Order not found"}), 404

    packing_required = _require_packing(g.db)
    allowed_statuses = [SO_PACKED] if packing_required else [SO_PICKED, SO_PACKED]

    if so.status not in allowed_statuses:
        if packing_required:
            return jsonify({"error": f"Order must be packed before shipping. Current status: {so.status}"}), 400
        return jsonify({"error": f"Order is not ready for shipping. Current status: {so.status}"}), 400

    # 1. Create item_fulfillments record
    result = g.db.execute(
        text(
            """
            INSERT INTO item_fulfillments (so_id, warehouse_id, tracking_number, carrier, ship_method, shipped_by, status)
            VALUES (:so_id, :wh, :tracking, :carrier, :ship_method, :shipped_by, :shipped_status)
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
            "shipped_status": SO_SHIPPED,
        },
    )
    fulfillment_id = result.fetchone()[0]

    # 2. Create fulfillment lines for each SO line with quantity_picked > 0
    so_lines = g.db.execute(
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
        pick_task = g.db.execute(
            text(
                """
                SELECT bin_id FROM pick_tasks
                WHERE so_id = :so_id AND item_id = :item_id AND status IN (:task_picked, :task_short)
                ORDER BY pick_task_id ASC
                LIMIT 1
                """
            ),
            {"so_id": so_id, "item_id": line.item_id, "task_picked": TASK_PICKED, "task_short": TASK_SHORT},
        ).fetchone()

        bin_id = pick_task.bin_id if pick_task else 1  # fallback shouldn't happen

        g.db.execute(
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
        g.db.execute(
            text(
                "UPDATE sales_order_lines SET quantity_shipped = quantity_picked, status = :status WHERE so_line_id = :sol_id"
            ),
            {"sol_id": line.so_line_id, "status": SO_SHIPPED},
        )

        lines_shipped += 1
        total_quantity += line.quantity_picked

    # 4. Update SO status with carrier and tracking
    g.db.execute(
        text(
            """
            UPDATE sales_orders
            SET status = :shipped_status, shipped_at = NOW(), carrier = :carrier, tracking_number = :tracking
            WHERE so_id = :so_id
            """
        ),
        {"so_id": so_id, "carrier": carrier, "tracking": tracking_number, "shipped_status": SO_SHIPPED},
    )

    # 5. Audit log
    write_audit_log(
        g.db,
        action_type=ACTION_SHIP,
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

    g.db.commit()

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
