"""Purchase Orders, Sales Orders, and Short Picks endpoints."""

import math
import uuid

from flask import g, jsonify, request
from sqlalchemy import text

from constants import (
    PO_OPEN, PO_PARTIAL, PO_RECEIVED, PO_CLOSED, PO_ARCHIVED,
    SO_OPEN, SO_PICKING, SO_PICKED, SO_PACKED, SO_SHIPPED, SO_CANCELLED,
    SO_FRAUD_REVIEW,
    TASK_PENDING, ADJ_PENDING,
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
    OVERRIDE_PO_FULL_EDIT,
    OVERRIDE_SO_FULL_EDIT,
    ROLE_ADMIN,
)
from middleware.auth_middleware import (
    has_override,
    require_admin_or_page_permission,
    require_auth,
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
    CreateSalesOrderRequest,
    MarkSalesOrdersPrintedRequest,
    UpdateSalesOrderAddressRequest,
    UpdateSalesOrderLineRequest,
    UpdateSalesOrderMemoRequest,
    UpdateSalesOrderRequest,
)
from services.audit_service import write_audit_log
from services.sales_order_service import (
    CancelNotAllowed,
    cancel_sales_order as _cancel_so,
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
        text("SELECT po_id, po_number, po_barcode, vendor_name, vendor_id, status, expected_date, warehouse_id, notes, created_at, received_at, created_by FROM purchase_orders WHERE po_id = :pid"),
        {"pid": po_id},
    ).fetchone()
    if not po:
        return jsonify({"error": "Purchase order not found"}), 404

    lines = g.db.execute(
        text("""
            SELECT pol.po_line_id, pol.line_number, pol.item_id, i.sku, i.item_name, i.upc,
                   pol.quantity_ordered, pol.quantity_received, pol.unit_cost, pol.status
            FROM purchase_order_lines pol JOIN items i ON i.item_id = pol.item_id
            WHERE pol.po_id = :pid ORDER BY pol.line_number
        """),
        {"pid": po_id},
    ).fetchall()

    return jsonify({
        "purchase_order": {
            "po_id": po.po_id, "po_number": po.po_number, "po_barcode": po.po_barcode,
            "vendor_name": po.vendor_name, "status": po.status,
            "expected_date": po.expected_date.isoformat() if po.expected_date else None,
            "warehouse_id": po.warehouse_id, "notes": po.notes,
            "created_at": po.created_at.isoformat() if po.created_at else None,
        },
        "lines": [
            {"po_line_id": l.po_line_id, "line_number": l.line_number, "item_id": l.item_id,
             "sku": l.sku, "item_name": l.item_name, "upc": l.upc,
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


def _po_edit_blocked(po_status: str) -> bool:
    """avid-overhaul-mk1 P7: PO_EDIT_BLOCKED_STATUSES check that yields
    to the po-full-edit override. ADMIN bypasses via has_override.

    Returns True iff the current user CANNOT edit a PO in po_status.
    """
    if po_status not in PO_EDIT_BLOCKED_STATUSES:
        return False
    return not has_override(OVERRIDE_PO_FULL_EDIT)


@admin_bp.route("/purchase-orders/<int:po_id>", methods=["PUT"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@validate_body(UpdatePurchaseOrderRequest)
@with_db
def update_purchase_order(po_id, validated):
    data = validated.model_dump(exclude_unset=True)

    po = g.db.execute(
        text("SELECT po_id, status, warehouse_id FROM purchase_orders WHERE po_id = :pid"),
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
    # can transition out of CLOSED before resuming line work. The
    # po-full-edit override (P7) lifts this restriction for users who
    # hold the grant; ADMIN bypasses via has_override.
    if header_data and _po_edit_blocked(po.status):
        return jsonify({
            "error": (
                f"Header edits are not allowed while the PO is {po.status} "
                "without the po-full-edit override"
            ),
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
        text("SELECT po_id, status, warehouse_id FROM purchase_orders WHERE po_id = :pid"),
        {"pid": po_id},
    ).fetchone()
    if not po:
        return jsonify({"error": "Purchase order not found"}), 404
    if _po_edit_blocked(po.status):
        return jsonify({
            "error": (
                f"Cannot add lines while the PO is {po.status} "
                "without the po-full-edit override"
            ),
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
    g.db.commit()

    return jsonify({
        "po_line_id": po_line_id,
        "po_id": po_id,
        "item_id": data["item_id"],
        "sku": item.sku,
        "quantity_ordered": data["quantity_ordered"],
        "quantity_received": 0,
        "line_number": next_ln,
    }), 201


@admin_bp.route("/purchase-orders/<int:po_id>/lines/<int:po_line_id>", methods=["PATCH"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@validate_body(UpdatePOLineRequest)
@with_db
def update_purchase_order_line(po_id, po_line_id, validated):
    data = validated.model_dump()

    po = g.db.execute(
        text("SELECT po_id, status, warehouse_id FROM purchase_orders WHERE po_id = :pid"),
        {"pid": po_id},
    ).fetchone()
    if not po:
        return jsonify({"error": "Purchase order not found"}), 404
    if _po_edit_blocked(po.status):
        return jsonify({
            "error": (
                f"Cannot edit lines while the PO is {po.status} "
                "without the po-full-edit override"
            ),
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
    g.db.commit()
    return jsonify({"po_line_id": po_line_id, "quantity_ordered": new_qty})


@admin_bp.route("/purchase-orders/<int:po_id>/lines/<int:po_line_id>", methods=["DELETE"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@with_db
def delete_purchase_order_line(po_id, po_line_id):
    po = g.db.execute(
        text("SELECT po_id, status, warehouse_id FROM purchase_orders WHERE po_id = :pid"),
        {"pid": po_id},
    ).fetchone()
    if not po:
        return jsonify({"error": "Purchase order not found"}), 404
    if _po_edit_blocked(po.status):
        return jsonify({
            "error": (
                f"Cannot remove lines while the PO is {po.status} "
                "without the po-full-edit override"
            ),
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
    g.db.commit()
    return jsonify({"po_line_id": po_line_id, "deleted": True})


@admin_bp.route("/purchase-orders/<int:po_id>/close", methods=["POST"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@with_db
def close_purchase_order(po_id):
    po = g.db.execute(
        text("SELECT po_id, status FROM purchase_orders WHERE po_id = :pid"),
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
    po = g.db.execute(
        text("SELECT po_id, status FROM purchase_orders WHERE po_id = :pid"),
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


# ── Sales Orders ──────────────────────────────────────────────────────────────

@admin_bp.route("/sales-orders", methods=["GET"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@with_db
def list_sales_orders():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 1000)

    where_clauses, params = [], {}
    status = request.args.get("status")
    warehouse_id = request.args.get("warehouse_id", type=int)
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
    if status:
        where_clauses.append("so.status = :status")
        params["status"] = status
    if warehouse_id:
        where_clauses.append("so.warehouse_id = :wid")
        params["wid"] = warehouse_id
    if hide_printed:
        where_clauses.append("so.printed_at IS NULL")
    if search:
        where_clauses.append("(so.so_number ILIKE :q OR so.customer_name ILIKE :q)")
        params["q"] = f"%{search}%"

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
            SELECT so.so_id, so.so_number, so.so_barcode, so.customer_name, so.customer_phone, so.customer_address,
                   so.status, so.priority, so.warehouse_id,
                   so.ship_method, so.ship_address, so.order_date, so.ship_by_date, so.created_at, so.created_by,
                   so.carrier, so.tracking_number, so.shipped_at, so.memo, so.printed_at,
                   so.billing_address_name, so.billing_address_line1, so.billing_address_line2,
                   so.billing_address_city, so.billing_address_state,
                   so.billing_address_postal_code, so.billing_address_country,
                   so.shipping_address_name, so.shipping_address_line1, so.shipping_address_line2,
                   so.shipping_address_city, so.shipping_address_state,
                   so.shipping_address_postal_code, so.shipping_address_country
                   {primary_bin_select}
            FROM sales_orders so {where_sql}
            ORDER BY so.so_id DESC LIMIT :limit OFFSET :offset
        """),
        params,
    ).fetchall()

    def _row_to_dict(r):
        out = {
            "so_id": r.so_id, "so_number": r.so_number, "so_barcode": r.so_barcode,
            "customer_name": r.customer_name, "customer_phone": r.customer_phone,
            "customer_address": r.customer_address,
            "status": r.status, "priority": r.priority,
            "warehouse_id": r.warehouse_id, "ship_method": r.ship_method,
            "ship_address": r.ship_address,
            "order_date": r.order_date.isoformat() if r.order_date else None,
            "ship_by_date": r.ship_by_date.isoformat() if r.ship_by_date else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "created_by": r.created_by,
            "carrier": r.carrier, "tracking_number": r.tracking_number,
            "shipped_at": r.shipped_at.isoformat() if r.shipped_at else None,
            "memo": r.memo,
            "printed_at": r.printed_at.isoformat() if r.printed_at else None,
            "billing_address_name": r.billing_address_name,
            "billing_address_line1": r.billing_address_line1,
            "billing_address_line2": r.billing_address_line2,
            "billing_address_city": r.billing_address_city,
            "billing_address_state": r.billing_address_state,
            "billing_address_postal_code": r.billing_address_postal_code,
            "billing_address_country": r.billing_address_country,
            "shipping_address_name": r.shipping_address_name,
            "shipping_address_line1": r.shipping_address_line1,
            "shipping_address_line2": r.shipping_address_line2,
            "shipping_address_city": r.shipping_address_city,
            "shipping_address_state": r.shipping_address_state,
            "shipping_address_postal_code": r.shipping_address_postal_code,
            "shipping_address_country": r.shipping_address_country,
        }
        if include_primary_bin:
            out["primary_bin_code"] = r.primary_bin_code
            out["primary_bin_pick_sequence"] = r.primary_bin_pick_sequence
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
            SELECT so_id, so_number, so_barcode, customer_name, status, priority,
                   warehouse_id, ship_method, ship_address,
                   order_date, ship_by_date, created_at, picked_at, packed_at,
                   shipped_at, created_by,
                   order_total, customer_shipping_paid, memo,
                   source_system,
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
        {"sid": so_id},
    ).fetchone()
    if not so:
        return jsonify({"error": "Sales order not found"}), 404

    lines = g.db.execute(
        text("""
            SELECT sol.so_line_id, sol.line_number, sol.item_id, i.sku, i.item_name, i.upc,
                   sol.quantity_ordered, sol.quantity_allocated, sol.quantity_picked, sol.quantity_packed, sol.quantity_shipped, sol.status
            FROM sales_order_lines sol JOIN items i ON i.item_id = sol.item_id
            WHERE sol.so_id = :sid ORDER BY sol.line_number
        """),
        {"sid": so_id},
    ).fetchall()

    return jsonify({
        "sales_order": {
            "so_id": so.so_id, "so_number": so.so_number, "so_barcode": so.so_barcode,
            "customer_name": so.customer_name, "status": so.status, "priority": so.priority,
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
            # avid-overhaul-mk1 P7 (mig 060): denormalised source_system
            # tag. NULL for admin-created and POS-created SOs.
            "source_system": so.source_system,
            # v1.8.0 (#288): structured billing/shipping address fields.
            **{name: getattr(so, name) for name in ADDRESS_FIELD_NAMES},
        },
        "lines": [
            {"so_line_id": l.so_line_id, "line_number": l.line_number, "item_id": l.item_id,
             "sku": l.sku, "item_name": l.item_name, "upc": l.upc,
             "quantity_ordered": l.quantity_ordered, "quantity_allocated": l.quantity_allocated,
             "quantity_picked": l.quantity_picked, "quantity_packed": l.quantity_packed,
             "quantity_shipped": l.quantity_shipped, "status": l.status}
            for l in lines
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
                   warehouse_id, ship_method,
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
            INSERT INTO sales_orders (so_number, so_barcode, customer_name, customer_phone, customer_address, warehouse_id, ship_method, ship_address, ship_by_date, memo, order_date, created_by, status, external_id)
            VALUES (:sn, :sb, :cust, :phone, :caddr, :wid, :ship, :addr, :ship_by, :memo, NOW(), :created_by, :status, :ext_id)
            RETURNING so_id
        """),
        {
            "sn": data["so_number"], "sb": data.get("so_barcode", data["so_number"]),
            "cust": data.get("customer_name"), "phone": data.get("customer_phone"),
            "caddr": data.get("customer_address"),
            "wid": data["warehouse_id"],
            "ship": data.get("ship_method"), "addr": data.get("ship_address"),
            "ship_by": data.get("ship_by_date"), "memo": data.get("memo"),
            "created_by": g.current_user["username"],
            "status": SO_OPEN,
            "ext_id": str(uuid.uuid4()),
        },
    )
    so_id = result.fetchone()[0]

    for line in data["lines"]:
        g.db.execute(
            text("INSERT INTO sales_order_lines (so_id, item_id, quantity_ordered, line_number) VALUES (:sid, :iid, :qty, :ln)"),
            {"sid": so_id, "iid": line["item_id"], "qty": line["quantity_ordered"], "ln": line.get("line_number") or 1},
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

    Status policy (avid-overhaul-mk1 P7):
      * ADMIN: edit at any status (including SHIPPED for legacy
        ShipRush backfill, unchanged from v1.10.4).
      * USER with so-full-edit override: edit at any status except
        SHIPPED.
      * USER without override: edit only when status = OPEN.

    Source-system reassignment (mig 060) is gated to ADMIN or
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
            "SELECT so_id, status, warehouse_id, source_system, "
            "       so_number, so_barcode, "
            "       customer_name, customer_phone, customer_address, "
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
        "customer_name", "customer_phone", "customer_address",
        "ship_method", "ship_address", "ship_by_date",
        "priority", "memo", "source_system",
        # v1.10.4: shipment-state edits for backfill of orders shipped via
        # external systems (legacy ShipRush bridge). Routine fulfillment
        # still flows through dockd ship.
        "status", "carrier", "tracking_number", "shipped_at",
    }
    fields, params, edits = [], {"sid": so_id}, []
    for col in ALLOWED_FIELDS:
        if col not in data:
            continue
        new_value = data[col]
        # source_system empty string -> NULL so a CSR can clear an
        # ERP mis-tag without picking a placeholder system. Other
        # text fields keep their existing semantics.
        if col == "source_system" and new_value == "":
            new_value = None
        old_value = getattr(so, col)
        # Coerce dates / ints on the audit side so the JSON details
        # match the wire representation operators sent.
        if old_value == new_value:
            continue
        fields.append(f"{col} = :{col}")
        params[col] = new_value
        edits.append((col, old_value, new_value))

    if not fields:
        return jsonify({"unchanged": True}), 200

    g.db.execute(
        text(f"UPDATE sales_orders SET {', '.join(fields)} WHERE so_id = :sid"),
        params,
    )

    user_id = g.current_user["user_id"]
    for field_changed, old_value, new_value in edits:
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
@require_admin_or_page_permission("sales-orders")
@validate_body(UpdateSalesOrderMemoRequest)
@with_db
def update_sales_order_memo(so_id, validated):
    """mig 054: edit the free-form memo on an SO. Empty string = clear
    to NULL. One audit row per actual change carrying old/new for
    forensic diff."""
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


@admin_bp.route("/sales-orders/<int:so_id>/push-to-queue", methods=["POST"])
@require_auth
@require_admin_or_page_permission("fraud")
@with_db
def push_sales_order_to_queue(so_id):
    """mig 054: clear an auto-flagged fraud SO and put it back into
    the picking queue. Only valid when status=FRAUD_REVIEW; any
    other current status is a 409 because nothing here knows how to
    transition out of (e.g.) PICKING. Status moves to OPEN."""
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
    return jsonify({
        "message": "Sales order cancelled",
        "pre_status": result["pre_status"],
        "audit_log_id": result["audit_log_id"],
    })


# ── Sales Order Lines (add / update / remove) ────────────────────────────────
#
# avid-overhaul-mk1 P7. Mirrors the PO line CRUD shape but layers an
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
