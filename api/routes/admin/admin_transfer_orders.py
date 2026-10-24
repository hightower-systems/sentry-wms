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
from middleware.auth_middleware import require_admin_or_page_permission, require_auth, check_warehouse_access
from middleware.db import with_db
from routes.admin import admin_bp
from schemas.csv_import import TransferOrderImportRow
from schemas.transfer_approval import TransferApprovalRequest
from services.transfer_allocations import transfer_pick_sources, prepare_transfer_allocations, apply_transfer_allocations
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
    submit_picks,
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
        "lines_snapshot": row.lines_snapshot,
        "review_source_txn_id": row.review_source_txn_id,
    }


# ----------------------------------------------------------------------
# List + detail
# ----------------------------------------------------------------------


@admin_bp.route("/transfer-orders", methods=["GET"])
@require_auth
@require_admin_or_page_permission("transfer-orders")
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
@require_admin_or_page_permission("transfer-orders")
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
                   status, external_id, lines_snapshot,
                   (SELECT CASE WHEN COUNT(DISTINCT event.source_txn_id) = 1 THEN MAX(event.source_txn_id::text) ELSE NULL END
                    FROM integration_events event WHERE event.aggregate_type = 'inventory_transfer'
                      AND event.aggregate_id = transfer_order_approvals.to_approval_id
                      AND event.aggregate_external_id = transfer_order_approvals.external_id
                      AND event.event_type = 'inventorytransfer.completed') AS review_source_txn_id
              FROM transfer_order_approvals
             WHERE to_id = :tid
             ORDER BY submitted_at DESC
            """
        ),
        {"tid": to_id},
    ).fetchall()

    pick_allocations = transfer_pick_sources(g.db, to_id, header.source_warehouse_id)
    source_inventory = g.db.execute(text("""
        SELECT inventory.inventory_id, inventory.item_id, inventory.bin_id AS source_bin_id,
               bin.bin_code, inventory.lot_number, inventory.quantity_on_hand, inventory.quantity_allocated
        FROM inventory JOIN bins bin ON bin.bin_id = inventory.bin_id
        WHERE inventory.warehouse_id = :warehouse_id AND bin.warehouse_id = :warehouse_id AND bin.is_active
          AND inventory.item_id IN (SELECT item_id FROM transfer_order_lines WHERE to_id = :to_id)
        ORDER BY inventory.inventory_id LIMIT 10001
    """), {"warehouse_id": header.source_warehouse_id, "to_id": to_id}).fetchall()
    destinations = g.db.execute(text("SELECT bin_id, bin_code, bin_type FROM bins WHERE warehouse_id = :warehouse_id AND is_active ORDER BY bin_id LIMIT 10001"), {"warehouse_id": header.destination_warehouse_id}).fetchall()
    if len(source_inventory) > 10000 or len(destinations) > 10000:
        return jsonify({"error": "Transfer allocation detail exceeds the supported limit"}), 409
    return jsonify({
        "transfer_order": _serialise_to_header(header),
        "pick_allocations": pick_allocations,
        "source_inventory": [{"inventory_id": row.inventory_id, "item_id": row.item_id,
            "source_bin_id": row.source_bin_id, "bin_code": row.bin_code, "lot_number": row.lot_number,
            "quantity_on_hand": row.quantity_on_hand, "quantity_allocated": row.quantity_allocated} for row in source_inventory],
        "destination_bins": [{"bin_id": row.bin_id, "bin_code": row.bin_code, "bin_type": row.bin_type} for row in destinations],
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
@require_admin_or_page_permission("transfer-orders")
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
@require_admin_or_page_permission("transfer-orders")
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
@require_admin_or_page_permission("transfer-orders")
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
@require_admin_or_page_permission("transfer-orders")
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
@require_admin_or_page_permission("transfer-orders")
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

    # The header lock serializes starts. Reuse its active batch so a repeated
    # request cannot create another set of pending tasks over the same picks.
    active = g.db.execute(text(
        "SELECT DISTINCT pb.batch_id, pb.batch_number FROM pick_batches pb "
        "JOIN pick_tasks pt ON pt.batch_id = pb.batch_id "
        "WHERE pt.to_id = :tid AND pb.status IN ('OPEN', 'IN_PROGRESS')"
    ), {"tid": to_id}).fetchall()
    if active:
        if len(active) != 1:
            return jsonify({"error": "multiple_active_pick_batches"}), 409
        return jsonify({"batch_id": active[0].batch_id,
                        "batch_number": active[0].batch_number,
                        "replayed": True}), 200

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
    result = submit_picks(g.db, to_id, g.current_user["username"])
    if not result["ok"]:
        reason = result["reason"]
        if reason == "not_found":
            return jsonify({"error": "Transfer order not found"}), 404
        if reason == "invalid_status":
            return jsonify({
                "error": "invalid_status_for_submit",
                "current_status": result["current_status"],
            }), 409
        # nothing_picked
        return jsonify({
            "error": "nothing_picked",
            "detail": (
                "No new picks since the last submit. Pick more lines "
                "before submitting."
            ),
        }), 422
    g.db.commit()
    return jsonify({
        "to_approval_id": result["to_approval_id"],
        "status": "PENDING",
        "to_status": result["to_status"],
        "line_count": result["line_count"],
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


@admin_bp.route(
    "/transfer-orders/<int:to_id>/approvals/<int:approval_id>/approve",
    methods=["POST"],
)
@require_auth
@require_admin_or_page_permission("transfer-orders")
@with_db
def approve_transfer_order_approval(to_id, approval_id):
    try:
        validated = TransferApprovalRequest.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": "invalid_transfer_allocations", "detail": str(exc)}), 422
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

    for warehouse_id in sorted({header.source_warehouse_id, header.destination_warehouse_id}):
        allowed, denial = check_warehouse_access(warehouse_id)
        if not allowed:
            return denial
    if approval.status == TO_APPROVAL_APPROVED:
        stored = (approval.lines_snapshot or {}).get("allocations", [])
        supplied = [entry.model_dump() for entry in validated.allocations] if validated.allocations is not None else None
        keys = ("to_line_id", "source_bin_id", "destination_bin_id", "quantity", "lot_number", "serial_number")
        def canonical(rows):
            return sorted(json.dumps([(row.get(key) or "").strip() or None if key in ("lot_number", "serial_number") else row.get(key) for key in keys]) for row in rows)
        if (not stored or (supplied is None and any(row.get("lot_number") or row.get("serial_number") for row in stored))
                or (supplied is not None and canonical(stored) != canonical(supplied))):
            return jsonify({"error": "approved_allocation_mismatch"}), 409
        return jsonify({"to_approval_id": approval_id, "status": "APPROVED", "replayed": True, "allocations": stored}), 200
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

    if header.status not in (TO_STATUS_AWAITING_APPROVAL, TO_STATUS_PARTIALLY_PICKED):
        return jsonify({"error": "transfer_not_awaiting_approval", "status": header.status}), 409
    snapshot_lines = (approval.lines_snapshot or {}).get("lines", []) or []
    if not snapshot_lines:
        return jsonify({
            "error": "approval_snapshot_empty",
            "detail": "Approval row has no line snapshot; nothing to approve.",
        }), 422

    event_lines = []
    try:
        allocations, stock_total, stock_ids = prepare_transfer_allocations(g.db, header, snapshot_lines, validated.allocations)
        apply_transfer_allocations(g.db, header, allocations, stock_total, stock_ids)
        for snap in sorted(snapshot_lines, key=lambda row: (row["item_id"], row["to_line_id"])):
            updated = g.db.execute(text("""
                UPDATE transfer_order_lines SET approved_qty = approved_qty + :qty,
                    status = CASE WHEN approved_qty + :qty = picked_qty AND picked_qty = committed_qty THEN 'APPROVED' ELSE status END
                WHERE to_line_id = :line_id AND to_id = :to_id AND item_id = :item_id
                  AND approved_qty + :qty <= picked_qty RETURNING to_line_id
            """), {"qty": snap["picked_in_snapshot"], "line_id": snap["to_line_id"], "to_id": to_id, "item_id": snap["item_id"]}).fetchone()
            if updated is None:
                raise ValueError("The transfer line changed during approval")
        external = {}
        for allocation in allocations:
            item_id = allocation["item_id"]
            if item_id not in external:
                item = g.db.execute(text("SELECT external_id FROM items WHERE item_id = :item_id"), {"item_id": item_id}).fetchone()
                external[item_id] = resolve_source_external_id(g.db, "item", item.external_id) or str(item.external_id)
            event_lines.append({"item_external_id": external[item_id], **{key: allocation[key] for key in ("to_line_id", "source_bin_id", "destination_bin_id", "quantity", "lot_number", "serial_number")}})
    except ValueError as exc:
        g.db.rollback()
        return jsonify({"error": "approval_failed", "detail": str(exc)}), 409
    allocation_snapshot = {**approval.lines_snapshot, "allocations": allocations}
    g.db.execute(text("UPDATE transfer_order_approvals SET lines_snapshot = CAST(:snapshot AS JSONB) WHERE to_approval_id = :approval_id AND status = :pending"), {"snapshot": json.dumps(allocation_snapshot), "approval_id": approval_id, "pending": TO_APPROVAL_PENDING})

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
            event_type="inventorytransfer.completed",
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
@require_admin_or_page_permission("transfer-orders")
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
