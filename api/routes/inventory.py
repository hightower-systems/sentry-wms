"""
Inventory management endpoints: cycle count creation, retrieval, and submission.
"""

from flask import Blueprint, g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_auth
from models.database import get_db
from services.audit_service import write_audit_log

inventory_bp = Blueprint("inventory", __name__)


@inventory_bp.route("/cycle-count/create", methods=["POST"])
@require_auth
def create_cycle_count():
    data = request.get_json()
    if not data or not data.get("warehouse_id") or not data.get("bin_ids"):
        return jsonify({"error": "warehouse_id and bin_ids are required"}), 400

    warehouse_id = data["warehouse_id"]
    bin_ids = data["bin_ids"]

    if not bin_ids:
        return jsonify({"error": "bin_ids must not be empty"}), 400

    db = next(get_db())
    try:
        # Validate warehouse
        wh = db.execute(
            text("SELECT warehouse_id FROM warehouses WHERE warehouse_id = :wh"),
            {"wh": warehouse_id},
        ).fetchone()
        if not wh:
            return jsonify({"error": "Warehouse not found"}), 404

        # Validate all bins
        for bid in bin_ids:
            b = db.execute(
                text("SELECT bin_id FROM bins WHERE bin_id = :bid AND warehouse_id = :wh"),
                {"bid": bid, "wh": warehouse_id},
            ).fetchone()
            if not b:
                return jsonify({"error": f"Bin {bid} not found in warehouse {warehouse_id}"}), 404

        username = g.current_user["username"]
        counts = []

        for bid in bin_ids:
            # Create cycle_counts record
            result = db.execute(
                text(
                    """
                    INSERT INTO cycle_counts (warehouse_id, bin_id, status, assigned_to)
                    VALUES (:wh, :bid, 'PENDING', :user)
                    RETURNING count_id
                    """
                ),
                {"wh": warehouse_id, "bid": bid, "user": username},
            )
            count_id = result.fetchone()[0]

            # Snapshot current inventory for this bin
            inv_rows = db.execute(
                text(
                    """
                    SELECT item_id, quantity_on_hand
                    FROM inventory
                    WHERE bin_id = :bid AND quantity_on_hand > 0
                    """
                ),
                {"bid": bid},
            ).fetchall()

            line_count = 0
            for inv in inv_rows:
                db.execute(
                    text(
                        """
                        INSERT INTO cycle_count_lines (count_id, item_id, expected_quantity)
                        VALUES (:cid, :iid, :qty)
                        """
                    ),
                    {"cid": count_id, "iid": inv.item_id, "qty": inv.quantity_on_hand},
                )
                line_count += 1

            bin_row = db.execute(
                text("SELECT bin_code FROM bins WHERE bin_id = :bid"),
                {"bid": bid},
            ).fetchone()

            counts.append({
                "count_id": count_id,
                "bin_id": bid,
                "bin_code": bin_row.bin_code,
                "status": "PENDING",
                "lines": line_count,
                "assigned_to": username,
            })

        db.commit()

        return jsonify({"message": "Cycle counts created", "counts": counts})
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@inventory_bp.route("/cycle-count/<int:count_id>")
@require_auth
def get_cycle_count(count_id):
    db = next(get_db())
    try:
        cc = db.execute(
            text(
                """
                SELECT cc.count_id, cc.bin_id, b.bin_code, b.bin_barcode,
                       cc.warehouse_id, cc.status, cc.assigned_to, cc.created_at
                FROM cycle_counts cc
                JOIN bins b ON b.bin_id = cc.bin_id
                WHERE cc.count_id = :cid
                """
            ),
            {"cid": count_id},
        ).fetchone()

        if not cc:
            return jsonify({"error": "Cycle count not found"}), 404

        lines = db.execute(
            text(
                """
                SELECT ccl.count_line_id, ccl.item_id, i.sku, i.item_name, i.upc,
                       ccl.expected_quantity, ccl.counted_quantity,
                       ccl.scanned
                FROM cycle_count_lines ccl
                JOIN items i ON i.item_id = ccl.item_id
                WHERE ccl.count_id = :cid
                ORDER BY ccl.count_line_id
                """
            ),
            {"cid": count_id},
        ).fetchall()

        return jsonify({
            "cycle_count": {
                "count_id": cc.count_id,
                "bin_id": cc.bin_id,
                "bin_code": cc.bin_code,
                "bin_barcode": cc.bin_barcode,
                "warehouse_id": cc.warehouse_id,
                "status": cc.status,
                "assigned_to": cc.assigned_to,
                "created_at": cc.created_at.isoformat() if cc.created_at else None,
            },
            "lines": [
                {
                    "count_line_id": l.count_line_id,
                    "item_id": l.item_id,
                    "sku": l.sku,
                    "item_name": l.item_name,
                    "upc": l.upc,
                    "expected_quantity": l.expected_quantity,
                    "counted_quantity": l.counted_quantity,
                    "variance": (l.counted_quantity - l.expected_quantity) if l.counted_quantity is not None else None,
                    "scanned": l.scanned,
                }
                for l in lines
            ],
        })
    finally:
        db.close()


@inventory_bp.route("/cycle-count/submit", methods=["POST"])
@require_auth
def submit_cycle_count():
    data = request.get_json()
    if not data or not data.get("count_id") or not data.get("lines"):
        return jsonify({"error": "count_id and lines are required"}), 400

    count_id = data["count_id"]
    submitted_lines = data["lines"]

    db = next(get_db())
    try:
        # Validate cycle count
        cc = db.execute(
            text(
                """
                SELECT cc.count_id, cc.bin_id, cc.status, cc.warehouse_id, b.bin_code
                FROM cycle_counts cc
                JOIN bins b ON b.bin_id = cc.bin_id
                WHERE cc.count_id = :cid
                """
            ),
            {"cid": count_id},
        ).fetchone()

        if not cc:
            return jsonify({"error": "Cycle count not found"}), 404
        if cc.status not in ("PENDING", "IN_PROGRESS"):
            return jsonify({"error": f"Cycle count status is {cc.status}, cannot submit"}), 400

        username = g.current_user["username"]
        adjustments = []
        lines_with_variance = 0
        lines_matched = 0

        for sub in submitted_lines:
            cl_id = sub.get("count_line_id")
            counted_qty = sub.get("counted_quantity")

            if counted_qty is None or counted_qty < 0:
                return jsonify({"error": f"counted_quantity must be >= 0 for line {cl_id}"}), 400

            # Load the count line
            cl = db.execute(
                text(
                    """
                    SELECT ccl.count_line_id, ccl.count_id, ccl.item_id, ccl.expected_quantity,
                           i.sku
                    FROM cycle_count_lines ccl
                    JOIN items i ON i.item_id = ccl.item_id
                    WHERE ccl.count_line_id = :cl_id AND ccl.count_id = :cid
                    """
                ),
                {"cl_id": cl_id, "cid": count_id},
            ).fetchone()

            if not cl:
                return jsonify({"error": f"Count line {cl_id} not found on this cycle count"}), 400

            # 1. Update count line
            db.execute(
                text(
                    """
                    UPDATE cycle_count_lines
                    SET counted_quantity = :qty, counted_by = :user, counted_at = NOW(), scanned = TRUE
                    WHERE count_line_id = :cl_id
                    """
                ),
                {"qty": counted_qty, "user": username, "cl_id": cl_id},
            )

            # 2. Calculate variance
            variance = counted_qty - cl.expected_quantity

            if variance != 0:
                lines_with_variance += 1

                # 3. Create adjustment
                reason_detail = f"Cycle count variance: expected {cl.expected_quantity}, counted {counted_qty}"
                adj_result = db.execute(
                    text(
                        """
                        INSERT INTO inventory_adjustments
                            (item_id, bin_id, warehouse_id, quantity_change, reason_code, reason_detail, adjusted_by, cycle_count_id)
                        VALUES (:iid, :bid, :wh, :change, 'CYCLE_COUNT', :detail, :user, :cid)
                        RETURNING adjustment_id
                        """
                    ),
                    {
                        "iid": cl.item_id,
                        "bid": cc.bin_id,
                        "wh": cc.warehouse_id,
                        "change": variance,
                        "detail": reason_detail,
                        "user": username,
                        "cid": count_id,
                    },
                )
                adj_id = adj_result.fetchone()[0]

                # Update inventory to match counted quantity
                db.execute(
                    text(
                        """
                        UPDATE inventory
                        SET quantity_on_hand = :qty, updated_at = NOW()
                        WHERE item_id = :iid AND bin_id = :bid
                        """
                    ),
                    {"qty": counted_qty, "iid": cl.item_id, "bid": cc.bin_id},
                )

                adjustments.append({
                    "sku": cl.sku,
                    "expected": cl.expected_quantity,
                    "counted": counted_qty,
                    "variance": variance,
                    "adjustment_id": adj_id,
                })
            else:
                lines_matched += 1

            # 4. Update last_counted_at
            db.execute(
                text(
                    "UPDATE inventory SET last_counted_at = NOW() WHERE item_id = :iid AND bin_id = :bid"
                ),
                {"iid": cl.item_id, "bid": cc.bin_id},
            )

        # Set final status
        final_status = "VARIANCE" if lines_with_variance > 0 else "COMPLETED"
        db.execute(
            text(
                "UPDATE cycle_counts SET status = :status, completed_at = NOW() WHERE count_id = :cid"
            ),
            {"status": final_status, "cid": count_id},
        )

        # Audit log
        write_audit_log(
            db,
            action_type="COUNT",
            entity_type="BIN",
            entity_id=cc.bin_id,
            user_id=username,
            warehouse_id=cc.warehouse_id,
            details={
                "count_id": count_id,
                "bin_code": cc.bin_code,
                "lines_with_variance": lines_with_variance,
                "lines_matched": lines_matched,
            },
        )

        db.commit()

        return jsonify({
            "message": "Cycle count submitted",
            "count_id": count_id,
            "bin_code": cc.bin_code,
            "status": final_status,
            "summary": {
                "total_lines": len(submitted_lines),
                "lines_with_variance": lines_with_variance,
                "lines_matched": lines_matched,
                "adjustments": adjustments,
            },
        })
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
