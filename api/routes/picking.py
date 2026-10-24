"""
Picking endpoints: batch creation, task management, pick confirmation, batch completion.
"""

from flask import Blueprint, g, jsonify, request
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from constants import (
    BATCH_OPEN, BATCH_IN_PROGRESS,
    TASK_PICKED, TASK_SHORT,
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
    InsufficientCoverageError,
    cancel_stale_self_batch,
    complete_batch,
    confirm_pick,
    create_pick_batch,
    full_revert_batch,
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

    # v1.8.0 (#295): if any pick_tasks row carries a to_id, this is a
    # TO batch; surface the to_number so the mobile screen can render
    # "TO {to_number}" instead of "X orders".
    to_row = g.db.execute(
        text("""
            SELECT DISTINCT pt.to_id, o.to_number
              FROM pick_tasks pt
              JOIN transfer_orders o ON o.to_id = pt.to_id
             WHERE pt.batch_id = :batch_id
             LIMIT 1
        """),
        {"batch_id": batch.batch_id},
    ).fetchone()
    kind = "TO" if to_row else "SO"

    return jsonify({
        "active": True,
        "batch_id": batch.batch_id,
        "total_picks": counts.total_picks,
        "completed_picks": counts.completed_picks,
        "total_orders": batch.total_orders,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "kind": kind,
        "to_id": to_row.to_id if to_row else None,
        "to_number": to_row.to_number if to_row else None,
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
            exclude_so_ids=validated.exclude_so_ids,
            resume_existing=validated.resume_existing,
        )
        return jsonify(result)
    except InsufficientCoverageError as e:
        return jsonify({
            "error_type": "insufficient_coverage",
            "error": str(e),
            "unpickable": e.unpickable,
        }), 409
    except ValueError as e:
        g.db.rollback()
        return jsonify({"error": str(e)}), 400


@picking_bp.route("/wave-validate", methods=["POST"])
@require_auth
@validate_body(WaveValidateRequest)
@with_db
def validate_so(validated):
    result = wave_validate(
        g.db,
        validated.barcode,
        validated.warehouse_id,
        username=g.current_user["username"],
    )
    if result.get("valid"):
        return jsonify(result)
    status = result.get("status_code", 400)
    return jsonify(result), status


@picking_bp.route("/wave-create", methods=["POST"])
@require_auth
@validate_body(WaveCreateRequest)
@with_db
def create_wave(validated):
    username = g.current_user["username"]
    # Two attempts at most. Two transient conflicts can strand a wave-create
    # that is safe to replay:
    #
    #   * AlreadyInBatch on this picker's own stranded OPEN batch. An aborted
    #     prior create (the phone gave up at its client timeout) still runs to
    #     its commit server-side and leaves an OPEN batch the picker never saw;
    #     the rescan then collides with it. cancel_stale_self_batch reverts
    #     that batch under a row lock -- but only if it is genuinely abandoned
    #     (no picking progress) -- and we retry once. A collision with another
    #     picker's batch, or with a batch the same operator is actively walking
    #     on another device, is surfaced as a 409, never auto-cancelled.
    #
    #   * A deadlock (40P01). A concurrent create can lock the same inventory
    #     rows in a different order (normalize walks by inventory_id, coverage
    #     by pick_sequence) and deadlock; Postgres kills one side. The killed
    #     transaction rolled back cleanly and is safe to replay, so we retry
    #     once instead of surfacing a raw 500 on the handheld.
    for attempt in range(2):
        try:
            result = wave_create(
                g.db,
                so_ids=validated.so_ids,
                warehouse_id=validated.warehouse_id,
                username=username,
                exclude_so_ids=validated.exclude_so_ids,
                resume_existing=validated.resume_existing,
            )
            return jsonify(result)
        except InsufficientCoverageError as e:
            # error_type distinguishes this 409 from already_in_batch so the
            # mobile client knows to render the per-SKU shortfall modal rather
            # than a one-line error popup.
            return jsonify({
                "error_type": "insufficient_coverage",
                "error": str(e),
                "unpickable": e.unpickable,
            }), 409
        except AlreadyInBatchError as e:
            g.db.rollback()
            if not validated.resume_existing and attempt == 0 and cancel_stale_self_batch(g.db, e.batch_id, username):
                continue
            return jsonify({
                "error_type": "already_in_batch",
                "error": str(e),
                "so_number": e.so_number,
                "batch_id": e.batch_id,
            }), 409
        except OperationalError as e:
            g.db.rollback()
            if attempt == 0 and getattr(e.orig, "pgcode", None) == "40P01":
                continue
            raise
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
    """Cancel a batch and unwind every effect (PENDING reservations,
    PICKED moves, SHORT residue). Operator semantic: cancel = wipe
    progress so the SO is indistinguishable from "never started
    picking" -- no Resume affordance, no half-stored state. Items the
    picker physically pulled are restored to their source bin in the
    WMS; the operator is responsible for the corresponding physical
    return. See services.picking_service.full_revert_batch for the
    per-task effects."""
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

    result = full_revert_batch(
        g.db, batch_id=batch_id,
        username=g.current_user["username"],
    )
    g.db.commit()
    return jsonify({
        "message": "Batch cancelled",
        "released_tasks": result["released_tasks"],
    })
