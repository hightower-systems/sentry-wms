"""Purchase Orders, Sales Orders, and Short Picks endpoints."""

import math

from flask import g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_auth, require_role
from middleware.db import with_db
from routes.admin import admin_bp


# ── Purchase Orders ───────────────────────────────────────────────────────────

@admin_bp.route("/purchase-orders", methods=["GET"])
@require_auth
@with_db
def list_purchase_orders():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)

    where_clauses, params = [], {}
    status = request.args.get("status")
    warehouse_id = request.args.get("warehouse_id", type=int)
    if status:
        where_clauses.append("status = :status")
        params["status"] = status
    if warehouse_id:
        where_clauses.append("warehouse_id = :wid")
        params["wid"] = warehouse_id

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
@require_role("ADMIN", "MANAGER")
@with_db
def create_purchase_order():
    data = request.get_json()
    if not data or not data.get("po_number") or not data.get("warehouse_id") or not data.get("lines"):
        return jsonify({"error": "po_number, warehouse_id, and lines are required"}), 400

    dup = g.db.execute(text("SELECT 1 FROM purchase_orders WHERE po_number = :pn"), {"pn": data["po_number"]}).fetchone()
    if dup:
        return jsonify({"error": f"Duplicate po_number: {data['po_number']}"}), 400

    # Validate items
    for line in data["lines"]:
        item = g.db.execute(text("SELECT 1 FROM items WHERE item_id = :iid"), {"iid": line["item_id"]}).fetchone()
        if not item:
            return jsonify({"error": f"Item {line['item_id']} not found"}), 400

    result = g.db.execute(
        text("""
            INSERT INTO purchase_orders (po_number, po_barcode, vendor_name, expected_date, warehouse_id, notes, created_by, status)
            VALUES (:pn, :pb, :vendor, :exp_date, :wid, :notes, :created_by, 'OPEN')
            RETURNING po_id
        """),
        {
            "pn": data["po_number"], "pb": data.get("po_barcode", data["po_number"]),
            "vendor": data.get("vendor_name"), "exp_date": data.get("expected_date"),
            "wid": data["warehouse_id"], "notes": data.get("notes"),
            "created_by": g.current_user["username"],
        },
    )
    po_id = result.fetchone()[0]

    for line in data["lines"]:
        g.db.execute(
            text("INSERT INTO purchase_order_lines (po_id, item_id, quantity_ordered, unit_cost, line_number) VALUES (:pid, :iid, :qty, :cost, :ln)"),
            {"pid": po_id, "iid": line["item_id"], "qty": line["quantity_ordered"],
             "cost": line.get("unit_cost"), "ln": line.get("line_number", 1)},
        )

    g.db.commit()

    # Re-fetch to return (save/restore g.db since get_purchase_order has @with_db)
    outer_db = g.db
    response = get_purchase_order(po_id)
    g.db = outer_db
    return response


@admin_bp.route("/purchase-orders/<int:po_id>", methods=["PUT"])
@require_auth
@require_role("ADMIN", "MANAGER")
@with_db
def update_purchase_order(po_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    po = g.db.execute(text("SELECT po_id, status FROM purchase_orders WHERE po_id = :pid"), {"pid": po_id}).fetchone()
    if not po:
        return jsonify({"error": "Purchase order not found"}), 404
    if po.status != "OPEN":
        return jsonify({"error": f"Can only update POs with OPEN status. Current: {po.status}"}), 400

    fields, params = [], {"pid": po_id}
    for col in ("po_number", "po_barcode", "vendor_name", "expected_date", "notes"):
        if col in data:
            fields.append(f"{col} = :{col}")
            params[col] = data[col]

    if not fields:
        return jsonify({"error": "No fields to update"}), 400

    g.db.execute(text(f"UPDATE purchase_orders SET {', '.join(fields)} WHERE po_id = :pid"), params)
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


@admin_bp.route("/purchase-orders/<int:po_id>/close", methods=["POST"])
@require_auth
@require_role("ADMIN", "MANAGER")
@with_db
def close_purchase_order(po_id):
    po = g.db.execute(text("SELECT po_id FROM purchase_orders WHERE po_id = :pid"), {"pid": po_id}).fetchone()
    if not po:
        return jsonify({"error": "Purchase order not found"}), 404

    g.db.execute(text("UPDATE purchase_orders SET status = 'CLOSED' WHERE po_id = :pid"), {"pid": po_id})
    g.db.commit()
    return jsonify({"message": "Purchase order closed"})


# ── Sales Orders ──────────────────────────────────────────────────────────────

@admin_bp.route("/sales-orders", methods=["GET"])
@require_auth
@with_db
def list_sales_orders():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)

    where_clauses, params = [], {}
    status = request.args.get("status")
    warehouse_id = request.args.get("warehouse_id", type=int)
    if status:
        where_clauses.append("status = :status")
        params["status"] = status
    if warehouse_id:
        where_clauses.append("warehouse_id = :wid")
        params["wid"] = warehouse_id

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    total = g.db.execute(text(f"SELECT COUNT(*) FROM sales_orders {where_sql}"), params).scalar()
    pages = max(1, math.ceil(total / per_page))

    params["limit"] = per_page
    params["offset"] = (page - 1) * per_page
    rows = g.db.execute(
        text(f"""
            SELECT so_id, so_number, so_barcode, customer_name, status, priority, warehouse_id,
                   ship_method, ship_address, order_date, ship_by_date, created_at, created_by,
                   carrier, tracking_number, shipped_at
            FROM sales_orders {where_sql} ORDER BY so_id DESC LIMIT :limit OFFSET :offset
        """),
        params,
    ).fetchall()

    return jsonify({
        "sales_orders": [
            {"so_id": r.so_id, "so_number": r.so_number, "so_barcode": r.so_barcode,
             "customer_name": r.customer_name, "status": r.status, "priority": r.priority,
             "warehouse_id": r.warehouse_id, "ship_method": r.ship_method, "ship_address": r.ship_address,
             "order_date": r.order_date.isoformat() if r.order_date else None,
             "ship_by_date": r.ship_by_date.isoformat() if r.ship_by_date else None,
             "created_at": r.created_at.isoformat() if r.created_at else None, "created_by": r.created_by,
             "carrier": r.carrier, "tracking_number": r.tracking_number,
             "shipped_at": r.shipped_at.isoformat() if r.shipped_at else None}
            for r in rows
        ],
        "total": total, "page": page, "per_page": per_page, "pages": pages,
    })


@admin_bp.route("/sales-orders/<int:so_id>", methods=["GET"])
@require_auth
@with_db
def get_sales_order(so_id):
    so = g.db.execute(
        text("SELECT so_id, so_number, so_barcode, customer_name, status, priority, warehouse_id, ship_method, ship_address, order_date, ship_by_date, created_at, picked_at, packed_at, shipped_at, created_by FROM sales_orders WHERE so_id = :sid"),
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


@admin_bp.route("/sales-orders", methods=["POST"])
@require_auth
@require_role("ADMIN", "MANAGER")
@with_db
def create_sales_order():
    data = request.get_json()
    if not data or not data.get("so_number") or not data.get("warehouse_id") or not data.get("lines"):
        return jsonify({"error": "so_number, warehouse_id, and lines are required"}), 400

    dup = g.db.execute(text("SELECT 1 FROM sales_orders WHERE so_number = :sn"), {"sn": data["so_number"]}).fetchone()
    if dup:
        return jsonify({"error": f"Duplicate so_number: {data['so_number']}"}), 400

    for line in data["lines"]:
        item = g.db.execute(text("SELECT 1 FROM items WHERE item_id = :iid"), {"iid": line["item_id"]}).fetchone()
        if not item:
            return jsonify({"error": f"Item {line['item_id']} not found"}), 400

    result = g.db.execute(
        text("""
            INSERT INTO sales_orders (so_number, so_barcode, customer_name, warehouse_id, ship_method, ship_address, ship_by_date, order_date, created_by, status)
            VALUES (:sn, :sb, :cust, :wid, :ship, :addr, :ship_by, NOW(), :created_by, 'OPEN')
            RETURNING so_id
        """),
        {
            "sn": data["so_number"], "sb": data.get("so_barcode", data["so_number"]),
            "cust": data.get("customer_name"), "wid": data["warehouse_id"],
            "ship": data.get("ship_method"), "addr": data.get("ship_address"),
            "ship_by": data.get("ship_by_date"), "created_by": g.current_user["username"],
        },
    )
    so_id = result.fetchone()[0]

    for line in data["lines"]:
        g.db.execute(
            text("INSERT INTO sales_order_lines (so_id, item_id, quantity_ordered, line_number) VALUES (:sid, :iid, :qty, :ln)"),
            {"sid": so_id, "iid": line["item_id"], "qty": line["quantity_ordered"], "ln": line.get("line_number", 1)},
        )

    g.db.commit()

    # Re-fetch to return (save/restore g.db since get_sales_order has @with_db)
    outer_db = g.db
    response = get_sales_order(so_id)
    g.db = outer_db
    return response


@admin_bp.route("/sales-orders/<int:so_id>", methods=["PUT"])
@require_auth
@require_role("ADMIN", "MANAGER")
@with_db
def update_sales_order(so_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    so = g.db.execute(text("SELECT so_id, status FROM sales_orders WHERE so_id = :sid"), {"sid": so_id}).fetchone()
    if not so:
        return jsonify({"error": "Sales order not found"}), 404
    if so.status != "OPEN":
        return jsonify({"error": f"Can only update SOs with OPEN status. Current: {so.status}"}), 400

    fields, params = [], {"sid": so_id}
    for col in ("so_number", "so_barcode", "customer_name", "ship_method", "ship_address", "ship_by_date", "priority"):
        if col in data:
            fields.append(f"{col} = :{col}")
            params[col] = data[col]

    if not fields:
        return jsonify({"error": "No fields to update"}), 400

    g.db.execute(text(f"UPDATE sales_orders SET {', '.join(fields)} WHERE so_id = :sid"), params)
    g.db.commit()

    row = g.db.execute(
        text("SELECT so_id, so_number, so_barcode, customer_name, status, warehouse_id, ship_method, ship_address, created_at FROM sales_orders WHERE so_id = :sid"),
        {"sid": so_id},
    ).fetchone()
    return jsonify({
        "so_id": row.so_id, "so_number": row.so_number, "so_barcode": row.so_barcode,
        "customer_name": row.customer_name, "status": row.status,
        "warehouse_id": row.warehouse_id, "ship_method": row.ship_method,
        "ship_address": row.ship_address, "created_at": row.created_at.isoformat() if row.created_at else None,
    })


@admin_bp.route("/sales-orders/<int:so_id>/cancel", methods=["POST"])
@require_auth
@require_role("ADMIN", "MANAGER")
@with_db
def cancel_sales_order(so_id):
    so = g.db.execute(text("SELECT so_id, status FROM sales_orders WHERE so_id = :sid"), {"sid": so_id}).fetchone()
    if not so:
        return jsonify({"error": "Sales order not found"}), 404
    if so.status not in ("OPEN", "ALLOCATED", "PICKING"):
        return jsonify({"error": f"Can only cancel OPEN, ALLOCATED, or PICKING orders. Current: {so.status}"}), 400

    # If ALLOCATED or PICKING, release allocated inventory
    if so.status in ("ALLOCATED", "PICKING"):
        lines = g.db.execute(
            text("SELECT so_line_id, item_id, quantity_allocated FROM sales_order_lines WHERE so_id = :sid AND quantity_allocated > 0"),
            {"sid": so_id},
        ).fetchall()

        for line in lines:
            # Find the inventory rows that were allocated via pick_tasks
            tasks = g.db.execute(
                text("SELECT bin_id, quantity_to_pick FROM pick_tasks WHERE so_line_id = :sol_id AND status = 'PENDING'"),
                {"sol_id": line.so_line_id},
            ).fetchall()

            for task in tasks:
                g.db.execute(
                    text("UPDATE inventory SET quantity_allocated = quantity_allocated - :qty WHERE item_id = :iid AND bin_id = :bid"),
                    {"qty": task.quantity_to_pick, "iid": line.item_id, "bid": task.bin_id},
                )

            g.db.execute(
                text("UPDATE sales_order_lines SET quantity_allocated = 0 WHERE so_line_id = :sol_id"),
                {"sol_id": line.so_line_id},
            )

        # Clean up pick batch data
        g.db.execute(text("DELETE FROM pick_tasks WHERE so_id = :sid"), {"sid": so_id})
        g.db.execute(text("DELETE FROM pick_batch_orders WHERE so_id = :sid"), {"sid": so_id})

    g.db.execute(text("UPDATE sales_orders SET status = 'CANCELLED' WHERE so_id = :sid"), {"sid": so_id})
    g.db.commit()
    return jsonify({"message": "Sales order cancelled"})


# ── Short Picks Report ────────────────────────────────────────────────────────

@admin_bp.route("/short-picks", methods=["GET"])
@require_auth
@require_role("ADMIN", "MANAGER")
@with_db
def get_short_picks():
    """Return recent short pick events from the audit log."""
    days = request.args.get("days", 30, type=int)
    warehouse_id = request.args.get("warehouse_id", type=int)
    wh_clause = "AND a.warehouse_id = :wid" if warehouse_id else ""
    params = {"days": days}
    if warehouse_id:
        params["wid"] = warehouse_id

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
            WHERE a.action_type = 'PICK'
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
