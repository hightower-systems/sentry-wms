"""
Inventory management endpoints: cycle count creation, retrieval, and submission.
"""

from flask import Blueprint, g, jsonify, request
from sqlalchemy import text

from constants import (
    COUNT_PENDING, COUNT_IN_PROGRESS, COUNT_COMPLETED, COUNT_VARIANCE,
    ADJ_PENDING, ACTION_COUNT,
)
from middleware.auth_middleware import require_auth, check_warehouse_access
from middleware.db import with_db
from schemas.cycle_count import CreateCycleCountRequest, SubmitCycleCountRequest
from services.audit_service import write_audit_log
from utils.validation import validate_body

inventory_bp = Blueprint("inventory", __name__)


@inventory_bp.route("/cycle-count/create", methods=["POST"])
@require_auth
@validate_body(CreateCycleCountRequest)
@with_db
def create_cycle_count(validated):
    warehouse_id = validated.warehouse_id
    bin_ids = validated.bin_ids

    # Validate warehouse
    wh = g.db.execute(
        text("SELECT warehouse_id FROM warehouses WHERE warehouse_id = :wh"),
        {"wh": warehouse_id},
    ).fetchone()
    if not wh:
        return jsonify({"error": "Warehouse not found"}), 404

    # Validate all bins
    for bid in bin_ids:
        b = g.db.execute(
            text("SELECT bin_id FROM bins WHERE bin_id = :bid AND warehouse_id = :wh"),
            {"bid": bid, "wh": warehouse_id},
        ).fetchone()
        if not b:
            return jsonify({"error": f"Bin {bid} not found in warehouse {warehouse_id}"}), 404

    username = g.current_user["username"]
    counts = []

    for bid in bin_ids:
        # Create cycle_counts record
        result = g.db.execute(
            text(
                """
                INSERT INTO cycle_counts (warehouse_id, bin_id, status, assigned_to)
                VALUES (:wh, :bid, :status, :user)
                RETURNING count_id
                """
            ),
            {"wh": warehouse_id, "bid": bid, "status": COUNT_PENDING, "user": username},
        )
        count_id = result.fetchone()[0]

        # Snapshot current inventory for this bin
        inv_rows = g.db.execute(
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
            g.db.execute(
                text(
                    """
                    INSERT INTO cycle_count_lines (count_id, item_id, expected_quantity)
                    VALUES (:cid, :iid, :qty)
                    """
                ),
                {"cid": count_id, "iid": inv.item_id, "qty": inv.quantity_on_hand},
            )
            line_count += 1

        bin_row = g.db.execute(
            text("SELECT bin_code FROM bins WHERE bin_id = :bid"),
            {"bid": bid},
        ).fetchone()

        counts.append({
            "count_id": count_id,
            "bin_id": bid,
            "bin_code": bin_row.bin_code,
            "status": COUNT_PENDING,
            "lines": line_count,
            "assigned_to": username,
        })

    g.db.commit()

    return jsonify({"message": "Cycle counts created", "counts": counts})


@inventory_bp.route("/cycle-count/<int:count_id>")
@require_auth
@with_db
def get_cycle_count(count_id):
    cc = g.db.execute(
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

    ok, denied = check_warehouse_access(cc.warehouse_id)
    if not ok:
        return denied

    lines = g.db.execute(
        text(
            """
            SELECT ccl.count_line_id, ccl.item_id, i.sku, i.item_name, i.upc,
                   ccl.expected_quantity, ccl.counted_quantity,
                   ccl.scanned, ccl.unexpected
            FROM cycle_count_lines ccl
            JOIN items i ON i.item_id = ccl.item_id
            WHERE ccl.count_id = :cid
            ORDER BY ccl.count_line_id
            """
        ),
        {"cid": count_id},
    ).fetchall()

    # Fetch blind count setting
    show_expected_row = g.db.execute(
        text("SELECT value FROM app_settings WHERE key = 'count_show_expected'")
    ).fetchone()
    show_expected = not show_expected_row or show_expected_row.value != "false"

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
        "show_expected": show_expected,
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
                "unexpected": l.unexpected if hasattr(l, "unexpected") else False,
            }
            for l in lines
        ],
    })


@inventory_bp.route("/cycle-count/submit", methods=["POST"])
@require_auth
@validate_body(SubmitCycleCountRequest)
@with_db
def submit_cycle_count(validated):
    count_id = validated.count_id
    submitted_lines = validated.lines

    # Validate cycle count
    cc = g.db.execute(
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

    ok, denied = check_warehouse_access(cc.warehouse_id)
    if not ok:
        return denied

    if cc.status not in (COUNT_PENDING, COUNT_IN_PROGRESS):
        return jsonify({"error": f"Cycle count status is {cc.status}, cannot submit"}), 400

    username = g.current_user["username"]
    adjustments = []
    lines_with_variance = 0
    lines_matched = 0

    for sub in submitted_lines:
        cl_id = sub.count_line_id
        counted_qty = sub.counted_quantity
        is_unexpected = sub.unexpected

        if is_unexpected:
            # Unexpected item  -  not in original snapshot. Create a new count line.
            item_id = sub.item_id
            sku = sub.sku or "UNKNOWN"
            if not item_id:
                return jsonify({"error": "item_id required for unexpected lines"}), 400

            new_line = g.db.execute(
                text(
                    """
                    INSERT INTO cycle_count_lines
                        (count_id, item_id, expected_quantity, counted_quantity, scanned, unexpected, counted_by, counted_at)
                    VALUES (:cid, :iid, 0, :qty, TRUE, TRUE, :user, NOW())
                    RETURNING count_line_id
                    """
                ),
                {"cid": count_id, "iid": item_id, "qty": counted_qty, "user": username},
            )
            new_cl_id = new_line.fetchone()[0]

            lines_with_variance += 1
            reason_detail = f"Cycle count unexpected item: counted {counted_qty} (not in snapshot)"
            adj_result = g.db.execute(
                text(
                    """
                    INSERT INTO inventory_adjustments
                        (item_id, bin_id, warehouse_id, quantity_change, reason_code, reason_detail, status, adjusted_by, cycle_count_id)
                    VALUES (:iid, :bid, :wh, :change, 'CYCLE_COUNT', :detail, :adj_status, :user, :cid)
                    RETURNING adjustment_id
                    """
                ),
                {
                    "iid": item_id,
                    "bid": cc.bin_id,
                    "wh": cc.warehouse_id,
                    "change": counted_qty,
                    "detail": reason_detail,
                    "adj_status": ADJ_PENDING,
                    "user": username,
                    "cid": count_id,
                },
            )
            adj_id = adj_result.fetchone()[0]
            adjustments.append({
                "sku": sku,
                "expected": 0,
                "counted": counted_qty,
                "variance": counted_qty,
                "adjustment_id": adj_id,
                "unexpected": True,
            })
            continue

        # Load the count line
        cl = g.db.execute(
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
        g.db.execute(
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

            # 3. Create pending adjustment (no inventory update  -  requires admin approval)
            reason_detail = f"Cycle count variance: expected {cl.expected_quantity}, counted {counted_qty}"
            adj_result = g.db.execute(
                text(
                    """
                    INSERT INTO inventory_adjustments
                        (item_id, bin_id, warehouse_id, quantity_change, reason_code, reason_detail, status, adjusted_by, cycle_count_id)
                    VALUES (:iid, :bid, :wh, :change, 'CYCLE_COUNT', :detail, :adj_status, :user, :cid)
                    RETURNING adjustment_id
                    """
                ),
                {
                    "iid": cl.item_id,
                    "bid": cc.bin_id,
                    "wh": cc.warehouse_id,
                    "change": variance,
                    "detail": reason_detail,
                    "adj_status": ADJ_PENDING,
                    "user": username,
                    "cid": count_id,
                },
            )
            adj_id = adj_result.fetchone()[0]

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
        g.db.execute(
            text(
                "UPDATE inventory SET last_counted_at = NOW() WHERE item_id = :iid AND bin_id = :bid"
            ),
            {"iid": cl.item_id, "bid": cc.bin_id},
        )

    # Set final status
    final_status = COUNT_VARIANCE if lines_with_variance > 0 else COUNT_COMPLETED
    g.db.execute(
        text(
            "UPDATE cycle_counts SET status = :status, completed_at = NOW() WHERE count_id = :cid"
        ),
        {"status": final_status, "cid": count_id},
    )

    # Audit log
    write_audit_log(
        g.db,
        action_type=ACTION_COUNT,
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

    g.db.commit()

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
