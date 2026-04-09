"""Items, Preferred Bins, CSV Import, and Inventory Overview endpoints."""

import math

from flask import g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_auth, require_role
from middleware.db import with_db
from routes.admin import admin_bp


# ── Items ─────────────────────────────────────────────────────────────────────

@admin_bp.route("/items", methods=["GET"])
@require_auth
@with_db
def list_items():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    category = request.args.get("category")
    active = request.args.get("active")

    where_clauses = []
    params = {}
    if category:
        where_clauses.append("category = :cat")
        params["cat"] = category
    if active is not None:
        where_clauses.append("is_active = :active")
        params["active"] = active.lower() == "true"

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    total = g.db.execute(text(f"SELECT COUNT(*) FROM items {where_sql}"), params).scalar()
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
            {where_sql.replace("category", "i.category").replace("is_active", "i.is_active")}
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
@require_role("ADMIN", "MANAGER")
@with_db
def create_item():
    data = request.get_json()
    if not data or not data.get("sku") or not data.get("item_name"):
        return jsonify({"error": "sku and item_name are required"}), 400

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
            "weight": data.get("weight_lbs"), "bin": data.get("default_bin_id"),
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
@require_role("ADMIN", "MANAGER")
@with_db
def update_item(item_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    existing = g.db.execute(text("SELECT item_id FROM items WHERE item_id = :iid"), {"iid": item_id}).fetchone()
    if not existing:
        return jsonify({"error": "Item not found"}), 404

    fields, params = [], {"iid": item_id}
    for col in ("sku", "item_name", "description", "upc", "category", "weight_lbs", "default_bin_id", "reorder_point", "reorder_qty", "is_active"):
        if col in data:
            fields.append(f"{col} = :{col}")
            params[col] = data[col]

    if not fields:
        return jsonify({"error": "No fields to update"}), 400

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


@admin_bp.route("/items/<int:item_id>", methods=["DELETE"])
@require_auth
@require_role("ADMIN", "MANAGER")
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
        return jsonify({"error": "Cannot deactivate item with existing inventory"}), 400

    g.db.execute(text("UPDATE items SET is_active = FALSE, updated_at = NOW() WHERE item_id = :iid"), {"iid": item_id})
    g.db.commit()
    return jsonify({"message": "Item deactivated"})


# ── Inventory Overview ────────────────────────────────────────────────────────

@admin_bp.route("/inventory", methods=["GET"])
@require_auth
@with_db
def list_inventory():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)

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
                   inv.lot_number, inv.last_counted_at
            FROM inventory inv
            JOIN items i ON i.item_id = inv.item_id
            JOIN bins b ON b.bin_id = inv.bin_id
            JOIN zones z ON z.zone_id = b.zone_id
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
             "quantity_available": r.quantity_available, "lot_number": r.lot_number,
             "last_counted_at": r.last_counted_at.isoformat() if r.last_counted_at else None}
            for r in rows
        ],
        "total": total, "page": page, "per_page": per_page, "pages": pages,
    })


# ── CSV Import ────────────────────────────────────────────────────────────────

@admin_bp.route("/import/<entity_type>", methods=["POST"])
@require_auth
@require_role("ADMIN", "MANAGER")
@with_db
def csv_import(entity_type):
    if entity_type not in ("items", "bins", "purchase-orders", "sales-orders"):
        return jsonify({"error": f"Unsupported entity type: {entity_type}"}), 400

    data = request.get_json()
    if not data or not data.get("records"):
        return jsonify({"error": "records array is required"}), 400

    records = data["records"]
    imported = 0
    errors = []

    for idx, rec in enumerate(records, 1):
        try:
            if entity_type == "items":
                _import_item(g.db, rec, idx, errors)
            elif entity_type == "bins":
                _import_bin(g.db, rec, idx, errors)
            else:
                errors.append({"row": idx, "error": f"Import for {entity_type} not yet supported"})
                continue
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
    if not rec.get("sku"):
        raise _SkipRow("Missing required field: sku")
    if not rec.get("item_name"):
        raise _SkipRow("Missing required field: item_name")

    dup = db.execute(text("SELECT 1 FROM items WHERE sku = :sku"), {"sku": rec["sku"]}).fetchone()
    if dup:
        raise _SkipRow(f"Duplicate SKU: {rec['sku']}")

    if rec.get("upc"):
        dup_upc = db.execute(text("SELECT 1 FROM items WHERE upc = :upc"), {"upc": rec["upc"]}).fetchone()
        if dup_upc:
            raise _SkipRow(f"Duplicate UPC: {rec['upc']}")

    db.execute(
        text("INSERT INTO items (sku, item_name, upc, category, weight_lbs) VALUES (:sku, :name, :upc, :cat, :weight)"),
        {"sku": rec["sku"], "name": rec["item_name"], "upc": rec.get("upc"),
         "cat": rec.get("category"), "weight": rec.get("weight_lbs")},
    )


def _import_bin(db, rec, idx, errors):
    if not rec.get("bin_code"):
        raise _SkipRow("Missing required field: bin_code")
    if not rec.get("bin_barcode"):
        raise _SkipRow("Missing required field: bin_barcode")
    if not rec.get("bin_type"):
        raise _SkipRow("Missing required field: bin_type")
    if not rec.get("zone_id"):
        raise _SkipRow("Missing required field: zone_id")
    if not rec.get("warehouse_id"):
        raise _SkipRow("Missing required field: warehouse_id")

    dup = db.execute(
        text("SELECT 1 FROM bins WHERE warehouse_id = :wid AND bin_code = :code"),
        {"wid": rec["warehouse_id"], "code": rec["bin_code"]},
    ).fetchone()
    if dup:
        raise _SkipRow(f"Duplicate bin_code: {rec['bin_code']}")

    db.execute(
        text("""
            INSERT INTO bins (zone_id, warehouse_id, bin_code, bin_barcode, bin_type, aisle, row_num, level_num, pick_sequence, putaway_sequence)
            VALUES (:zid, :wid, :code, :barcode, :type, :aisle, :row, :level, :pick_seq, :put_seq)
        """),
        {
            "zid": rec["zone_id"], "wid": rec["warehouse_id"], "code": rec["bin_code"],
            "barcode": rec["bin_barcode"], "type": rec["bin_type"],
            "aisle": rec.get("aisle"), "row": rec.get("row_num"), "level": rec.get("level_num"),
            "pick_seq": rec.get("pick_sequence", 0), "put_seq": rec.get("putaway_sequence", 0),
        },
    )


# ── Preferred Bins ────────────────────────────────────────────────────────────

@admin_bp.route("/preferred-bins", methods=["GET"])
@require_auth
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
@with_db
def create_preferred_bin():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    item_id = data.get("item_id")
    bin_id = data.get("bin_id")
    priority = data.get("priority", 1)

    if not item_id or not bin_id:
        return jsonify({"error": "item_id and bin_id are required"}), 400

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
@with_db
def update_preferred_bin(preferred_bin_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    priority = data.get("priority")
    if priority is None:
        return jsonify({"error": "priority is required"}), 400

    g.db.execute(
        text("UPDATE preferred_bins SET priority = :priority, updated_at = NOW() WHERE preferred_bin_id = :pbid"),
        {"priority": priority, "pbid": preferred_bin_id},
    )
    g.db.commit()
    return jsonify({"message": "Priority updated"})


@admin_bp.route("/preferred-bins/<int:preferred_bin_id>", methods=["DELETE"])
@require_auth
@with_db
def delete_preferred_bin(preferred_bin_id):
    g.db.execute(
        text("DELETE FROM preferred_bins WHERE preferred_bin_id = :pbid"),
        {"pbid": preferred_bin_id},
    )
    g.db.commit()
    return jsonify({"message": "Preferred bin deleted"})
