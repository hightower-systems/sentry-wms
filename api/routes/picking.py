"""
Picking endpoints: batch creation, task management, pick confirmation, batch completion.
"""

from flask import Blueprint, g, jsonify, request
from sqlalchemy import text

from constants import (
    BATCH_OPEN, BATCH_IN_PROGRESS, BATCH_CANCELLED,
    SO_OPEN, SO_PICKING,
    TASK_PENDING, TASK_PICKED, TASK_SHORT, TASK_SKIPPED,
)
from middleware.auth_middleware import require_auth, check_warehouse_access
from middleware.db import with_db
from schemas.pick_walks import (
    CancelBatchRequest,
    CompleteBatchRequest,
    ConfirmPickRequest,
    CreateBatchRequest,
    ShortPickRequest,
    WaveCreateRequest,
    WaveValidateRequest,
)
from services.picking_service import (
    AlreadyInBatchError,
    BarcodeError,
    complete_batch,
    confirm_pick,
    create_pick_batch,
    get_batch_tasks,
    get_next_task,
    short_pick,
    wave_create,
    wave_validate,
)
from utils.validation import validate_body

picking_bp = Blueprint("picking", __name__)


@picking_bp.route("/active-batch")
@require_auth
@with_db
def active_batch():
    username = g.current_user["username"]
    batch = g.db.execute(
        text("""
            SELECT batch_id, total_orders, created_at
            FROM pick_batches
            WHERE assigned_to = :username
              AND status IN (:s_open, :s_inprog)
            ORDER BY created_at DESC
            LIMIT 1
        """),
        {"username": username, "s_open": BATCH_OPEN, "s_inprog": BATCH_IN_PROGRESS},
    ).fetchone()

    if not batch:
        return jsonify({"active": False})

    counts = g.db.execute(
        text("""
            SELECT
                COUNT(*) AS total_picks,
                COUNT(*) FILTER (WHERE status IN (:s_picked, :s_short)) AS completed_picks
            FROM pick_tasks
            WHERE batch_id = :batch_id
        """),
        {"batch_id": batch.batch_id, "s_picked": TASK_PICKED, "s_short": TASK_SHORT},
    ).fetchone()

    return jsonify({
        "active": True,
        "batch_id": batch.batch_id,
        "total_picks": counts.total_picks,
        "completed_picks": counts.completed_picks,
        "total_orders": batch.total_orders,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
    })


@picking_bp.route("/create-batch", methods=["POST"])
@require_auth
@validate_body(CreateBatchRequest)
@with_db
def create_batch(validated):
    try:
        result = create_pick_batch(
            g.db,
            so_identifiers=validated.so_identifiers,
            warehouse_id=validated.warehouse_id,
            username=g.current_user["username"],
        )
        return jsonify(result)
    except ValueError as e:
        g.db.rollback()
        return jsonify({"error": str(e)}), 400


@picking_bp.route("/wave-validate", methods=["POST"])
@require_auth
@validate_body(WaveValidateRequest)
@with_db
def validate_so(validated):
    result = wave_validate(g.db, validated.so_barcode, validated.warehouse_id)
    if result.get("valid"):
        return jsonify(result)
    # Determine status code based on error type
    if "already in active pick batch" in result.get("error", ""):
        return jsonify(result), 409
    if "not found" in result.get("error", ""):
        return jsonify(result), 404
    return jsonify(result), 400


@picking_bp.route("/wave-create", methods=["POST"])
@require_auth
@validate_body(WaveCreateRequest)
@with_db
def create_wave(validated):
    try:
        result = wave_create(
            g.db,
            so_ids=validated.so_ids,
            warehouse_id=validated.warehouse_id,
            username=g.current_user["username"],
        )
        return jsonify(result)
    except AlreadyInBatchError as e:
        g.db.rollback()
        return jsonify({"error": str(e), "so_number": e.so_number, "batch_id": e.batch_id}), 409
    except ValueError as e:
        g.db.rollback()
        return jsonify({"error": str(e)}), 400


@picking_bp.route("/batch/<int:batch_id>")
@require_auth
@with_db
def get_batch(batch_id):
    batch_row = g.db.execute(
        text("SELECT warehouse_id FROM pick_batches WHERE batch_id = :bid"),
        {"bid": batch_id},
    ).fetchone()
    if not batch_row:
        return jsonify({"error": "Batch not found"}), 404
    ok, denied = check_warehouse_access(batch_row.warehouse_id)
    if not ok:
        return denied
    result = get_batch_tasks(g.db, batch_id)
    if not result:
        return jsonify({"error": "Batch not found"}), 404
    return jsonify(result)


@picking_bp.route("/batch/<int:batch_id>/next")
@require_auth
@with_db
def next_task(batch_id):
    batch_row = g.db.execute(
        text("SELECT warehouse_id FROM pick_batches WHERE batch_id = :bid"),
        {"bid": batch_id},
    ).fetchone()
    if batch_row:
        ok, denied = check_warehouse_access(batch_row.warehouse_id)
        if not ok:
            return denied
    task = get_next_task(g.db, batch_id)
    if not task:
        return jsonify({"message": "All tasks complete"})
    return jsonify(task)


@picking_bp.route("/confirm", methods=["POST"])
@require_auth
@validate_body(ConfirmPickRequest)
@with_db
def confirm(validated):
    # Warehouse access check via pick task's batch
    task_wh = g.db.execute(
        text("SELECT pb.warehouse_id FROM pick_tasks pt JOIN pick_batches pb ON pb.batch_id = pt.batch_id WHERE pt.pick_task_id = :tid"),
        {"tid": validated.pick_task_id},
    ).fetchone()
    if task_wh:
        ok, denied = check_warehouse_access(task_wh.warehouse_id)
        if not ok:
            return denied

    try:
        result = confirm_pick(
            g.db,
            pick_task_id=validated.pick_task_id,
            scanned_barcode=validated.scanned_barcode,
            quantity_picked=validated.quantity_picked,
            username=g.current_user["username"],
        )
        return jsonify({"message": "Pick confirmed", **result})
    except BarcodeError as e:
        g.db.rollback()
        return jsonify({"error": str(e)}), 400
    except ValueError as e:
        g.db.rollback()
        return jsonify({"error": str(e)}), 400


@picking_bp.route("/short", methods=["POST"])
@require_auth
@validate_body(ShortPickRequest)
@with_db
def short(validated):
    task_wh = g.db.execute(
        text("SELECT pb.warehouse_id FROM pick_tasks pt JOIN pick_batches pb ON pb.batch_id = pt.batch_id WHERE pt.pick_task_id = :tid"),
        {"tid": validated.pick_task_id},
    ).fetchone()
    if task_wh:
        ok, denied = check_warehouse_access(task_wh.warehouse_id)
        if not ok:
            return denied

    try:
        result = short_pick(
            g.db,
            pick_task_id=validated.pick_task_id,
            quantity_available=validated.quantity_available,
            username=g.current_user["username"],
        )
        return jsonify({"message": "Short pick recorded", **result})
    except ValueError as e:
        g.db.rollback()
        return jsonify({"error": str(e)}), 400


@picking_bp.route("/complete-batch", methods=["POST"])
@require_auth
@validate_body(CompleteBatchRequest)
@with_db
def complete(validated):
    batch_wh = g.db.execute(
        text("SELECT warehouse_id FROM pick_batches WHERE batch_id = :bid"),
        {"bid": validated.batch_id},
    ).fetchone()
    if batch_wh:
        ok, denied = check_warehouse_access(batch_wh.warehouse_id)
        if not ok:
            return denied

    try:
        result = complete_batch(
            g.db,
            batch_id=validated.batch_id,
            username=g.current_user["username"],
        )
        return jsonify({"message": "Batch completed", **result})
    except ValueError as e:
        g.db.rollback()
        return jsonify({"error": str(e)}), 400


@picking_bp.route("/cancel-batch", methods=["POST"])
@require_auth
@validate_body(CancelBatchRequest)
@with_db
def cancel_batch(validated):
    """Cancel/delete a batch  -  releases allocated inventory and resets SO statuses."""
    batch_id = validated.batch_id
    batch = g.db.execute(
        text("SELECT batch_id, status, warehouse_id FROM pick_batches WHERE batch_id = :bid"),
        {"bid": batch_id},
    ).fetchone()
    if not batch:
        return jsonify({"error": "Batch not found"}), 404

    ok, denied = check_warehouse_access(batch.warehouse_id)
    if not ok:
        return denied

    # Release allocated inventory for pending tasks
    pending_tasks = g.db.execute(
        text("""
            SELECT pick_task_id, item_id, bin_id, quantity_to_pick
            FROM pick_tasks
            WHERE batch_id = :bid AND status = :task_status
        """),
        {"bid": batch_id, "task_status": TASK_PENDING},
    ).fetchall()

    for task in pending_tasks:
        g.db.execute(
            text("""
                UPDATE inventory
                SET quantity_allocated = GREATEST(0, quantity_allocated - :qty)
                WHERE item_id = :iid AND bin_id = :bid
            """),
            {"qty": task.quantity_to_pick, "iid": task.item_id, "bid": task.bin_id},
        )

    # Reset SO statuses back to OPEN for orders that haven't been picked
    g.db.execute(
        text("""
            UPDATE sales_orders SET status = :so_status
            WHERE so_id IN (
                SELECT DISTINCT so_id FROM pick_tasks WHERE batch_id = :bid
            ) AND status IN (:s_picking, :s_open)
        """),
        {"bid": batch_id, "so_status": SO_OPEN, "s_picking": SO_PICKING, "s_open": SO_OPEN},
    )

    # Mark batch and all pending tasks as cancelled
    g.db.execute(
        text("UPDATE pick_tasks SET status = :new_status WHERE batch_id = :bid AND status = :old_status"),
        {"bid": batch_id, "new_status": TASK_SKIPPED, "old_status": TASK_PENDING},
    )
    g.db.execute(
        text("UPDATE pick_batches SET status = :batch_status WHERE batch_id = :bid"),
        {"bid": batch_id, "batch_status": BATCH_CANCELLED},
    )

    g.db.commit()
    return jsonify({"message": "Batch cancelled"})
