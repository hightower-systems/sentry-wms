"""Admin CRUD + pre-approval lifecycle for transfer orders (v1.8.0 #290).

Cookie-auth + ADMIN role. Picking is on the picker-facing
/api/picking/transfer-orders/<to_id>/* surface (Pass 4.3); admin
approval is the dedicated /approvals surface (Pass 4.4).

State-machine + audit constants live in
services.transfer_order_service + constants.py respectively. This
module orchestrates the admin lifecycle without re-deriving state.
"""

import json
import math
import uuid
from typing import List, Optional

from flask import g, jsonify, request
from psycopg2.errors import UniqueViolation
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from datetime import datetime, timezone

from constants import (
    ACTION_TO_APPROVED,
    ACTION_TO_CANCELLED,
    ACTION_TO_CLOSED,
    ACTION_TO_CREATED,
    ACTION_TO_DELETED,
    ACTION_TO_LINE_SHORT_CLOSED,
    ACTION_TO_REJECTED,
    ACTION_TO_SUBMITTED,
)
from middleware.auth_middleware import require_auth, require_role
from middleware.db import with_db
from routes.admin import admin_bp
from schemas.csv_import import TransferOrderImportRow
from services.audit_service import write_audit_log
from services.events_service import emit_event, resolve_source_external_id
from services.transfer_order_service import (
    TO_APPROVAL_APPROVED,
    TO_APPROVAL_PENDING,
    TO_APPROVAL_REJECTED,
    TO_LINE_PENDING,
    TO_LINE_PARTIALLY_PICKED,
    TO_LINE_PICKED,
    TO_LINE_SHORT_CLOSED,
    TO_STATUS_AWAITING_APPROVAL,
    TO_STATUS_CANCELLED,
    TO_STATUS_CLOSED,
    TO_STATUS_OPEN,
    TO_STATUS_PARTIALLY_PICKED,
    evaluate_to_closure,
    generate_to_number,
    validate_header_transition,
    validate_line_transition,
)


# ============================================================
# Import schema
# ============================================================


class _ImportRequest(BaseModel):
    """Top-level shape for POST /api/admin/transfer-orders/import."""

    model_config = ConfigDict(extra="forbid")

    source_warehouse_code: str = Field(..., min_length=1, max_length=20)
    destination_warehouse_code: str = Field(..., min_length=1, max_length=20)
    notes: Optional[str] = Field(None, max_length=2000)
    records: List[dict] = Field(..., min_length=1, max_length=5000)

    @field_validator("source_warehouse_code", "destination_warehouse_code")
    @classmethod
    def _strip(cls, v):
        return v.strip()


# ----------------------------------------------------------------------
# Serialisation helpers
# ----------------------------------------------------------------------


def _serialise_to_header(row) -> dict:
    return {
        "to_id": row.to_id,
        "to_number": row.to_number,
        "source_warehouse_id": row.source_warehouse_id,
        "destination_warehouse_id": row.destination_warehouse_id,
        "status": row.status,
        "created_by": row.created_by,
        "notes": row.notes,
        "external_id": str(row.external_id),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _serialise_to_line(row) -> dict:
    return {
        "to_line_id": row.to_line_id,
        "to_id": row.to_id,
        "item_id": row.item_id,
        "sku": getattr(row, "sku", None),
        "item_name": getattr(row, "item_name", None),
        "line_number": row.line_number,
        "requested_qty": row.requested_qty,
        "committed_qty": row.committed_qty,
        "picked_qty": row.picked_qty,
        "approved_qty": row.approved_qty,
        "status": row.status,
    }


def _serialise_to_approval(row) -> dict:
    return {
        "to_approval_id": row.to_approval_id,
        "to_id": row.to_id,
        "submitted_by": row.submitted_by,
        "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "rejected_at": row.rejected_at.isoformat() if row.rejected_at else None,
        "rejection_reason": row.rejection_reason,
        "status": row.status,
        "external_id": str(row.external_id),
    }


# ----------------------------------------------------------------------
# List + detail
# ----------------------------------------------------------------------


@admin_bp.route("/transfer-orders", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def list_transfer_orders():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 1000)

    status_filter = request.args.get("status")
    source_warehouse = request.args.get("source_warehouse_id", type=int)
    destination_warehouse = request.args.get("destination_warehouse_id", type=int)

    where_clauses, params = [], {}
    if status_filter:
        where_clauses.append("status = :status")
        params["status"] = status_filter
    if source_warehouse:
        where_clauses.append("source_warehouse_id = :swid")
        params["swid"] = source_warehouse
    if destination_warehouse:
        where_clauses.append("destination_warehouse_id = :dwid")
        params["dwid"] = destination_warehouse

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    total = g.db.execute(
        text(f"SELECT COUNT(*) FROM transfer_orders {where_sql}"),
        params,
    ).scalar()
    pages = max(1, math.ceil(total / per_page)) if total else 1

    params["limit"] = per_page
    params["offset"] = (page - 1) * per_page
    rows = g.db.execute(
        text(
            f"""
            SELECT to_id, to_number, source_warehouse_id, destination_warehouse_id,
                   status, created_by, notes, external_id, created_at, updated_at
              FROM transfer_orders {where_sql}
             ORDER BY to_id DESC
             LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).fetchall()

    return jsonify({
        "transfer_orders": [_serialise_to_header(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
    })


@admin_bp.route("/transfer-orders/<int:to_id>", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def get_transfer_order(to_id):
    header = g.db.execute(
        text(
            """
            SELECT to_id, to_number, source_warehouse_id, destination_warehouse_id,
                   status, created_by, notes, external_id, created_at, updated_at
              FROM transfer_orders WHERE to_id = :tid
            """
        ),
        {"tid": to_id},
    ).fetchone()
    if not header:
        return jsonify({"error": "Transfer order not found"}), 404

    lines = g.db.execute(
        text(
            """
            SELECT tol.to_line_id, tol.to_id, tol.item_id,
                   i.sku, i.item_name,
                   tol.line_number, tol.requested_qty, tol.committed_qty,
                   tol.picked_qty, tol.approved_qty, tol.status
              FROM transfer_order_lines tol
              JOIN items i ON i.item_id = tol.item_id
             WHERE tol.to_id = :tid
             ORDER BY tol.line_number
            """
        ),
        {"tid": to_id},
    ).fetchall()

    approvals = g.db.execute(
        text(
            """
            SELECT to_approval_id, to_id, submitted_by, submitted_at,
                   approved_by, approved_at, rejected_at, rejection_reason,
                   status, external_id
              FROM transfer_order_approvals
             WHERE to_id = :tid
             ORDER BY submitted_at DESC
            """
        ),
        {"tid": to_id},
    ).fetchall()

    return jsonify({
        "transfer_order": _serialise_to_header(header),
        "lines": [_serialise_to_line(r) for r in lines],
        "approvals": [_serialise_to_approval(r) for r in approvals],
    })


# ----------------------------------------------------------------------
# Pre-approval lifecycle: cancel + delete
# ----------------------------------------------------------------------


def _release_reservations(db, to_id: int, source_warehouse_id: int) -> None:
    """For every line on the TO with committed_qty > approved_qty,
    decrement inventory.quantity_allocated at the source warehouse so
    the reservation does not survive the cancellation. Inventory is
    per-bin so the release walks rows in the same order the import +
    picker use (inventory_id ASC) and decrements each row up to its
    own current allocated value."""
    lines = db.execute(
        text(
            """
            SELECT to_line_id, item_id, committed_qty - approved_qty AS to_release
              FROM transfer_order_lines
             WHERE to_id = :tid AND committed_qty > approved_qty
             ORDER BY item_id ASC
            """
        ),
        {"tid": to_id},
    ).fetchall()
    for row in lines:
        if row.to_release <= 0:
            continue
        _release_inventory_allocation(
            db, row.item_id, source_warehouse_id, row.to_release,
        )


def _release_inventory_allocation(
    db, item_id: int, warehouse_id: int, delta: int,
) -> None:
    """Decrement quantity_allocated across (item, warehouse) inventory
    rows until ``delta`` units are released. Walks inventory_id ASC
    under FOR UPDATE so concurrent operations see deterministic lock
    ordering. Each row gives up at most its own quantity_allocated to
    avoid driving the column negative."""
    if delta <= 0:
        return
    inv_rows = db.execute(
        text(
            """
            SELECT inv.inventory_id, inv.quantity_allocated
              FROM inventory inv
             WHERE inv.item_id = :iid AND inv.warehouse_id = :wid
               AND inv.quantity_allocated > 0
             ORDER BY inv.inventory_id ASC
             FOR UPDATE OF inv
            """
        ),
        {"iid": item_id, "wid": warehouse_id},
    ).fetchall()
    remaining = delta
    for ir in inv_rows:
        if remaining <= 0:
            break
        give = min(remaining, ir.quantity_allocated)
        if give == 0:
            continue
        db.execute(
            text(
                "UPDATE inventory "
                "   SET quantity_allocated = quantity_allocated - :delta "
                " WHERE inventory_id = :inv_id"
            ),
            {"delta": give, "inv_id": ir.inventory_id},
        )
        remaining -= give


@admin_bp.route("/transfer-orders/<int:to_id>/cancel", methods=["POST"])
@require_auth
@require_role("ADMIN")
@with_db
def cancel_transfer_order(to_id):
    header = g.db.execute(
        text(
            "SELECT to_id, to_number, status, source_warehouse_id "
            "  FROM transfer_orders WHERE to_id = :tid FOR UPDATE"
        ),
        {"tid": to_id},
    ).fetchone()
    if not header:
        return jsonify({"error": "Transfer order not found"}), 404

    try:
        validate_header_transition(header.status, TO_STATUS_CANCELLED)
    except ValueError as exc:
        return jsonify({
            "error": "invalid_status_for_cancel",
            "current_status": header.status,
            "detail": str(exc),
        }), 409

    # Block cancel once any approval has flipped non-PENDING (an
    # APPROVED or REJECTED approval row already moved inventory or
    # produced an audit trail; cancelling beneath it loses state).
    non_pending_approvals = g.db.execute(
        text(
            "SELECT COUNT(*) FROM transfer_order_approvals "
            " WHERE to_id = :tid AND status <> 'PENDING'"
        ),
        {"tid": to_id},
    ).scalar()
    if non_pending_approvals > 0:
        return jsonify({
            "error": "to_already_partially_approved",
            "non_pending_approvals": non_pending_approvals,
        }), 409

    _release_reservations(g.db, to_id, header.source_warehouse_id)

    g.db.execute(
        text(
            "UPDATE transfer_orders SET status = :st, updated_at = NOW() "
            " WHERE to_id = :tid"
        ),
        {"st": TO_STATUS_CANCELLED, "tid": to_id},
    )

    write_audit_log(
        g.db,
        action_type=ACTION_TO_CANCELLED,
        entity_type="TO",
        entity_id=to_id,
        user_id=g.current_user["username"],
        warehouse_id=header.source_warehouse_id,
        details={
            "to_number": header.to_number,
            "previous_status": header.status,
        },
    )
    g.db.commit()
    return jsonify({"to_id": to_id, "status": TO_STATUS_CANCELLED})


@admin_bp.route("/transfer-orders/<int:to_id>", methods=["DELETE"])
@require_auth
@require_role("ADMIN")
@with_db
def delete_transfer_order(to_id):
    header = g.db.execute(
        text(
            "SELECT to_id, to_number, status, source_warehouse_id "
            "  FROM transfer_orders WHERE to_id = :tid FOR UPDATE"
        ),
        {"tid": to_id},
    ).fetchone()
    if not header:
        return jsonify({"error": "Transfer order not found"}), 404

    # Hard delete only allowed pre-approval and pre-pick. Once any line
    # has picked_qty > 0 OR any approval row exists, the audit trail
    # would lose state; require cancel instead.
    any_picks = g.db.execute(
        text(
            "SELECT COUNT(*) FROM transfer_order_lines "
            " WHERE to_id = :tid AND picked_qty > 0"
        ),
        {"tid": to_id},
    ).scalar()
    any_approvals = g.db.execute(
        text(
            "SELECT COUNT(*) FROM transfer_order_approvals "
            " WHERE to_id = :tid"
        ),
        {"tid": to_id},
    ).scalar()
    if any_picks > 0 or any_approvals > 0:
        return jsonify({
            "error": "to_not_deletable",
            "any_picks": any_picks,
            "any_approvals": any_approvals,
            "detail": (
                "Use cancel for TOs with picks or approvals; delete "
                "only applies to OPEN TOs with no downstream activity."
            ),
        }), 409

    _release_reservations(g.db, to_id, header.source_warehouse_id)

    write_audit_log(
        g.db,
        action_type=ACTION_TO_DELETED,
        entity_type="TO",
        entity_id=to_id,
        user_id=g.current_user["username"],
        warehouse_id=header.source_warehouse_id,
        details={
            "to_number": header.to_number,
            "previous_status": header.status,
        },
    )
    g.db.execute(
        text("DELETE FROM transfer_orders WHERE to_id = :tid"),
        {"tid": to_id},
    )
    g.db.commit()
    return ("", 204)


# ----------------------------------------------------------------------
# Line short-close
# ----------------------------------------------------------------------


@admin_bp.route(
    "/transfer-orders/<int:to_id>/lines/<int:line_id>/short-close",
    methods=["POST"],
)
@require_auth
@require_role("ADMIN")
@with_db
def short_close_to_line(to_id, line_id):
    line = g.db.execute(
        text(
            """
            SELECT tol.to_line_id, tol.to_id, tol.item_id, tol.line_number,
                   tol.requested_qty, tol.committed_qty, tol.picked_qty,
                   tol.approved_qty, tol.status,
                   o.source_warehouse_id, o.to_number
              FROM transfer_order_lines tol
              JOIN transfer_orders o ON o.to_id = tol.to_id
             WHERE tol.to_line_id = :lid AND tol.to_id = :tid
             FOR UPDATE OF tol
            """
        ),
        {"lid": line_id, "tid": to_id},
    ).fetchone()
    if not line:
        return jsonify({"error": "Transfer order line not found"}), 404

    try:
        validate_line_transition(line.status, TO_LINE_SHORT_CLOSED)
    except ValueError as exc:
        return jsonify({
            "error": "invalid_line_status_for_short_close",
            "current_status": line.status,
            "detail": str(exc),
        }), 409

    # Short-close only applies to the remaining (committed - approved)
    # quantity. The release re-credits inventory.quantity_allocated for
    # the still-reserved committed_qty - approved_qty, and the line
    # transitions to SHORT_CLOSED to lock the remaining out.
    remaining = line.committed_qty - line.approved_qty
    if remaining > 0:
        _release_inventory_allocation(
            g.db, line.item_id, line.source_warehouse_id, remaining,
        )

    g.db.execute(
        text(
            "UPDATE transfer_order_lines "
            "   SET status = :st "
            " WHERE to_line_id = :lid"
        ),
        {"st": TO_LINE_SHORT_CLOSED, "lid": line_id},
    )

    write_audit_log(
        g.db,
        action_type=ACTION_TO_LINE_SHORT_CLOSED,
        entity_type="TO_LINE",
        entity_id=line_id,
        user_id=g.current_user["username"],
        warehouse_id=line.source_warehouse_id,
        details={
            "to_id": to_id,
            "to_number": line.to_number,
            "line_number": line.line_number,
            "previous_status": line.status,
            "released_qty": remaining,
        },
    )
    g.db.commit()
    return jsonify({
        "to_line_id": line_id,
        "status": TO_LINE_SHORT_CLOSED,
        "released_qty": remaining,
    })


# ============================================================
# CSV import (#291)
# ============================================================


def _resolve_warehouse_id(db, code: str) -> Optional[int]:
    row = db.execute(
        text(
            "SELECT warehouse_id FROM warehouses "
            " WHERE warehouse_code = :code AND is_active = TRUE"
        ),
        {"code": code},
    ).fetchone()
    return row.warehouse_id if row else None


@admin_bp.route("/transfer-orders/import", methods=["POST"])
@require_auth
@require_role("ADMIN")
@with_db
def import_transfer_order():
    """Build a single TO from a list of (sku, quantity) records.

    Reservations land in inventory.quantity_allocated at the source
    warehouse so SO ATP at picking_service.py:106-125 sees the
    reservation automatically (no SO-side code change). Lines whose
    requested_qty exceeds available are accepted with
    committed_qty = available; the response carries a shortage payload
    so the frontend renders the ShortageModal.
    """

    try:
        body = _ImportRequest.model_validate(request.get_json() or {})
    except ValidationError as exc:
        return jsonify({
            "error": "validation_error",
            "details": exc.errors(include_url=False, include_context=False),
        }), 422

    if body.source_warehouse_code == body.destination_warehouse_code:
        return jsonify({
            "error": "source_and_destination_must_differ",
            "warehouse_code": body.source_warehouse_code,
        }), 422

    src_id = _resolve_warehouse_id(g.db, body.source_warehouse_code)
    if src_id is None:
        return jsonify({
            "error": "unknown_warehouse",
            "field": "source_warehouse_code",
            "value": body.source_warehouse_code,
        }), 404
    dst_id = _resolve_warehouse_id(g.db, body.destination_warehouse_code)
    if dst_id is None:
        return jsonify({
            "error": "unknown_warehouse",
            "field": "destination_warehouse_code",
            "value": body.destination_warehouse_code,
        }), 404

    # Per-row Pydantic + SKU resolution. Aggregate row errors so the
    # operator sees every bad row at once rather than fixing one at a
    # time.
    errors = []
    rows = []  # list of {row_index, sku, item_id, requested_qty}
    for idx, raw in enumerate(body.records):
        try:
            row = TransferOrderImportRow.model_validate(raw)
        except ValidationError as exc:
            errors.append({
                "row_index": idx,
                "error_kind": "validation_error",
                "details": exc.errors(
                    include_url=False, include_context=False,
                ),
            })
            continue
        item_row = g.db.execute(
            text("SELECT item_id FROM items WHERE sku = :sku"),
            {"sku": row.sku},
        ).fetchone()
        if item_row is None:
            errors.append({
                "row_index": idx,
                "error_kind": "unknown_sku",
                "sku": row.sku,
            })
            continue
        rows.append({
            "row_index": idx,
            "sku": row.sku,
            "item_id": item_row.item_id,
            "requested_qty": row.quantity,
        })
    if errors:
        return jsonify({
            "error": "row_errors",
            "rows": errors,
        }), 422

    # Sort by item_id ASC before locking inventory. This matches the
    # picker + cancel-release ordering so concurrent operations
    # acquire row locks in a deterministic sequence (deadlock
    # prevention; plan section 4.4).
    rows.sort(key=lambda r: r["item_id"])

    # Inventory is per-bin (one row per (item_id, bin_id, lot_number))
    # so the reservation distributes across all rows for (item,
    # warehouse). Walk rows in inventory_id ASC under FOR UPDATE OF
    # inv to match the picking_service.py:106-125 lock ordering;
    # decrement quantity_on_hand-quantity_allocated availability per
    # row until requested_qty is satisfied or rows exhausted.
    shortages = []
    line_inserts = []  # (item_id, line_number, requested, committed)
    for line_number, row in enumerate(rows, start=1):
        inv_rows = g.db.execute(
            text(
                """
                SELECT inv.inventory_id, inv.quantity_on_hand,
                       inv.quantity_allocated
                  FROM inventory inv
                 WHERE inv.item_id = :iid AND inv.warehouse_id = :wid
                 ORDER BY inv.inventory_id ASC
                 FOR UPDATE OF inv
                """
            ),
            {"iid": row["item_id"], "wid": src_id},
        ).fetchall()
        total_available = sum(
            max(0, ir.quantity_on_hand - ir.quantity_allocated)
            for ir in inv_rows
        )
        committed = max(0, min(row["requested_qty"], total_available))
        remaining = committed
        for ir in inv_rows:
            if remaining <= 0:
                break
            avail = max(0, ir.quantity_on_hand - ir.quantity_allocated)
            take = min(remaining, avail)
            if take == 0:
                continue
            g.db.execute(
                text(
                    "UPDATE inventory "
                    "   SET quantity_allocated = quantity_allocated + :delta "
                    " WHERE inventory_id = :inv_id"
                ),
                {"delta": take, "inv_id": ir.inventory_id},
            )
            remaining -= take
        if committed < row["requested_qty"]:
            shortages.append({
                "row_index": row["row_index"],
                "sku": row["sku"],
                "requested_qty": row["requested_qty"],
                "available_qty": total_available,
                "committed_qty": committed,
                "shortfall": row["requested_qty"] - committed,
            })
        line_inserts.append((
            row["item_id"], line_number, row["requested_qty"], committed,
        ))

    # Insert TO header. Same-millisecond collision retried once before
    # surfacing 500.
    to_id = None
    for attempt in range(2):
        to_number = generate_to_number()
        try:
            result = g.db.execute(
                text(
                    """
                    INSERT INTO transfer_orders
                        (to_number, source_warehouse_id, destination_warehouse_id,
                         created_by, notes, external_id)
                    VALUES (:tn, :src, :dst, :cby, :notes, :ext)
                    RETURNING to_id, to_number
                    """
                ),
                {
                    "tn": to_number,
                    "src": src_id,
                    "dst": dst_id,
                    "cby": g.current_user["username"],
                    "notes": body.notes,
                    "ext": str(uuid.uuid4()),
                },
            )
            row = result.fetchone()
            to_id = row.to_id
            to_number = row.to_number
            break
        except IntegrityError as exc:
            g.db.rollback()
            if isinstance(exc.orig, UniqueViolation) and attempt == 0:
                continue
            raise
    if to_id is None:
        return jsonify({"error": "to_number_collision"}), 500

    for item_id, line_number, requested, committed in line_inserts:
        # Pick the line state at insert time: PENDING when fully
        # committed, SHORT_CLOSED when committed=0 (no available
        # inventory to commit so the line cannot be picked). Plan
        # section 4.13 marks committed=0 as a valid shortage path;
        # the line lands SHORT_CLOSED so it does not block closure
        # and the operator surfaces it in the shortage modal.
        line_status = TO_LINE_PENDING if committed > 0 else TO_LINE_SHORT_CLOSED
        g.db.execute(
            text(
                """
                INSERT INTO transfer_order_lines
                    (to_id, item_id, line_number, requested_qty,
                     committed_qty, status)
                VALUES (:tid, :iid, :ln, :req, :com, :st)
                """
            ),
            {
                "tid": to_id, "iid": item_id, "ln": line_number,
                "req": requested, "com": committed, "st": line_status,
            },
        )

    write_audit_log(
        g.db,
        action_type=ACTION_TO_CREATED,
        entity_type="TO",
        entity_id=to_id,
        user_id=g.current_user["username"],
        warehouse_id=src_id,
        details={
            "to_number": to_number,
            "source_warehouse_id": src_id,
            "destination_warehouse_id": dst_id,
            "line_count": len(line_inserts),
            "shortage_count": len(shortages),
        },
    )
    g.db.commit()

    return jsonify({
        "to_id": to_id,
        "to_number": to_number,
        "source_warehouse_id": src_id,
        "destination_warehouse_id": dst_id,
        "line_count": len(line_inserts),
        "shortages": shortages,
    }), 201


# ============================================================
# Start-picking (#292)
# ============================================================


@admin_bp.route(
    "/transfer-orders/<int:to_id>/start-picking", methods=["POST"],
)
@require_auth
@require_role("ADMIN")
@with_db
def start_to_picking(to_id):
    """Create a pick_batch + pick_tasks for the TO so the picker can
    scan via the existing mobile picking flow.

    One pick_task per (TO line, inventory bin) at the source warehouse,
    consuming committed_qty across bins in inventory_id ASC. Bin-level
    distribution mirrors picking_service.create_pick_batch_for_orders
    so concurrent SO + TO batch creation acquires inventory locks in
    the same order. The XOR CHECK from mig 049 enforces so_id NULL +
    to_id NOT NULL on every inserted row.
    """
    header = g.db.execute(
        text(
            "SELECT to_id, to_number, status, source_warehouse_id "
            "  FROM transfer_orders WHERE to_id = :tid FOR UPDATE"
        ),
        {"tid": to_id},
    ).fetchone()
    if not header:
        return jsonify({"error": "Transfer order not found"}), 404
    if header.status not in (TO_STATUS_OPEN, TO_STATUS_PARTIALLY_PICKED):
        return jsonify({
            "error": "invalid_status_for_start_picking",
            "current_status": header.status,
        }), 409

    # Lines that still need picking (committed > picked) and are not
    # SHORT_CLOSED. Walk in line_number ASC for predictable picker UX.
    lines = g.db.execute(
        text(
            """
            SELECT to_line_id, item_id, line_number,
                   committed_qty, picked_qty
              FROM transfer_order_lines
             WHERE to_id = :tid
               AND status IN ('PENDING', 'PARTIALLY_PICKED')
               AND committed_qty > picked_qty
             ORDER BY line_number
            """
        ),
        {"tid": to_id},
    ).fetchall()
    if not lines:
        return jsonify({
            "error": "no_lines_to_pick",
            "detail": "Every line is either fully picked or short-closed.",
        }), 409

    # Create pick_batch anchor.
    from datetime import datetime
    batch_number = (
        f"BATCH-TO-{header.to_number[3:]}-"
        f"{datetime.now().strftime('%H%M%S%f')[:9]}"
    )
    batch_row = g.db.execute(
        text(
            "INSERT INTO pick_batches "
            "(batch_number, warehouse_id, status, assigned_to) "
            "VALUES (:bn, :wid, 'OPEN', :user) RETURNING batch_id"
        ),
        {
            "bn": batch_number,
            "wid": header.source_warehouse_id,
            "user": g.current_user["username"],
        },
    ).fetchone()
    batch_id = batch_row.batch_id

    # For each line, walk inventory bins at the source warehouse (in
    # inventory_id ASC) and create pick_tasks until the line's
    # remaining commitment is covered.
    pick_sequence = 0
    tasks_created = 0
    for line in lines:
        remaining = line.committed_qty - line.picked_qty
        inv_rows = g.db.execute(
            text(
                """
                SELECT inv.inventory_id, inv.bin_id, inv.quantity_on_hand,
                       inv.quantity_allocated
                  FROM inventory inv
                 WHERE inv.item_id = :iid AND inv.warehouse_id = :wid
                   AND inv.quantity_on_hand > 0
                 ORDER BY inv.inventory_id ASC
                """
            ),
            {"iid": line.item_id, "wid": header.source_warehouse_id},
        ).fetchall()
        for ir in inv_rows:
            if remaining <= 0:
                break
            take = min(remaining, ir.quantity_on_hand)
            if take == 0:
                continue
            pick_sequence += 1
            g.db.execute(
                text(
                    """
                    INSERT INTO pick_tasks
                        (batch_id, to_id, to_line_id, item_id, bin_id,
                         quantity_to_pick, pick_sequence, status)
                    VALUES (:bid, :tid, :lid, :iid, :binid, :qty,
                            :seq, 'PENDING')
                    """
                ),
                {
                    "bid": batch_id,
                    "tid": to_id,
                    "lid": line.to_line_id,
                    "iid": line.item_id,
                    "binid": ir.bin_id,
                    "qty": take,
                    "seq": pick_sequence,
                },
            )
            remaining -= take
            tasks_created += 1
        if remaining > 0:
            # Source warehouse cannot fulfil the committed quantity in
            # any bin. The line keeps its committed_qty (the importer
            # already validated availability) but the picker can't
            # walk to anything; surface for operator action.
            g.db.rollback()
            return jsonify({
                "error": "no_pickable_inventory",
                "to_line_id": line.to_line_id,
                "item_id": line.item_id,
                "remaining": remaining,
            }), 409

    g.db.commit()
    return jsonify({
        "batch_id": batch_id,
        "batch_number": batch_number,
        "tasks_created": tasks_created,
    }), 201


# ============================================================
# Picker-facing TO read endpoint (#292)
# ============================================================


@admin_bp.route("/picker/transfer-orders/<int:to_id>", methods=["GET"])
@require_auth
@with_db
def picker_get_transfer_order(to_id):
    """Read-only TO state for the mobile picking screen. No ADMIN
    gate; any authenticated user with warehouse access can view."""
    header = g.db.execute(
        text(
            """
            SELECT to_id, to_number, source_warehouse_id,
                   destination_warehouse_id, status,
                   created_by, notes, external_id, created_at, updated_at
              FROM transfer_orders WHERE to_id = :tid
            """
        ),
        {"tid": to_id},
    ).fetchone()
    if not header:
        return jsonify({"error": "Transfer order not found"}), 404

    lines = g.db.execute(
        text(
            """
            SELECT tol.to_line_id, tol.to_id, tol.item_id,
                   i.sku, i.item_name,
                   tol.line_number, tol.requested_qty, tol.committed_qty,
                   tol.picked_qty, tol.approved_qty, tol.status
              FROM transfer_order_lines tol
              JOIN items i ON i.item_id = tol.item_id
             WHERE tol.to_id = :tid
             ORDER BY tol.line_number
            """
        ),
        {"tid": to_id},
    ).fetchall()

    return jsonify({
        "transfer_order": _serialise_to_header(header),
        "lines": [_serialise_to_line(r) for r in lines],
    })


# ============================================================
# Picker submit (#293)
# ============================================================


@admin_bp.route("/picker/transfer-orders/<int:to_id>/submit", methods=["POST"])
@require_auth
@with_db
def submit_transfer_order_picks(to_id):
    """Picker batches their picks since the last submit into a
    transfer_order_approvals row for admin review. Multiple submits
    per TO are normal: each batch is approved or rejected
    independently. Inventory does not move at submit time.
    """
    header = g.db.execute(
        text(
            "SELECT to_id, to_number, status, source_warehouse_id "
            "  FROM transfer_orders WHERE to_id = :tid FOR UPDATE"
        ),
        {"tid": to_id},
    ).fetchone()
    if not header:
        return jsonify({"error": "Transfer order not found"}), 404
    if header.status not in (
        TO_STATUS_PARTIALLY_PICKED, TO_STATUS_AWAITING_APPROVAL,
    ):
        return jsonify({
            "error": "invalid_status_for_submit",
            "current_status": header.status,
        }), 409

    # Lines with new picks since the last submit: picked_qty exceeds
    # what has already been APPROVED on this line. Snapshots the
    # delta so the approval row is independent of subsequent picks.
    lines = g.db.execute(
        text(
            """
            SELECT to_line_id, item_id, picked_qty, approved_qty,
                   committed_qty, status
              FROM transfer_order_lines
             WHERE to_id = :tid AND picked_qty > approved_qty
             ORDER BY line_number
            """
        ),
        {"tid": to_id},
    ).fetchall()
    if not lines:
        return jsonify({
            "error": "nothing_picked",
            "detail": (
                "No new picks since the last submit. Pick more lines "
                "before submitting."
            ),
        }), 422

    snapshot_lines = [
        {
            "to_line_id": line.to_line_id,
            "item_id": line.item_id,
            "picked_in_snapshot": line.picked_qty - line.approved_qty,
        }
        for line in lines
    ]
    approval_row = g.db.execute(
        text(
            """
            INSERT INTO transfer_order_approvals
                (to_id, submitted_by, lines_snapshot, external_id)
            VALUES (:tid, :sub, CAST(:snap AS JSONB), :ext)
            RETURNING to_approval_id
            """
        ),
        {
            "tid": to_id,
            "sub": g.current_user["username"],
            "snap": json.dumps({"lines": snapshot_lines}),
            "ext": str(uuid.uuid4()),
        },
    ).fetchone()
    approval_id = approval_row.to_approval_id

    # Header status: AWAITING_APPROVAL when every line is fully
    # picked or short-closed; otherwise PARTIALLY_PICKED stays.
    open_lines = g.db.execute(
        text(
            "SELECT COUNT(*) FROM transfer_order_lines "
            " WHERE to_id = :tid "
            "   AND status IN (:pending, :partial)"
        ),
        {
            "tid": to_id,
            "pending": TO_LINE_PENDING,
            "partial": TO_LINE_PARTIALLY_PICKED,
        },
    ).scalar()
    new_status = (
        TO_STATUS_AWAITING_APPROVAL if open_lines == 0
        else TO_STATUS_PARTIALLY_PICKED
    )
    if header.status != new_status:
        g.db.execute(
            text(
                "UPDATE transfer_orders SET status = :st, updated_at = NOW() "
                " WHERE to_id = :tid"
            ),
            {"st": new_status, "tid": to_id},
        )

    write_audit_log(
        g.db,
        action_type=ACTION_TO_SUBMITTED,
        entity_type="TO",
        entity_id=to_id,
        user_id=g.current_user["username"],
        warehouse_id=header.source_warehouse_id,
        details={
            "to_number": header.to_number,
            "to_approval_id": approval_id,
            "line_count": len(snapshot_lines),
        },
    )
    g.db.commit()
    return jsonify({
        "to_approval_id": approval_id,
        "status": "PENDING",
        "to_status": new_status,
        "line_count": len(snapshot_lines),
    }), 201


# ============================================================
# Admin approve (#293)
# ============================================================


def _self_approval_blocked(db) -> bool:
    row = db.execute(
        text(
            "SELECT value FROM app_settings "
            " WHERE key = 'transfer_order_block_self_approval'"
        ),
    ).fetchone()
    return bool(row and str(row.value).lower() == "true")


def _decrement_source_inventory(
    db, item_id: int, warehouse_id: int, qty: int,
) -> None:
    """Decrement quantity_allocated AND quantity_on_hand at source by
    qty, distributing across bins in inventory_id ASC up to each row's
    own quantity_allocated. Mirrors the import + cancel lock ordering
    so concurrent operations stay deadlock-free."""
    inv_rows = db.execute(
        text(
            """
            SELECT inv.inventory_id, inv.quantity_on_hand,
                   inv.quantity_allocated
              FROM inventory inv
             WHERE inv.item_id = :iid AND inv.warehouse_id = :wid
               AND inv.quantity_allocated > 0
             ORDER BY inv.inventory_id ASC
             FOR UPDATE OF inv
            """
        ),
        {"iid": item_id, "wid": warehouse_id},
    ).fetchall()
    remaining = qty
    for ir in inv_rows:
        if remaining <= 0:
            break
        give = min(remaining, ir.quantity_allocated, ir.quantity_on_hand)
        if give == 0:
            continue
        db.execute(
            text(
                """
                UPDATE inventory
                   SET quantity_allocated = quantity_allocated - :delta,
                       quantity_on_hand   = quantity_on_hand   - :delta,
                       updated_at = NOW()
                 WHERE inventory_id = :inv_id
                """
            ),
            {"delta": give, "inv_id": ir.inventory_id},
        )
        remaining -= give
    if remaining > 0:
        # Source under-funded: import-time reservation must have been
        # released by a concurrent cancel. Surface as 409 so the
        # admin investigates rather than silently absorbing.
        raise ValueError(
            f"source inventory under-funded: still {remaining} units to "
            f"deduct for item_id={item_id} at warehouse_id={warehouse_id}"
        )


def _credit_destination_inventory(
    db, item_id: int, dest_warehouse_id: int, qty: int,
) -> None:
    """Add qty to the destination warehouse's inventory. Targets the
    first Staging bin at the destination; INSERTs a new inventory
    row if none exists for the (item, bin) pair. Raises ValueError
    when the destination has no Staging bin (operator must add one).
    """
    bin_row = db.execute(
        text(
            """
            SELECT bin_id FROM bins
             WHERE warehouse_id = :wid
               AND bin_type = 'Staging'
             ORDER BY bin_id ASC LIMIT 1
            """
        ),
        {"wid": dest_warehouse_id},
    ).fetchone()
    if not bin_row:
        raise ValueError(
            f"destination warehouse_id={dest_warehouse_id} has no "
            f"Staging bin; add one before approving the transfer."
        )
    bin_id = bin_row.bin_id
    existing = db.execute(
        text(
            """
            SELECT inventory_id FROM inventory
             WHERE item_id = :iid AND bin_id = :bid AND lot_number IS NULL
             FOR UPDATE
            """
        ),
        {"iid": item_id, "bid": bin_id},
    ).fetchone()
    if existing:
        db.execute(
            text(
                "UPDATE inventory "
                "   SET quantity_on_hand = quantity_on_hand + :qty, "
                "       updated_at = NOW() "
                " WHERE inventory_id = :inv_id"
            ),
            {"qty": qty, "inv_id": existing.inventory_id},
        )
    else:
        db.execute(
            text(
                "INSERT INTO inventory "
                "(item_id, bin_id, warehouse_id, quantity_on_hand) "
                "VALUES (:iid, :bid, :wid, :qty)"
            ),
            {
                "iid": item_id, "bid": bin_id,
                "wid": dest_warehouse_id, "qty": qty,
            },
        )


@admin_bp.route(
    "/transfer-orders/<int:to_id>/approvals/<int:approval_id>/approve",
    methods=["POST"],
)
@require_auth
@require_role("ADMIN")
@with_db
def approve_transfer_order_approval(to_id, approval_id):
    approval = g.db.execute(
        text(
            """
            SELECT to_approval_id, to_id, submitted_by, status,
                   approved_by, lines_snapshot, external_id
              FROM transfer_order_approvals
             WHERE to_approval_id = :aid AND to_id = :tid
             FOR UPDATE
            """
        ),
        {"aid": approval_id, "tid": to_id},
    ).fetchone()
    if not approval:
        return jsonify({"error": "Approval row not found"}), 404
    if approval.status != TO_APPROVAL_PENDING:
        return jsonify({
            "error": "approval_not_pending",
            "current_status": approval.status,
            "approved_by": approval.approved_by,
        }), 409

    if (_self_approval_blocked(g.db)
            and approval.submitted_by == g.current_user["username"]):
        return jsonify({
            "error": "self_approval_blocked",
            "submitted_by": approval.submitted_by,
            "detail": (
                "app_settings.transfer_order_block_self_approval is TRUE; "
                "a different admin must approve this submission."
            ),
        }), 403

    header = g.db.execute(
        text(
            "SELECT to_id, to_number, status, source_warehouse_id, "
            "       destination_warehouse_id, external_id "
            "  FROM transfer_orders WHERE to_id = :tid FOR UPDATE"
        ),
        {"tid": to_id},
    ).fetchone()
    if not header:
        return jsonify({"error": "Transfer order not found"}), 404

    snapshot_lines = (approval.lines_snapshot or {}).get("lines", []) or []
    if not snapshot_lines:
        return jsonify({
            "error": "approval_snapshot_empty",
            "detail": "Approval row has no line snapshot; nothing to approve.",
        }), 422

    # Process lines in item_id ASC for deterministic inventory locking.
    snapshot_lines.sort(key=lambda r: r["item_id"])

    event_lines = []
    try:
        for snap in snapshot_lines:
            line_id = snap["to_line_id"]
            item_id = snap["item_id"]
            qty = int(snap["picked_in_snapshot"])
            if qty <= 0:
                continue
            g.db.execute(
                text(
                    """
                    UPDATE transfer_order_lines
                       SET approved_qty = approved_qty + :qty,
                           status = CASE
                               WHEN approved_qty + :qty = picked_qty
                                    AND picked_qty = committed_qty
                                    THEN 'APPROVED'
                               ELSE status
                           END
                     WHERE to_line_id = :lid
                    """
                ),
                {"qty": qty, "lid": line_id},
            )
            _decrement_source_inventory(
                g.db, item_id, header.source_warehouse_id, qty,
            )
            _credit_destination_inventory(
                g.db, item_id, header.destination_warehouse_id, qty,
            )
            item_external = g.db.execute(
                text("SELECT external_id FROM items WHERE item_id = :iid"),
                {"iid": item_id},
            ).fetchone()
            # Resolve canonical UUID to source-system external_id for the
            # outbound payload. Canonical UUID fallback when no upstream
            # mapping exists. See resolve_source_external_id docstring.
            item_source_external_id = (
                resolve_source_external_id(g.db, "item", item_external.external_id)
                or str(item_external.external_id)
            )
            event_lines.append({
                "item_external_id": item_source_external_id,
                "quantity": qty,
            })
    except ValueError as exc:
        g.db.rollback()
        return jsonify({"error": "approval_failed", "detail": str(exc)}), 409

    approved_at = datetime.now(timezone.utc)
    g.db.execute(
        text(
            """
            UPDATE transfer_order_approvals
               SET status = :st, approved_by = :by, approved_at = :at
             WHERE to_approval_id = :aid
            """
        ),
        {
            "st": TO_APPROVAL_APPROVED,
            "by": g.current_user["username"],
            "at": approved_at,
            "aid": approval_id,
        },
    )

    write_audit_log(
        g.db,
        action_type=ACTION_TO_APPROVED,
        entity_type="TO_APPROVAL",
        entity_id=approval_id,
        user_id=g.current_user["username"],
        warehouse_id=header.source_warehouse_id,
        details={
            "to_id": to_id,
            "to_number": header.to_number,
            "line_count": len(event_lines),
        },
    )

    closed = False
    if evaluate_to_closure(g.db, to_id):
        g.db.execute(
            text(
                "UPDATE transfer_orders "
                "   SET status = :st, updated_at = NOW() "
                " WHERE to_id = :tid"
            ),
            {"st": TO_STATUS_CLOSED, "tid": to_id},
        )
        write_audit_log(
            g.db,
            action_type=ACTION_TO_CLOSED,
            entity_type="TO",
            entity_id=to_id,
            user_id=g.current_user["username"],
            warehouse_id=header.source_warehouse_id,
            details={"to_number": header.to_number},
        )
        closed = True

    if event_lines:
        emit_event(
            g.db,
            event_type="transfer.completed",
            event_version=1,
            aggregate_type="inventory_transfer",
            aggregate_id=approval_id,
            aggregate_external_id=approval.external_id,
            warehouse_id=header.destination_warehouse_id,
            source_txn_id=getattr(g, "source_txn_id", None) or str(uuid.uuid4()),
            payload={
                "transfer_external_id": str(approval.external_id),
                "from_warehouse_id": header.source_warehouse_id,
                "to_warehouse_id": header.destination_warehouse_id,
                "lines": event_lines,
                "completed_at": approved_at.isoformat().replace(
                    "+00:00", "Z",
                ),
            },
        )
    g.db.commit()
    return jsonify({
        "to_approval_id": approval_id,
        "status": "APPROVED",
        "to_closed": closed,
        "line_count": len(event_lines),
    })


# ============================================================
# Admin reject (#293)
# ============================================================


@admin_bp.route(
    "/transfer-orders/<int:to_id>/approvals/<int:approval_id>/reject",
    methods=["POST"],
)
@require_auth
@require_role("ADMIN")
@with_db
def reject_transfer_order_approval(to_id, approval_id):
    body = request.get_json() or {}
    rejection_reason = (body.get("rejection_reason") or "").strip()[:1000]
    approval = g.db.execute(
        text(
            """
            SELECT to_approval_id, to_id, submitted_by, status, external_id
              FROM transfer_order_approvals
             WHERE to_approval_id = :aid AND to_id = :tid
             FOR UPDATE
            """
        ),
        {"aid": approval_id, "tid": to_id},
    ).fetchone()
    if not approval:
        return jsonify({"error": "Approval row not found"}), 404
    if approval.status != TO_APPROVAL_PENDING:
        return jsonify({
            "error": "approval_not_pending",
            "current_status": approval.status,
        }), 409

    rejected_at = datetime.now(timezone.utc)
    g.db.execute(
        text(
            """
            UPDATE transfer_order_approvals
               SET status = :st, rejected_at = :at,
                   rejection_reason = :reason
             WHERE to_approval_id = :aid
            """
        ),
        {
            "st": TO_APPROVAL_REJECTED,
            "at": rejected_at,
            "reason": rejection_reason or None,
            "aid": approval_id,
        },
    )

    header = g.db.execute(
        text(
            "SELECT to_number, source_warehouse_id "
            "  FROM transfer_orders WHERE to_id = :tid"
        ),
        {"tid": to_id},
    ).fetchone()

    write_audit_log(
        g.db,
        action_type=ACTION_TO_REJECTED,
        entity_type="TO_APPROVAL",
        entity_id=approval_id,
        user_id=g.current_user["username"],
        warehouse_id=header.source_warehouse_id if header else None,
        details={
            "to_id": to_id,
            "to_number": header.to_number if header else None,
            "submitted_by": approval.submitted_by,
            "rejection_reason": rejection_reason or None,
        },
    )
    g.db.commit()
    return jsonify({
        "to_approval_id": approval_id,
        "status": "REJECTED",
    })
