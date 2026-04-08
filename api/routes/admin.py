"""
Admin CRUD endpoints for the web admin panel.
Covers warehouses, zones, bins, items, POs, SOs, users, audit log,
inventory overview, CSV import, and dashboard stats.
"""

import math
from datetime import datetime, timezone

import bcrypt
from flask import Blueprint, g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_auth, require_role
from models.database import get_db

admin_bp = Blueprint("admin", __name__)

VALID_ZONE_TYPES = ("RECEIVING", "STORAGE", "PICKING", "STAGING", "SHIPPING")
VALID_BIN_TYPES = ("Staging", "PickableStaging", "Pickable")
VALID_ROLES = ("ADMIN", "MANAGER", "PICKER", "RECEIVER", "PACKER")


def _paginate(query_base, count_base, params, page, per_page):
    """Add pagination to a query. Returns (rows, total, pages)."""
    total = None
    pages = 1
    offset = (page - 1) * per_page
    params["limit"] = per_page
    params["offset"] = offset
    return per_page, offset


# ── Warehouses ────────────────────────────────────────────────────────────────

@admin_bp.route("/warehouses", methods=["GET"])
@require_auth
def list_warehouses():
    db = next(get_db())
    try:
        rows = db.execute(
            text("SELECT warehouse_id, warehouse_code, warehouse_name, address, is_active, created_at FROM warehouses ORDER BY warehouse_id")
        ).fetchall()
        return jsonify({
            "warehouses": [
                {"warehouse_id": r.warehouse_id, "warehouse_code": r.warehouse_code, "warehouse_name": r.warehouse_name,
                 "address": r.address, "is_active": r.is_active, "created_at": r.created_at.isoformat() if r.created_at else None}
                for r in rows
            ]
        })
    finally:
        db.close()


@admin_bp.route("/warehouses/<int:warehouse_id>", methods=["GET"])
@require_auth
def get_warehouse(warehouse_id):
    db = next(get_db())
    try:
        wh = db.execute(
            text("SELECT warehouse_id, warehouse_code, warehouse_name, address, is_active, created_at FROM warehouses WHERE warehouse_id = :wid"),
            {"wid": warehouse_id},
        ).fetchone()
        if not wh:
            return jsonify({"error": "Warehouse not found"}), 404

        zones = db.execute(
            text("SELECT zone_id, warehouse_id, zone_code, zone_name, zone_type, is_active FROM zones WHERE warehouse_id = :wid ORDER BY zone_id"),
            {"wid": warehouse_id},
        ).fetchall()

        return jsonify({
            "warehouse": {"warehouse_id": wh.warehouse_id, "warehouse_code": wh.warehouse_code, "warehouse_name": wh.warehouse_name,
                          "address": wh.address, "is_active": wh.is_active, "created_at": wh.created_at.isoformat() if wh.created_at else None},
            "zones": [{"zone_id": z.zone_id, "warehouse_id": z.warehouse_id, "zone_code": z.zone_code,
                        "zone_name": z.zone_name, "zone_type": z.zone_type, "is_active": z.is_active} for z in zones],
        })
    finally:
        db.close()


@admin_bp.route("/warehouses", methods=["POST"])
@require_auth
@require_role("ADMIN", "MANAGER")
def create_warehouse():
    data = request.get_json()
    if not data or not data.get("warehouse_code") or not data.get("warehouse_name"):
        return jsonify({"error": "warehouse_code and warehouse_name are required"}), 400

    db = next(get_db())
    try:
        dup = db.execute(text("SELECT 1 FROM warehouses WHERE warehouse_code = :c"), {"c": data["warehouse_code"]}).fetchone()
        if dup:
            return jsonify({"error": f"Duplicate warehouse_code: {data['warehouse_code']}"}), 400

        result = db.execute(
            text("INSERT INTO warehouses (warehouse_code, warehouse_name, address) VALUES (:code, :name, :addr) RETURNING warehouse_id, warehouse_code, warehouse_name, address, is_active, created_at"),
            {"code": data["warehouse_code"], "name": data["warehouse_name"], "addr": data.get("address")},
        )
        row = result.fetchone()
        db.commit()
        return jsonify({
            "warehouse_id": row.warehouse_id, "warehouse_code": row.warehouse_code, "warehouse_name": row.warehouse_name,
            "address": row.address, "is_active": row.is_active, "created_at": row.created_at.isoformat() if row.created_at else None,
        }), 201
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@admin_bp.route("/warehouses/<int:warehouse_id>", methods=["PUT"])
@require_auth
@require_role("ADMIN", "MANAGER")
def update_warehouse(warehouse_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    db = next(get_db())
    try:
        wh = db.execute(text("SELECT warehouse_id FROM warehouses WHERE warehouse_id = :wid"), {"wid": warehouse_id}).fetchone()
        if not wh:
            return jsonify({"error": "Warehouse not found"}), 404

        fields, params = [], {"wid": warehouse_id}
        for col in ("warehouse_code", "warehouse_name", "address", "is_active"):
            if col in data:
                fields.append(f"{col} = :{col}")
                params[col] = data[col]

        if not fields:
            return jsonify({"error": "No fields to update"}), 400

        db.execute(text(f"UPDATE warehouses SET {', '.join(fields)} WHERE warehouse_id = :wid"), params)
        db.commit()

        row = db.execute(
            text("SELECT warehouse_id, warehouse_code, warehouse_name, address, is_active, created_at FROM warehouses WHERE warehouse_id = :wid"),
            {"wid": warehouse_id},
        ).fetchone()
        return jsonify({
            "warehouse_id": row.warehouse_id, "warehouse_code": row.warehouse_code, "warehouse_name": row.warehouse_name,
            "address": row.address, "is_active": row.is_active, "created_at": row.created_at.isoformat() if row.created_at else None,
        })
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ── Zones ─────────────────────────────────────────────────────────────────────

@admin_bp.route("/zones", methods=["GET"])
@require_auth
def list_zones():
    db = next(get_db())
    try:
        warehouse_id = request.args.get("warehouse_id", type=int)
        if warehouse_id:
            rows = db.execute(
                text("SELECT zone_id, warehouse_id, zone_code, zone_name, zone_type, is_active FROM zones WHERE warehouse_id = :wid ORDER BY zone_id"),
                {"wid": warehouse_id},
            ).fetchall()
        else:
            rows = db.execute(text("SELECT zone_id, warehouse_id, zone_code, zone_name, zone_type, is_active FROM zones ORDER BY zone_id")).fetchall()

        return jsonify({
            "zones": [{"zone_id": z.zone_id, "warehouse_id": z.warehouse_id, "zone_code": z.zone_code,
                        "zone_name": z.zone_name, "zone_type": z.zone_type, "is_active": z.is_active} for z in rows]
        })
    finally:
        db.close()


@admin_bp.route("/zones", methods=["POST"])
@require_auth
@require_role("ADMIN", "MANAGER")
def create_zone():
    data = request.get_json()
    if not data or not data.get("warehouse_id") or not data.get("zone_code") or not data.get("zone_name") or not data.get("zone_type"):
        return jsonify({"error": "warehouse_id, zone_code, zone_name, and zone_type are required"}), 400

    if data["zone_type"] not in VALID_ZONE_TYPES:
        return jsonify({"error": f"zone_type must be one of: {', '.join(VALID_ZONE_TYPES)}"}), 400

    db = next(get_db())
    try:
        dup = db.execute(
            text("SELECT 1 FROM zones WHERE warehouse_id = :wid AND zone_code = :code"),
            {"wid": data["warehouse_id"], "code": data["zone_code"]},
        ).fetchone()
        if dup:
            return jsonify({"error": f"Duplicate zone_code '{data['zone_code']}' in warehouse {data['warehouse_id']}"}), 400

        result = db.execute(
            text("INSERT INTO zones (warehouse_id, zone_code, zone_name, zone_type) VALUES (:wid, :code, :name, :type) RETURNING zone_id, warehouse_id, zone_code, zone_name, zone_type, is_active"),
            {"wid": data["warehouse_id"], "code": data["zone_code"], "name": data["zone_name"], "type": data["zone_type"]},
        )
        row = result.fetchone()
        db.commit()
        return jsonify({"zone_id": row.zone_id, "warehouse_id": row.warehouse_id, "zone_code": row.zone_code,
                        "zone_name": row.zone_name, "zone_type": row.zone_type, "is_active": row.is_active}), 201
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@admin_bp.route("/zones/<int:zone_id>", methods=["PUT"])
@require_auth
@require_role("ADMIN", "MANAGER")
def update_zone(zone_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    if "zone_type" in data and data["zone_type"] not in VALID_ZONE_TYPES:
        return jsonify({"error": f"zone_type must be one of: {', '.join(VALID_ZONE_TYPES)}"}), 400

    db = next(get_db())
    try:
        existing = db.execute(text("SELECT zone_id FROM zones WHERE zone_id = :zid"), {"zid": zone_id}).fetchone()
        if not existing:
            return jsonify({"error": "Zone not found"}), 404

        fields, params = [], {"zid": zone_id}
        for col in ("zone_code", "zone_name", "zone_type", "is_active"):
            if col in data:
                fields.append(f"{col} = :{col}")
                params[col] = data[col]

        if not fields:
            return jsonify({"error": "No fields to update"}), 400

        db.execute(text(f"UPDATE zones SET {', '.join(fields)} WHERE zone_id = :zid"), params)
        db.commit()

        row = db.execute(
            text("SELECT zone_id, warehouse_id, zone_code, zone_name, zone_type, is_active FROM zones WHERE zone_id = :zid"),
            {"zid": zone_id},
        ).fetchone()
        return jsonify({"zone_id": row.zone_id, "warehouse_id": row.warehouse_id, "zone_code": row.zone_code,
                        "zone_name": row.zone_name, "zone_type": row.zone_type, "is_active": row.is_active})
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ── Bins ──────────────────────────────────────────────────────────────────────

@admin_bp.route("/bins", methods=["GET"])
@require_auth
def list_bins():
    db = next(get_db())
    try:
        where_clauses = []
        params = {}
        warehouse_id = request.args.get("warehouse_id", type=int)
        zone_id = request.args.get("zone_id", type=int)
        if warehouse_id:
            where_clauses.append("b.warehouse_id = :wid")
            params["wid"] = warehouse_id
        if zone_id:
            where_clauses.append("b.zone_id = :zid")
            params["zid"] = zone_id

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        rows = db.execute(
            text(f"""
                SELECT b.bin_id, b.zone_id, z.zone_name, b.warehouse_id, b.bin_code, b.bin_barcode, b.bin_type,
                       b.aisle, b.row_num, b.level_num, b.position_num, b.pick_sequence, b.putaway_sequence, b.is_active
                FROM bins b
                JOIN zones z ON z.zone_id = b.zone_id
                {where_sql}
                ORDER BY b.bin_id
            """),
            params,
        ).fetchall()

        return jsonify({
            "bins": [
                {"bin_id": r.bin_id, "zone_id": r.zone_id, "zone_name": r.zone_name, "warehouse_id": r.warehouse_id,
                 "bin_code": r.bin_code, "bin_barcode": r.bin_barcode, "bin_type": r.bin_type,
                 "aisle": r.aisle, "row_num": r.row_num, "level_num": r.level_num, "position_num": r.position_num,
                 "pick_sequence": r.pick_sequence, "putaway_sequence": r.putaway_sequence, "is_active": r.is_active}
                for r in rows
            ]
        })
    finally:
        db.close()


@admin_bp.route("/bins/<int:bin_id>", methods=["GET"])
@require_auth
def get_bin(bin_id):
    db = next(get_db())
    try:
        b = db.execute(
            text("""
                SELECT b.bin_id, b.zone_id, z.zone_name, b.warehouse_id, b.bin_code, b.bin_barcode, b.bin_type,
                       b.aisle, b.row_num, b.level_num, b.position_num, b.pick_sequence, b.putaway_sequence, b.is_active
                FROM bins b JOIN zones z ON z.zone_id = b.zone_id
                WHERE b.bin_id = :bid
            """),
            {"bid": bin_id},
        ).fetchone()
        if not b:
            return jsonify({"error": "Bin not found"}), 404

        inv_rows = db.execute(
            text("""
                SELECT inv.item_id, i.sku, i.item_name, inv.quantity_on_hand, inv.quantity_allocated
                FROM inventory inv JOIN items i ON i.item_id = inv.item_id
                WHERE inv.bin_id = :bid
            """),
            {"bid": bin_id},
        ).fetchall()

        return jsonify({
            "bin": {"bin_id": b.bin_id, "zone_id": b.zone_id, "zone_name": b.zone_name, "warehouse_id": b.warehouse_id,
                    "bin_code": b.bin_code, "bin_barcode": b.bin_barcode, "bin_type": b.bin_type,
                    "aisle": b.aisle, "row_num": b.row_num, "level_num": b.level_num, "position_num": b.position_num,
                    "pick_sequence": b.pick_sequence, "putaway_sequence": b.putaway_sequence, "is_active": b.is_active},
            "inventory": [{"item_id": r.item_id, "sku": r.sku, "item_name": r.item_name,
                           "quantity_on_hand": r.quantity_on_hand, "quantity_allocated": r.quantity_allocated} for r in inv_rows],
        })
    finally:
        db.close()


@admin_bp.route("/bins", methods=["POST"])
@require_auth
@require_role("ADMIN", "MANAGER")
def create_bin():
    data = request.get_json()
    if not data or not data.get("zone_id") or not data.get("warehouse_id") or not data.get("bin_code") or not data.get("bin_barcode") or not data.get("bin_type"):
        return jsonify({"error": "zone_id, warehouse_id, bin_code, bin_barcode, and bin_type are required"}), 400

    if data["bin_type"] not in VALID_BIN_TYPES:
        return jsonify({"error": f"bin_type must be one of: {', '.join(VALID_BIN_TYPES)}"}), 400

    db = next(get_db())
    try:
        dup = db.execute(
            text("SELECT 1 FROM bins WHERE warehouse_id = :wid AND bin_code = :code"),
            {"wid": data["warehouse_id"], "code": data["bin_code"]},
        ).fetchone()
        if dup:
            return jsonify({"error": f"Duplicate bin_code '{data['bin_code']}' in warehouse {data['warehouse_id']}"}), 400

        result = db.execute(
            text("""
                INSERT INTO bins (zone_id, warehouse_id, bin_code, bin_barcode, bin_type, aisle, row_num, level_num, position_num, pick_sequence, putaway_sequence)
                VALUES (:zone_id, :wid, :code, :barcode, :type, :aisle, :row, :level, :pos, :pick_seq, :put_seq)
                RETURNING bin_id, zone_id, warehouse_id, bin_code, bin_barcode, bin_type, aisle, row_num, level_num, position_num, pick_sequence, putaway_sequence, is_active
            """),
            {
                "zone_id": data["zone_id"], "wid": data["warehouse_id"], "code": data["bin_code"],
                "barcode": data["bin_barcode"], "type": data["bin_type"],
                "aisle": data.get("aisle"), "row": data.get("row_num"), "level": data.get("level_num"),
                "pos": data.get("position_num"), "pick_seq": data.get("pick_sequence", 0), "put_seq": data.get("putaway_sequence", 0),
            },
        )
        row = result.fetchone()
        db.commit()
        return jsonify({
            "bin_id": row.bin_id, "zone_id": row.zone_id, "warehouse_id": row.warehouse_id,
            "bin_code": row.bin_code, "bin_barcode": row.bin_barcode, "bin_type": row.bin_type,
            "aisle": row.aisle, "row_num": row.row_num, "level_num": row.level_num,
            "position_num": row.position_num, "pick_sequence": row.pick_sequence,
            "putaway_sequence": row.putaway_sequence, "is_active": row.is_active,
        }), 201
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@admin_bp.route("/bins/<int:bin_id>", methods=["PUT"])
@require_auth
@require_role("ADMIN", "MANAGER")
def update_bin(bin_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    if "bin_type" in data and data["bin_type"] not in VALID_BIN_TYPES:
        return jsonify({"error": f"bin_type must be one of: {', '.join(VALID_BIN_TYPES)}"}), 400

    db = next(get_db())
    try:
        existing = db.execute(text("SELECT bin_id FROM bins WHERE bin_id = :bid"), {"bid": bin_id}).fetchone()
        if not existing:
            return jsonify({"error": "Bin not found"}), 404

        fields, params = [], {"bid": bin_id}
        for col in ("bin_code", "bin_barcode", "bin_type", "aisle", "row_num", "level_num", "position_num", "pick_sequence", "putaway_sequence", "is_active", "zone_id"):
            if col in data:
                fields.append(f"{col} = :{col}")
                params[col] = data[col]

        if not fields:
            return jsonify({"error": "No fields to update"}), 400

        db.execute(text(f"UPDATE bins SET {', '.join(fields)} WHERE bin_id = :bid"), params)
        db.commit()

        row = db.execute(
            text("""
                SELECT b.bin_id, b.zone_id, z.zone_name, b.warehouse_id, b.bin_code, b.bin_barcode, b.bin_type,
                       b.aisle, b.row_num, b.level_num, b.position_num, b.pick_sequence, b.putaway_sequence, b.is_active
                FROM bins b JOIN zones z ON z.zone_id = b.zone_id WHERE b.bin_id = :bid
            """),
            {"bid": bin_id},
        ).fetchone()
        return jsonify({
            "bin_id": row.bin_id, "zone_id": row.zone_id, "zone_name": row.zone_name, "warehouse_id": row.warehouse_id,
            "bin_code": row.bin_code, "bin_barcode": row.bin_barcode, "bin_type": row.bin_type,
            "aisle": row.aisle, "row_num": row.row_num, "level_num": row.level_num,
            "position_num": row.position_num, "pick_sequence": row.pick_sequence,
            "putaway_sequence": row.putaway_sequence, "is_active": row.is_active,
        })
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ── Items ─────────────────────────────────────────────────────────────────────

@admin_bp.route("/items", methods=["GET"])
@require_auth
def list_items():
    db = next(get_db())
    try:
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

        total = db.execute(text(f"SELECT COUNT(*) FROM items {where_sql}"), params).scalar()
        pages = max(1, math.ceil(total / per_page))

        params["limit"] = per_page
        params["offset"] = (page - 1) * per_page
        rows = db.execute(
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
    finally:
        db.close()


@admin_bp.route("/items/<int:item_id>", methods=["GET"])
@require_auth
def get_item(item_id):
    db = next(get_db())
    try:
        item = db.execute(
            text("SELECT item_id, sku, item_name, description, upc, barcode_aliases, category, weight_lbs, length_in, width_in, height_in, default_bin_id, reorder_point, reorder_qty, is_lot_tracked, is_serial_tracked, is_active, created_at, updated_at FROM items WHERE item_id = :iid"),
            {"iid": item_id},
        ).fetchone()
        if not item:
            return jsonify({"error": "Item not found"}), 404

        inv_rows = db.execute(
            text("""
                SELECT inv.bin_id, b.bin_code, z.zone_name, inv.quantity_on_hand, inv.quantity_allocated
                FROM inventory inv JOIN bins b ON b.bin_id = inv.bin_id JOIN zones z ON z.zone_id = b.zone_id
                WHERE inv.item_id = :iid
            """),
            {"iid": item_id},
        ).fetchall()

        pref_rows = db.execute(
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
    finally:
        db.close()


@admin_bp.route("/items", methods=["POST"])
@require_auth
@require_role("ADMIN", "MANAGER")
def create_item():
    data = request.get_json()
    if not data or not data.get("sku") or not data.get("item_name"):
        return jsonify({"error": "sku and item_name are required"}), 400

    db = next(get_db())
    try:
        dup = db.execute(text("SELECT 1 FROM items WHERE sku = :sku"), {"sku": data["sku"]}).fetchone()
        if dup:
            return jsonify({"error": f"Duplicate SKU: {data['sku']}"}), 400

        if data.get("upc"):
            dup_upc = db.execute(text("SELECT 1 FROM items WHERE upc = :upc"), {"upc": data["upc"]}).fetchone()
            if dup_upc:
                return jsonify({"error": f"Duplicate UPC: {data['upc']}"}), 400

        result = db.execute(
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
        db.commit()
        return jsonify({
            "item_id": row.item_id, "sku": row.sku, "item_name": row.item_name,
            "description": row.description, "upc": row.upc, "category": row.category,
            "weight_lbs": float(row.weight_lbs) if row.weight_lbs else None,
            "default_bin_id": row.default_bin_id, "is_active": row.is_active,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }), 201
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@admin_bp.route("/items/<int:item_id>", methods=["PUT"])
@require_auth
@require_role("ADMIN", "MANAGER")
def update_item(item_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    db = next(get_db())
    try:
        existing = db.execute(text("SELECT item_id FROM items WHERE item_id = :iid"), {"iid": item_id}).fetchone()
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
        db.execute(text(f"UPDATE items SET {', '.join(fields)} WHERE item_id = :iid"), params)
        db.commit()

        row = db.execute(
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
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@admin_bp.route("/items/<int:item_id>", methods=["DELETE"])
@require_auth
@require_role("ADMIN", "MANAGER")
def delete_item(item_id):
    db = next(get_db())
    try:
        existing = db.execute(text("SELECT item_id FROM items WHERE item_id = :iid"), {"iid": item_id}).fetchone()
        if not existing:
            return jsonify({"error": "Item not found"}), 404

        has_inv = db.execute(
            text("SELECT 1 FROM inventory WHERE item_id = :iid AND quantity_on_hand > 0 LIMIT 1"),
            {"iid": item_id},
        ).fetchone()
        if has_inv:
            return jsonify({"error": "Cannot deactivate item with existing inventory"}), 400

        db.execute(text("UPDATE items SET is_active = FALSE, updated_at = NOW() WHERE item_id = :iid"), {"iid": item_id})
        db.commit()
        return jsonify({"message": "Item deactivated"})
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ── Purchase Orders ───────────────────────────────────────────────────────────

@admin_bp.route("/purchase-orders", methods=["GET"])
@require_auth
def list_purchase_orders():
    db = next(get_db())
    try:
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
        total = db.execute(text(f"SELECT COUNT(*) FROM purchase_orders {where_sql}"), params).scalar()
        pages = max(1, math.ceil(total / per_page))

        params["limit"] = per_page
        params["offset"] = (page - 1) * per_page
        rows = db.execute(
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
    finally:
        db.close()


@admin_bp.route("/purchase-orders/<int:po_id>", methods=["GET"])
@require_auth
def get_purchase_order(po_id):
    db = next(get_db())
    try:
        po = db.execute(
            text("SELECT po_id, po_number, po_barcode, vendor_name, vendor_id, status, expected_date, warehouse_id, notes, created_at, received_at, created_by FROM purchase_orders WHERE po_id = :pid"),
            {"pid": po_id},
        ).fetchone()
        if not po:
            return jsonify({"error": "Purchase order not found"}), 404

        lines = db.execute(
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
    finally:
        db.close()


@admin_bp.route("/purchase-orders", methods=["POST"])
@require_auth
@require_role("ADMIN", "MANAGER")
def create_purchase_order():
    data = request.get_json()
    if not data or not data.get("po_number") or not data.get("warehouse_id") or not data.get("lines"):
        return jsonify({"error": "po_number, warehouse_id, and lines are required"}), 400

    db = next(get_db())
    try:
        dup = db.execute(text("SELECT 1 FROM purchase_orders WHERE po_number = :pn"), {"pn": data["po_number"]}).fetchone()
        if dup:
            return jsonify({"error": f"Duplicate po_number: {data['po_number']}"}), 400

        # Validate items
        for line in data["lines"]:
            item = db.execute(text("SELECT 1 FROM items WHERE item_id = :iid"), {"iid": line["item_id"]}).fetchone()
            if not item:
                return jsonify({"error": f"Item {line['item_id']} not found"}), 400

        result = db.execute(
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
            db.execute(
                text("INSERT INTO purchase_order_lines (po_id, item_id, quantity_ordered, unit_cost, line_number) VALUES (:pid, :iid, :qty, :cost, :ln)"),
                {"pid": po_id, "iid": line["item_id"], "qty": line["quantity_ordered"],
                 "cost": line.get("unit_cost"), "ln": line.get("line_number", 1)},
            )

        db.commit()

        # Re-fetch to return
        return get_purchase_order(po_id)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@admin_bp.route("/purchase-orders/<int:po_id>", methods=["PUT"])
@require_auth
@require_role("ADMIN", "MANAGER")
def update_purchase_order(po_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    db = next(get_db())
    try:
        po = db.execute(text("SELECT po_id, status FROM purchase_orders WHERE po_id = :pid"), {"pid": po_id}).fetchone()
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

        db.execute(text(f"UPDATE purchase_orders SET {', '.join(fields)} WHERE po_id = :pid"), params)
        db.commit()

        row = db.execute(
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
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@admin_bp.route("/purchase-orders/<int:po_id>/close", methods=["POST"])
@require_auth
@require_role("ADMIN", "MANAGER")
def close_purchase_order(po_id):
    db = next(get_db())
    try:
        po = db.execute(text("SELECT po_id FROM purchase_orders WHERE po_id = :pid"), {"pid": po_id}).fetchone()
        if not po:
            return jsonify({"error": "Purchase order not found"}), 404

        db.execute(text("UPDATE purchase_orders SET status = 'CLOSED' WHERE po_id = :pid"), {"pid": po_id})
        db.commit()
        return jsonify({"message": "Purchase order closed"})
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ── Sales Orders ──────────────────────────────────────────────────────────────

@admin_bp.route("/sales-orders", methods=["GET"])
@require_auth
def list_sales_orders():
    db = next(get_db())
    try:
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
        total = db.execute(text(f"SELECT COUNT(*) FROM sales_orders {where_sql}"), params).scalar()
        pages = max(1, math.ceil(total / per_page))

        params["limit"] = per_page
        params["offset"] = (page - 1) * per_page
        rows = db.execute(
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
    finally:
        db.close()


@admin_bp.route("/sales-orders/<int:so_id>", methods=["GET"])
@require_auth
def get_sales_order(so_id):
    db = next(get_db())
    try:
        so = db.execute(
            text("SELECT so_id, so_number, so_barcode, customer_name, status, priority, warehouse_id, ship_method, ship_address, order_date, ship_by_date, created_at, picked_at, packed_at, shipped_at, created_by FROM sales_orders WHERE so_id = :sid"),
            {"sid": so_id},
        ).fetchone()
        if not so:
            return jsonify({"error": "Sales order not found"}), 404

        lines = db.execute(
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
    finally:
        db.close()


@admin_bp.route("/sales-orders", methods=["POST"])
@require_auth
@require_role("ADMIN", "MANAGER")
def create_sales_order():
    data = request.get_json()
    if not data or not data.get("so_number") or not data.get("warehouse_id") or not data.get("lines"):
        return jsonify({"error": "so_number, warehouse_id, and lines are required"}), 400

    db = next(get_db())
    try:
        dup = db.execute(text("SELECT 1 FROM sales_orders WHERE so_number = :sn"), {"sn": data["so_number"]}).fetchone()
        if dup:
            return jsonify({"error": f"Duplicate so_number: {data['so_number']}"}), 400

        for line in data["lines"]:
            item = db.execute(text("SELECT 1 FROM items WHERE item_id = :iid"), {"iid": line["item_id"]}).fetchone()
            if not item:
                return jsonify({"error": f"Item {line['item_id']} not found"}), 400

        result = db.execute(
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
            db.execute(
                text("INSERT INTO sales_order_lines (so_id, item_id, quantity_ordered, line_number) VALUES (:sid, :iid, :qty, :ln)"),
                {"sid": so_id, "iid": line["item_id"], "qty": line["quantity_ordered"], "ln": line.get("line_number", 1)},
            )

        db.commit()
        return get_sales_order(so_id)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@admin_bp.route("/sales-orders/<int:so_id>", methods=["PUT"])
@require_auth
@require_role("ADMIN", "MANAGER")
def update_sales_order(so_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    db = next(get_db())
    try:
        so = db.execute(text("SELECT so_id, status FROM sales_orders WHERE so_id = :sid"), {"sid": so_id}).fetchone()
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

        db.execute(text(f"UPDATE sales_orders SET {', '.join(fields)} WHERE so_id = :sid"), params)
        db.commit()

        row = db.execute(
            text("SELECT so_id, so_number, so_barcode, customer_name, status, warehouse_id, ship_method, ship_address, created_at FROM sales_orders WHERE so_id = :sid"),
            {"sid": so_id},
        ).fetchone()
        return jsonify({
            "so_id": row.so_id, "so_number": row.so_number, "so_barcode": row.so_barcode,
            "customer_name": row.customer_name, "status": row.status,
            "warehouse_id": row.warehouse_id, "ship_method": row.ship_method,
            "ship_address": row.ship_address, "created_at": row.created_at.isoformat() if row.created_at else None,
        })
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@admin_bp.route("/sales-orders/<int:so_id>/cancel", methods=["POST"])
@require_auth
@require_role("ADMIN", "MANAGER")
def cancel_sales_order(so_id):
    db = next(get_db())
    try:
        so = db.execute(text("SELECT so_id, status FROM sales_orders WHERE so_id = :sid"), {"sid": so_id}).fetchone()
        if not so:
            return jsonify({"error": "Sales order not found"}), 404
        if so.status not in ("OPEN", "ALLOCATED", "PICKING"):
            return jsonify({"error": f"Can only cancel OPEN, ALLOCATED, or PICKING orders. Current: {so.status}"}), 400

        # If ALLOCATED or PICKING, release allocated inventory
        if so.status in ("ALLOCATED", "PICKING"):
            lines = db.execute(
                text("SELECT so_line_id, item_id, quantity_allocated FROM sales_order_lines WHERE so_id = :sid AND quantity_allocated > 0"),
                {"sid": so_id},
            ).fetchall()

            for line in lines:
                # Find the inventory rows that were allocated via pick_tasks
                tasks = db.execute(
                    text("SELECT bin_id, quantity_to_pick FROM pick_tasks WHERE so_line_id = :sol_id AND status = 'PENDING'"),
                    {"sol_id": line.so_line_id},
                ).fetchall()

                for task in tasks:
                    db.execute(
                        text("UPDATE inventory SET quantity_allocated = quantity_allocated - :qty WHERE item_id = :iid AND bin_id = :bid"),
                        {"qty": task.quantity_to_pick, "iid": line.item_id, "bid": task.bin_id},
                    )

                db.execute(
                    text("UPDATE sales_order_lines SET quantity_allocated = 0 WHERE so_line_id = :sol_id"),
                    {"sol_id": line.so_line_id},
                )

            # Clean up pick batch data
            db.execute(text("DELETE FROM pick_tasks WHERE so_id = :sid"), {"sid": so_id})
            db.execute(text("DELETE FROM pick_batch_orders WHERE so_id = :sid"), {"sid": so_id})

        db.execute(text("UPDATE sales_orders SET status = 'CANCELLED' WHERE so_id = :sid"), {"sid": so_id})
        db.commit()
        return jsonify({"message": "Sales order cancelled"})
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ── Users ─────────────────────────────────────────────────────────────────────

@admin_bp.route("/users", methods=["GET"])
@require_auth
@require_role("ADMIN", "MANAGER")
def list_users():
    db = next(get_db())
    try:
        rows = db.execute(
            text("SELECT user_id, username, full_name, role, warehouse_id, is_active, created_at, last_login FROM users ORDER BY user_id")
        ).fetchall()
        return jsonify({
            "users": [
                {"user_id": r.user_id, "username": r.username, "full_name": r.full_name,
                 "role": r.role, "warehouse_id": r.warehouse_id, "is_active": r.is_active,
                 "created_at": r.created_at.isoformat() if r.created_at else None,
                 "last_login": r.last_login.isoformat() if r.last_login else None}
                for r in rows
            ]
        })
    finally:
        db.close()


@admin_bp.route("/users", methods=["POST"])
@require_auth
@require_role("ADMIN", "MANAGER")
def create_user():
    data = request.get_json()
    if not data or not data.get("username") or not data.get("password") or not data.get("full_name") or not data.get("role"):
        return jsonify({"error": "username, password, full_name, and role are required"}), 400

    if data["role"] not in VALID_ROLES:
        return jsonify({"error": f"role must be one of: {', '.join(VALID_ROLES)}"}), 400

    db = next(get_db())
    try:
        dup = db.execute(text("SELECT 1 FROM users WHERE username = :u"), {"u": data["username"]}).fetchone()
        if dup:
            return jsonify({"error": f"Duplicate username: {data['username']}"}), 400

        pw_hash = bcrypt.hashpw(data["password"].encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        result = db.execute(
            text("""
                INSERT INTO users (username, password_hash, full_name, role, warehouse_id)
                VALUES (:u, :pw, :name, :role, :wid)
                RETURNING user_id, username, full_name, role, warehouse_id, is_active, created_at
            """),
            {"u": data["username"], "pw": pw_hash, "name": data["full_name"],
             "role": data["role"], "wid": data.get("warehouse_id")},
        )
        row = result.fetchone()
        db.commit()
        return jsonify({
            "user_id": row.user_id, "username": row.username, "full_name": row.full_name,
            "role": row.role, "warehouse_id": row.warehouse_id, "is_active": row.is_active,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }), 201
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@admin_bp.route("/users/<int:user_id>", methods=["PUT"])
@require_auth
@require_role("ADMIN", "MANAGER")
def update_user(user_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    if "role" in data and data["role"] not in VALID_ROLES:
        return jsonify({"error": f"role must be one of: {', '.join(VALID_ROLES)}"}), 400

    db = next(get_db())
    try:
        existing = db.execute(text("SELECT user_id FROM users WHERE user_id = :uid"), {"uid": user_id}).fetchone()
        if not existing:
            return jsonify({"error": "User not found"}), 404

        fields, params = [], {"uid": user_id}
        for col in ("full_name", "role", "warehouse_id", "is_active"):
            if col in data:
                fields.append(f"{col} = :{col}")
                params[col] = data[col]

        if "password" in data:
            pw_hash = bcrypt.hashpw(data["password"].encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
            fields.append("password_hash = :pw_hash")
            params["pw_hash"] = pw_hash

        if not fields:
            return jsonify({"error": "No fields to update"}), 400

        db.execute(text(f"UPDATE users SET {', '.join(fields)} WHERE user_id = :uid"), params)
        db.commit()

        row = db.execute(
            text("SELECT user_id, username, full_name, role, warehouse_id, is_active, created_at, last_login FROM users WHERE user_id = :uid"),
            {"uid": user_id},
        ).fetchone()
        return jsonify({
            "user_id": row.user_id, "username": row.username, "full_name": row.full_name,
            "role": row.role, "warehouse_id": row.warehouse_id, "is_active": row.is_active,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "last_login": row.last_login.isoformat() if row.last_login else None,
        })
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@admin_bp.route("/users/<int:user_id>", methods=["DELETE"])
@require_auth
@require_role("ADMIN", "MANAGER")
def delete_user(user_id):
    db = next(get_db())
    try:
        existing = db.execute(text("SELECT user_id FROM users WHERE user_id = :uid"), {"uid": user_id}).fetchone()
        if not existing:
            return jsonify({"error": "User not found"}), 404

        if g.current_user["user_id"] == user_id:
            return jsonify({"error": "Cannot deactivate yourself"}), 400

        db.execute(text("UPDATE users SET is_active = FALSE WHERE user_id = :uid"), {"uid": user_id})
        db.commit()
        return jsonify({"message": "User deactivated"})
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ── Audit Log ─────────────────────────────────────────────────────────────────

@admin_bp.route("/audit-log", methods=["GET"])
@require_auth
def list_audit_log():
    db = next(get_db())
    try:
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 50, type=int)

        where_clauses, params = [], {}
        action_type = request.args.get("action_type")
        user_id = request.args.get("user_id")
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")

        if action_type:
            where_clauses.append("action_type = :action_type")
            params["action_type"] = action_type
        if user_id:
            where_clauses.append("user_id = :user_id")
            params["user_id"] = user_id
        if start_date:
            where_clauses.append("created_at >= :start_date")
            params["start_date"] = start_date
        if end_date:
            where_clauses.append("created_at <= :end_date")
            params["end_date"] = end_date

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        total = db.execute(text(f"SELECT COUNT(*) FROM audit_log {where_sql}"), params).scalar()
        pages = max(1, math.ceil(total / per_page))

        params["limit"] = per_page
        params["offset"] = (page - 1) * per_page
        rows = db.execute(
            text(f"""
                SELECT log_id, action_type, entity_type, entity_id, user_id, device_id, warehouse_id, details, created_at
                FROM audit_log {where_sql} ORDER BY created_at DESC LIMIT :limit OFFSET :offset
            """),
            params,
        ).fetchall()

        return jsonify({
            "entries": [
                {"log_id": r.log_id, "action_type": r.action_type, "entity_type": r.entity_type,
                 "entity_id": r.entity_id, "user_id": r.user_id, "device_id": r.device_id,
                 "warehouse_id": r.warehouse_id, "details": r.details,
                 "created_at": r.created_at.isoformat() if r.created_at else None}
                for r in rows
            ],
            "total": total, "page": page, "per_page": per_page, "pages": pages,
        })
    finally:
        db.close()


# ── Inventory Overview ────────────────────────────────────────────────────────

@admin_bp.route("/inventory", methods=["GET"])
@require_auth
def list_inventory():
    db = next(get_db())
    try:
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
        total = db.execute(
            text(f"SELECT COUNT(*) FROM inventory inv {where_sql}"), params
        ).scalar()
        pages = max(1, math.ceil(total / per_page))

        params["limit"] = per_page
        params["offset"] = (page - 1) * per_page
        rows = db.execute(
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
    finally:
        db.close()


# ── CSV Import ────────────────────────────────────────────────────────────────

@admin_bp.route("/import/<entity_type>", methods=["POST"])
@require_auth
@require_role("ADMIN", "MANAGER")
def csv_import(entity_type):
    if entity_type not in ("items", "bins", "purchase-orders", "sales-orders"):
        return jsonify({"error": f"Unsupported entity type: {entity_type}"}), 400

    data = request.get_json()
    if not data or not data.get("records"):
        return jsonify({"error": "records array is required"}), 400

    records = data["records"]
    imported = 0
    errors = []

    db = next(get_db())
    try:
        for idx, rec in enumerate(records, 1):
            try:
                if entity_type == "items":
                    _import_item(db, rec, idx, errors)
                elif entity_type == "bins":
                    _import_bin(db, rec, idx, errors)
                else:
                    errors.append({"row": idx, "error": f"Import for {entity_type} not yet supported"})
                    continue
                imported += 1
            except _SkipRow as e:
                errors.append({"row": idx, "error": str(e)})

        db.commit()
        return jsonify({
            "message": "Import complete",
            "total": len(records),
            "imported": imported,
            "skipped": len(errors),
            "errors": errors,
        })
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


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


# ── Dashboard Stats ───────────────────────────────────────────────────────────

@admin_bp.route("/dashboard", methods=["GET"])
@require_auth
def dashboard():
    db = next(get_db())
    try:
        warehouse_id = request.args.get("warehouse_id", type=int)
        wh_filter = "AND warehouse_id = :wid" if warehouse_id else ""
        wh_params = {"wid": warehouse_id} if warehouse_id else {}

        open_pos = db.execute(text(f"SELECT COUNT(*) FROM purchase_orders WHERE status IN ('OPEN', 'PARTIAL') {wh_filter}"), wh_params).scalar()

        pending_receipts = db.execute(
            text(f"SELECT COALESCE(SUM(pol.quantity_ordered - pol.quantity_received), 0) FROM purchase_order_lines pol JOIN purchase_orders po ON po.po_id = pol.po_id WHERE po.status IN ('OPEN', 'PARTIAL') {wh_filter.replace('warehouse_id', 'po.warehouse_id')}"),
            wh_params,
        ).scalar()

        items_awaiting_putaway = db.execute(
            text(f"SELECT COALESCE(SUM(inv.quantity_on_hand), 0) FROM inventory inv JOIN bins b ON b.bin_id = inv.bin_id WHERE b.bin_type = 'Staging' {wh_filter.replace('warehouse_id', 'inv.warehouse_id')}"),
            wh_params,
        ).scalar()

        open_sos = db.execute(text(f"SELECT COUNT(*) FROM sales_orders WHERE status = 'OPEN' {wh_filter}"), wh_params).scalar()
        ready_to_pick = db.execute(text(f"SELECT COUNT(*) FROM sales_orders WHERE status IN ('OPEN') {wh_filter}"), wh_params).scalar()
        in_picking = db.execute(text(f"SELECT COUNT(*) FROM sales_orders WHERE status = 'PICKING' {wh_filter}"), wh_params).scalar()
        # Toggle-aware pack/ship counts
        packing_row = db.execute(
            text("SELECT value FROM app_settings WHERE key = 'require_packing_before_shipping'")
        ).fetchone()
        require_packing = not packing_row or packing_row.value != "false"

        picked_count = db.execute(text(f"SELECT COUNT(*) FROM sales_orders WHERE status = 'PICKED' {wh_filter}"), wh_params).scalar()
        packed_count = db.execute(text(f"SELECT COUNT(*) FROM sales_orders WHERE status = 'PACKED' {wh_filter}"), wh_params).scalar()

        if require_packing:
            ready_to_pack = picked_count
            orders_packed = packed_count
            ready_to_ship = packed_count
        else:
            ready_to_pack = 0
            orders_packed = 0
            ready_to_ship = picked_count + packed_count

        total_skus = db.execute(text("SELECT COUNT(*) FROM items WHERE is_active = TRUE")).scalar()
        total_bins = db.execute(text(f"SELECT COUNT(*) FROM bins WHERE is_active = TRUE {wh_filter}"), wh_params).scalar()

        low_stock = db.execute(
            text("""
                SELECT COUNT(*) FROM (
                    SELECT i.item_id
                    FROM items i
                    LEFT JOIN inventory inv ON inv.item_id = i.item_id
                    WHERE i.is_active = TRUE AND i.reorder_point IS NOT NULL AND i.reorder_point > 0
                    GROUP BY i.item_id, i.reorder_point
                    HAVING COALESCE(SUM(inv.quantity_on_hand), 0) <= i.reorder_point
                ) sub
            """)
        ).scalar()

        recent = db.execute(
            text(f"SELECT action_type, user_id, details, created_at FROM audit_log {('WHERE warehouse_id = :wid' if warehouse_id else '')} ORDER BY created_at DESC LIMIT 10"),
            wh_params,
        ).fetchall()

        # Short picks in last 7 days
        short_pick_count = db.execute(
            text(f"SELECT COUNT(*) FROM audit_log WHERE action_type = 'PICK' AND details->>'type' = 'SHORT_PICK' AND created_at >= NOW() - INTERVAL '7 days' {('AND warehouse_id = :wid' if warehouse_id else '')}"),
            wh_params,
        ).scalar()

        result = {
            "open_pos": open_pos,
            "pending_receipts": int(pending_receipts),
            "items_awaiting_putaway": int(items_awaiting_putaway),
            "open_sos": open_sos,
            "orders_ready_to_pick": ready_to_pick,
            "orders_in_picking": in_picking,
            "ready_to_ship": ready_to_ship,
            "require_packing": require_packing,
            "total_skus": total_skus,
            "total_bins": total_bins,
            "short_picks_7d": short_pick_count,
            "low_stock_items": low_stock,
            "recent_activity": [
                {"action": r.action_type, "user": r.user_id,
                 "detail": str(r.details) if r.details else None,
                 "time": r.created_at.isoformat() if r.created_at else None}
                for r in recent
            ],
        }

        if require_packing:
            result["ready_to_pack"] = ready_to_pack
            result["orders_packed"] = orders_packed

        return jsonify(result)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@admin_bp.route("/settings", methods=["GET"])
@require_auth
def get_settings():
    db = next(get_db())
    try:
        rows = db.execute(text("SELECT id, key, value, updated_at FROM app_settings ORDER BY key")).fetchall()
        return jsonify({
            "settings": [
                {"id": r.id, "key": r.key, "value": r.value,
                 "updated_at": r.updated_at.isoformat() if r.updated_at else None}
                for r in rows
            ]
        })
    finally:
        db.close()


@admin_bp.route("/settings/<setting_key>", methods=["GET"])
@require_auth
def get_setting(setting_key):
    db = next(get_db())
    try:
        row = db.execute(
            text("SELECT id, key, value FROM app_settings WHERE key = :key"),
            {"key": setting_key},
        ).fetchone()
        if not row:
            return jsonify({"error": "Setting not found"}), 404
        return jsonify({"key": row.key, "value": row.value})
    finally:
        db.close()


@admin_bp.route("/settings", methods=["PUT"])
@require_auth
@require_role("ADMIN")
def update_settings():
    data = request.get_json()
    if not data or not data.get("settings"):
        return jsonify({"error": "settings object is required"}), 400

    db = next(get_db())
    try:
        # Toggle protection: reject disabling packing when PACKED orders exist
        if data["settings"].get("require_packing_before_shipping") == "false":
            packed_count = db.execute(
                text("SELECT COUNT(*) FROM sales_orders WHERE status = 'PACKED'")
            ).scalar()
            if packed_count > 0:
                return jsonify({
                    "error": f"Cannot disable packing. {packed_count} order{'s' if packed_count != 1 else ''} in PACKED status. Ship them before disabling."
                }), 400

        for key, value in data["settings"].items():
            db.execute(
                text(
                    """
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (:key, :value, NOW())
                    ON CONFLICT (key) DO UPDATE SET value = :value, updated_at = NOW()
                    """
                ),
                {"key": key, "value": str(value)},
            )
        db.commit()
        return jsonify({"message": "Settings updated"})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Cycle Counts (admin view)
# ---------------------------------------------------------------------------

@admin_bp.route("/cycle-counts", methods=["GET"])
@require_auth
def list_cycle_counts():
    db = next(get_db())
    try:
        rows = db.execute(
            text(
                """
                SELECT cc.count_id, cc.status, cc.assigned_to, cc.created_at,
                       cc.completed_at, b.bin_code, b.bin_id
                FROM cycle_counts cc
                JOIN bins b ON b.bin_id = cc.bin_id
                ORDER BY cc.created_at DESC
                LIMIT 200
                """
            )
        ).fetchall()

        counts = []
        for r in rows:
            lines = db.execute(
                text(
                    """
                    SELECT ccl.count_line_id, i.sku, i.item_name,
                           ccl.expected_quantity, ccl.counted_quantity,
                           (ccl.counted_quantity - ccl.expected_quantity) AS variance
                    FROM cycle_count_lines ccl
                    JOIN items i ON i.item_id = ccl.item_id
                    WHERE ccl.count_id = :cid
                    ORDER BY i.sku
                    """
                ),
                {"cid": r.count_id},
            ).fetchall()

            counts.append({
                "count_id": r.count_id,
                "bin_code": r.bin_code,
                "status": r.status,
                "assigned_to": r.assigned_to,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "lines": [
                    {
                        "count_line_id": l.count_line_id,
                        "sku": l.sku,
                        "item_name": l.item_name,
                        "expected_quantity": l.expected_quantity,
                        "counted_quantity": l.counted_quantity,
                        "variance": l.variance,
                    }
                    for l in lines
                ],
            })

        return jsonify({"cycle_counts": counts})
    finally:
        db.close()


@admin_bp.route("/preferred-bins", methods=["GET"])
@require_auth
def list_preferred_bins():
    db = next(get_db())
    try:
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

        rows = db.execute(
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
    finally:
        db.close()


@admin_bp.route("/preferred-bins", methods=["POST"])
@require_auth
def create_preferred_bin():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    item_id = data.get("item_id")
    bin_id = data.get("bin_id")
    priority = data.get("priority", 1)

    if not item_id or not bin_id:
        return jsonify({"error": "item_id and bin_id are required"}), 400

    db = next(get_db())
    try:
        db.execute(
            text(
                """
                INSERT INTO preferred_bins (item_id, bin_id, priority)
                VALUES (:item_id, :bin_id, :priority)
                ON CONFLICT (item_id, bin_id) DO UPDATE SET priority = :priority, updated_at = NOW()
                """
            ),
            {"item_id": item_id, "bin_id": bin_id, "priority": priority},
        )
        db.commit()
        return jsonify({"message": "Preferred bin saved"})
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@admin_bp.route("/preferred-bins/<int:preferred_bin_id>", methods=["PUT"])
@require_auth
def update_preferred_bin(preferred_bin_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    priority = data.get("priority")
    if priority is None:
        return jsonify({"error": "priority is required"}), 400

    db = next(get_db())
    try:
        db.execute(
            text("UPDATE preferred_bins SET priority = :priority, updated_at = NOW() WHERE preferred_bin_id = :pbid"),
            {"priority": priority, "pbid": preferred_bin_id},
        )
        db.commit()
        return jsonify({"message": "Priority updated"})
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@admin_bp.route("/preferred-bins/<int:preferred_bin_id>", methods=["DELETE"])
@require_auth
def delete_preferred_bin(preferred_bin_id):
    db = next(get_db())
    try:
        db.execute(
            text("DELETE FROM preferred_bins WHERE preferred_bin_id = :pbid"),
            {"pbid": preferred_bin_id},
        )
        db.commit()
        return jsonify({"message": "Preferred bin deleted"})
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Short Picks Report
# ---------------------------------------------------------------------------

@admin_bp.route("/short-picks", methods=["GET"])
@require_auth
@require_role("ADMIN", "MANAGER")
def get_short_picks():
    """Return recent short pick events from the audit log."""
    db = get_db()
    try:
        days = request.args.get("days", 30, type=int)
        warehouse_id = request.args.get("warehouse_id", type=int)
        wh_clause = "AND a.warehouse_id = :wid" if warehouse_id else ""
        params = {"days": days}
        if warehouse_id:
            params["wid"] = warehouse_id

        rows = db.execute(
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
    finally:
        db.close()
