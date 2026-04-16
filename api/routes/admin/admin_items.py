"""Items, Preferred Bins, CSV Import, and Inventory Overview endpoints."""

import math

from flask import g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_auth, require_role
from middleware.db import with_db
from routes.admin import admin_bp
from schemas.items import CreateItemRequest, CreatePreferredBinRequest, UpdateItemRequest, UpdatePreferredBinRequest
from utils.validation import validate_body


# ── Items ─────────────────────────────────────────────────────────────────────

@admin_bp.route("/items", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def list_items():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 1000)
    category = request.args.get("category")
    active = request.args.get("active")

    search = request.args.get("q", "")

    where_clauses = []
    params = {}
    if category:
        where_clauses.append("i.category = :cat")
        params["cat"] = category
    if active is not None:
        where_clauses.append("i.is_active = :active")
        params["active"] = active.lower() == "true"
    if search:
        where_clauses.append("(i.sku ILIKE :search OR i.item_name ILIKE :search OR i.upc ILIKE :search)")
        params["search"] = f"%{search}%"

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    total = g.db.execute(text(f"SELECT COUNT(*) FROM items i {where_sql}"), params).scalar()
    pages = max(1, math.ceil(total / per_page))

    params["limit"] = per_page
    params["offset"] = (page - 1) * per_page
    rows = g.db.execute(
        text(f"""
            SELECT i.item_id, i.sku, i.item_name, i.upc, i.category, i.weight_lbs,
                   i.default_bin_id, i.is_active, i.created_at,
                   b.bin_code AS default_bin_code
            FROM items i
            LEFT JOIN preferred_bins pb ON pb.item_id = i.item_id AND pb.priority = 1
            LEFT JOIN bins b ON b.bin_id = COALESCE(pb.bin_id, i.default_bin_id)
            {where_sql}
            ORDER BY i.item_id LIMIT :limit OFFSET :offset
        """),
        params,
    ).fetchall()

    return jsonify({
        "items": [
            {"item_id": r.item_id, "sku": r.sku, "item_name": r.item_name, "upc": r.upc,
             "category": r.category, "weight_lbs": float(r.weight_lbs) if r.weight_lbs else None,
             "default_bin_id": r.default_bin_id, "default_bin_code": r.default_bin_code,
             "is_active": r.is_active,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows
        ],
        "total": total, "page": page, "per_page": per_page, "pages": pages,
    })


@admin_bp.route("/items/<int:item_id>", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def get_item(item_id):
    item = g.db.execute(
        text("SELECT item_id, sku, item_name, description, upc, barcode_aliases, category, weight_lbs, length_in, width_in, height_in, default_bin_id, reorder_point, reorder_qty, is_lot_tracked, is_serial_tracked, is_active, created_at, updated_at FROM items WHERE item_id = :iid"),
        {"iid": item_id},
    ).fetchone()
    if not item:
        return jsonify({"error": "Item not found"}), 404

    inv_rows = g.db.execute(
        text("""
            SELECT inv.bin_id, b.bin_code, z.zone_name, inv.quantity_on_hand, inv.quantity_allocated
            FROM inventory inv JOIN bins b ON b.bin_id = inv.bin_id JOIN zones z ON z.zone_id = b.zone_id
            WHERE inv.item_id = :iid
        """),
        {"iid": item_id},
    ).fetchall()

    pref_rows = g.db.execute(
        text("""
            SELECT pb.preferred_bin_id, pb.bin_id, b.bin_code, z.zone_name, pb.priority
            FROM preferred_bins pb JOIN bins b ON b.bin_id = pb.bin_id JOIN zones z ON z.zone_id = b.zone_id
            WHERE pb.item_id = :iid ORDER BY pb.priority
        """),
        {"iid": item_id},
    ).fetchall()

    return jsonify({
        "item": {
            "item_id": item.item_id, "sku": item.sku, "item_name": item.item_name,
            "description": item.description, "upc": item.upc, "barcode_aliases": item.barcode_aliases,
            "category": item.category, "weight_lbs": float(item.weight_lbs) if item.weight_lbs else None,
            "length_in": float(item.length_in) if item.length_in else None,
            "width_in": float(item.width_in) if item.width_in else None,
            "height_in": float(item.height_in) if item.height_in else None,
            "default_bin_id": item.default_bin_id, "reorder_point": item.reorder_point,
            "reorder_qty": item.reorder_qty, "is_lot_tracked": item.is_lot_tracked,
            "is_serial_tracked": item.is_serial_tracked, "is_active": item.is_active,
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        },
        "inventory": [
            {"bin_id": r.bin_id, "bin_code": r.bin_code, "zone_name": r.zone_name,
             "quantity_on_hand": r.quantity_on_hand, "quantity_allocated": r.quantity_allocated}
            for r in inv_rows
        ],
        "preferred_bins": [
            {"preferred_bin_id": r.preferred_bin_id, "bin_id": r.bin_id, "bin_code": r.bin_code,
             "zone_name": r.zone_name, "priority": r.priority}
            for r in pref_rows
        ],
    })


@admin_bp.route("/items", methods=["POST"])
@require_auth
@require_role("ADMIN")
@validate_body(CreateItemRequest)
@with_db
def create_item(validated):
    data = validated.model_dump()

    dup = g.db.execute(text("SELECT 1 FROM items WHERE sku = :sku"), {"sku": data["sku"]}).fetchone()
    if dup:
        return jsonify({"error": f"Duplicate SKU: {data['sku']}"}), 400

    if data.get("upc"):
        dup_upc = g.db.execute(text("SELECT 1 FROM items WHERE upc = :upc"), {"upc": data["upc"]}).fetchone()
        if dup_upc:
            return jsonify({"error": f"Duplicate UPC: {data['upc']}"}), 400

    result = g.db.execute(
        text("""
            INSERT INTO items (sku, item_name, description, upc, category, weight_lbs, default_bin_id)
            VALUES (:sku, :name, :desc, :upc, :cat, :weight, :bin)
            RETURNING item_id, sku, item_name, description, upc, category, weight_lbs, default_bin_id, is_active, created_at
        """),
        {
            "sku": data["sku"], "name": data["item_name"], "desc": data.get("description"),
            "upc": data.get("upc"), "cat": data.get("category"),
            "weight": float(data["weight_lbs"]) if data.get("weight_lbs") is not None else None,
            "bin": data.get("default_bin_id"),
        },
    )
    row = result.fetchone()
    g.db.commit()
    return jsonify({
        "item_id": row.item_id, "sku": row.sku, "item_name": row.item_name,
        "description": row.description, "upc": row.upc, "category": row.category,
        "weight_lbs": float(row.weight_lbs) if row.weight_lbs else None,
        "default_bin_id": row.default_bin_id, "is_active": row.is_active,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }), 201


@admin_bp.route("/items/<int:item_id>", methods=["PUT"])
@require_auth
@require_role("ADMIN")
@validate_body(UpdateItemRequest)
@with_db
def update_item(item_id, validated):
    data = validated.model_dump(exclude_unset=True)

    existing = g.db.execute(text("SELECT item_id FROM items WHERE item_id = :iid"), {"iid": item_id}).fetchone()
    if not existing:
        return jsonify({"error": "Item not found"}), 404

    ALLOWED_FIELDS = {"sku", "item_name", "description", "upc", "category", "weight_lbs", "default_bin_id", "reorder_point", "reorder_qty", "is_active"}
    fields, params = [], {"iid": item_id}
    for col in ALLOWED_FIELDS:
        if col in data:
            fields.append(f"{col} = :{col}")
            params[col] = data[col]

    if not fields:
        return jsonify({"error": "No valid fields provided"}), 400

    fields.append("updated_at = NOW()")
    g.db.execute(text(f"UPDATE items SET {', '.join(fields)} WHERE item_id = :iid"), params)
    g.db.commit()

    row = g.db.execute(
        text("SELECT item_id, sku, item_name, upc, category, weight_lbs, default_bin_id, is_active, created_at, updated_at FROM items WHERE item_id = :iid"),
        {"iid": item_id},
    ).fetchone()
    return jsonify({
        "item_id": row.item_id, "sku": row.sku, "item_name": row.item_name, "upc": row.upc,
        "category": row.category, "weight_lbs": float(row.weight_lbs) if row.weight_lbs else None,
        "default_bin_id": row.default_bin_id, "is_active": row.is_active,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    })


@admin_bp.route("/items/<int:item_id>/archive", methods=["POST"])
@require_auth
@require_role("ADMIN")
@with_db
def archive_item(item_id):
    existing = g.db.execute(text("SELECT item_id, is_active FROM items WHERE item_id = :iid"), {"iid": item_id}).fetchone()
    if not existing:
        return jsonify({"error": "Item not found"}), 404

    new_active = not existing.is_active
    g.db.execute(text("UPDATE items SET is_active = :active, updated_at = NOW() WHERE item_id = :iid"), {"iid": item_id, "active": new_active})
    g.db.commit()
    return jsonify({"message": "Item restored" if new_active else "Item archived", "is_active": new_active})


@admin_bp.route("/items/<int:item_id>", methods=["DELETE"])
@require_auth
@require_role("ADMIN")
@with_db
def delete_item(item_id):
    existing = g.db.execute(text("SELECT item_id FROM items WHERE item_id = :iid"), {"iid": item_id}).fetchone()
    if not existing:
        return jsonify({"error": "Item not found"}), 404

    has_inv = g.db.execute(
        text("SELECT 1 FROM inventory WHERE item_id = :iid AND quantity_on_hand > 0 LIMIT 1"),
        {"iid": item_id},
    ).fetchone()
    if has_inv:
        return jsonify({"error": "Cannot delete item with existing inventory"}), 400

    # Check for references in order lines
    has_orders = g.db.execute(
        text("SELECT 1 FROM sales_order_lines WHERE item_id = :iid LIMIT 1"),
        {"iid": item_id},
    ).fetchone()
    if has_orders:
        return jsonify({"error": "Cannot delete item with order history. Use archive instead."}), 400

    has_po = g.db.execute(
        text("SELECT 1 FROM purchase_order_lines WHERE item_id = :iid LIMIT 1"),
        {"iid": item_id},
    ).fetchone()
    if has_po:
        return jsonify({"error": "Cannot delete item with PO history. Use archive instead."}), 400

    # Safe to hard delete  -  clean up related records first
    g.db.execute(text("DELETE FROM preferred_bins WHERE item_id = :iid"), {"iid": item_id})
    g.db.execute(text("DELETE FROM cycle_count_lines WHERE item_id = :iid"), {"iid": item_id})
    g.db.execute(text("DELETE FROM inventory_adjustments WHERE item_id = :iid"), {"iid": item_id})
    g.db.execute(text("DELETE FROM inventory WHERE item_id = :iid"), {"iid": item_id})
    g.db.execute(text("DELETE FROM items WHERE item_id = :iid"), {"iid": item_id})
    g.db.commit()
    return jsonify({"message": "Item deleted"})


# ── Inventory Overview ────────────────────────────────────────────────────────

@admin_bp.route("/inventory", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def list_inventory():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 1000)

    where_clauses, params = [], {}
    warehouse_id = request.args.get("warehouse_id", type=int)
    item_id = request.args.get("item_id", type=int)
    if warehouse_id:
        where_clauses.append("inv.warehouse_id = :wid")
        params["wid"] = warehouse_id
    if item_id:
        where_clauses.append("inv.item_id = :iid")
        params["iid"] = item_id

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    total = g.db.execute(
        text(f"SELECT COUNT(*) FROM inventory inv {where_sql}"), params
    ).scalar()
    pages = max(1, math.ceil(total / per_page))

    params["limit"] = per_page
    params["offset"] = (page - 1) * per_page
    rows = g.db.execute(
        text(f"""
            SELECT inv.inventory_id, inv.item_id, i.sku, i.item_name, inv.bin_id, b.bin_code, z.zone_name,
                   inv.quantity_on_hand, inv.quantity_allocated,
                   (inv.quantity_on_hand - inv.quantity_allocated) AS quantity_available,
                   COALESCE((
                       SELECT SUM(sol.quantity_ordered - sol.quantity_shipped)
                       FROM sales_order_lines sol
                       JOIN sales_orders so ON so.so_id = sol.so_id
                       WHERE sol.item_id = inv.item_id
                         AND so.status IN ('OPEN', 'PICKING', 'PICKED', 'PACKED')
                         AND sol.quantity_ordered > sol.quantity_shipped
                   ), 0) AS committed_to_orders,
                   inv.lot_number, inv.last_counted_at
            FROM inventory inv
            JOIN items i ON i.item_id = inv.item_id
            JOIN bins b ON b.bin_id = inv.bin_id
            LEFT JOIN zones z ON z.zone_id = b.zone_id
            {where_sql}
            ORDER BY inv.inventory_id LIMIT :limit OFFSET :offset
        """),
        params,
    ).fetchall()

    return jsonify({
        "inventory": [
            {"inventory_id": r.inventory_id, "item_id": r.item_id, "sku": r.sku, "item_name": r.item_name,
             "bin_id": r.bin_id, "bin_code": r.bin_code, "zone_name": r.zone_name,
             "quantity_on_hand": r.quantity_on_hand, "quantity_allocated": r.quantity_allocated,
             "quantity_available": r.quantity_available,
             "committed_to_orders": r.committed_to_orders,
             "lot_number": r.lot_number,
             "last_counted_at": r.last_counted_at.isoformat() if r.last_counted_at else None}
            for r in rows
        ],
        "total": total, "page": page, "per_page": per_page, "pages": pages,
    })


# ── CSV Import ────────────────────────────────────────────────────────────────

@admin_bp.route("/import/<entity_type>", methods=["POST"])
@require_auth
@require_role("ADMIN")
@with_db
def csv_import(entity_type):
    if entity_type not in ("items", "bins", "purchase-orders", "sales-orders"):
        return jsonify({"error": f"Unsupported entity type: {entity_type}"}), 400

    data = request.get_json()
    # Accept either "records" key or entity_type key (e.g., "items")
    records = data.get("records") or data.get(entity_type) or data.get(entity_type.replace("-", "_"))
    if not data or not records:
        return jsonify({"error": "records array is required"}), 400

    if len(records) > 5000:
        return jsonify({"error": "Import limited to 5000 records per file"}), 400

    # Default warehouse_id for PO/SO import (can be overridden per record)
    default_warehouse_id = data.get("warehouse_id")

    imported = 0
    errors = []

    for idx, rec in enumerate(records, 1):
        try:
            if entity_type == "items":
                _import_item(g.db, rec, idx, errors)
            elif entity_type == "bins":
                _import_bin(g.db, rec, idx, errors)
            elif entity_type == "purchase-orders":
                _import_purchase_order(g.db, rec, idx, errors, default_warehouse_id)
            elif entity_type == "sales-orders":
                _import_sales_order(g.db, rec, idx, errors, default_warehouse_id)
            imported += 1
        except _SkipRow as e:
            errors.append({"row": idx, "error": str(e)})

    g.db.commit()
    return jsonify({
        "message": "Import complete",
        "total": len(records),
        "imported": imported,
        "skipped": len(errors),
        "errors": errors,
    })


class _SkipRow(Exception):
    pass


def _import_item(db, rec, idx, errors):
    sku = rec.get("sku")
    # Accept "name" or "item_name"
    name = rec.get("item_name") or rec.get("name")
    if not sku:
        raise _SkipRow("Missing required field: sku")
    if not name:
        raise _SkipRow("Missing required field: name")

    dup = db.execute(text("SELECT 1 FROM items WHERE sku = :sku"), {"sku": sku}).fetchone()
    if dup:
        raise _SkipRow(f"Duplicate SKU: {sku}")

    upc = rec.get("upc")
    if upc:
        dup_upc = db.execute(text("SELECT 1 FROM items WHERE upc = :upc"), {"upc": upc}).fetchone()
        if dup_upc:
            raise _SkipRow(f"Duplicate UPC: {upc}")

    # Resolve default_bin by code if provided
    default_bin_id = None
    if rec.get("default_bin"):
        bin_row = db.execute(text("SELECT bin_id FROM bins WHERE bin_code = :code"), {"code": rec["default_bin"]}).fetchone()
        if bin_row:
            default_bin_id = bin_row.bin_id

    result = db.execute(
        text("INSERT INTO items (sku, item_name, description, upc, category, weight_lbs, default_bin_id) VALUES (:sku, :name, :desc, :upc, :cat, :weight, :bin) RETURNING item_id"),
        {"sku": sku, "name": name, "desc": rec.get("description"),
         "upc": upc, "cat": rec.get("category"),
         "weight": rec.get("weight_lbs") or rec.get("weight") or None,
         "bin": default_bin_id},
    )

    # If quantity provided, create inventory in default bin
    qty = rec.get("quantity") or rec.get("qty")
    if qty and default_bin_id:
        item_id = result.fetchone()[0]
        qty_int = int(qty)
        if qty_int > 0:
            # Get warehouse_id from the bin
            wh_row = db.execute(text("SELECT warehouse_id FROM bins WHERE bin_id = :bid"), {"bid": default_bin_id}).fetchone()
            wh_id = wh_row.warehouse_id if wh_row else 1
            db.execute(
                text("INSERT INTO inventory (item_id, bin_id, warehouse_id, quantity_on_hand) VALUES (:iid, :bid, :wid, :qty)"),
                {"iid": item_id, "bid": default_bin_id, "wid": wh_id, "qty": qty_int},
            )


def _import_bin(db, rec, idx, errors):
    bin_code = rec.get("bin_code")
    if not bin_code:
        raise _SkipRow("Missing required field: bin_code")

    # Resolve zone by name or code if zone_id not provided
    zone_id = rec.get("zone_id")
    zone_value = rec.get("zone", "").strip()
    if not zone_id and zone_value:
        zone_row = db.execute(
            text("SELECT zone_id FROM zones WHERE LOWER(zone_code) = LOWER(:z) OR LOWER(zone_name) = LOWER(:z) LIMIT 1"),
            {"z": zone_value},
        ).fetchone()
        if zone_row:
            zone_id = zone_row.zone_id
    if not zone_id:
        if zone_value:
            raise _SkipRow(f"Zone '{zone_value}' not found. Create the zone first, then import bins.")
        raise _SkipRow("Missing required field: zone (or zone_id)")

    # Get warehouse_id from zone if not provided
    warehouse_id = rec.get("warehouse_id")
    if not warehouse_id:
        wh_row = db.execute(text("SELECT warehouse_id FROM zones WHERE zone_id = :zid"), {"zid": zone_id}).fetchone()
        warehouse_id = wh_row.warehouse_id if wh_row else 1

    bin_type = rec.get("bin_type", "Pickable")
    bin_barcode = rec.get("bin_barcode") or bin_code

    dup = db.execute(
        text("SELECT 1 FROM bins WHERE warehouse_id = :wid AND bin_code = :code"),
        {"wid": warehouse_id, "code": bin_code},
    ).fetchone()
    if dup:
        raise _SkipRow(f"Duplicate bin_code: {bin_code}")

    db.execute(
        text("""
            INSERT INTO bins (zone_id, warehouse_id, bin_code, bin_barcode, bin_type, aisle, row_num, level_num, pick_sequence, putaway_sequence, description)
            VALUES (:zid, :wid, :code, :barcode, :type, :aisle, :row, :level, :pick_seq, :put_seq, :desc)
        """),
        {
            "zid": zone_id, "wid": warehouse_id, "code": bin_code,
            "barcode": bin_barcode, "type": bin_type,
            "aisle": rec.get("aisle"), "row": rec.get("row_num"), "level": rec.get("level_num"),
            "pick_seq": rec.get("pick_sequence", 0), "put_seq": rec.get("putaway_sequence", 0),
            "desc": rec.get("description"),
        },
    )


def _import_purchase_order(db, rec, idx, errors, default_warehouse_id=None):
    po_number = rec.get("po_number")
    sku = rec.get("sku")
    if not po_number:
        raise _SkipRow("Missing required field: po_number")
    if not sku:
        raise _SkipRow("Missing required field: sku")

    quantity = int(rec.get("quantity") or rec.get("quantity_expected") or 0)
    if quantity <= 0:
        raise _SkipRow("quantity must be > 0")

    warehouse_id = rec.get("warehouse_id") or default_warehouse_id
    if not warehouse_id:
        raise _SkipRow("Missing required field: warehouse_id")

    # Find item by SKU
    item_row = db.execute(text("SELECT item_id FROM items WHERE sku = :sku"), {"sku": sku}).fetchone()
    if not item_row:
        raise _SkipRow(f"Item not found: {sku}")

    # Find or create PO
    po_row = db.execute(text("SELECT po_id FROM purchase_orders WHERE po_number = :pn"), {"pn": po_number}).fetchone()
    if not po_row:
        result = db.execute(
            text("""
                INSERT INTO purchase_orders (po_number, po_barcode, vendor_name, expected_date, warehouse_id, status)
                VALUES (:pn, :pn, :vendor, :exp_date, :wid, 'OPEN')
                RETURNING po_id
            """),
            {"pn": po_number, "vendor": rec.get("vendor"), "exp_date": rec.get("expected_date") or None, "wid": warehouse_id},
        )
        po_id = result.fetchone()[0]
    else:
        po_id = po_row.po_id

    # Get next line number
    max_ln = db.execute(text("SELECT COALESCE(MAX(line_number), 0) FROM purchase_order_lines WHERE po_id = :pid"), {"pid": po_id}).scalar()

    db.execute(
        text("INSERT INTO purchase_order_lines (po_id, item_id, quantity_ordered, line_number) VALUES (:pid, :iid, :qty, :ln)"),
        {"pid": po_id, "iid": item_row.item_id, "qty": quantity, "ln": max_ln + 1},
    )


def _import_sales_order(db, rec, idx, errors, default_warehouse_id=None):
    so_number = rec.get("so_number")
    sku = rec.get("sku")
    if not so_number:
        raise _SkipRow("Missing required field: so_number")
    if not sku:
        raise _SkipRow("Missing required field: sku")

    quantity = int(rec.get("quantity") or rec.get("quantity_ordered") or 0)
    if quantity <= 0:
        raise _SkipRow("quantity must be > 0")

    warehouse_id = rec.get("warehouse_id") or default_warehouse_id
    if not warehouse_id:
        raise _SkipRow("Missing required field: warehouse_id")

    # Find item by SKU
    item_row = db.execute(text("SELECT item_id FROM items WHERE sku = :sku"), {"sku": sku}).fetchone()
    if not item_row:
        raise _SkipRow(f"Item not found: {sku}")

    # Find or create SO
    so_row = db.execute(text("SELECT so_id FROM sales_orders WHERE so_number = :sn"), {"sn": so_number}).fetchone()
    if not so_row:
        result = db.execute(
            text("""
                INSERT INTO sales_orders (so_number, so_barcode, customer_name, customer_phone, customer_address, warehouse_id, order_date, status)
                VALUES (:sn, :sn, :cust, :phone, :caddr, :wid, NOW(), 'OPEN')
                RETURNING so_id
            """),
            {"sn": so_number, "cust": rec.get("customer"), "phone": rec.get("customer_phone"), "caddr": rec.get("customer_address"), "wid": warehouse_id},
        )
        so_id = result.fetchone()[0]
    else:
        so_id = so_row.so_id

    max_ln = db.execute(text("SELECT COALESCE(MAX(line_number), 0) FROM sales_order_lines WHERE so_id = :sid"), {"sid": so_id}).scalar()

    db.execute(
        text("INSERT INTO sales_order_lines (so_id, item_id, quantity_ordered, line_number) VALUES (:sid, :iid, :qty, :ln)"),
        {"sid": so_id, "iid": item_row.item_id, "qty": quantity, "ln": max_ln + 1},
    )


# ── Preferred Bins ────────────────────────────────────────────────────────────

@admin_bp.route("/preferred-bins", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def list_preferred_bins():
    item_id = request.args.get("item_id", type=int)
    bin_id = request.args.get("bin_id", type=int)
    search = request.args.get("q", "")

    where_clauses = []
    params = {}
    if item_id:
        where_clauses.append("pb.item_id = :item_id")
        params["item_id"] = item_id
    if bin_id:
        where_clauses.append("pb.bin_id = :bin_id")
        params["bin_id"] = bin_id
    if search:
        where_clauses.append("(i.sku ILIKE :search OR i.item_name ILIKE :search)")
        params["search"] = f"%{search}%"

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    rows = g.db.execute(
        text(f"""
            SELECT pb.preferred_bin_id, pb.item_id, pb.bin_id, pb.priority, pb.notes,
                   pb.updated_at,
                   i.sku, i.item_name,
                   b.bin_code, z.zone_name
            FROM preferred_bins pb
            JOIN items i ON i.item_id = pb.item_id
            JOIN bins b ON b.bin_id = pb.bin_id
            LEFT JOIN zones z ON z.zone_id = b.zone_id
            {where_sql}
            ORDER BY i.sku, pb.priority
        """),
        params,
    ).fetchall()

    return jsonify({
        "preferred_bins": [
            {
                "preferred_bin_id": r.preferred_bin_id,
                "item_id": r.item_id,
                "bin_id": r.bin_id,
                "priority": r.priority,
                "notes": r.notes,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                "sku": r.sku,
                "item_name": r.item_name,
                "bin_code": r.bin_code,
                "zone_name": r.zone_name,
            }
            for r in rows
        ]
    })


@admin_bp.route("/preferred-bins", methods=["POST"])
@require_auth
@require_role("ADMIN")
@validate_body(CreatePreferredBinRequest)
@with_db
def create_preferred_bin(validated):
    item_id = validated.item_id
    bin_id = validated.bin_id
    priority = validated.priority

    g.db.execute(
        text(
            """
            INSERT INTO preferred_bins (item_id, bin_id, priority)
            VALUES (:item_id, :bin_id, :priority)
            ON CONFLICT (item_id, bin_id) DO UPDATE SET priority = :priority, updated_at = NOW()
            """
        ),
        {"item_id": item_id, "bin_id": bin_id, "priority": priority},
    )
    g.db.commit()
    return jsonify({"message": "Preferred bin saved"})


@admin_bp.route("/preferred-bins/<int:preferred_bin_id>", methods=["PUT"])
@require_auth
@require_role("ADMIN")
@validate_body(UpdatePreferredBinRequest)
@with_db
def update_preferred_bin(preferred_bin_id, validated):
    priority = validated.priority

    g.db.execute(
        text("UPDATE preferred_bins SET priority = :priority, updated_at = NOW() WHERE preferred_bin_id = :pbid"),
        {"priority": priority, "pbid": preferred_bin_id},
    )
    g.db.commit()
    return jsonify({"message": "Priority updated"})


@admin_bp.route("/preferred-bins/<int:preferred_bin_id>", methods=["DELETE"])
@require_auth
@require_role("ADMIN")
@with_db
def delete_preferred_bin(preferred_bin_id):
    g.db.execute(
        text("DELETE FROM preferred_bins WHERE preferred_bin_id = :pbid"),
        {"pbid": preferred_bin_id},
    )
    g.db.commit()
    return jsonify({"message": "Preferred bin deleted"})
