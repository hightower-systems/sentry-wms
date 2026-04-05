"""
Picking endpoints: batch creation, task management, pick confirmation, batch completion.
"""

from flask import Blueprint, g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_auth
from models.database import get_db
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

picking_bp = Blueprint("picking", __name__)


@picking_bp.route("/active-batch")
@require_auth
def active_batch():
    username = g.current_user["username"]
    db = next(get_db())
    try:
        batch = db.execute(
            text("""
                SELECT batch_id, total_orders, created_at
                FROM pick_batches
                WHERE assigned_to = :username
                  AND status IN ('OPEN', 'IN_PROGRESS')
                ORDER BY created_at DESC
                LIMIT 1
            """),
            {"username": username},
        ).fetchone()

        if not batch:
            return jsonify({"active": False})

        counts = db.execute(
            text("""
                SELECT
                    COUNT(*) AS total_picks,
                    COUNT(*) FILTER (WHERE status IN ('PICKED', 'SHORT')) AS completed_picks
                FROM pick_tasks
                WHERE batch_id = :batch_id
            """),
            {"batch_id": batch.batch_id},
        ).fetchone()

        return jsonify({
            "active": True,
            "batch_id": batch.batch_id,
            "total_picks": counts.total_picks,
            "completed_picks": counts.completed_picks,
            "total_orders": batch.total_orders,
            "created_at": batch.created_at.isoformat() if batch.created_at else None,
        })
    finally:
        db.close()


@picking_bp.route("/create-batch", methods=["POST"])
@require_auth
def create_batch():
    data = request.get_json()
    if not data or not data.get("so_identifiers") or not data.get("warehouse_id"):
        return jsonify({"error": "so_identifiers and warehouse_id are required"}), 400

    db = next(get_db())
    try:
        result = create_pick_batch(
            db,
            so_identifiers=data["so_identifiers"],
            warehouse_id=data["warehouse_id"],
            username=g.current_user["username"],
        )
        return jsonify(result)
    except ValueError as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@picking_bp.route("/wave-validate", methods=["POST"])
@require_auth
def validate_so():
    data = request.get_json()
    if not data or not data.get("so_barcode") or not data.get("warehouse_id"):
        return jsonify({"error": "so_barcode and warehouse_id are required"}), 400

    db = next(get_db())
    try:
        result = wave_validate(db, data["so_barcode"], data["warehouse_id"])
        if result.get("valid"):
            return jsonify(result)
        # Determine status code based on error type
        if "already in active pick batch" in result.get("error", ""):
            return jsonify(result), 409
        if "not found" in result.get("error", ""):
            return jsonify(result), 404
        return jsonify(result), 400
    finally:
        db.close()


@picking_bp.route("/wave-create", methods=["POST"])
@require_auth
def create_wave():
    data = request.get_json()
    if not data or not data.get("so_ids") or not data.get("warehouse_id"):
        return jsonify({"error": "so_ids and warehouse_id are required"}), 400

    db = next(get_db())
    try:
        result = wave_create(
            db,
            so_ids=data["so_ids"],
            warehouse_id=data["warehouse_id"],
            username=g.current_user["username"],
        )
        return jsonify(result)
    except AlreadyInBatchError as e:
        db.rollback()
        return jsonify({"error": str(e), "so_number": e.so_number, "batch_id": e.batch_id}), 409
    except ValueError as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@picking_bp.route("/batch/<int:batch_id>")
@require_auth
def get_batch(batch_id):
    db = next(get_db())
    try:
        result = get_batch_tasks(db, batch_id)
        if not result:
            return jsonify({"error": "Batch not found"}), 404
        return jsonify(result)
    finally:
        db.close()


@picking_bp.route("/batch/<int:batch_id>/next")
@require_auth
def next_task(batch_id):
    db = next(get_db())
    try:
        task = get_next_task(db, batch_id)
        if not task:
            return jsonify({"message": "All tasks complete"})
        return jsonify(task)
    finally:
        db.close()


@picking_bp.route("/confirm", methods=["POST"])
@require_auth
def confirm():
    data = request.get_json()
    if not data or not data.get("pick_task_id") or not data.get("scanned_barcode"):
        return jsonify({"error": "pick_task_id and scanned_barcode are required"}), 400

    quantity_picked = data.get("quantity_picked", 0)
    if quantity_picked <= 0:
        return jsonify({"error": "quantity_picked must be greater than 0"}), 400

    db = next(get_db())
    try:
        result = confirm_pick(
            db,
            pick_task_id=data["pick_task_id"],
            scanned_barcode=data["scanned_barcode"],
            quantity_picked=quantity_picked,
            username=g.current_user["username"],
        )
        return jsonify({"message": "Pick confirmed", **result})
    except BarcodeError as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    except ValueError as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@picking_bp.route("/short", methods=["POST"])
@require_auth
def short():
    data = request.get_json()
    if not data or not data.get("pick_task_id"):
        return jsonify({"error": "pick_task_id is required"}), 400

    quantity_available = data.get("quantity_available", 0)
    if quantity_available < 0:
        return jsonify({"error": "quantity_available cannot be negative"}), 400

    db = next(get_db())
    try:
        result = short_pick(
            db,
            pick_task_id=data["pick_task_id"],
            quantity_available=quantity_available,
            username=g.current_user["username"],
        )
        return jsonify({"message": "Short pick recorded", **result})
    except ValueError as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@picking_bp.route("/complete-batch", methods=["POST"])
@require_auth
def complete():
    data = request.get_json()
    if not data or not data.get("batch_id"):
        return jsonify({"error": "batch_id is required"}), 400

    db = next(get_db())
    try:
        result = complete_batch(
            db,
            batch_id=data["batch_id"],
            username=g.current_user["username"],
        )
        return jsonify({"message": "Batch completed", **result})
    except ValueError as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
