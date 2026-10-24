"""Purchase Orders, Sales Orders, and Short Picks endpoints."""

import math
import uuid
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from flask import g, jsonify, request
from sqlalchemy import text

from constants import (
    PO_OPEN, PO_PARTIAL, PO_RECEIVED, PO_CLOSED, PO_ARCHIVED,
    SO_OPEN, SO_PICKED, SO_PACKED, SO_SHIPPED, SO_CANCELLED, SO_REFUNDED,
    SO_FRAUD_REVIEW, SO_WAITING_STOCK,
    TASK_PENDING, ADJ_PENDING, ADJ_APPROVED, ADJ_REASON_SHORT,
    ORDER_TYPE_BACKORDER,
    order_type_allows_fulfillment_ops,
    CANCEL_REASONS, CANCEL_REASON_OTHER, CANCEL_REASON_REFUNDED,
    COMPANY_TIMEZONE,
    ACTION_PICK,
    ACTION_PO_STATUS_CHANGED,
    ACTION_PO_LINE_ADDED,
    ACTION_PO_LINE_UPDATED,
    ACTION_PO_LINE_REMOVED,
    ACTION_SO_ADDRESS_EDITED,
    ACTION_SO_FRAUD_CLEARED,
    ACTION_SO_MEMO_EDITED,
    ACTION_SO_HEADER_EDITED,
    ACTION_SO_LINE_ADDED,
    ACTION_SO_LINE_UPDATED,
    ACTION_SO_LINE_REMOVED,
    ACTION_SO_SOURCE_SYSTEM_REASSIGNED,
    ACTION_SO_ALLOCATION_RELEASED,
    ACTION_PARTIAL_FULFILL,
    ACTION_BACKORDER_CANCELLED,
    OVERRIDE_SO_FULL_EDIT,
    ROLE_ADMIN,
)
from middleware.auth_middleware import (
    has_override,
    require_admin_or_page_permission,
    require_auth,
    require_role,
)
from middleware.db import with_db
from routes.admin import admin_bp
from schemas.purchase_orders import (
    AddPOLineRequest,
    CreatePurchaseOrderRequest,
    UpdatePOLineRequest,
    UpdatePurchaseOrderRequest,
)
from schemas.sales_orders import (
    ADDRESS_FIELD_NAMES,
    AddSalesOrderLineRequest,
    AdminPickRequest,
    CancelBackorderRequest,
    CreateSalesOrderRequest,
    MarkSalesOrdersPrintedRequest,
    PartialFulfillRequest,
    RevertSalesOrderStatusRequest,
    UpdateSalesOrderAddressRequest,
    UpdateLineAllocationRequest,
    UpdateSalesOrderLineRequest,
    UpdateSalesOrderMemoRequest,
    UpdateSalesOrderRequest,
    CreateRmaRequest,
    ReceiveRmaRequest,
)
from services.audit_service import write_audit_log
from services.events_service import (
    emit_event,
    get_user_external_id,
    resolve_source_external_id,
)
from services.sales_order_service import (
    AdminPickError,
    CancelNotAllowed,
    ReturnVoidNotAllowed,
    RevertNotAllowed,
    cancel_sales_order as _cancel_so,
    maybe_promote_so_to_picked,
    record_admin_pick,
    revert_sales_order_status as _revert_so_status,
    create_rma as _create_rma,
    receive_rma as _receive_rma,
    void_return_order as _void_return,
)
from services.receiving_service import (
    UnreceiveError,
    record_unreceive,
    recompute_po_status,
)
from services.shipping_service import (
    AdminShipError,
    carrier_from_ship_method,
    record_admin_ship,
    record_ship,
)
from services.webhook_dispatcher.backorder_notifier import (
    dispatch_backorder_notification,
)
from utils.validation import validate_body


# ── Purchase Orders ───────────────────────────────────────────────────────────

@admin_bp.route("/purchase-orders", methods=["GET"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@with_db
def list_purchase_orders():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 1000)

    where_clauses, params = [], {}
    status = request.args.get("status")
    warehouse_id = request.args.get("warehouse_id", type=int)
    search = (request.args.get("q") or "").strip()
    # ARCHIVED is hidden by default so the active list stays focused
    # on POs that still need work. Caller opts in by passing
    # include_archived=true OR by setting status=ARCHIVED explicitly.
    include_archived = request.args.get("include_archived", "false").lower() == "true"
    if status:
        where_clauses.append("status = :status")
        params["status"] = status
    elif not include_archived:
        where_clauses.append("status <> :archived")
        params["archived"] = PO_ARCHIVED
    if warehouse_id:
        where_clauses.append("warehouse_id = :wid")
        params["wid"] = warehouse_id
    if search:
        where_clauses.append("(po_number ILIKE :q OR vendor_name ILIKE :q)")
        params["q"] = f"%{search}%"

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    total = g.db.execute(text(f"SELECT COUNT(*) FROM purchase_orders {where_sql}"), params).scalar()
    pages = max(1, math.ceil(total / per_page))

    params["limit"] = per_page
    params["offset"] = (page - 1) * per_page
    rows = g.db.execute(
        text(f"""
            SELECT po_id, po_number, po_barcode, vendor_name, status, expected_date,
                   warehouse_id, notes, created_at, received_at, created_by
            FROM purchase_orders {where_sql} ORDER BY po_id DESC LIMIT :limit OFFSET :offset
        """),
        params,
    ).fetchall()

    return jsonify({
        "purchase_orders": [
            {"po_id": r.po_id, "po_number": r.po_number, "po_barcode": r.po_barcode,
             "vendor_name": r.vendor_name, "status": r.status,
             "expected_date": r.expected_date.isoformat() if r.expected_date else None,
             "warehouse_id": r.warehouse_id, "notes": r.notes,
             "created_at": r.created_at.isoformat() if r.created_at else None,
             "received_at": r.received_at.isoformat() if r.received_at else None,
             "created_by": r.created_by}
            for r in rows
        ],
        "total": total, "page": page, "per_page": per_page, "pages": pages,
    })


@admin_bp.route("/purchase-orders/<int:po_id>", methods=["GET"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@with_db
def get_purchase_order(po_id):
    po = g.db.execute(
        text("SELECT po_id, external_id, latest_inbound_id, po_number, po_barcode, vendor_name, vendor_id, status, expected_date, warehouse_id, notes, created_at, received_at, created_by FROM purchase_orders WHERE po_id = :pid"),
        {"pid": po_id},
    ).fetchone()
    if not po:
        return jsonify({"error": "Purchase order not found"}), 404

    lines = g.db.execute(
        text("""
            SELECT pol.po_line_id, pol.line_number, pol.item_id, i.sku, i.item_name, i.upc, i.mpn,
                   pol.quantity_ordered, pol.quantity_received, pol.unit_cost, pol.status
            FROM purchase_order_lines pol JOIN items i ON i.item_id = pol.item_id
            WHERE pol.po_id = :pid ORDER BY pol.line_number
        """),
        {"pid": po_id},
    ).fetchall()

    return jsonify({
        "purchase_order": {
            "po_id": po.po_id, "external_id": str(po.external_id), "latest_inbound_id": po.latest_inbound_id, "po_number": po.po_number, "po_barcode": po.po_barcode,
            "vendor_name": po.vendor_name, "status": po.status,
            "expected_date": po.expected_date.isoformat() if po.expected_date else None,
            "warehouse_id": po.warehouse_id, "notes": po.notes,
            "created_at": po.created_at.isoformat() if po.created_at else None,
        },
        "lines": [
            {"po_line_id": l.po_line_id, "line_number": l.line_number, "item_id": l.item_id,
             "sku": l.sku, "item_name": l.item_name, "upc": l.upc, "mpn": l.mpn,
             "quantity_ordered": l.quantity_ordered, "quantity_received": l.quantity_received,
             "unit_cost": float(l.unit_cost) if l.unit_cost else None, "status": l.status}
            for l in lines
        ],
    })


@admin_bp.route("/purchase-orders", methods=["POST"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@validate_body(CreatePurchaseOrderRequest)
@with_db
def create_purchase_order(validated):
    data = validated.model_dump()

    dup = g.db.execute(text("SELECT 1 FROM purchase_orders WHERE po_number = :pn"), {"pn": data["po_number"]}).fetchone()
    if dup:
        return jsonify({"error": f"Duplicate po_number: {data['po_number']}"}), 400

    # Validate items exist in DB
    for line in data["lines"]:
        item = g.db.execute(text("SELECT 1 FROM items WHERE item_id = :iid"), {"iid": line["item_id"]}).fetchone()
        if not item:
            return jsonify({"error": f"Item {line['item_id']} not found"}), 400

    result = g.db.execute(
        text("""
            INSERT INTO purchase_orders (po_number, po_barcode, vendor_name, expected_date, warehouse_id, notes, created_by, status, external_id)
            VALUES (:pn, :pb, :vendor, :exp_date, :wid, :notes, :created_by, :status, :ext_id)
            RETURNING po_id
        """),
        {
            "pn": data["po_number"], "pb": data.get("po_barcode", data["po_number"]),
            "vendor": data.get("vendor_name"), "exp_date": data.get("expected_date"),
            "wid": data["warehouse_id"], "notes": data.get("notes"),
            "created_by": g.current_user["username"], "status": PO_OPEN,
            "ext_id": str(uuid.uuid4()),
        },
    )
    po_id = result.fetchone()[0]

    for line in data["lines"]:
        g.db.execute(
            text("INSERT INTO purchase_order_lines (po_id, item_id, quantity_ordered, unit_cost, line_number) VALUES (:pid, :iid, :qty, :cost, :ln)"),
            {"pid": po_id, "iid": line["item_id"], "qty": line["quantity_ordered"],
             "cost": float(line["unit_cost"]) if line.get("unit_cost") is not None else None,
             "ln": line.get("line_number") or 1},
        )

    g.db.commit()

    # Re-fetch to return (save/restore g.db since get_purchase_order has @with_db)
    outer_db = g.db
    response = get_purchase_order(po_id)
    g.db = outer_db
    return response


PO_STATUS_VALUES = {PO_OPEN, PO_PARTIAL, PO_RECEIVED, PO_CLOSED, PO_ARCHIVED}
PO_EDIT_BLOCKED_STATUSES = {PO_CLOSED, PO_ARCHIVED}


def _emit_po_edit(db, *, po_id, po_external_id, po_number, warehouse_id,
                  status, changes, username):
    """Emit purchaseorderedit.completed carrying the per-field diff so the
    ERP can reverse-sync a PO edit made in Sentry -- the outbound half of
    the both-ways PO-edit flow. Mirrors salesorderedit.completed.

    No-op when ``changes`` is empty: the event schema requires at least
    one change, and a re-PATCH that lands the same values is not an edit
    worth shipping. purchase_order_external_id carries the source-system
    id when one exists (cross_system_mappings), falling back to the
    Sentry canonical UUID so the consumer always has a key to act on.
    """
    if not changes:
        return
    po_source_external_id = (
        resolve_source_external_id(db, "purchase_order", po_external_id)
        or str(po_external_id)
    )
    emit_event(
        db,
        event_type="purchaseorderedit.completed",
        event_version=1,
        aggregate_type="purchase_order",
        aggregate_id=po_id,
        aggregate_external_id=po_external_id,
        warehouse_id=warehouse_id,
        source_txn_id=g.source_txn_id,
        payload={
            "purchase_order_external_id": po_source_external_id,
            "po_number": po_number,
            "status": status,
            "changes": changes,
            "edited_by_user_external_id": get_user_external_id(db, username),
            "edited_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    )


@admin_bp.route("/purchase-orders/<int:po_id>", methods=["PUT"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@validate_body(UpdatePurchaseOrderRequest)
@with_db
def update_purchase_order(po_id, validated):
    data = validated.model_dump(exclude_unset=True)

    po = g.db.execute(
        text(
            "SELECT po_id, po_number, po_barcode, vendor_name, expected_date, "
            "notes, external_id, status, warehouse_id "
            "FROM purchase_orders WHERE po_id = :pid"
        ),
        {"pid": po_id},
    ).fetchone()
    if not po:
        return jsonify({"error": "Purchase order not found"}), 404

    new_status = data.get("status")
    if new_status is not None:
        if new_status not in PO_STATUS_VALUES:
            return jsonify({"error": f"Invalid status: {new_status}"}), 400
        # ARCHIVED is reachable only from CLOSED so an active PO cannot
        # be hidden by a single dropdown change. All other transitions
        # (including ARCHIVED -> any other) are allowed; admins use the
        # dropdown to un-archive when needed.
        if new_status == PO_ARCHIVED and po.status != PO_CLOSED:
            return jsonify({
                "error": "PO must be CLOSED before it can be ARCHIVED",
            }), 400

    HEADER_FIELDS = {"po_number", "po_barcode", "vendor_name", "expected_date", "notes"}
    header_data = {k: v for k, v in data.items() if k in HEADER_FIELDS}
    # Header edits are blocked when the PO is locked (CLOSED or
    # ARCHIVED). The status field itself is still editable so an admin
    # can transition out of CLOSED before resuming line work.
    if header_data and po.status in PO_EDIT_BLOCKED_STATUSES:
        return jsonify({
            "error": f"Header edits are not allowed while the PO is {po.status}",
        }), 400

    set_fragments, params = [], {"pid": po_id}
    for col, val in header_data.items():
        set_fragments.append(f"{col} = :{col}")
        params[col] = val
    if new_status is not None and new_status != po.status:
        set_fragments.append("status = :status")
        params["status"] = new_status

    if not set_fragments:
        return jsonify({"error": "No valid fields provided"}), 400

    g.db.execute(
        text(f"UPDATE purchase_orders SET {', '.join(set_fragments)} WHERE po_id = :pid"),
        params,
    )

    if new_status is not None and new_status != po.status:
        write_audit_log(
            g.db,
            ACTION_PO_STATUS_CHANGED,
            "PO",
            po_id,
            g.current_user["user_id"],
            po.warehouse_id,
            details={"old_status": po.status, "new_status": new_status},
        )

    status_changed = new_status is not None and new_status != po.status
    changes = [
        {
            "field": field,
            "old_value": str(getattr(po, field)) if getattr(po, field) is not None else None,
            "new_value": str(val) if val is not None else None,
        }
        for field, val in header_data.items()
        if val != getattr(po, field)
    ]
    if status_changed:
        changes.append({
            "field": "status",
            "old_value": po.status,
            "new_value": new_status,
        })
    _emit_po_edit(
        g.db,
        po_id=po_id,
        po_external_id=po.external_id,
        po_number=po.po_number,
        warehouse_id=po.warehouse_id,
        status=new_status if status_changed else po.status,
        changes=changes,
        username=g.current_user["username"],
    )

    g.db.commit()

    row = g.db.execute(
        text("SELECT po_id, po_number, po_barcode, vendor_name, status, expected_date, warehouse_id, notes, created_at FROM purchase_orders WHERE po_id = :pid"),
        {"pid": po_id},
    ).fetchone()
    return jsonify({
        "po_id": row.po_id, "po_number": row.po_number, "po_barcode": row.po_barcode,
        "vendor_name": row.vendor_name, "status": row.status,
        "expected_date": row.expected_date.isoformat() if row.expected_date else None,
        "warehouse_id": row.warehouse_id, "notes": row.notes,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    })


# ── Purchase Order Lines (add / update / remove) ─────────────────────────────

@admin_bp.route("/purchase-orders/<int:po_id>/lines", methods=["POST"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@validate_body(AddPOLineRequest)
@with_db
def add_purchase_order_line(po_id, validated):
    data = validated.model_dump()

    po = g.db.execute(
        text("SELECT po_id, po_number, external_id, status, warehouse_id FROM purchase_orders WHERE po_id = :pid"),
        {"pid": po_id},
    ).fetchone()
    if not po:
        return jsonify({"error": "Purchase order not found"}), 404
    if po.status in PO_EDIT_BLOCKED_STATUSES:
        return jsonify({
            "error": f"Cannot add lines while the PO is {po.status}",
        }), 400

    item = g.db.execute(
        text("SELECT item_id, sku FROM items WHERE item_id = :iid"),
        {"iid": data["item_id"]},
    ).fetchone()
    if not item:
        return jsonify({"error": f"Item {data['item_id']} not found"}), 400

    # Reject duplicate items on the same PO; the receive endpoint
    # already does LIMIT 1 against (po_id, item_id) and would not be
    # able to disambiguate two lines with the same item_id.
    dup = g.db.execute(
        text("SELECT 1 FROM purchase_order_lines WHERE po_id = :pid AND item_id = :iid"),
        {"pid": po_id, "iid": data["item_id"]},
    ).fetchone()
    if dup:
        return jsonify({"error": f"Item {item.sku} is already on this PO"}), 400

    next_ln = g.db.execute(
        text("SELECT COALESCE(MAX(line_number), 0) + 1 FROM purchase_order_lines WHERE po_id = :pid"),
        {"pid": po_id},
    ).scalar()

    result = g.db.execute(
        text(
            """
            INSERT INTO purchase_order_lines
                (po_id, item_id, quantity_ordered, unit_cost, line_number)
            VALUES (:pid, :iid, :qty, :cost, :ln)
            RETURNING po_line_id
            """
        ),
        {
            "pid": po_id, "iid": data["item_id"],
            "qty": data["quantity_ordered"],
            "cost": float(data["unit_cost"]) if data.get("unit_cost") is not None else None,
            "ln": next_ln,
        },
    )
    po_line_id = result.fetchone()[0]

    write_audit_log(
        g.db,
        ACTION_PO_LINE_ADDED,
        "PO_LINE",
        po_line_id,
        g.current_user["user_id"],
        po.warehouse_id,
        details={
            "po_id": po_id, "sku": item.sku,
            "quantity_ordered": data["quantity_ordered"],
        },
    )

    # A new unreceived line on an already-RECEIVED PO reopens it to
    # PARTIAL; recompute so the header reflects the added obligation.
    new_po_status = recompute_po_status(g.db, po_id)
    _emit_po_edit(
        g.db,
        po_id=po_id,
        po_external_id=po.external_id,
        po_number=po.po_number,
        warehouse_id=po.warehouse_id,
        status=new_po_status,
        changes=[{
            "field": f"line[{item.sku}]",
            "old_value": None,
            "new_value": str(data["quantity_ordered"]),
        }],
        username=g.current_user["username"],
    )
    g.db.commit()

    return jsonify({
        "po_line_id": po_line_id,
        "po_id": po_id,
        "item_id": data["item_id"],
        "sku": item.sku,
        "quantity_ordered": data["quantity_ordered"],
        "quantity_received": 0,
        "line_number": next_ln,
        "po_status": new_po_status,
    }), 201


@admin_bp.route("/purchase-orders/<int:po_id>/lines/<int:po_line_id>", methods=["PATCH"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@validate_body(UpdatePOLineRequest)
@with_db
def update_purchase_order_line(po_id, po_line_id, validated):
    data = validated.model_dump()

    po = g.db.execute(
        text("SELECT po_id, po_number, external_id, status, warehouse_id FROM purchase_orders WHERE po_id = :pid"),
        {"pid": po_id},
    ).fetchone()
    if not po:
        return jsonify({"error": "Purchase order not found"}), 404
    if po.status in PO_EDIT_BLOCKED_STATUSES:
        return jsonify({
            "error": f"Cannot edit lines while the PO is {po.status}",
        }), 400

    line = g.db.execute(
        text(
            """
            SELECT pol.po_line_id, pol.quantity_ordered, pol.quantity_received, i.sku
            FROM purchase_order_lines pol JOIN items i ON i.item_id = pol.item_id
            WHERE pol.po_line_id = :plid AND pol.po_id = :pid
            """
        ),
        {"plid": po_line_id, "pid": po_id},
    ).fetchone()
    if not line:
        return jsonify({"error": "Line not found"}), 404

    new_qty = data["quantity_ordered"]
    # Blocking ordered<received keeps variance non-negative. The
    # operator either reverses receipts first or leaves the line as-is.
    if new_qty < line.quantity_received:
        return jsonify({
            "error": (
                f"quantity_ordered ({new_qty}) cannot be less than "
                f"quantity_received ({line.quantity_received})"
            ),
        }), 400

    g.db.execute(
        text("UPDATE purchase_order_lines SET quantity_ordered = :qty WHERE po_line_id = :plid"),
        {"qty": new_qty, "plid": po_line_id},
    )

    # Re-derive the line + PO header status from the new ordered qty. This
    # is what makes the over-receipt case work: bumping a RECEIVED line's
    # quantity_ordered above what was received reopens it to PARTIAL (and
    # the header with it) so the remaining unit becomes receivable again.
    new_po_status = recompute_po_status(g.db, po_id)
    new_line_status = g.db.execute(
        text("SELECT status FROM purchase_order_lines WHERE po_line_id = :plid"),
        {"plid": po_line_id},
    ).scalar()

    write_audit_log(
        g.db,
        ACTION_PO_LINE_UPDATED,
        "PO_LINE",
        po_line_id,
        g.current_user["user_id"],
        po.warehouse_id,
        details={
            "po_id": po_id, "sku": line.sku,
            "old_quantity_ordered": line.quantity_ordered,
            "new_quantity_ordered": new_qty,
        },
    )

    changes = []
    if new_qty != line.quantity_ordered:
        changes.append({
            "field": f"line[{line.sku}].quantity_ordered",
            "old_value": str(line.quantity_ordered),
            "new_value": str(new_qty),
        })
    _emit_po_edit(
        g.db,
        po_id=po_id,
        po_external_id=po.external_id,
        po_number=po.po_number,
        warehouse_id=po.warehouse_id,
        status=new_po_status,
        changes=changes,
        username=g.current_user["username"],
    )

    g.db.commit()
    return jsonify({
        "po_line_id": po_line_id,
        "quantity_ordered": new_qty,
        "line_status": new_line_status,
        "po_status": new_po_status,
    })


@admin_bp.route("/purchase-orders/<int:po_id>/lines/<int:po_line_id>", methods=["DELETE"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@with_db
def delete_purchase_order_line(po_id, po_line_id):
    po = g.db.execute(
        text("SELECT po_id, po_number, external_id, status, warehouse_id FROM purchase_orders WHERE po_id = :pid"),
        {"pid": po_id},
    ).fetchone()
    if not po:
        return jsonify({"error": "Purchase order not found"}), 404
    if po.status in PO_EDIT_BLOCKED_STATUSES:
        return jsonify({
            "error": f"Cannot remove lines while the PO is {po.status}",
        }), 400

    line = g.db.execute(
        text(
            """
            SELECT pol.po_line_id, pol.quantity_ordered, pol.quantity_received, i.sku
            FROM purchase_order_lines pol JOIN items i ON i.item_id = pol.item_id
            WHERE pol.po_line_id = :plid AND pol.po_id = :pid
            """
        ),
        {"plid": po_line_id, "pid": po_id},
    ).fetchone()
    if not line:
        return jsonify({"error": "Line not found"}), 404

    # Blocking removal of lines with received qty preserves inventory
    # history. To remove, operator first reverses receipts via a
    # separate workflow; the soft-close-by-setting-qty-equal-to-
    # received path is intentionally not exposed here.
    if line.quantity_received > 0:
        return jsonify({
            "error": (
                f"Line has {line.quantity_received} received units; "
                "remove the receipts before deleting the line"
            ),
        }), 400

    g.db.execute(
        text("DELETE FROM purchase_order_lines WHERE po_line_id = :plid"),
        {"plid": po_line_id},
    )

    write_audit_log(
        g.db,
        ACTION_PO_LINE_REMOVED,
        "PO_LINE",
        po_line_id,
        g.current_user["user_id"],
        po.warehouse_id,
        details={
            "po_id": po_id, "sku": line.sku,
            "quantity_ordered": line.quantity_ordered,
        },
    )

    # Removing a line can complete the PO (e.g. the only unreceived line
    # is gone, leaving the rest fully received); recompute the header.
    new_po_status = recompute_po_status(g.db, po_id)
    _emit_po_edit(
        g.db,
        po_id=po_id,
        po_external_id=po.external_id,
        po_number=po.po_number,
        warehouse_id=po.warehouse_id,
        status=new_po_status,
        changes=[{
            "field": f"line[{line.sku}]",
            "old_value": str(line.quantity_ordered),
            "new_value": None,
        }],
        username=g.current_user["username"],
    )
    g.db.commit()
    return jsonify({"po_line_id": po_line_id, "deleted": True, "po_status": new_po_status})


def _inbound_lifecycle_error(table, identity_column, identity):
    body = request.get_json() if request.is_json else None
    if body is None or body == {}:
        return None
    if not isinstance(body, dict) or set(body) != {"expected_inbound_id"}:
        return jsonify({"error": "Choose a valid source version"}), 400
    expected = body["expected_inbound_id"]
    if type(expected) is not int or expected <= 0:
        return jsonify({"error": "Choose a valid source version"}), 400
    row = g.db.execute(
        text(f"SELECT latest_inbound_id FROM {table} WHERE {identity_column} = :identity FOR UPDATE"),
        {"identity": identity},
    ).fetchone()
    if not row:
        return jsonify({"error": "Order not found"}), 404
    if row.latest_inbound_id != expected:
        return jsonify({"error": "The source version has changed"}), 409
    return None


@admin_bp.route("/purchase-orders/<int:po_id>/close", methods=["POST"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@with_db
def close_purchase_order(po_id):
    source_error = _inbound_lifecycle_error("purchase_orders", "po_id", po_id)
    if source_error:
        return source_error
    po = g.db.execute(
        text("SELECT po_id, status FROM purchase_orders WHERE po_id = :pid FOR UPDATE"),
        {"pid": po_id},
    ).fetchone()
    if not po:
        return jsonify({"error": "Purchase order not found"}), 404
    if po.status == PO_CLOSED:
        return jsonify({"error": "Purchase order is already CLOSED"}), 409

    g.db.execute(
        text("UPDATE purchase_orders SET status = :status WHERE po_id = :pid"),
        {"pid": po_id, "status": PO_CLOSED},
    )
    g.db.commit()
    return jsonify({"message": "Purchase order closed", "status": PO_CLOSED})


@admin_bp.route("/purchase-orders/<int:po_id>/reopen", methods=["POST"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@with_db
def reopen_purchase_order(po_id):
    source_error = _inbound_lifecycle_error("purchase_orders", "po_id", po_id)
    if source_error:
        return source_error
    po = g.db.execute(
        text("SELECT po_id, status FROM purchase_orders WHERE po_id = :pid FOR UPDATE"),
        {"pid": po_id},
    ).fetchone()
    if not po:
        return jsonify({"error": "Purchase order not found"}), 404
    if po.status != PO_CLOSED:
        return jsonify({
            "error": f"Only CLOSED purchase orders can be reopened. Current status: {po.status}"
        }), 409

    g.db.execute(
        text("UPDATE purchase_orders SET status = :status WHERE po_id = :pid"),
        {"pid": po_id, "status": PO_OPEN},
    )
    g.db.commit()
    return jsonify({"message": "Purchase order reopened", "status": PO_OPEN})


# ── PO Receipts: admin unreceive (double-scan fix) ────────────────────────


@admin_bp.route("/purchase-orders/<int:po_id>/receipts", methods=["GET"])
@require_auth
@require_admin_or_page_permission("receiving")
@with_db
def list_po_receipts(po_id):
    """Receipt history for a single PO, scoped to the admin Receiving
    modal. Returns one row per item_receipts entry with the human-
    facing context the operator needs to decide what to unreceive
    (sku + bin code + receiver + when). Excludes RMA goods-in rows
    (po_id IS NULL) by virtue of the WHERE filter.
    """
    rows = g.db.execute(
        text(
            """
            SELECT r.receipt_id, r.po_line_id, r.item_id, r.quantity_received,
                   r.bin_id, r.warehouse_id, r.received_by, r.received_at,
                   r.lot_number, r.serial_number, r.notes, r.external_id,
                   i.sku, i.item_name,
                   b.bin_code,
                   pol.line_number
              FROM item_receipts r
              JOIN items i  ON i.item_id  = r.item_id
              JOIN bins  b  ON b.bin_id   = r.bin_id
              LEFT JOIN purchase_order_lines pol
                ON pol.po_line_id = r.po_line_id
             WHERE r.po_id = :pid
             ORDER BY r.received_at DESC, r.receipt_id DESC
            """
        ),
        {"pid": po_id},
    ).fetchall()
    return jsonify({
        "po_id": po_id,
        "receipts": [
            {
                "receipt_id": r.receipt_id,
                "po_line_id": r.po_line_id,
                "line_number": r.line_number,
                "item_id": r.item_id,
                "sku": r.sku,
                "item_name": r.item_name,
                "quantity_received": r.quantity_received,
                "bin_id": r.bin_id,
                "bin_code": r.bin_code,
                "warehouse_id": r.warehouse_id,
                "received_by": r.received_by,
                "received_at": r.received_at.isoformat() if r.received_at else None,
                "lot_number": r.lot_number,
                "serial_number": r.serial_number,
                "notes": r.notes,
                "external_id": str(r.external_id),
            }
            for r in rows
        ],
    })


@admin_bp.route("/receipts/<int:receipt_id>/unreceive", methods=["POST"])
@require_auth
@require_admin_or_page_permission("receiving")
@with_db
def unreceive_receipt(receipt_id):
    """Reverse one PO receipt (admin double-scan fix).

    Body is optional: {"reason": "<free text>"}. Walks the warehouse
    inventory pool to back out the receipt qty (preferring the
    receipt's original bin, then other bins holding the item),
    decrements the PO line, recomputes PO status, hard-deletes the
    item_receipts row, writes one ACTION_RECEIVE_CANCEL audit row
    with details.source='admin_unreceive', and emits
    receipt.cancelled/1 so a downstream ERP/warehouse subscriber
    decrements its inbound count.

    Auth: receiving page permission (matches the GET) or ADMIN. The
    decorator gates both shapes.

    Idempotency: a same-second double click reuses g.source_txn_id;
    integration_events ON CONFLICT (aggregate_type, aggregate_id,
    event_type, source_txn_id) DO NOTHING absorbs the second event
    cleanly. The second mutation pass would 404 on the receipt
    (already deleted by the first), so the client gets a clear error
    on the second click rather than a silent double-reverse.
    """
    body = request.get_json(silent=True) or {}
    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        return jsonify({"error": "reason must be a string"}), 422
    if reason and len(reason) > 500:
        return jsonify({"error": "reason must be 500 chars or fewer"}), 422

    try:
        result = record_unreceive(
            g.db,
            receipt_id=receipt_id,
            username=g.current_user["username"],
            reason=reason,
        )
    except UnreceiveError as exc:
        if exc.kind == "not_found":
            status_code = 404
        elif exc.kind == "insufficient_available":
            status_code = 409
        else:
            status_code = 422
        return jsonify({
            "error": str(exc),
            "kind": exc.kind,
            **exc.context,
        }), status_code

    g.db.commit()
    return jsonify({
        "message": "Receipt reversed",
        **result,
    })


# ── Source Systems ────────────────────────────────────────────────────────────

@admin_bp.route("/source-systems", methods=["GET"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@with_db
def list_source_systems():
    """Read-only list of canonical source_system
    tags for the SO edit modal's source_system picker.

    /admin/scope-catalog already serves the same list but is gated by
    api-tokens, which the sales-orders operator role does not get by
    default. This focused endpoint keeps the SO edit surface usable
    without granting the broader token permission.
    """
    rows = g.db.execute(
        text(
            "SELECT source_system, kind "
            "  FROM inbound_source_systems_allowlist "
            " ORDER BY source_system"
        )
    ).fetchall()
    return jsonify({
        "source_systems": [
            {"source_system": r.source_system, "kind": r.kind}
            for r in rows
        ],
    })


# ── Sales Orders ──────────────────────────────────────────────────────────────

@admin_bp.route("/sales-orders", methods=["GET"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@with_db
def list_sales_orders():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 1000)

    where_clauses, params = [], {}
    # mig 076: voided returns (RMAs an operator soft-deleted) are hidden from
    # every SO listing -- they are "deleted". Only return SOs ever carry
    # voided_at, so this never affects normal sales orders (voided_at NULL).
    where_clauses.append("so.voided_at IS NULL")
    status = request.args.get("status")
    warehouse_id = request.args.get("warehouse_id", type=int)
    # Opt-in order_type filter. The RMA admin page passes order_type=return to
    # list only return SOs; pages that show the full ledger leave it unset.
    order_type = request.args.get("order_type")
    # Opt-in from the Sales Orders page: drop the post-fulfillment records
    # (returns/RMAs + refund credit memos), which have their own Returns page.
    # Other ledgers (Fraud, etc.) leave it unset and still see every type; the
    # Returns tabs request them explicitly via order_type, so this never fires
    # there.
    exclude_post_fulfillment = (
        request.args.get("exclude_post_fulfillment", "false").lower() == "true"
    )
    search = (request.args.get("q") or "").strip()
    # Opt-in filter from the Picking Tickets queue. When set, drops SOs
    # whose ticket was already confirm-rendered to the operator (mig
    # 057 printed_at). Pages that show the full SO ledger (Sales
    # Orders, Fraud, POS Activity) leave the param unset.
    hide_printed = request.args.get("hide_printed", "false").lower() == "true"
    # Opt-in primary-bin computation for the Picking Tickets queue.
    # When set, each row carries the bin_code + pick_sequence of the
    # preferred bin with the LOWEST pick_sequence among the SO's
    # line items, scoped to the SO's warehouse. The list page sorts
    # by pick_sequence so a picker walks the warehouse in physical
    # order; the shipper unpacks the cart in the same order.
    include_primary_bin = request.args.get("include_primary_bin", "false").lower() == "true"
    # Opt-in line-count for the Picking Tickets "Long Orders" filter. When
    # set, each row carries line_count = the number of line items on the
    # SO, computed live from sales_order_lines (ix_sales_order_lines_so).
    # Derived on read rather than stored so it always reflects the SO's
    # CURRENT lines even after an operator adds or removes one -- a stored
    # creation-time flag would go stale against the line-edit endpoints.
    # Pages that don't filter by size leave the param unset and skip the
    # subquery.
    include_line_count = request.args.get("include_line_count", "false").lower() == "true"
    # Opt-in filter for the Local Pickup dashboard: keep only orders whose
    # ship_method names a local pickup / will-call. These are free-text
    # values like "Local Pickup (Free)", so match either
    # word ("local" or "pickup") rather than forcing a canonical enum. Pages
    # that show the full SO ledger leave the param unset.
    local_pickup = request.args.get("local_pickup", "false").lower() == "true"
    if status:
        # Accept a comma-separated list so a worklist can show several
        # statuses at once (e.g. the Local Pickup dashboard's OPEN+PICKED
        # active view). A single value still binds as plain equality.
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if len(statuses) == 1:
            where_clauses.append("so.status = :status")
            params["status"] = statuses[0]
        elif statuses:
            keys = []
            for i, s in enumerate(statuses):
                k = f"status_{i}"
                keys.append(f":{k}")
                params[k] = s
            where_clauses.append(f"so.status IN ({', '.join(keys)})")
    if warehouse_id:
        where_clauses.append("so.warehouse_id = :wid")
        params["wid"] = warehouse_id
    if order_type:
        where_clauses.append("so.order_type = :order_type")
        params["order_type"] = order_type
    elif exclude_post_fulfillment:
        where_clauses.append("so.order_type NOT IN ('return', 'refund')")
    # The picking-ticket queue (include_primary_bin) is goods-OUT only: a
    # return is inbound and must never enter the printable/pickable queue,
    # regardless of any other filter. Enforced here at the queue's data
    # source so no caller can surface a return to be picked or printed.
    if include_primary_bin:
        where_clauses.append("so.order_type NOT IN ('return', 'refund')")
    if hide_printed:
        where_clauses.append("so.printed_at IS NULL")
    if search:
        where_clauses.append("(so.so_number ILIKE :q OR so.customer_name ILIKE :q)")
        params["q"] = f"%{search}%"
    if local_pickup:
        where_clauses.append(
            "(so.ship_method ILIKE '%local%' OR so.ship_method ILIKE '%pickup%')"
        )

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    total = g.db.execute(
        text(f"SELECT COUNT(*) FROM sales_orders so {where_sql}"),
        params,
    ).scalar()
    pages = max(1, math.ceil(total / per_page))

    params["limit"] = per_page
    params["offset"] = (page - 1) * per_page
    # Primary-bin subqueries only render when the caller asks for them;
    # other pages avoid the per-row scalar subquery cost. Both columns
    # come from the same logical row, so the planner can fold them
    # into one index scan even though they read as two subqueries.
    # Live line-item count for the "Long Orders" filter. Opt-in so pages
    # that don't need it skip the per-row COUNT (backed by
    # ix_sales_order_lines_so, so it is a cheap index-only count).
    line_count_select = ""
    if include_line_count:
        line_count_select = """
            , (SELECT COUNT(*)
                 FROM sales_order_lines sol
                WHERE sol.so_id = so.so_id) AS line_count
        """
    primary_bin_select = ""
    if include_primary_bin:
        primary_bin_select = """
            , (SELECT b.bin_code
                 FROM sales_order_lines sol
                 JOIN preferred_bins pb ON pb.item_id = sol.item_id
                 JOIN bins b ON b.bin_id = pb.bin_id
                WHERE sol.so_id = so.so_id
                  AND b.warehouse_id = so.warehouse_id
                ORDER BY b.pick_sequence ASC NULLS LAST, pb.priority ASC
                LIMIT 1) AS primary_bin_code
            , (SELECT b.pick_sequence
                 FROM sales_order_lines sol
                 JOIN preferred_bins pb ON pb.item_id = sol.item_id
                 JOIN bins b ON b.bin_id = pb.bin_id
                WHERE sol.so_id = so.so_id
                  AND b.warehouse_id = so.warehouse_id
                ORDER BY b.pick_sequence ASC NULLS LAST, pb.priority ASC
                LIMIT 1) AS primary_bin_pick_sequence
        """
    rows = g.db.execute(
        text(f"""
            SELECT so.so_id, so.external_id, so.so_number, so.so_barcode, so.customer_name, so.customer_phone, so.customer_address,
                   so.status, so.priority, so.warehouse_id, so.order_type, so.parent_so_id,
                   so.ship_method, so.ship_address, so.order_date, so.ship_by_date, so.created_at, so.created_by,
                   so.carrier, so.tracking_number, so.shipped_at,
                   (so.shipped_at AT TIME ZONE :company_tz)::date AS shipped_date_local,
                   so.memo, so.printed_at,
                   so.billing_address_line1, so.billing_address_line2,
                   so.billing_address_city, so.billing_address_state,
                   so.billing_address_postal_code,
                   so.shipping_address_line1, so.shipping_address_line2,
                   so.shipping_address_city, so.shipping_address_state,
                   so.shipping_address_postal_code
                   {primary_bin_select}
                   {line_count_select}
            FROM sales_orders so {where_sql}
            ORDER BY so.so_id DESC LIMIT :limit OFFSET :offset
        """),
        {**params, "company_tz": COMPANY_TIMEZONE},
    ).fetchall()

    def _row_to_dict(r):
        out = {
            "so_id": r.so_id, "external_id": str(r.external_id), "so_number": r.so_number, "so_barcode": r.so_barcode,
            "customer_name": r.customer_name, "customer_phone": r.customer_phone,
            "customer_address": r.customer_address,
            "status": r.status, "priority": r.priority,
            "warehouse_id": r.warehouse_id,
            "order_type": r.order_type, "parent_so_id": r.parent_so_id,
            "ship_method": r.ship_method,
            "ship_address": r.ship_address,
            "order_date": r.order_date.isoformat() if r.order_date else None,
            "ship_by_date": r.ship_by_date.isoformat() if r.ship_by_date else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "created_by": r.created_by,
            "carrier": r.carrier, "tracking_number": r.tracking_number,
            "shipped_at": r.shipped_at.isoformat() if r.shipped_at else None,
            "shipped_date_local": r.shipped_date_local.isoformat() if r.shipped_date_local else None,
            "memo": r.memo,
            "printed_at": r.printed_at.isoformat() if r.printed_at else None,
            "billing_address_line1": r.billing_address_line1,
            "billing_address_line2": r.billing_address_line2,
            "billing_address_city": r.billing_address_city,
            "billing_address_state": r.billing_address_state,
            "billing_address_postal_code": r.billing_address_postal_code,
            "shipping_address_line1": r.shipping_address_line1,
            "shipping_address_line2": r.shipping_address_line2,
            "shipping_address_city": r.shipping_address_city,
            "shipping_address_state": r.shipping_address_state,
            "shipping_address_postal_code": r.shipping_address_postal_code,
        }
        if include_primary_bin:
            out["primary_bin_code"] = r.primary_bin_code
            out["primary_bin_pick_sequence"] = r.primary_bin_pick_sequence
        if include_line_count:
            out["line_count"] = r.line_count
        return out

    return jsonify({
        "sales_orders": [_row_to_dict(r) for r in rows],
        "total": total, "page": page, "per_page": per_page, "pages": pages,
    })


@admin_bp.route("/sales-orders/<int:so_id>", methods=["GET"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@with_db
def get_sales_order(so_id):
    so = g.db.execute(
        text("""
            SELECT so_id, external_id, latest_inbound_id, so_number, so_barcode,
                   customer_name, customer_phone, customer_email, customer_address,
                   status, priority,
                   warehouse_id, ship_method, ship_address,
                   order_date, ship_by_date, created_at, picked_at, packed_at,
                   shipped_at, created_by,
                   (shipped_at AT TIME ZONE :company_tz)::date AS shipped_date_local,
                   order_total, customer_shipping_paid, memo,
                   source_system,
                   order_origin,
                   carrier, tracking_number,
                   order_type, parent_so_id,
                   (SELECT p.so_number FROM sales_orders p
                     WHERE p.so_id = sales_orders.parent_so_id) AS parent_so_number,
                   backorder_opened_at, backorder_fulfillable_at,
                   cancellation_reason,
                   billing_address_name, billing_address_line1, billing_address_line2,
                   billing_address_city, billing_address_state,
                   billing_address_postal_code, billing_address_country,
                   billing_address_phone,
                   shipping_address_name, shipping_address_line1, shipping_address_line2,
                   shipping_address_city, shipping_address_state,
                   shipping_address_postal_code, shipping_address_country,
                   shipping_address_phone
              FROM sales_orders WHERE so_id = :sid
        """),
        {"sid": so_id, "company_tz": COMPANY_TIMEZONE},
    ).fetchone()
    if not so:
        return jsonify({"error": "Sales order not found"}), 404

    lines = g.db.execute(
        text("""
            SELECT sol.so_line_id, sol.original_so_line_id, sol.line_number, sol.item_id, i.sku, i.item_name, i.upc,
                   sol.quantity_ordered, sol.quantity_allocated, sol.quantity_picked, sol.quantity_packed, sol.quantity_shipped, sol.quantity_received, sol.status
            FROM sales_order_lines sol JOIN items i ON i.item_id = sol.item_id
            WHERE sol.so_id = :sid ORDER BY sol.line_number
        """),
        {"sid": so_id},
    ).fetchall()

    # so-refinement: pick_tasks still in PICKED state. The revert-status
    # modal needs item / bin attribution to offer per-pick release; the
    # detail GET is the natural place to return it so the modal does
    # not need a second round-trip when the operator demotes status.
    pick_tasks = g.db.execute(
        text("""
            SELECT pt.pick_task_id, pt.so_line_id, pt.item_id, pt.bin_id,
                   pt.quantity_picked, pt.picked_at, pt.picked_by,
                   pt.status,
                   i.sku, i.item_name, b.bin_code
              FROM pick_tasks pt
              JOIN items i ON i.item_id = pt.item_id
              JOIN bins b ON b.bin_id = pt.bin_id
             WHERE pt.so_id = :sid AND pt.status = 'PICKED'
             ORDER BY pt.pick_task_id
        """),
        {"sid": so_id},
    ).fetchall()

    receipts = g.db.execute(
        text("""
            SELECT ir.external_id, ir.so_line_id, ir.item_id, i.sku,
                   ir.warehouse_id, ir.bin_id, ir.quantity_received,
                   ir.lot_number, ir.serial_number
              FROM item_receipts ir
              JOIN items i ON i.item_id = ir.item_id
             WHERE ir.so_id = :sid
             ORDER BY ir.receipt_id
             LIMIT 1001
        """),
        {"sid": so_id},
    ).fetchall()
    if len(receipts) > 1000:
        return jsonify({"error": "Receipt detail exceeds the supported limit"}), 409

    picking_batches = g.db.execute(text("""
        SELECT batch.batch_id
        FROM pick_batch_orders membership
        JOIN pick_batches batch ON batch.batch_id = membership.batch_id
        WHERE membership.so_id = :sid AND batch.warehouse_id = :warehouse_id
        ORDER BY batch.batch_id
        LIMIT 501
    """), {"sid": so_id, "warehouse_id": so.warehouse_id}).fetchall()
    if len(picking_batches) > 500:
        return jsonify({"error": "Picking batch detail exceeds the supported limit"}), 409

    shipment_rows = g.db.execute(text("""
        SELECT shipment.fulfillment_id, shipment.external_id, shipment.status,
               shipment.carrier, shipment.tracking_number, shipment.shipped_at, shipment.voided_at,
               line.so_line_id, line.item_id, line.bin_id, line.quantity_shipped,
               line.lot_number, line.serial_number
        FROM item_fulfillments shipment
        JOIN item_fulfillment_lines line ON line.fulfillment_id = shipment.fulfillment_id
        WHERE shipment.so_id = :sid
        ORDER BY shipment.shipped_at DESC, shipment.fulfillment_id DESC, line.fulfillment_line_id
        LIMIT 1001
    """), {"sid": so_id}).fetchall()
    if len(shipment_rows) > 1000:
        return jsonify({"error": "Shipment detail exceeds the supported limit"}), 409
    shipments = {}
    for row in shipment_rows:
        shipment = shipments.setdefault(row.fulfillment_id, {
            "fulfillment_id": row.fulfillment_id, "external_id": str(row.external_id),
            "status": row.status, "carrier": row.carrier, "tracking_number": row.tracking_number,
            "shipped_at": row.shipped_at.isoformat() if row.shipped_at else None,
            "voided_at": row.voided_at.isoformat() if row.voided_at else None, "allocations": [],
        })
        shipment["allocations"].append({"so_line_id": row.so_line_id, "item_id": row.item_id,
            "bin_id": row.bin_id, "quantity_shipped": row.quantity_shipped,
            "lot_number": row.lot_number, "serial_number": row.serial_number})
    latest_shipment = next((entry for entry in shipments.values()
                            if entry["status"] == "SHIPPED" and entry["voided_at"] is None), None)

    return jsonify({
        "picking_batch_ids": [batch.batch_id for batch in picking_batches],
        "shipments": list(shipments.values()),
        "shipment_allocations": latest_shipment["allocations"] if latest_shipment else [],
        "receipts": [
            {"external_id": str(r.external_id), "so_line_id": r.so_line_id,
             "item_id": r.item_id, "sku": r.sku, "warehouse_id": r.warehouse_id,
             "bin_id": r.bin_id, "quantity_received": r.quantity_received,
             "lot_number": r.lot_number, "serial_number": r.serial_number}
            for r in receipts
        ],
        "sales_order": {
            "so_id": so.so_id, "external_id": str(so.external_id), "latest_inbound_id": so.latest_inbound_id, "so_number": so.so_number, "so_barcode": so.so_barcode,
            "customer_name": so.customer_name,
            # customer_phone + customer_address were previously missing
            # from this response, so the edit modal reloaded with the
            # phone field blank after a save and operators assumed
            # the save had failed. Both fields are returned now so
            # round-trip edits display the new value.
            "customer_phone": so.customer_phone,
            "customer_email": so.customer_email,
            "customer_address": so.customer_address,
            "status": so.status, "priority": so.priority,
            "warehouse_id": so.warehouse_id, "ship_method": so.ship_method, "ship_address": so.ship_address,
            "order_date": so.order_date.isoformat() if so.order_date else None,
            "ship_by_date": so.ship_by_date.isoformat() if so.ship_by_date else None,
            "created_at": so.created_at.isoformat() if so.created_at else None,
            "created_by": so.created_by,
            # v1.8.0 (#282): per-order cost fields. Null vs 0.00 is
            # distinct (not provided vs explicitly zero). Decimal
            # serialised as string so the JSON does not lose precision.
            "order_total": str(so.order_total) if so.order_total is not None else None,
            "customer_shipping_paid": (
                str(so.customer_shipping_paid)
                if so.customer_shipping_paid is not None else None
            ),
            # v1.9.0: free-text operator-facing note (mig 055).
            "memo": so.memo,
            # Shipped date as the company-local calendar date (UTC
            # shipped_at converted via COMPANY_TIMEZONE in SQL). The admin
            # modal displays and edits this single value so the read-only
            # view and the date picker can never disagree.
            "shipped_date_local": (
                so.shipped_date_local.isoformat() if so.shipped_date_local else None
            ),
            # source_system: denormalised tag (mig 062). NULL for
            # admin-created and POS-created SOs.
            "source_system": so.source_system,
            # mig 063: free-text upstream-origin label populated by the
            # inbound payload mapping. NULL when the connector has not
            # been wired to provide it.
            "order_origin": so.order_origin,
            # so-refinement: shipment-state fields. Populated by dockd
            # ship POST; surfaced here so the edit modal can show the
            # current tracking number and the view modal can display it.
            "carrier": so.carrier,
            "tracking_number": so.tracking_number,
            # Backorder lifecycle (mig 067). The admin Edit modal uses
            # order_type + parent_so_id to gate the "Partially Fulfill"
            # button (rejects backorders to enforce the one-level
            # chaining cap). backorder_*_at + cancellation_reason are
            # read-only context for the Backorders page detail view.
            "order_type":                so.order_type,
            "parent_so_id":              so.parent_so_id,
            # The parent's readable number, resolved here so callers that
            # only want to name the original ("Original order" on the
            # Refunds modal) do not need a second round-trip. NULL on a
            # root order. The Related Records tab uses the /related
            # endpoint instead, which returns the whole family.
            "parent_so_number":          so.parent_so_number,
            "backorder_opened_at":       so.backorder_opened_at.isoformat() if so.backorder_opened_at else None,
            "backorder_fulfillable_at":  so.backorder_fulfillable_at.isoformat() if so.backorder_fulfillable_at else None,
            "cancellation_reason":       so.cancellation_reason,
            # v1.8.0 (#288): structured billing/shipping address fields.
            **{name: getattr(so, name) for name in ADDRESS_FIELD_NAMES},
        },
        "lines": [
            {"so_line_id": l.so_line_id, "original_so_line_id": l.original_so_line_id, "line_number": l.line_number, "item_id": l.item_id,
             "sku": l.sku, "item_name": l.item_name, "upc": l.upc,
             "quantity_ordered": l.quantity_ordered, "quantity_allocated": l.quantity_allocated,
             "quantity_picked": l.quantity_picked, "quantity_packed": l.quantity_packed,
             "quantity_shipped": l.quantity_shipped, "quantity_received": l.quantity_received,
             "status": l.status}
            for l in lines
        ],
        "pick_tasks": [
            {"pick_task_id": p.pick_task_id, "so_line_id": p.so_line_id,
             "item_id": p.item_id, "sku": p.sku, "item_name": p.item_name,
             "bin_id": p.bin_id, "bin_code": p.bin_code,
             "quantity_picked": p.quantity_picked,
             "picked_at": p.picked_at.isoformat() if p.picked_at else None,
             "picked_by": p.picked_by,
             "status": p.status}
            for p in pick_tasks
        ],
    })


def _picking_ticket_branding(db):
    """Configurable packing-slip branding from app_settings. All four keys
    default to empty so a fresh install renders a clean, unbranded slip; an
    operator sets them under Settings > Picking Ticket."""
    rows = db.execute(
        text(
            "SELECT key, value FROM app_settings WHERE key IN ("
            "'picking_ticket_company_name', 'picking_ticket_company_address', "
            "'picking_ticket_logo_url', 'picking_ticket_returns_text')"
        )
    ).fetchall()
    vals = {r.key: r.value for r in rows}
    return {
        "company_name": vals.get("picking_ticket_company_name", ""),
        "company_address": vals.get("picking_ticket_company_address", ""),
        "logo_url": vals.get("picking_ticket_logo_url", ""),
        "returns_text": vals.get("picking_ticket_returns_text", ""),
    }


# Depth cap for the family walk, applied in BOTH directions. sales_orders.
# parent_so_id is a self-FK with no CHECK forbidding a self-reference or a
# cycle, so an UPDATE that pointed an ancestor at one of its own descendants
# would spin a recursive CTE forever and hang the request. Real families are
# 3 nodes deep at the very most (a sale -> its backorder -> an RMA against
# that backorder), so 32 is far beyond any legitimate shape while still
# terminating a corrupt one.
_RELATED_MAX_DEPTH = 32


@admin_bp.route("/sales-orders/<int:so_id>/related", methods=["GET"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@with_db
def get_sales_order_related(so_id):
    """Every sales order in this SO's parent/child family.

    Post-fulfillment records hang off their original via parent_so_id: a
    backorder, an RMA (return), a refund credit memo, a replacement or an
    exchange. One original can carry several at once, and a child can
    itself have children -- partial-fulfill is gated on order_type rather
    than parent_so_id, so a replacement can spawn a backorder, and Create
    RMA runs against any shipped order including a child.

    So the family is a tree, not a single hop. This walks UP parent_so_id
    to the root, then back DOWN to every descendant, and returns the whole
    thing flat with a `depth` on each row for the caller to indent by. The
    same family comes back whichever member is asked for, so the operator
    sees the complete picture from any record in it.

    Deliberate inclusions, both of which other SO listings drop:

    - Voided returns (voided_at set). The list endpoints hide them because
      they are "deleted", but hiding them HERE is what makes an RMA appear
      to vanish. They come back flagged so the UI can grey them out.
    - Refund credit memos (order_type='refund'), which the Sales Orders
      page filters out via exclude_post_fulfillment.

    Relationships come only from parent_so_id. Never from so_number: the
    readable "<orig>-RMA" convention is cosmetic and predates it, and real
    children exist that do not follow it (legacy POS-REF-* refunds, and
    hand-entered numbers). Never from the parent's refund_so_id either,
    which is stamped only on a FULL refund and so misses partial ones.
    """
    exists = g.db.execute(
        text("SELECT so_id FROM sales_orders WHERE so_id = :sid"),
        {"sid": so_id},
    ).fetchone()
    if not exists:
        return jsonify({"error": "Sales order not found"}), 404

    rows = g.db.execute(
        text("""
            WITH RECURSIVE ancestors AS (
                SELECT so_id, parent_so_id, 0 AS hops
                  FROM sales_orders
                 WHERE so_id = :sid
                UNION ALL
                SELECT p.so_id, p.parent_so_id, a.hops + 1
                  FROM sales_orders p
                  JOIN ancestors a ON p.so_id = a.parent_so_id
                 WHERE a.hops < :max_depth
            ),
            root AS (
                -- The topmost ancestor reached. Ordering by hops rather
                -- than filtering on parent_so_id IS NULL means a family
                -- whose walk hit the depth cap still returns the highest
                -- record found instead of nothing at all.
                SELECT so_id FROM ancestors ORDER BY hops DESC LIMIT 1
            ),
            family AS (
                SELECT so_id, 0 AS depth FROM root
                UNION ALL
                SELECT c.so_id, f.depth + 1
                  FROM sales_orders c
                  JOIN family f ON c.parent_so_id = f.so_id
                 WHERE f.depth < :max_depth
            )
            SELECT so.so_id, so.external_id, so.so_number, so.order_type, so.status,
                   so.warehouse_id, so.customer_name, so.parent_so_id,
                   so.created_at, so.voided_at, so.cancellation_reason,
                   f.depth
              FROM family f
              JOIN sales_orders so ON so.so_id = f.so_id
             ORDER BY f.depth, so.created_at, so.so_id
        """),
        {"sid": so_id, "max_depth": _RELATED_MAX_DEPTH},
    ).fetchall()

    # Line items for every member in one round-trip rather than one query
    # per record. Families are small (the widest in production carries four
    # children), so this stays a single indexed lookup on so_id.
    family_ids = [r.so_id for r in rows]
    lines_by_so = {}
    if family_ids:
        line_rows = g.db.execute(
            text("""
                SELECT sol.so_id, sol.so_line_id, sol.line_number,
                       i.sku, i.item_name,
                       sol.quantity_ordered, sol.quantity_shipped,
                       sol.quantity_received
                  FROM sales_order_lines sol
                  JOIN items i ON i.item_id = sol.item_id
                 WHERE sol.so_id = ANY(:ids)
                 ORDER BY sol.so_id, sol.line_number
            """),
            {"ids": family_ids},
        ).fetchall()
        for lr in line_rows:
            lines_by_so.setdefault(lr.so_id, []).append({
                "so_line_id":        lr.so_line_id,
                "line_number":       lr.line_number,
                "sku":               lr.sku,
                "item_name":         lr.item_name,
                "quantity_ordered":  lr.quantity_ordered,
                "quantity_shipped":  lr.quantity_shipped,
                "quantity_received": lr.quantity_received,
            })

    return jsonify({
        "so_id": so_id,
        # The family always contains at least the record itself, so a
        # count of 1 means "no relatives". The UI reads related_count for
        # its tab badge so it does not have to special-case that.
        "related_count": max(0, len(rows) - 1),
        "records": [
            {
                "so_id":          r.so_id,
                "external_id":    str(r.external_id),
                "so_number":      r.so_number,
                "order_type":     r.order_type,
                "status":         r.status,
                "warehouse_id":   r.warehouse_id,
                "customer_name":  r.customer_name,
                "parent_so_id":   r.parent_so_id,
                "depth":          r.depth,
                # The record the operator is looking at. Flagged server-
                # side so the UI marks "you are here" without comparing
                # ids itself.
                "is_current":     r.so_id == so_id,
                # Soft-deleted return (mig 076). Rendered greyed rather
                # than dropped -- see the docstring.
                "is_voided":      r.voided_at is not None,
                "voided_at":      r.voided_at.isoformat() if r.voided_at else None,
                "cancellation_reason": r.cancellation_reason,
                "created_at":     r.created_at.isoformat() if r.created_at else None,
                "lines":          lines_by_so.get(r.so_id, []),
            }
            for r in rows
        ],
    })


@admin_bp.route("/sales-orders/<int:so_id>/picking-ticket", methods=["GET"])
@require_auth
@require_admin_or_page_permission("picking-tickets")
@with_db
def get_picking_ticket(so_id):
    """Picking-ticket view of an SO: header + lines with the top-priority
    preferred bin for each item, scoped to the SO's warehouse. Used by
    the Outbound > Picking Tickets print page."""
    so = g.db.execute(
        text("""
            SELECT so_id, so_number, customer_name, status,
                   warehouse_id, ship_method, ship_address,
                   order_date, ship_by_date, created_at,
                   shipping_address_name, shipping_address_line1, shipping_address_line2,
                   shipping_address_city, shipping_address_state,
                   shipping_address_postal_code, shipping_address_country
              FROM sales_orders WHERE so_id = :sid
        """),
        {"sid": so_id},
    ).fetchone()
    if not so:
        return jsonify({"error": "Sales order not found"}), 404

    # Per line, fetch the top-priority preferred bin scoped to the SO's
    # warehouse. A LEFT JOIN LATERAL keeps the row when there is no
    # preferred bin so the picker still sees the SKU with a blank bin
    # column rather than the line dropping out entirely.
    lines = g.db.execute(
        text("""
            SELECT sol.so_line_id, sol.line_number, sol.item_id,
                   i.sku, i.item_name, i.upc,
                   sol.quantity_ordered, sol.quantity_shipped,
                   pref.bin_code AS preferred_bin_code
            FROM sales_order_lines sol
            JOIN items i ON i.item_id = sol.item_id
            LEFT JOIN LATERAL (
                SELECT b.bin_code
                  FROM preferred_bins pb
                  JOIN bins b ON b.bin_id = pb.bin_id
                 WHERE pb.item_id = sol.item_id
                   AND b.warehouse_id = :wid
                 ORDER BY pb.priority ASC
                 LIMIT 1
            ) pref ON TRUE
            WHERE sol.so_id = :sid
            ORDER BY sol.line_number
        """),
        {"sid": so_id, "wid": so.warehouse_id},
    ).fetchall()

    return jsonify({
        "sales_order": {
            "so_id": so.so_id, "so_number": so.so_number,
            "customer_name": so.customer_name, "status": so.status,
            "warehouse_id": so.warehouse_id, "ship_method": so.ship_method,
            # Legacy single-string address: pickingGroups.js falls back to
            # it when the structured shipping_address_* are not yet
            # backfilled, so the print page must see it too or those
            # orders group on screen but lose their banner in print.
            "ship_address": so.ship_address,
            "order_date": so.order_date.isoformat() if so.order_date else None,
            "ship_by_date": so.ship_by_date.isoformat() if so.ship_by_date else None,
            "created_at": so.created_at.isoformat() if so.created_at else None,
            "shipping_address_name": so.shipping_address_name,
            "shipping_address_line1": so.shipping_address_line1,
            "shipping_address_line2": so.shipping_address_line2,
            "shipping_address_city": so.shipping_address_city,
            "shipping_address_state": so.shipping_address_state,
            "shipping_address_postal_code": so.shipping_address_postal_code,
            "shipping_address_country": so.shipping_address_country,
        },
        "lines": [
            {"so_line_id": l.so_line_id, "line_number": l.line_number,
             "sku": l.sku, "item_name": l.item_name, "upc": l.upc,
             "quantity_ordered": l.quantity_ordered,
             "quantity_shipped": l.quantity_shipped,
             "preferred_bin_code": l.preferred_bin_code}
            for l in lines
        ],
        "branding": _picking_ticket_branding(g.db),
    })


@admin_bp.route("/sales-orders/mark-printed", methods=["POST"])
@require_auth
@require_admin_or_page_permission("picking-tickets")
@validate_body(MarkSalesOrdersPrintedRequest)
@with_db
def mark_sales_orders_printed(validated):
    """Stamp printed_at = NOW() for a batch of SOs whose picking
    tickets the client has successfully rendered. The picking ticket
    print pages POST here so the Picking Tickets queue can hide
    already-printed orders without coupling render success to a
    server-side GET side effect."""
    so_ids = validated.so_ids
    rows = g.db.execute(
        text(
            """
            UPDATE sales_orders
               SET printed_at = NOW()
             WHERE so_id = ANY(:ids)
               AND printed_at IS NULL
            RETURNING so_id, printed_at
            """
        ),
        {"ids": so_ids},
    ).fetchall()
    g.db.commit()
    return jsonify({
        "marked": [
            {"so_id": r.so_id, "printed_at": r.printed_at.isoformat()}
            for r in rows
        ],
    })


@admin_bp.route("/sales-orders", methods=["POST"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@validate_body(CreateSalesOrderRequest)
@with_db
def create_sales_order(validated):
    data = validated.model_dump()

    dup = g.db.execute(text("SELECT 1 FROM sales_orders WHERE so_number = :sn"), {"sn": data["so_number"]}).fetchone()
    if dup:
        return jsonify({"error": f"Duplicate so_number: {data['so_number']}"}), 400

    # Validate items exist in DB
    for line in data["lines"]:
        item = g.db.execute(text("SELECT 1 FROM items WHERE item_id = :iid"), {"iid": line["item_id"]}).fetchone()
        if not item:
            return jsonify({"error": f"Item {line['item_id']} not found"}), 400

    result = g.db.execute(
        text("""
            INSERT INTO sales_orders (so_number, so_barcode, customer_name, customer_phone, customer_email, customer_address, warehouse_id, ship_method, ship_address, ship_by_date, memo, order_origin, order_date, created_by, status, external_id)
            VALUES (:sn, :sb, :cust, :phone, :cemail, :caddr, :wid, :ship, :addr, :ship_by, :memo, :origin, NOW(), :created_by, :status, :ext_id)
            RETURNING so_id
        """),
        {
            "sn": data["so_number"], "sb": data.get("so_barcode", data["so_number"]),
            "cust": data.get("customer_name"), "phone": data.get("customer_phone"),
            "cemail": data.get("customer_email"),
            "caddr": data.get("customer_address"),
            "wid": data["warehouse_id"],
            "ship": data.get("ship_method"), "addr": data.get("ship_address"),
            "ship_by": data.get("ship_by_date"), "memo": data.get("memo"),
            "origin": data.get("order_origin"),
            "created_by": g.current_user["username"],
            "status": SO_OPEN,
            "ext_id": str(uuid.uuid4()),
        },
    )
    so_id = result.fetchone()[0]

    for line in data["lines"]:
        line_result = g.db.execute(
            text(
                "INSERT INTO sales_order_lines "
                "  (so_id, item_id, quantity_ordered, line_number) "
                "VALUES (:sid, :iid, :qty, :ln) "
                "RETURNING so_line_id"
            ),
            {"sid": so_id, "iid": line["item_id"], "qty": line["quantity_ordered"], "ln": line.get("line_number") or 1},
        )
        so_line_id = line_result.fetchone()[0]

        # Reserve as much of the line as the warehouse can cover right
        # now. Without this, the SO sits OPEN with quantity_allocated=0
        # on every line until a picker batches it (hours / days), and
        # in the meantime POS sales and concurrent inbound orders can
        # claim the same on_hand stock -- the structural cause of
        # oversells. Walk the (item, warehouse) inventory rows
        # in fullest-bin-first order, bumping quantity_allocated until
        # the line is satisfied or available stock is exhausted.
        # Partial reservation is fine: the SO line stays OPEN with
        # quantity_allocated < quantity_ordered and the picker handles
        # the short later.
        remaining = int(line["quantity_ordered"])
        inv_rows = g.db.execute(
            text(
                """
                SELECT inventory_id,
                       (quantity_on_hand - quantity_allocated) AS available
                  FROM inventory
                 WHERE item_id = :iid AND warehouse_id = :wid
                   AND quantity_on_hand > quantity_allocated
                 ORDER BY available DESC, inventory_id
                 FOR UPDATE
                """
            ),
            {"iid": line["item_id"], "wid": data["warehouse_id"]},
        ).fetchall()
        allocated_for_line = 0
        for inv in inv_rows:
            if remaining <= 0:
                break
            take = min(int(inv.available), remaining)
            if take <= 0:
                continue
            g.db.execute(
                text(
                    """
                    UPDATE inventory
                       SET quantity_allocated = quantity_allocated + :take,
                           updated_at         = NOW()
                     WHERE inventory_id = :iid
                    """
                ),
                {"take": take, "iid": inv.inventory_id},
            )
            allocated_for_line += take
            remaining -= take
        if allocated_for_line > 0:
            g.db.execute(
                text(
                    "UPDATE sales_order_lines "
                    "   SET quantity_allocated = :alloc "
                    " WHERE so_line_id = :sid"
                ),
                {"alloc": allocated_for_line, "sid": so_line_id},
            )

    g.db.commit()

    # Re-fetch to return (save/restore g.db since get_sales_order has @with_db)
    outer_db = g.db
    response = get_sales_order(so_id)
    g.db = outer_db
    return response


@admin_bp.route("/sales-orders/<int:so_id>", methods=["PUT"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@validate_body(UpdateSalesOrderRequest)
@with_db
def update_sales_order(so_id, validated):
    """Admin edit of SO non-line fields.

    Status policy:
      * ADMIN: edit at any status (including SHIPPED, for backfill of
        orders shipped through external systems).
      * USER with the so-full-edit override: edit at any status except
        SHIPPED.
      * USER without the override: edit only when status = OPEN.

    Source-system reassignment (mig 062) is gated to ADMIN or
    so-full-edit because it changes which ERP a downstream replay
    would target. The 16 address fields stay on /address PATCH so the
    address-only edit path keeps its own audit shape.

    Per-field audit rows mirror the address surface: one row per
    actually-changed field carrying old/new so a post-edit forensic
    diff does not require scanning row state.
    """
    data = validated.model_dump(exclude_unset=True)

    so = g.db.execute(
        text(
            "SELECT so_id, external_id, status, warehouse_id, source_system, order_origin, "
            "       so_number, so_barcode, "
            "       customer_name, customer_phone, customer_email, customer_address, "
            "       ship_method, ship_address, ship_by_date, "
            "       priority, memo, "
            "       status AS cur_status, carrier, tracking_number, shipped_at "
            "  FROM sales_orders WHERE so_id = :sid FOR UPDATE"
        ),
        {"sid": so_id},
    ).fetchone()
    if not so:
        return jsonify({"error": "Sales order not found"}), 404

    role = g.current_user.get("role")
    is_admin = role == ROLE_ADMIN
    has_so_override = has_override(OVERRIDE_SO_FULL_EDIT)

    if not is_admin:
        # Non-admin status gate. Without override: only OPEN. With
        # override: any non-SHIPPED status.
        if has_so_override:
            if so.status == SO_SHIPPED:
                return jsonify({
                    "error": "Cannot edit a SHIPPED order; void the shipment first",
                    "current_status": so.status,
                }), 403
        elif so.status != SO_OPEN:
            return jsonify({
                "error": "non-admin can only edit OPEN sales orders without the so-full-edit override",
                "current_status": so.status,
            }), 403

    # source_system reassignment is the riskier knob: ADMIN-or-override.
    # If the field was sent by a caller lacking authority, reject before
    # the UPDATE so partial writes never land.
    if "source_system" in data and not (is_admin or has_so_override):
        return jsonify({
            "error": "source_system reassignment requires ADMIN or so-full-edit override",
        }), 403

    ALLOWED_FIELDS = {
        "so_number", "so_barcode",
        "customer_name", "customer_phone", "customer_email", "customer_address",
        "ship_method", "ship_address", "ship_by_date",
        "priority", "memo", "source_system",
        # Shipment-state edits for backfill of orders shipped via
        # external systems (e.g. a retiring shipping bridge) so the warehouse view
        # reflects the right status without going through the dockd
        # PICKED -> PACKED -> SHIPPED chain. Direct UPDATE -- does NOT
        # write inventory_movements or outbox events; for one-off data
        # corrections only. Routine fulfillment still flows through
        # /api/v1/dockd/orders/<so_number>/ship.
        "status", "carrier", "tracking_number", "shipped_at",
        # mig 063: free-text upstream-origin label.
        "order_origin",
    }
    # so-ship-events: a manual status -> SHIPPED flip is a real ship,
    # recorded like every dockd / fulfill ship (item_fulfillments row +
    # line flips + ACTION_SHIP audit + ship.confirmed event) through the
    # shared record_ship body rather than a bare header UPDATE. Gated the
    # same way: order in PICKED/PACKED, every line picked (record_ship's
    # under-pick guard), and a tracking number present. The modal has no
    # carrier field -- Ship Method already conveys the operator's choice --
    # so carrier is derived from the ship method below.
    is_ship_transition = (
        data.get("status") == SO_SHIPPED and so.status != SO_SHIPPED
    )
    # record_ship owns these on a ship transition; the header UPDATE and
    # the per-field audit loop both skip them so nothing double-writes.
    SHIP_OWNED = {"status", "carrier", "tracking_number", "shipped_at"}

    ship_tracking = data["tracking_number"] if "tracking_number" in data else so.tracking_number
    ship_method = data["ship_method"] if "ship_method" in data else so.ship_method
    # Honour an explicit carrier if one is ever sent (or already on the
    # header), otherwise derive a contract-valid carrier from the ship method:
    # ship.confirmed/1 requires a non-null carrier string, and the manual-ship
    # modal has no carrier field. carrier_from_ship_method falls back to
    # "Other" so the value is always present.
    explicit_carrier = data["carrier"] if "carrier" in data else so.carrier
    ship_carrier = (
        explicit_carrier
        if (explicit_carrier and explicit_carrier.strip())
        else carrier_from_ship_method(ship_method)
    )
    if is_ship_transition:
        if so.status not in (SO_PICKED, SO_PACKED):
            return jsonify({
                "error": "Can only ship an order from PICKED or PACKED",
                "current_status": so.status,
            }), 422
        # tracking_number is required for carrier ships but not for
        # local-pickup orders (the customer walks in to collect; nothing
        # ships, so no tracking label exists). Detect local pickup by
        # ship_method substring match -- ship methods are free-text
        # values like "Local Pickup in <city> (Free)" and Will-Call
        # variants; the loose contains-"pickup" check covers both
        # without forcing operators onto a canonical enum.
        is_local_pickup = bool(
            ship_method and "pickup" in ship_method.lower()
        )
        if not (ship_tracking and ship_tracking.strip()) and not is_local_pickup:
            return jsonify({
                "error": (
                    "tracking_number is required to ship unless "
                    "ship_method indicates local pickup"
                ),
            }), 422

    fields, params, edits = [], {"sid": so_id}, []
    for col in ALLOWED_FIELDS:
        if col not in data:
            continue
        # shipped_at is handled separately below: it is a calendar date
        # anchored at noon in the company timezone, not a raw passthrough.
        if col == "shipped_at":
            continue
        new_value = data[col]
        # source_system empty string -> NULL so a CSR can clear an
        # ERP mis-tag without picking a placeholder system. Other
        # text fields keep their existing semantics.
        if col == "source_system" and new_value == "":
            new_value = None
        old_value = getattr(so, col)
        if old_value == new_value:
            continue
        edits.append((col, old_value, new_value))
        if is_ship_transition and col in SHIP_OWNED:
            continue
        fields.append(f"{col} = :{col}")
        params[col] = new_value

    # shipped_at: operator-entered Shipped Date. The wire value is a
    # calendar date (YYYY-MM-DD); anchor it at noon in COMPANY_TIMEZONE
    # so the stored UTC instant always reads back as that same date
    # regardless of DST. Empty string clears to NULL. dockd ship writes
    # the precise NOW() instant directly and is unaffected. Change
    # detection compares company-local dates so a no-op re-save writes no
    # audit row. On a ship transition the date is handed to record_ship so
    # the fulfillment row, SO header, and ship.confirmed share one instant.
    shipped_at_override = None
    if "shipped_at" in data:
        raw = (data["shipped_at"] or "").strip()
        old_local = (
            so.shipped_at.astimezone(ZoneInfo(COMPANY_TIMEZONE)).date().isoformat()
            if so.shipped_at else ""
        )
        if raw != old_local:
            edits.append(("shipped_at", so.shipped_at, raw or None))
            if is_ship_transition:
                if raw:
                    shipped_at_override = datetime.combine(
                        date.fromisoformat(raw),
                        time(12, 0),
                        tzinfo=ZoneInfo(COMPANY_TIMEZONE),
                    ).astimezone(timezone.utc)
            elif raw:
                # CAST(... AS date), not :shipped_date::date -- SQLAlchemy
                # text() mis-parses a :param immediately followed by the ::
                # cast operator and leaves the bind unsubstituted.
                fields.append(
                    "shipped_at = (CAST(:shipped_date AS date) + time '12:00') "
                    "AT TIME ZONE :company_tz"
                )
                params["shipped_date"] = raw
                params["company_tz"] = COMPANY_TIMEZONE
            else:
                fields.append("shipped_at = NULL")

    if not edits:
        return jsonify({"unchanged": True}), 200

    username = g.current_user["username"]

    # Record the ship first: record_ship's under-pick guard raises before
    # it writes anything, so a refused ship fails the whole edit before any
    # header field lands. It runs inside this open transaction (no commit)
    # under the FOR UPDATE lock taken above.
    if is_ship_transition:
        try:
            record_ship(
                g.db,
                so_id=so_id,
                so_number=so.so_number,
                so_external_id=so.external_id,
                warehouse_id=so.warehouse_id,
                tracking_number=ship_tracking,
                carrier=ship_carrier,
                ship_method=ship_method,
                username=username,
                source_txn_id=g.source_txn_id,
                pre_ship_status=so.status,
                shipped_at_override=shipped_at_override,
                audit_details_extra={"via": "admin_so_edit"},
            )
        except ValueError as e:
            g.db.rollback()
            return jsonify({"error": str(e)}), 422

    if fields:
        g.db.execute(
            text(f"UPDATE sales_orders SET {', '.join(fields)} WHERE so_id = :sid"),
            params,
        )

    user_id = g.current_user["user_id"]
    for field_changed, old_value, new_value in edits:
        # The ship fields are recorded by record_ship's ACTION_SHIP row on a
        # transition; skip their per-field header-edit rows here.
        if is_ship_transition and field_changed in SHIP_OWNED:
            continue
        action = (
            ACTION_SO_SOURCE_SYSTEM_REASSIGNED
            if field_changed == "source_system"
            else ACTION_SO_HEADER_EDITED
        )
        write_audit_log(
            g.db,
            action_type=action,
            entity_type="SO",
            entity_id=so_id,
            user_id=user_id,
            warehouse_id=so.warehouse_id,
            details={
                "field_changed": field_changed,
                "old_value": str(old_value) if old_value is not None else None,
                "new_value": str(new_value) if new_value is not None else None,
            },
        )

    # so-ship-events: every admin header edit emits salesorderedit.completed
    # carrying the per-field diff. A ship transition additionally emitted
    # ship.confirmed above (inside record_ship).
    final_status = SO_SHIPPED if is_ship_transition else (data.get("status") or so.status)
    so_source_external_id = (
        resolve_source_external_id(g.db, "sales_order", so.external_id)
        or str(so.external_id)
    )
    emit_event(
        g.db,
        event_type="salesorderedit.completed",
        event_version=1,
        aggregate_type="sales_order",
        aggregate_id=so_id,
        aggregate_external_id=so.external_id,
        warehouse_id=so.warehouse_id,
        source_txn_id=g.source_txn_id,
        payload={
            "sales_order_external_id": so_source_external_id,
            "so_number": so.so_number,
            "status": final_status,
            "changes": [
                {
                    "field": field_changed,
                    "old_value": str(old_value) if old_value is not None else None,
                    "new_value": str(new_value) if new_value is not None else None,
                }
                for field_changed, old_value, new_value in edits
            ],
            "edited_by_user_external_id": get_user_external_id(g.db, username),
            "edited_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    )

    g.db.commit()

    row = g.db.execute(
        text(
            "SELECT so_id, so_number, so_barcode, customer_name, status, "
            "       warehouse_id, ship_method, ship_address, source_system, "
            "       created_at "
            "  FROM sales_orders WHERE so_id = :sid"
        ),
        {"sid": so_id},
    ).fetchone()
    return jsonify({
        "so_id": row.so_id, "so_number": row.so_number, "so_barcode": row.so_barcode,
        "customer_name": row.customer_name, "status": row.status,
        "warehouse_id": row.warehouse_id, "ship_method": row.ship_method,
        "ship_address": row.ship_address, "source_system": row.source_system,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "edited_fields": [e[0] for e in edits],
    })


@admin_bp.route("/sales-orders/<int:so_id>/address", methods=["PATCH"])
@require_auth
@validate_body(UpdateSalesOrderAddressRequest)
@with_db
def update_sales_order_address(so_id, validated):
    """v1.8.0 (#288): edit the 16 structured billing/shipping address
    fields on a sales_order. Status gate: ADMIN can edit at any
    status; non-admin only at status='OPEN'. One audit row per
    actually-changed field carrying {field_changed, old_value,
    new_value} so investigators can reconstruct the diff without
    scanning the 16-column row state.
    """
    role = g.current_user.get("role")

    so = g.db.execute(
        text(f"""
            SELECT so_id, status, warehouse_id,
                   {", ".join(ADDRESS_FIELD_NAMES)}
              FROM sales_orders WHERE so_id = :sid FOR UPDATE
        """),
        {"sid": so_id},
    ).fetchone()
    if not so:
        return jsonify({"error": "Sales order not found"}), 404
    if role != ROLE_ADMIN and so.status != SO_OPEN:
        return jsonify({
            "error": "non-admin can only edit address on OPEN sales orders",
            "current_status": so.status,
        }), 403

    data = validated.model_dump(exclude_unset=True)
    if not data:
        return jsonify({"error": "no address fields provided"}), 400

    fields, params, edits = [], {"sid": so_id}, []
    for col, new_value in data.items():
        old_value = getattr(so, col)
        # Treat empty string as explicit clear -> NULL.
        normalized_new = new_value if new_value != "" else None
        if old_value == normalized_new:
            continue
        fields.append(f"{col} = :{col}")
        params[col] = normalized_new
        edits.append((col, old_value, normalized_new))

    if not fields:
        return jsonify({"unchanged": True}), 200

    g.db.execute(
        text(f"UPDATE sales_orders SET {', '.join(fields)} WHERE so_id = :sid"),
        params,
    )

    for field_changed, old_value, new_value in edits:
        write_audit_log(
            g.db,
            action_type=ACTION_SO_ADDRESS_EDITED,
            entity_type="SO",
            entity_id=so_id,
            user_id=g.current_user["username"],
            warehouse_id=so.warehouse_id,
            details={
                "field_changed": field_changed,
                "old_value": old_value,
                "new_value": new_value,
            },
        )

    g.db.commit()
    return jsonify({
        "so_id": so_id,
        "edited_fields": [e[0] for e in edits],
    })


@admin_bp.route("/sales-orders/<int:so_id>/memo", methods=["PATCH"])
@require_auth
@require_admin_or_page_permission("fraud")
@validate_body(UpdateSalesOrderMemoRequest)
@with_db
def update_sales_order_memo(so_id, validated):
    """Edit the free-form CSR memo on an SO (Outbound > Fraud page).
    Empty string clears to NULL. One audit row per actual change
    carrying old/new for a forensic diff."""
    so = g.db.execute(
        text("SELECT so_id, memo, warehouse_id FROM sales_orders WHERE so_id = :sid FOR UPDATE"),
        {"sid": so_id},
    ).fetchone()
    if not so:
        return jsonify({"error": "Sales order not found"}), 404

    new_memo = validated.memo if validated.memo != "" else None
    if so.memo == new_memo:
        return jsonify({"unchanged": True, "memo": so.memo}), 200

    g.db.execute(
        text("UPDATE sales_orders SET memo = :memo WHERE so_id = :sid"),
        {"memo": new_memo, "sid": so_id},
    )
    write_audit_log(
        g.db,
        action_type=ACTION_SO_MEMO_EDITED,
        entity_type="SO",
        entity_id=so_id,
        user_id=g.current_user["username"],
        warehouse_id=so.warehouse_id,
        details={"old_value": so.memo, "new_value": new_memo},
    )
    g.db.commit()
    return jsonify({"so_id": so_id, "memo": new_memo})


@admin_bp.route(
    "/sales-orders/<int:so_id>/lines/<int:so_line_id>/allocation", methods=["PATCH"]
)
@require_auth
@require_admin_or_page_permission("sales-orders")
@validate_body(UpdateLineAllocationRequest)
@with_db
def adjust_sales_order_line_allocation(so_id, so_line_id, validated):
    """Inline stepper edit of a line's standing inventory reservation.

    A rarely-used manual escape hatch: the picking flow normalizes
    reservations on its own, but an operator occasionally needs to nudge a
    line's quantity_allocated by hand. ``delta`` is the signed step; the
    result is clamped to [quantity_picked, quantity_ordered] and
    inventory.quantity_allocated is reconciled by the same amount -- reserving
    from available stock when raising, releasing back when lowering -- so the
    line-level and inventory-level reservations stay in lockstep.
    """
    line = g.db.execute(
        text(
            "SELECT sol.so_line_id, sol.item_id, sol.quantity_ordered, "
            "       sol.quantity_allocated, sol.quantity_picked, so.warehouse_id "
            "  FROM sales_order_lines sol "
            "  JOIN sales_orders so ON so.so_id = sol.so_id "
            " WHERE sol.so_line_id = :lid AND sol.so_id = :sid "
            " FOR UPDATE OF sol"
        ),
        {"lid": so_line_id, "sid": so_id},
    ).fetchone()
    if not line:
        return jsonify({"error": "Sales order line not found"}), 404

    current = line.quantity_allocated
    # Never below what's already picked (those units left the shelf), never
    # above what was ordered.
    target = max(
        line.quantity_picked,
        min(line.quantity_ordered, current + validated.delta),
    )
    if target == current:
        return jsonify(
            {"unchanged": True, "so_line_id": so_line_id, "quantity_allocated": current}
        ), 200

    if target > current:
        # Reserve the increase from available stock, fullest bin first. If the
        # warehouse cannot cover the whole step, reserve what it can; the final
        # value reflects what was achievable.
        remaining = target - current
        rows = g.db.execute(
            text(
                "SELECT inventory_id, (quantity_on_hand - quantity_allocated) AS available "
                "  FROM inventory "
                " WHERE item_id = :iid AND warehouse_id = :wh "
                "   AND quantity_on_hand > quantity_allocated "
                " ORDER BY available DESC, inventory_id "
                " FOR UPDATE"
            ),
            {"iid": line.item_id, "wh": line.warehouse_id},
        ).fetchall()
        for inv in rows:
            if remaining <= 0:
                break
            take = min(remaining, int(inv.available))
            if take <= 0:
                continue
            g.db.execute(
                text(
                    "UPDATE inventory SET quantity_allocated = quantity_allocated + :q, "
                    "       updated_at = NOW() WHERE inventory_id = :iid"
                ),
                {"q": take, "iid": inv.inventory_id},
            )
            remaining -= take
        target -= remaining  # back off to what could actually be reserved
    else:
        # Release the decrease back to inventory, walking allocated rows in the
        # warehouse-wide lock order.
        remaining = current - target
        rows = g.db.execute(
            text(
                "SELECT inventory_id, quantity_allocated FROM inventory "
                " WHERE item_id = :iid AND warehouse_id = :wh "
                "   AND quantity_allocated > 0 "
                " ORDER BY inventory_id ASC FOR UPDATE"
            ),
            {"iid": line.item_id, "wh": line.warehouse_id},
        ).fetchall()
        for inv in rows:
            if remaining <= 0:
                break
            dec = min(remaining, inv.quantity_allocated)
            g.db.execute(
                text(
                    "UPDATE inventory SET quantity_allocated = quantity_allocated - :q, "
                    "       updated_at = NOW() WHERE inventory_id = :iid"
                ),
                {"q": dec, "iid": inv.inventory_id},
            )
            remaining -= dec

    if target == current:
        # A raise that found no free stock: nothing moved.
        g.db.rollback()
        return jsonify(
            {"unchanged": True, "so_line_id": so_line_id, "quantity_allocated": current}
        ), 200

    g.db.execute(
        text("UPDATE sales_order_lines SET quantity_allocated = :q WHERE so_line_id = :lid"),
        {"q": target, "lid": so_line_id},
    )
    write_audit_log(
        g.db,
        action_type=ACTION_SO_LINE_UPDATED,
        entity_type="SO",
        entity_id=so_id,
        user_id=g.current_user["username"],
        warehouse_id=line.warehouse_id,
        details={
            "field_changed": "quantity_allocated",
            "so_line_id": so_line_id,
            "old_value": current,
            "new_value": target,
        },
    )
    g.db.commit()
    return jsonify({"so_line_id": so_line_id, "quantity_allocated": target})


@admin_bp.route("/sales-orders/<int:so_id>/push-to-queue", methods=["POST"])
@require_auth
@require_admin_or_page_permission("fraud")
@with_db
def push_sales_order_to_queue(so_id):
    """Clear an auto-flagged fraud SO and return it to the picking
    queue. Only valid when status=FRAUD_REVIEW; any other current
    status is a 409 because this path does not know how to transition
    out of (e.g.) PICKING. Status moves to OPEN."""
    so = g.db.execute(
        text("SELECT so_id, status, warehouse_id FROM sales_orders WHERE so_id = :sid FOR UPDATE"),
        {"sid": so_id},
    ).fetchone()
    if not so:
        return jsonify({"error": "Sales order not found"}), 404
    if so.status != SO_FRAUD_REVIEW:
        return jsonify({
            "error": f"Can only push FRAUD_REVIEW orders to the queue. Current: {so.status}",
            "current_status": so.status,
        }), 409

    g.db.execute(
        text("UPDATE sales_orders SET status = :status WHERE so_id = :sid"),
        {"status": SO_OPEN, "sid": so_id},
    )
    write_audit_log(
        g.db,
        action_type=ACTION_SO_FRAUD_CLEARED,
        entity_type="SO",
        entity_id=so_id,
        user_id=g.current_user["username"],
        warehouse_id=so.warehouse_id,
        details={"from_status": SO_FRAUD_REVIEW, "to_status": SO_OPEN},
    )
    g.db.commit()
    return jsonify({"so_id": so_id, "status": SO_OPEN})


@admin_bp.route("/sales-orders/<int:so_id>/cancel", methods=["POST"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@with_db
def cancel_sales_order(so_id):
    """Operator-initiated cancel. Delegates to the shared
    sales_order_service.cancel_sales_order so audit-log writing,
    per-status unwind, and SHIPPED rejection match the inbound path."""
    source_error = _inbound_lifecycle_error("sales_orders", "so_id", so_id)
    if source_error:
        return source_error
    username = g.current_user["username"]
    try:
        result = _cancel_so(
            g.db, so_id=so_id, source="admin", username=username,
        )
    except CancelNotAllowed as exc:
        if exc.current_status == "UNKNOWN":
            return jsonify({"error": "Sales order not found"}), 404
        return jsonify({
            "error": str(exc),
            "current_status": exc.current_status,
        }), 400
    g.db.commit()

    # Drain any backorder.cancelled notifications collected by the
    # service (cascade-cancelled child BOs land here). Fire-and-forget;
    # the daemon threads exit independently of the request.
    for event_type, payload, warehouse_id in result.get("deferred_notifications", []):
        dispatch_backorder_notification(
            event_type=event_type,
            payload=payload,
            warehouse_id=warehouse_id,
        )

    return jsonify({
        "message": "Sales order cancelled",
        "pre_status": result["pre_status"],
        "audit_log_id": result["audit_log_id"],
    })


@admin_bp.route("/sales-orders/<int:so_id>/revert-status", methods=["POST"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@validate_body(RevertSalesOrderStatusRequest)
@with_db
def revert_sales_order_status(so_id, validated):
    """so-refinement: admin demotes an SO from PICKED/PACKED/SHIPPED
    back to an earlier status. Delegates to the shared service so the
    pick-task release, unpack, and unship effects share one transaction
    and one audit shape. RevertNotAllowed.kind discriminates the 4xx
    response so the frontend can route the error to the right UI."""
    try:
        result = _revert_so_status(
            g.db,
            so_id=so_id,
            new_status=validated.new_status,
            release_pick_task_ids=validated.release_pick_task_ids,
            username=g.current_user["username"],
        )
    except RevertNotAllowed as exc:
        status_code = 404 if exc.kind == "not_found" else (
            409 if exc.kind == "picked_qty_remaining" else 400
        )
        body = {"error": str(exc), "kind": exc.kind, **exc.context}
        return jsonify(body), status_code
    g.db.commit()
    return jsonify({
        "message": "Sales order status reverted",
        **result,
    })


# Admin virtual pick: bin availability lookup for the admin-pick modal.
#
# Powers the per-line bin dropdown. Returns bins in the SO's warehouse
# that hold the line's item with available stock (on_hand - allocated
# > 0), sorted by preferred-bin priority first then bin_code. The
# admin-pick POST also validates warehouse + availability on submit,
# so a stale dropdown read doesn't corrupt the pick -- this endpoint
# is purely for UX.


@admin_bp.route(
    "/sales-orders/<int:so_id>/lines/<int:so_line_id>/available-bins",
    methods=["GET"],
)
@require_auth
@require_admin_or_page_permission("sales-orders")
@with_db
def admin_pick_available_bins(so_id, so_line_id):
    """List bins where the SO line's item has available stock.

    Scope: bins in the same warehouse as the SO. Filter:
    quantity_on_hand - quantity_allocated > 0. Order: preferred-bin
    priority ascending (NULL LAST so non-preferred bins still appear),
    then bin_code ascending for a stable tiebreaker.
    """
    so = g.db.execute(
        text(
            "SELECT s.warehouse_id, l.item_id "
            "  FROM sales_orders s "
            "  JOIN sales_order_lines l ON l.so_id = s.so_id "
            " WHERE s.so_id = :sid AND l.so_line_id = :lid"
        ),
        {"sid": so_id, "lid": so_line_id},
    ).fetchone()
    if so is None:
        return jsonify({
            "error": "sales order line not found or not on this SO",
        }), 404

    rows = g.db.execute(
        text(
            """
            SELECT b.bin_id, b.bin_code, z.zone_name,
                   inv.quantity_on_hand, inv.quantity_allocated,
                   (inv.quantity_on_hand - inv.quantity_allocated)
                       AS quantity_available,
                   pb.priority AS preferred_priority
              FROM inventory inv
              JOIN bins b   ON b.bin_id = inv.bin_id
              JOIN zones z  ON z.zone_id = b.zone_id
              LEFT JOIN preferred_bins pb
                ON pb.item_id = inv.item_id AND pb.bin_id = inv.bin_id
             WHERE inv.item_id = :iid
               AND b.warehouse_id = :wh
               AND inv.quantity_on_hand - inv.quantity_allocated > 0
             ORDER BY pb.priority NULLS LAST, b.bin_code ASC
            """
        ),
        {"iid": so.item_id, "wh": so.warehouse_id},
    ).fetchall()

    return jsonify({
        "warehouse_id": so.warehouse_id,
        "item_id": so.item_id,
        "bins": [
            {
                "bin_id": r.bin_id,
                "bin_code": r.bin_code,
                "zone_name": r.zone_name,
                "quantity_on_hand": r.quantity_on_hand,
                "quantity_allocated": r.quantity_allocated,
                "quantity_available": r.quantity_available,
                "preferred_priority": r.preferred_priority,
            }
            for r in rows
        ],
    })


# Admin virtual pick: operator-driven OPEN -> PICKED via real picks.
#
# The use case is the SO got picked physically but the digital pick never
# landed (legacy bridge, stuck batch, fulfilled-on-arrival inbound). The
# normal ship guard (quantity_picked > 0 per line) blocks the manual
# OPEN -> SHIPPED flip until this fires. End state is indistinguishable
# from a handheld pick: line counters bump, inventory decrements, audit
# row per pick, pick.confirmed emits when the SO completes. Undo lives
# in the existing release-pick-tasks UI -- the synthetic pick_tasks
# created here slot in like real ones.


@admin_bp.route("/sales-orders/<int:so_id>/admin-pick", methods=["POST"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@validate_body(AdminPickRequest)
@with_db
def admin_pick_sales_order(so_id, validated):
    """Apply a batched admin virtual pick to an OPEN sales order.

    Body shape: {lines: [{so_line_id, bin_id, quantity}, ...]}. Multiple
    entries with the same so_line_id are allowed (split-bin). The
    service layer is all-or-nothing in one transaction; any failure
    rolls back every line in the batch.

    Auth: ADMIN role OR so-full-edit override. The base sales-orders
    page permission gates entry; the stricter check is here because
    admin-pick mutates inventory.
    """
    role = g.current_user.get("role")
    if role != ROLE_ADMIN and not has_override(OVERRIDE_SO_FULL_EDIT):
        return jsonify({
            "error": "admin-pick requires ADMIN or so-full-edit override",
        }), 403

    try:
        result = record_admin_pick(
            g.db,
            so_id=so_id,
            picks=[ln.model_dump() for ln in validated.lines],
            username=g.current_user["username"],
        )
        promoted = maybe_promote_so_to_picked(
            g.db,
            so_id=so_id,
            username=g.current_user["username"],
        )
    except AdminPickError as exc:
        status_code = 409 if exc.kind == "insufficient_available" else 422
        return jsonify({
            "error": str(exc),
            "kind": exc.kind,
            **exc.context,
        }), status_code

    g.db.commit()
    return jsonify({
        "message": "Admin pick applied",
        "promoted_to_picked": bool(promoted),
        **result,
    })


@admin_bp.route("/sales-orders/<int:so_id>/void-return", methods=["POST"])
@require_auth
@require_role(ROLE_ADMIN)
@with_db
def void_return_order(so_id):
    """ADMIN-only soft-delete of a mistakenly created return SO (RMA).

    Delegates to sales_order_service.void_return_order, which gates on
    order_type='return' + status=OPEN + no received goods + no linked
    refund, stamps voided_at/voided_by, and writes a RETURN_VOID audit
    entry. The row persists for audit; list_sales_orders hides voided
    returns so the RMA drops off the page. Strictly ADMIN (more
    restrictive than the cancel route's page-permission gate) because it
    is a destructive operator action."""
    username = g.current_user["username"]
    try:
        result = _void_return(g.db, so_id=so_id, username=username)
    except ReturnVoidNotAllowed as exc:
        if exc.reason == "not_found":
            return jsonify({"error": "Return order not found"}), 404
        return jsonify({
            "error": str(exc),
            "reason": exc.reason,
            "current_status": exc.current_status,
        }), 409
    g.db.commit()
    return jsonify({
        "message": "Return already voided" if result["already_voided"] else "Return voided",
        "so_number": result["so_number"],
        "audit_log_id": result["audit_log_id"],
    })


# ── Partial Fulfill / Backorders ─────────────────────────────────────────────
#
# Partial-fulfill shrinks one or more lines on an existing SO and
# creates a new backorder SO (status=WAITING_STOCK) carrying the
# shorted qty. The original SO continues through pack -> ship with
# what was actually picked; the BO waits for the receiving hook in
# receiving.py to flip it to OPEN when matching stock arrives.
#
# Status gate: {OPEN, PICKED}. OPEN is allowed for any sales-orders
# user; PICKED requires ADMIN or the so-full-edit override (same
# escalation as the post-OPEN line edit gate at _so_line_edit_gate).
# Validation rule `short_qty <= quantity_ordered - quantity_picked`
# keeps PICKED-shorting safe: it is only valid when the SO has
# unpicked qty (short-picks happened during the batch). The
# defective-tote case (PICKED-with-everything-picked) requires the
# operator to cancel and recreate; out of scope for v1.
#
# BO chaining is capped at one level: a SO with parent_so_id IS NOT
# NULL rejects partial-fulfill (the UI hides the button too, but the
# server is authoritative).

ADDRESS_COPY_FIELDS = (
    "billing_address_name", "billing_address_line1", "billing_address_line2",
    "billing_address_city", "billing_address_state",
    "billing_address_postal_code", "billing_address_country",
    "billing_address_phone",
    "shipping_address_name", "shipping_address_line1", "shipping_address_line2",
    "shipping_address_city", "shipping_address_state",
    "shipping_address_postal_code", "shipping_address_country",
    "shipping_address_phone",
)


def _select_so_for_partial_fulfill(db, so_id):
    """Returns the SO row with every column the BO clone needs.

    Locked FOR UPDATE so a concurrent /cancel or /update cannot
    transition past us mid-shrink. Centralised so the partial-fulfill
    handler does not carry a 30-column SELECT inline."""
    cols = (
        "so_id, so_number, external_id, status, warehouse_id, parent_so_id, "
        "order_type, customer_name, customer_id, customer_phone, "
        "customer_address, ship_method, ship_address, source_system, memo, "
        "created_by, "
        + ", ".join(ADDRESS_COPY_FIELDS)
    )
    return db.execute(
        text(f"SELECT {cols} FROM sales_orders WHERE so_id = :sid FOR UPDATE"),
        {"sid": so_id},
    ).fetchone()


def _write_short_adjustment(db, *, item_id, warehouse_id, short_qty, reason_detail, bo_so_number, username):
    """If there is on-hand stock for this item in this warehouse, write
    a SHORT inventory_adjustments row and decrement on-hand from the bin
    with the most available units.

    Returns the adjusted qty (0 if no on-hand stock existed). The bin
    pick is deterministic (most-available, then lowest bin_id) so
    tests can assert against it."""
    bin_row = db.execute(
        text(
            "SELECT bin_id, quantity_on_hand, quantity_allocated "
            "  FROM inventory "
            " WHERE item_id = :iid AND warehouse_id = :wid "
            "   AND (quantity_on_hand - quantity_allocated) > 0 "
            " ORDER BY (quantity_on_hand - quantity_allocated) DESC, bin_id ASC "
            " LIMIT 1"
        ),
        {"iid": item_id, "wid": warehouse_id},
    ).fetchone()
    if not bin_row:
        return 0
    available = bin_row.quantity_on_hand - bin_row.quantity_allocated
    adj_qty = min(available, short_qty)
    if adj_qty <= 0:
        return 0
    db.execute(
        text(
            "UPDATE inventory "
            "   SET quantity_on_hand = GREATEST(0, quantity_on_hand - :qty), "
            "       updated_at = NOW() "
            " WHERE item_id = :iid AND bin_id = :bid"
        ),
        {"qty": adj_qty, "iid": item_id, "bid": bin_row.bin_id},
    )
    db.execute(
        text(
            "INSERT INTO inventory_adjustments ("
            "  item_id, bin_id, warehouse_id, quantity_change, reason_code, "
            "  reason_detail, status, adjusted_by, adjusted_at, external_id"
            ") VALUES ("
            "  :iid, :bid, :wid, :qty_change, :reason_code, :reason_detail, "
            "  :status, :user, NOW(), :ext_id"
            ")"
        ),
        {
            "iid": item_id,
            "bid": bin_row.bin_id,
            "wid": warehouse_id,
            "qty_change": -adj_qty,
            "reason_code": ADJ_REASON_SHORT,
            "reason_detail": f"{reason_detail} (BO: {bo_so_number})",
            "status": ADJ_APPROVED,
            "user": username,
            "ext_id": str(uuid.uuid4()),
        },
    )
    return adj_qty


@admin_bp.route("/sales-orders/<int:so_id>/partial-fulfill", methods=["POST"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@validate_body(PartialFulfillRequest)
@with_db
def partial_fulfill_sales_order(so_id, validated):
    """Ship what's pickable, create a backorder SO for the rest."""
    so = _select_so_for_partial_fulfill(g.db, so_id)
    if not so:
        return jsonify({"error": "Sales order not found"}), 404

    if so.status not in (SO_OPEN, SO_PICKED):
        return jsonify({
            "error": f"Can only partial-fulfill OPEN or PICKED orders. Current: {so.status}",
            "current_status": so.status,
        }), 400

    # A return SO (RMA goods-in) is inbound and runs a separate status
    # lifecycle; it can never be partial-fulfilled. replacement/exchange
    # children -- which also carry a parent_so_id -- ARE eligible, so the
    # gate is keyed on order_type, not on parent_so_id.
    if not order_type_allows_fulfillment_ops(so.order_type):
        return jsonify({"error": "Cannot partial-fulfill a return SO."}), 400

    # BO chaining cap: a backorder cannot itself spawn a backorder. Keyed
    # on order_type (not parent_so_id) so replacement/exchange children
    # stay eligible while a backorder still must be cancelled + recreated.
    if so.order_type == ORDER_TYPE_BACKORDER:
        return jsonify({
            "error": "This SO is a backorder; cancel it and create a fresh standalone SO instead of chaining backorders.",
            "parent_so_id": so.parent_so_id,
        }), 400

    # PICKED requires ADMIN or so-full-edit override (mirrors
    # _so_line_edit_gate). OPEN is allowed for any sales-orders user.
    role = g.current_user.get("role")
    if so.status == SO_PICKED and role != ROLE_ADMIN and not has_override(OVERRIDE_SO_FULL_EDIT):
        return jsonify({
            "error": "Partial-fulfill on a PICKED order requires ADMIN or so-full-edit override.",
            "current_status": so.status,
        }), 403

    bo_so_number = f"{so.so_number}-BO"
    reason_detail_base = (validated.reason_detail or "partial-fulfill").strip() or "partial-fulfill"
    username = g.current_user["username"]

    # 1. Validate every input line up-front (item exists, qty bounds).
    #    Validating first lets us reject 400 cleanly without partial
    #    inventory writes lingering in the transaction.
    line_inputs = []
    for entry in validated.lines:
        line = g.db.execute(
            text(
                "SELECT sol.so_line_id, sol.item_id, sol.quantity_ordered, "
                "       sol.quantity_picked, sol.line_number, "
                "       i.sku, i.item_name, i.external_id AS item_external_id "
                "  FROM sales_order_lines sol "
                "  JOIN items i ON i.item_id = sol.item_id "
                " WHERE sol.so_line_id = :sol_id AND sol.so_id = :sid "
                " FOR UPDATE OF sol"
            ),
            {"sol_id": entry.so_line_id, "sid": so_id},
        ).fetchone()
        if not line:
            return jsonify({
                "error": f"so_line_id {entry.so_line_id} not on SO {so.so_number}",
            }), 400
        unshipped = line.quantity_ordered - line.quantity_picked
        if entry.short_qty > unshipped:
            return jsonify({
                "error": (
                    f"short_qty {entry.short_qty} on line {line.line_number} "
                    f"exceeds unshipped qty ({line.quantity_ordered} ordered - "
                    f"{line.quantity_picked} picked = {unshipped})"
                ),
                "so_line_id": entry.so_line_id,
            }), 400
        line_inputs.append((entry, line))

    # 2. Apply per-line shrink + adjustment.
    short_summary = []
    for entry, line in line_inputs:
        new_qty = line.quantity_ordered - entry.short_qty
        if new_qty == 0:
            # Hard-delete shrunk-to-zero lines. Zero-qty rows break the
            # dockd payload + picker assumptions.
            g.db.execute(
                text("DELETE FROM sales_order_lines WHERE so_line_id = :sol_id"),
                {"sol_id": line.so_line_id},
            )
        else:
            g.db.execute(
                text(
                    "UPDATE sales_order_lines SET quantity_ordered = :qty "
                    " WHERE so_line_id = :sol_id"
                ),
                {"qty": new_qty, "sol_id": line.so_line_id},
            )

        adj_qty = _write_short_adjustment(
            g.db,
            item_id=line.item_id,
            warehouse_id=so.warehouse_id,
            short_qty=entry.short_qty,
            reason_detail=reason_detail_base,
            bo_so_number=bo_so_number,
            username=username,
        )

        short_summary.append({
            "so_line_id": line.so_line_id,
            "item_id": line.item_id,
            "sku": line.sku,
            "item_name": line.item_name,
            "item_external_id": str(line.item_external_id),
            "line_number": line.line_number,
            "ordered_before": line.quantity_ordered,
            "short_qty": entry.short_qty,
            "ordered_after": new_qty,
            "line_deleted": new_qty == 0,
            "adjustment_qty": adj_qty,
        })

    # 3. Create the BO SO. Address fields + customer + ship_method
    #    copied from the parent; order_total / customer_shipping_paid
    #    are NULL on the BO because they were already paid on the
    #    parent (downstream revenue reports must filter via
    #    parent_so_id IS NULL or COALESCE).
    bo_memo = f"[BACKORDER from {so.so_number}]\n" + (so.memo or "")
    bo_external_id = str(uuid.uuid4())
    address_cols = ", ".join(ADDRESS_COPY_FIELDS)
    address_binds = ", ".join(f":{f}" for f in ADDRESS_COPY_FIELDS)
    address_params = {f: getattr(so, f) for f in ADDRESS_COPY_FIELDS}

    bo_row = g.db.execute(
        text(f"""
            INSERT INTO sales_orders (
                so_number, customer_name, customer_id, customer_phone,
                customer_address, status, warehouse_id, ship_method,
                ship_address, {address_cols},
                memo, order_total, customer_shipping_paid,
                order_type, parent_so_id, backorder_opened_at,
                created_by, external_id, source_system
            ) VALUES (
                :so_number, :customer_name, :customer_id, :customer_phone,
                :customer_address, :status, :warehouse_id, :ship_method,
                :ship_address, {address_binds},
                :memo, NULL, NULL,
                :order_type, :parent_so_id, NOW(),
                :created_by, :external_id, :source_system
            ) RETURNING so_id
        """),
        {
            "so_number": bo_so_number,
            "customer_name": so.customer_name,
            "customer_id": so.customer_id,
            "customer_phone": so.customer_phone,
            "customer_address": so.customer_address,
            "status": SO_WAITING_STOCK,
            "warehouse_id": so.warehouse_id,
            "ship_method": so.ship_method,
            "ship_address": so.ship_address,
            "memo": bo_memo,
            "order_type": ORDER_TYPE_BACKORDER,
            "parent_so_id": so.so_id,
            "created_by": username,
            "external_id": bo_external_id,
            "source_system": so.source_system,
            **address_params,
        },
    ).fetchone()
    bo_so_id = bo_row.so_id

    # 4. BO lines: one per shorted entry, line_number sequential.
    for idx, shrink in enumerate(short_summary, 1):
        backorder_line = g.db.execute(
            text(
                "INSERT INTO sales_order_lines "
                "  (so_id, item_id, quantity_ordered, line_number, status, original_so_line_id) "
                "VALUES (:sid, :iid, :qty, :ln, 'PENDING', :original_line_id) "
                "RETURNING so_line_id"
            ),
            {
                "sid": bo_so_id,
                "iid": shrink["item_id"],
                "qty": shrink["short_qty"],
                "ln": idx,
                "original_line_id": None if shrink["line_deleted"] else shrink["so_line_id"],
            },
        ).fetchone()
        shrink["backorder_so_line_id"] = backorder_line.so_line_id

    # 5. Audit log. Two rows in the same transaction:
    #    - Original SO: shrink summary + BO pointer.
    #    - BO SO: creation hook + parent pointer.
    write_audit_log(
        g.db,
        action_type=ACTION_PARTIAL_FULFILL,
        entity_type="SO",
        entity_id=so.so_id,
        user_id=username,
        warehouse_id=so.warehouse_id,
        details={
            "kind": "shrink",
            "bo_so_id": bo_so_id,
            "bo_so_number": bo_so_number,
            "reason_detail": reason_detail_base,
            "pre_status": so.status,
            "lines": [
                {
                    k: v for k, v in s.items()
                    if k in (
                        "so_line_id", "sku", "line_number", "ordered_before",
                        "short_qty", "ordered_after", "line_deleted",
                        "adjustment_qty",
                    )
                }
                for s in short_summary
            ],
        },
    )
    write_audit_log(
        g.db,
        action_type=ACTION_PARTIAL_FULFILL,
        entity_type="SO",
        entity_id=bo_so_id,
        user_id=username,
        warehouse_id=so.warehouse_id,
        details={
            "kind": "created_from_partial_fulfill",
            "parent_so_id": so.so_id,
            "parent_so_number": so.so_number,
            "lines": [
                {"sku": s["sku"], "qty": s["short_qty"]} for s in short_summary
            ],
        },
    )

    # 6. Emit backorder.opened on the integration_events outbox. No
    #    consumer wires this yet (the Teams adapter does; marketplace
    #    adapter is deferred to a follow-up branch), but emitting now
    #    means "wire later" does not require an event-history
    #    backfill.
    user_external_id = get_user_external_id(g.db, username)
    opened_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    backorder_opened_payload = {
        "backorder_so_external_id": bo_external_id,
        "backorder_so_number": bo_so_number,
        "parent_so_external_id": str(so.external_id),
        "parent_so_number": so.so_number,
        "warehouse_id": so.warehouse_id,
        "customer_name": so.customer_name,
        "items": [
            {
                "item_external_id": s["item_external_id"],
                "sku": s["sku"],
                "item_name": s["item_name"],
                "qty": s["short_qty"],
                "backorder_so_line_id": s["backorder_so_line_id"],
                "parent_so_line_id": s["so_line_id"],
                "parent_line_number": s["line_number"],
            }
            for s in short_summary
        ],
        "opened_by_user_external_id": user_external_id,
        "opened_at": opened_at,
    }
    emit_event(
        g.db,
        event_type="backorder.opened",
        event_version=1,
        aggregate_type="sales_order",
        aggregate_id=bo_so_id,
        aggregate_external_id=bo_external_id,
        warehouse_id=so.warehouse_id,
        source_txn_id=g.source_txn_id,
        payload=backorder_opened_payload,
    )

    g.db.commit()

    # Fire-and-forget Teams notification after the commit lands. The
    # daemon thread opens its own DB connection, so it does not need
    # the request transaction to stay alive.
    dispatch_backorder_notification(
        event_type="backorder.opened",
        payload=backorder_opened_payload,
        warehouse_id=so.warehouse_id,
    )

    return jsonify({
        "original_so": {
            "so_id": so.so_id,
            "so_number": so.so_number,
            "status": so.status,
        },
        "backorder_so": {
            "so_id": bo_so_id,
            "so_number": bo_so_number,
            "status": SO_WAITING_STOCK,
            "external_id": bo_external_id,
        },
        "shrink_summary": short_summary,
    })


@admin_bp.route("/sales-orders/<int:bo_so_id>/cancel-backorder", methods=["POST"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@validate_body(CancelBackorderRequest)
@with_db
def cancel_backorder(bo_so_id, validated):
    """Cancel a backorder SO. Thin wrapper that stamps
    cancellation_reason then delegates to the shared cancel service.

    The backorder.cancelled event is emitted inside
    sales_order_service.cancel_sales_order (so the parent-cancel
    cascade emits too); this endpoint only sets the reason and
    handles the BO-specific validation."""
    reason = (validated.cancellation_reason or CANCEL_REASON_OTHER).strip()
    if reason not in CANCEL_REASONS:
        return jsonify({
            "error": (
                f"Invalid cancellation_reason. Allowed: "
                f"{', '.join(CANCEL_REASONS)}"
            ),
        }), 400

    bo = g.db.execute(
        text(
            "SELECT so_id, status, parent_so_id, order_type "
            "  FROM sales_orders WHERE so_id = :sid"
        ),
        {"sid": bo_so_id},
    ).fetchone()
    if not bo:
        return jsonify({"error": "Sales order not found"}), 404
    if bo.parent_so_id is None or bo.order_type != ORDER_TYPE_BACKORDER:
        return jsonify({
            "error": (
                "Not a backorder SO. Use /cancel for non-backorder orders."
            ),
        }), 400
    if bo.status == SO_CANCELLED:
        return jsonify({
            "error": "Backorder already cancelled.",
            "current_status": bo.status,
        }), 400

    # Stamp the reason BEFORE cancel_sales_order so the service-side
    # backorder.cancelled emit sees it on the row.
    g.db.execute(
        text(
            "UPDATE sales_orders SET cancellation_reason = :reason "
            " WHERE so_id = :sid"
        ),
        {"reason": reason, "sid": bo_so_id},
    )

    try:
        result = _cancel_so(
            g.db, so_id=bo_so_id, source="admin",
            username=g.current_user["username"],
        )
    except CancelNotAllowed as exc:
        if exc.current_status == "UNKNOWN":
            return jsonify({"error": "Sales order not found"}), 404
        return jsonify({
            "error": str(exc),
            "current_status": exc.current_status,
        }), 400

    # A backorder cancelled because the customer was refunded reads as
    # REFUNDED, not a plain cancel. _cancel_so already ran the inventory
    # unwind + emitted backorder.cancelled; this only relabels the terminal
    # status, matching the POS full-refund path and the mig 074 backfill.
    if reason == CANCEL_REASON_REFUNDED:
        g.db.execute(
            text("UPDATE sales_orders SET status = :st WHERE so_id = :sid"),
            {"st": SO_REFUNDED, "sid": bo_so_id},
        )

    g.db.commit()

    # Fire-and-forget Teams cards for any backorder.cancelled events
    # the service emitted (operator-initiated cancels emit exactly
    # one here; a cascade-initiated cancel emits N).
    for event_type, payload, warehouse_id in result.get("deferred_notifications", []):
        dispatch_backorder_notification(
            event_type=event_type,
            payload=payload,
            warehouse_id=warehouse_id,
        )

    return jsonify({
        "message": "Backorder cancelled",
        "bo_so_id": bo_so_id,
        "cancellation_reason": reason,
        "audit_log_id": result["audit_log_id"],
    })


@admin_bp.route("/backorders", methods=["GET"])
@require_auth
@require_admin_or_page_permission("backorders")
@with_db
def list_backorders():
    """List backorder SOs grouped by tab.

    Query params:
      - warehouse_id (optional): scope to one warehouse
      - tab: 'waiting' (status=WAITING_STOCK) or 'ready-to-ship'
        (status IN OPEN/PICKED/PACKED AND backorder_opened_at is set)
        Defaults to 'waiting'.

    Response carries per-BO items[] (sku, item_name, qty, open_po)
    + days_waiting; the ready-to-ship tab also carries
    fulfillable_since (backorder_fulfillable_at).

    open_po is the soonest-expected open PO line for the same item in
    the backorder's warehouse, or null. This was previously omitted on
    the grounds that purchase_order_lines has no expected-arrival
    column, which is true of the line but not of the header:
    purchase_orders.expected_date is what this reads."""
    tab = (request.args.get("tab") or "waiting").lower()
    warehouse_id = request.args.get("warehouse_id", type=int)

    if tab == "waiting":
        status_clause = "bo.status = :waiting"
        params = {"waiting": SO_WAITING_STOCK}
    elif tab in ("ready-to-ship", "ready_to_ship", "ready"):
        status_clause = (
            "bo.status IN (:s_open, :s_picked, :s_packed) "
            "  AND bo.backorder_opened_at IS NOT NULL"
        )
        params = {"s_open": SO_OPEN, "s_picked": SO_PICKED, "s_packed": SO_PACKED}
    else:
        return jsonify({
            "error": "tab must be 'waiting' or 'ready-to-ship'",
            "got": tab,
        }), 400

    if warehouse_id:
        status_clause += " AND bo.warehouse_id = :wid"
        params["wid"] = warehouse_id

    rows = g.db.execute(
        text(
            f"""
            SELECT bo.so_id, bo.so_number, bo.warehouse_id, bo.status,
                   bo.customer_name, bo.backorder_opened_at,
                   bo.backorder_fulfillable_at,
                   parent.so_number AS parent_so_number,
                   EXTRACT(EPOCH FROM (NOW() - bo.backorder_opened_at))::bigint AS waiting_seconds
              FROM sales_orders bo
              LEFT JOIN sales_orders parent ON parent.so_id = bo.parent_so_id
             WHERE {status_clause}
             ORDER BY bo.backorder_opened_at ASC
            """
        ),
        # No order_type filter: a POS create-without-stock backorder keeps its
        # natural order_type (sale / replacement / exchange) and is marked a
        # backorder only by status=WAITING_STOCK + backorder_opened_at. The tabs
        # already scope correctly on their own -- waiting on WAITING_STOCK (only
        # backorders sit there) and ready-to-ship on backorder_opened_at IS NOT
        # NULL (a normal order never has it) -- so both admin -BO backorders and
        # POS backorders show without leaking normal orders.
        params,
    ).fetchall()

    so_ids = [r.so_id for r in rows]
    warehouse_by_so = {r.so_id: r.warehouse_id for r in rows}
    items_by_so = {}
    if so_ids:
        line_rows = g.db.execute(
            text(
                "SELECT sol.so_id, sol.item_id, i.sku, i.item_name, "
                "       sol.quantity_ordered "
                "  FROM sales_order_lines sol "
                "  JOIN items i ON i.item_id = sol.item_id "
                " WHERE sol.so_id = ANY(:so_ids) "
                " ORDER BY sol.line_number"
            ),
            {"so_ids": so_ids},
        ).fetchall()

        # "so I know if it has been ordered". Nothing links a backorder
        # to the PO that will satisfy it, so this is derived: the soonest-
        # expected open PO line for the same item in the backorder's own
        # warehouse. That cannot tell a PO placed for this backorder from
        # general replenishment, which is why it reports the PO number rather
        # than a checkbox -- the operator can see what is being claimed.
        #
        # Led by po_id, not item_id: there is no index on
        # purchase_order_lines(item_id), but ix_purchase_order_lines_po_item
        # (mig 077) is a composite led by po_id, so joining down from the open
        # POs uses it. Open + partial POs are a small set.
        open_po_by_item = {}
        item_ids = sorted({lr.item_id for lr in line_rows})
        if item_ids:
            po_rows = g.db.execute(
                text(
                    """
                    SELECT DISTINCT ON (pol.item_id, po.warehouse_id)
                           pol.item_id, po.warehouse_id, po.po_number,
                           po.expected_date,
                           (pol.quantity_ordered - pol.quantity_received)
                               AS quantity_remaining
                      FROM purchase_orders po
                      JOIN purchase_order_lines pol ON pol.po_id = po.po_id
                     WHERE po.status IN (:po_open, :po_partial)
                       AND po.warehouse_id = ANY(:wids)
                       AND pol.item_id = ANY(:item_ids)
                       AND pol.quantity_received < pol.quantity_ordered
                     ORDER BY pol.item_id, po.warehouse_id,
                              po.expected_date ASC NULLS LAST, po.po_id ASC
                    """
                ),
                {
                    "po_open": PO_OPEN,
                    "po_partial": PO_PARTIAL,
                    "wids": sorted(set(warehouse_by_so.values())),
                    "item_ids": item_ids,
                },
            ).fetchall()
            open_po_by_item = {
                (pr.item_id, pr.warehouse_id): {
                    "po_number": pr.po_number,
                    "expected_date": (
                        pr.expected_date.isoformat() if pr.expected_date else None
                    ),
                    "quantity_remaining": pr.quantity_remaining,
                }
                for pr in po_rows
            }

        for lr in line_rows:
            items_by_so.setdefault(lr.so_id, []).append({
                "sku": lr.sku,
                "item_name": lr.item_name,
                "qty": lr.quantity_ordered,
                # null means "no open PO covers this item here", which the
                # queue renders explicitly. Absence must not read the same as
                # a field that failed to load.
                "open_po": open_po_by_item.get(
                    (lr.item_id, warehouse_by_so.get(lr.so_id))
                ),
            })

    return jsonify({
        "tab": tab,
        "backorders": [
            {
                "so_id":           r.so_id,
                "so_number":       r.so_number,
                "parent_so_number": r.parent_so_number,
                "warehouse_id":    r.warehouse_id,
                "status":          r.status,
                "customer_name":   r.customer_name,
                "items":           items_by_so.get(r.so_id, []),
                "days_waiting":    (int(r.waiting_seconds) // 86400) if r.waiting_seconds else 0,
                "backorder_opened_at": (
                    r.backorder_opened_at.isoformat()
                    if r.backorder_opened_at else None
                ),
                "fulfillable_since": (
                    r.backorder_fulfillable_at.isoformat()
                    if r.backorder_fulfillable_at else None
                ),
            }
            for r in rows
        ],
    })


@admin_bp.route("/sales-orders/<int:so_id>/admin-ship", methods=["POST"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@with_db
def admin_ship_sales_order(so_id):
    """Hand-stamp shipped quantity on a picked-but-unshipped sales order.

    Ships every shippable line (quantity_shipped = quantity_picked) with full
    fulfillment + audit bookkeeping, so the Create RMA button renders. Emits
    ship.confirmed only for a genuinely-unshipped order (a stranded-SHIPPED
    repair emits nothing -- its revenue is already in the GL). Refuses (409)
    when the SO already has a fulfillment, keeping a second ship.confirmed from
    double-counting downstream.

    Optional body: {"acknowledge_shortfall": true} to ship the picked floor of a
    legacy partial whose under-pick has no SHORT marker (the UI sets this after
    surfacing the blocking SKUs). Otherwise a silent shortfall is refused (422
    silent_shortfall).

    A dedicated route because a SHIPPED SO is hard-terminal for the normal line
    editor. Auth: ADMIN role OR so-full-edit override, same as admin-pick.
    """
    role = g.current_user.get("role")
    if role != ROLE_ADMIN and not has_override(OVERRIDE_SO_FULL_EDIT):
        return jsonify({
            "error": "admin-ship requires ADMIN or so-full-edit override",
        }), 403

    body = request.get_json(silent=True) or {}
    acknowledge_shortfall = bool(body.get("acknowledge_shortfall"))

    try:
        result = record_admin_ship(
            g.db,
            so_id=so_id,
            username=g.current_user["username"],
            source_txn_id=g.source_txn_id,
            acknowledge_shortfall=acknowledge_shortfall,
        )
    except AdminShipError as exc:
        status_code = {
            "already_fulfilled": 409,
            "not_found": 404,
        }.get(exc.kind, 422)
        g.db.rollback()
        return jsonify({
            "error": str(exc),
            "kind": exc.kind,
            **exc.context,
        }), status_code
    except ValueError as exc:
        # Defensive: any residual under-pick guard that raises ValueError.
        g.db.rollback()
        return jsonify({"error": str(exc)}), 422

    g.db.commit()
    return jsonify({
        "message": "Admin ship applied",
        "fulfillment_id": result["fulfillment_id"],
        "lines_shipped": result["lines_shipped"],
        "total_quantity": result["total_quantity"],
    })


# RMA (returns): create the goods-in RMA, then receive against it.


@admin_bp.route("/sales-orders/<int:so_id>/create-rma", methods=["POST"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@validate_body(CreateRmaRequest)
@with_db
def create_rma_route(so_id, validated):
    """Create the <orig>-RMA goods-in SO (order_type=return) for the selected
    return lines against the original SO. Operational only: no money or goods
    move here; the warehouse receives against the RMA later."""
    try:
        result = _create_rma(
            g.db,
            original_so_id=so_id,
            lines=[ln.model_dump() for ln in validated.lines],
            created_by=g.current_user["username"],
            memo=validated.memo,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    g.db.commit()
    return jsonify({"message": "RMA created", **result}), 201


@admin_bp.route("/sales-orders/<int:so_id>/receive-return", methods=["POST"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@validate_body(ReceiveRmaRequest)
@with_db
def receive_return_route(so_id, validated):
    """Receive one item into one bin against the return SO (the <orig>-RMA at
    so_id): restocks inventory, advances the RMA status, and emits
    return.received with the destination bin/warehouse as the disposition
    signal a downstream ledger maps to its GL."""
    username = g.current_user["username"]
    try:
        result = _receive_rma(
            g.db,
            rma_so_id=so_id,
            item_id=validated.item_id,
            quantity=validated.quantity,
            warehouse_id=validated.warehouse_id,
            bin_id=validated.bin_id,
            received_by=username,
            received_by_external_id=get_user_external_id(g.db, username),
            source_txn_id=g.source_txn_id,
            notes=validated.notes,
            idempotency_key=validated.idempotency_key,
            so_line_id=validated.so_line_id,
            expected_quantity_received=validated.expected_quantity_received,
            lot_number=validated.lot_number,
            serial_number=validated.serial_number,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    g.db.commit()
    return jsonify({"message": "Return received", **result})


# ── Sales Order Lines (add / update / remove) ────────────────────────────────
#
# Mirrors the PO line surface shape but layers an
# allocation-release pass because SO lines can have inventory committed
# to them post-OPEN. Status gate is the same three-tier model as
# update_sales_order: OPEN for any sales-orders user, anything else
# requires so-full-edit override, SHIPPED + CANCELLED are terminal.
#
# Pick / pack / ship state on the line is treated as a hard wall:
# quantity_picked > 0 means units have already left their source bin;
# quantity_shipped > 0 means the load has left the building. Either
# blocks edits even with the override because reconciling them needs
# the unwind paths in sales_order_service, not a direct line mutation.

SO_LINE_EDIT_TERMINAL_STATUSES = {SO_SHIPPED, SO_CANCELLED}


def _so_line_edit_gate(so):
    """Returns (allowed: bool, error_response_tuple_or_None).

    Encapsulates the status-tier + override check that all SO line
    CRUD endpoints share so the three handlers stay in lock-step.
    """
    if so.status in SO_LINE_EDIT_TERMINAL_STATUSES:
        return False, (jsonify({
            "error": f"Cannot edit lines on a {so.status} order",
            "current_status": so.status,
        }), 400)
    role = g.current_user.get("role")
    if role == ROLE_ADMIN:
        return True, None
    if so.status == SO_OPEN:
        return True, None
    if has_override(OVERRIDE_SO_FULL_EDIT):
        return True, None
    return False, (jsonify({
        "error": "Line edits past OPEN require ADMIN or so-full-edit override",
        "current_status": so.status,
    }), 403)


def _release_so_line_allocation(db, so_line_id: int, warehouse_id: int) -> int:
    """Release allocations attached to a single SO line.

    Walks the line's pending pick_tasks, decrements
    inventory.quantity_allocated by each task's quantity_to_pick, zeroes
    out sales_order_lines.quantity_allocated, then deletes the line's
    pending pick_tasks rows. The pick_batch_orders row is left
    untouched because it may still cover the SO's other lines.

    Returns the total quantity released. Caller is responsible for
    writing the audit row and for committing the transaction.

    Mirrors the per-line subset of _unwind_allocated in
    sales_order_service so the bin-level effect of a line shrink/delete
    is identical to a full-SO cancel at the OPEN/PICKING boundary.
    """
    line = db.execute(
        text(
            "SELECT so_line_id, item_id, quantity_allocated "
            "  FROM sales_order_lines WHERE so_line_id = :sol_id"
        ),
        {"sol_id": so_line_id},
    ).fetchone()
    if not line or line.quantity_allocated == 0:
        return 0

    tasks = db.execute(
        text(
            "SELECT bin_id, quantity_to_pick FROM pick_tasks "
            " WHERE so_line_id = :sol_id AND status = :task_status"
        ),
        {"sol_id": so_line_id, "task_status": TASK_PENDING},
    ).fetchall()
    released_total = 0
    for task in tasks:
        db.execute(
            text(
                "UPDATE inventory "
                "   SET quantity_allocated = GREATEST(0, quantity_allocated - :qty) "
                " WHERE item_id = :iid AND bin_id = :bid"
            ),
            {"qty": task.quantity_to_pick, "iid": line.item_id, "bid": task.bin_id},
        )
        released_total += task.quantity_to_pick

    db.execute(
        text(
            "DELETE FROM pick_tasks "
            " WHERE so_line_id = :sol_id AND status = :task_status"
        ),
        {"sol_id": so_line_id, "task_status": TASK_PENDING},
    )
    db.execute(
        text(
            "UPDATE sales_order_lines SET quantity_allocated = 0 "
            " WHERE so_line_id = :sol_id"
        ),
        {"sol_id": so_line_id},
    )
    return released_total


@admin_bp.route("/sales-orders/<int:so_id>/lines", methods=["POST"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@validate_body(AddSalesOrderLineRequest)
@with_db
def add_sales_order_line(so_id, validated):
    data = validated.model_dump()

    so = g.db.execute(
        text("SELECT so_id, status, warehouse_id FROM sales_orders WHERE so_id = :sid FOR UPDATE"),
        {"sid": so_id},
    ).fetchone()
    if not so:
        return jsonify({"error": "Sales order not found"}), 404

    allowed, err = _so_line_edit_gate(so)
    if not allowed:
        return err

    item = g.db.execute(
        text("SELECT item_id, sku FROM items WHERE item_id = :iid"),
        {"iid": data["item_id"]},
    ).fetchone()
    if not item:
        return jsonify({"error": f"Item {data['item_id']} not found"}), 400

    # Duplicate-item guard mirrors the PO surface: allocation +
    # pick-batch creation paths fetch by (so_id, item_id) and cannot
    # disambiguate two rows with the same item_id.
    dup = g.db.execute(
        text("SELECT 1 FROM sales_order_lines WHERE so_id = :sid AND item_id = :iid"),
        {"sid": so_id, "iid": data["item_id"]},
    ).fetchone()
    if dup:
        return jsonify({"error": f"Item {item.sku} is already on this SO"}), 400

    next_ln = g.db.execute(
        text("SELECT COALESCE(MAX(line_number), 0) + 1 FROM sales_order_lines WHERE so_id = :sid"),
        {"sid": so_id},
    ).scalar()

    result = g.db.execute(
        text(
            """
            INSERT INTO sales_order_lines
                (so_id, item_id, quantity_ordered, line_number)
            VALUES (:sid, :iid, :qty, :ln)
            RETURNING so_line_id
            """
        ),
        {
            "sid": so_id, "iid": data["item_id"],
            "qty": data["quantity_ordered"], "ln": next_ln,
        },
    )
    so_line_id = result.fetchone()[0]

    write_audit_log(
        g.db,
        ACTION_SO_LINE_ADDED,
        "SO_LINE",
        so_line_id,
        g.current_user["user_id"],
        so.warehouse_id,
        details={
            "so_id": so_id, "sku": item.sku,
            "quantity_ordered": data["quantity_ordered"],
        },
    )
    g.db.commit()

    return jsonify({
        "so_line_id": so_line_id, "so_id": so_id,
        "item_id": data["item_id"], "sku": item.sku,
        "quantity_ordered": data["quantity_ordered"],
        "quantity_allocated": 0, "quantity_picked": 0,
        "quantity_packed": 0, "quantity_shipped": 0,
        "line_number": next_ln,
    }), 201


@admin_bp.route("/sales-orders/<int:so_id>/lines/<int:so_line_id>", methods=["PATCH"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@validate_body(UpdateSalesOrderLineRequest)
@with_db
def update_sales_order_line(so_id, so_line_id, validated):
    data = validated.model_dump()

    so = g.db.execute(
        text("SELECT so_id, status, warehouse_id FROM sales_orders WHERE so_id = :sid FOR UPDATE"),
        {"sid": so_id},
    ).fetchone()
    if not so:
        return jsonify({"error": "Sales order not found"}), 404

    allowed, err = _so_line_edit_gate(so)
    if not allowed:
        return err

    line = g.db.execute(
        text(
            """
            SELECT sol.so_line_id, sol.quantity_ordered, sol.quantity_allocated,
                   sol.quantity_picked, sol.quantity_packed, sol.quantity_shipped,
                   i.sku
              FROM sales_order_lines sol JOIN items i ON i.item_id = sol.item_id
             WHERE sol.so_line_id = :sol_id AND sol.so_id = :sid
            """
        ),
        {"sol_id": so_line_id, "sid": so_id},
    ).fetchone()
    if not line:
        return jsonify({"error": "Line not found"}), 404

    new_qty = data["quantity_ordered"]
    # Picked / packed / shipped units already left their source bin or
    # the building. Going below those numbers would orphan physical
    # movements; require the operator to unwind via the dedicated paths
    # (short-pick / void-ship) before shrinking the line.
    picked_or_packed = max(line.quantity_picked, line.quantity_packed)
    if new_qty < picked_or_packed:
        return jsonify({
            "error": (
                f"quantity_ordered ({new_qty}) cannot be less than "
                f"already-picked/packed quantity ({picked_or_packed})"
            ),
        }), 400
    if new_qty < line.quantity_shipped:
        return jsonify({
            "error": (
                f"quantity_ordered ({new_qty}) cannot be less than "
                f"already-shipped quantity ({line.quantity_shipped})"
            ),
        }), 400

    released = 0
    # Shrinking below quantity_allocated releases the full line
    # allocation; re-allocation happens at next pick-batch create.
    # Going from N to a smaller N' that is still >= quantity_allocated
    # leaves the existing allocation in place because the picker can
    # still fulfil it.
    if new_qty < line.quantity_allocated and line.quantity_allocated > 0:
        released = _release_so_line_allocation(g.db, so_line_id, so.warehouse_id)
        if released > 0:
            write_audit_log(
                g.db,
                ACTION_SO_ALLOCATION_RELEASED,
                "SO_LINE",
                so_line_id,
                g.current_user["user_id"],
                so.warehouse_id,
                details={
                    "so_id": so_id, "sku": line.sku,
                    "released_quantity": released,
                    "reason": "line_quantity_reduced",
                },
            )

    g.db.execute(
        text(
            "UPDATE sales_order_lines SET quantity_ordered = :qty "
            " WHERE so_line_id = :sol_id"
        ),
        {"qty": new_qty, "sol_id": so_line_id},
    )

    write_audit_log(
        g.db,
        ACTION_SO_LINE_UPDATED,
        "SO_LINE",
        so_line_id,
        g.current_user["user_id"],
        so.warehouse_id,
        details={
            "so_id": so_id, "sku": line.sku,
            "old_quantity_ordered": line.quantity_ordered,
            "new_quantity_ordered": new_qty,
        },
    )
    g.db.commit()
    return jsonify({
        "so_line_id": so_line_id,
        "quantity_ordered": new_qty,
        "released_allocation": released,
    })


@admin_bp.route("/sales-orders/<int:so_id>/lines/<int:so_line_id>", methods=["DELETE"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@with_db
def delete_sales_order_line(so_id, so_line_id):
    so = g.db.execute(
        text("SELECT so_id, status, warehouse_id FROM sales_orders WHERE so_id = :sid FOR UPDATE"),
        {"sid": so_id},
    ).fetchone()
    if not so:
        return jsonify({"error": "Sales order not found"}), 404

    allowed, err = _so_line_edit_gate(so)
    if not allowed:
        return err

    line = g.db.execute(
        text(
            """
            SELECT sol.so_line_id, sol.quantity_ordered, sol.quantity_allocated,
                   sol.quantity_picked, sol.quantity_packed, sol.quantity_shipped,
                   i.sku
              FROM sales_order_lines sol JOIN items i ON i.item_id = sol.item_id
             WHERE sol.so_line_id = :sol_id AND sol.so_id = :sid
            """
        ),
        {"sol_id": so_line_id, "sid": so_id},
    ).fetchone()
    if not line:
        return jsonify({"error": "Line not found"}), 404

    # Picked / packed / shipped units block deletion at any tier. To
    # remove these the operator must unwind through the dedicated
    # surfaces (short-pick / void-ship) so inventory movements stay
    # consistent.
    if line.quantity_picked > 0 or line.quantity_packed > 0 or line.quantity_shipped > 0:
        return jsonify({
            "error": (
                "Line has picked/packed/shipped units; unwind via the "
                "ship-void or short-pick path before removing the line"
            ),
            "quantity_picked": line.quantity_picked,
            "quantity_packed": line.quantity_packed,
            "quantity_shipped": line.quantity_shipped,
        }), 400

    released = 0
    if line.quantity_allocated > 0:
        released = _release_so_line_allocation(g.db, so_line_id, so.warehouse_id)
        if released > 0:
            write_audit_log(
                g.db,
                ACTION_SO_ALLOCATION_RELEASED,
                "SO_LINE",
                so_line_id,
                g.current_user["user_id"],
                so.warehouse_id,
                details={
                    "so_id": so_id, "sku": line.sku,
                    "released_quantity": released,
                    "reason": "line_deleted",
                },
            )

    g.db.execute(
        text("DELETE FROM sales_order_lines WHERE so_line_id = :sol_id"),
        {"sol_id": so_line_id},
    )

    write_audit_log(
        g.db,
        ACTION_SO_LINE_REMOVED,
        "SO_LINE",
        so_line_id,
        g.current_user["user_id"],
        so.warehouse_id,
        details={
            "so_id": so_id, "sku": line.sku,
            "quantity_ordered": line.quantity_ordered,
        },
    )
    g.db.commit()
    return jsonify({
        "so_line_id": so_line_id, "deleted": True,
        "released_allocation": released,
    })


# ── Short Picks Report ────────────────────────────────────────────────────────

@admin_bp.route("/short-picks", methods=["GET"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@with_db
def get_short_picks():
    """Return recent short pick events from the audit log."""
    days = request.args.get("days", 30, type=int)
    warehouse_id = request.args.get("warehouse_id", type=int)
    wh_clause = "AND a.warehouse_id = :wid" if warehouse_id else ""
    params = {"days": days}
    if warehouse_id:
        params["wid"] = warehouse_id

    params["action_type"] = ACTION_PICK
    rows = g.db.execute(
        text(f"""
            SELECT a.log_id, a.user_id, a.created_at,
                   a.details->>'sku' AS sku,
                   (a.details->>'quantity_to_pick')::int AS qty_expected,
                   (a.details->>'quantity_picked')::int AS qty_picked,
                   (a.details->>'shortage')::int AS shortage,
                   b.bin_code,
                   a.details->>'batch_id' AS batch_id
            FROM audit_log a
            LEFT JOIN bins b ON b.bin_id = (a.details->>'bin_id')::int
            WHERE a.action_type = :action_type
              AND a.details->>'type' = 'SHORT_PICK'
              AND a.created_at >= NOW() - make_interval(days => :days)
              {wh_clause}
            ORDER BY a.created_at DESC
            LIMIT 100
        """),
        params,
    ).fetchall()

    return jsonify({
        "short_picks": [
            {
                "log_id": r.log_id,
                "user": r.user_id,
                "sku": r.sku,
                "qty_expected": r.qty_expected,
                "qty_picked": r.qty_picked,
                "shortage": r.shortage,
                "bin_code": r.bin_code,
                "batch_id": r.batch_id,
                "timestamp": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "total": len(rows),
    })
