"""Warehouse, Zone, and Bin endpoints."""

from flask import g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_auth, require_role
from middleware.db import with_db
from routes.admin import VALID_BIN_TYPES, VALID_ZONE_TYPES, admin_bp


# ── Warehouses ────────────────────────────────────────────────────────────────

@admin_bp.route("/warehouses", methods=["GET"])
@require_auth
@with_db
def list_warehouses():
    rows = g.db.execute(
        text("SELECT warehouse_id, warehouse_code, warehouse_name, address, is_active, created_at FROM warehouses ORDER BY warehouse_id")
    ).fetchall()
    return jsonify({
        "warehouses": [
            {"warehouse_id": r.warehouse_id, "warehouse_code": r.warehouse_code, "warehouse_name": r.warehouse_name,
             "address": r.address, "is_active": r.is_active, "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows
        ]
    })


@admin_bp.route("/warehouses/<int:warehouse_id>", methods=["GET"])
@require_auth
@with_db
def get_warehouse(warehouse_id):
    wh = g.db.execute(
        text("SELECT warehouse_id, warehouse_code, warehouse_name, address, is_active, created_at FROM warehouses WHERE warehouse_id = :wid"),
        {"wid": warehouse_id},
    ).fetchone()
    if not wh:
        return jsonify({"error": "Warehouse not found"}), 404

    zones = g.db.execute(
        text("SELECT zone_id, warehouse_id, zone_code, zone_name, zone_type, is_active FROM zones WHERE warehouse_id = :wid ORDER BY zone_id"),
        {"wid": warehouse_id},
    ).fetchall()

    return jsonify({
        "warehouse": {"warehouse_id": wh.warehouse_id, "warehouse_code": wh.warehouse_code, "warehouse_name": wh.warehouse_name,
                      "address": wh.address, "is_active": wh.is_active, "created_at": wh.created_at.isoformat() if wh.created_at else None},
        "zones": [{"zone_id": z.zone_id, "warehouse_id": z.warehouse_id, "zone_code": z.zone_code,
                    "zone_name": z.zone_name, "zone_type": z.zone_type, "is_active": z.is_active} for z in zones],
    })


@admin_bp.route("/warehouses", methods=["POST"])
@require_auth
@require_role("ADMIN", "MANAGER")
@with_db
def create_warehouse():
    data = request.get_json()
    if not data or not data.get("warehouse_code") or not data.get("warehouse_name"):
        return jsonify({"error": "warehouse_code and warehouse_name are required"}), 400

    dup = g.db.execute(text("SELECT 1 FROM warehouses WHERE warehouse_code = :c"), {"c": data["warehouse_code"]}).fetchone()
    if dup:
        return jsonify({"error": f"Duplicate warehouse_code: {data['warehouse_code']}"}), 400

    result = g.db.execute(
        text("INSERT INTO warehouses (warehouse_code, warehouse_name, address) VALUES (:code, :name, :addr) RETURNING warehouse_id, warehouse_code, warehouse_name, address, is_active, created_at"),
        {"code": data["warehouse_code"], "name": data["warehouse_name"], "addr": data.get("address")},
    )
    row = result.fetchone()
    g.db.commit()
    return jsonify({
        "warehouse_id": row.warehouse_id, "warehouse_code": row.warehouse_code, "warehouse_name": row.warehouse_name,
        "address": row.address, "is_active": row.is_active, "created_at": row.created_at.isoformat() if row.created_at else None,
    }), 201


@admin_bp.route("/warehouses/<int:warehouse_id>", methods=["PUT"])
@require_auth
@require_role("ADMIN", "MANAGER")
@with_db
def update_warehouse(warehouse_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    wh = g.db.execute(text("SELECT warehouse_id FROM warehouses WHERE warehouse_id = :wid"), {"wid": warehouse_id}).fetchone()
    if not wh:
        return jsonify({"error": "Warehouse not found"}), 404

    fields, params = [], {"wid": warehouse_id}
    for col in ("warehouse_code", "warehouse_name", "address", "is_active"):
        if col in data:
            fields.append(f"{col} = :{col}")
            params[col] = data[col]

    if not fields:
        return jsonify({"error": "No fields to update"}), 400

    g.db.execute(text(f"UPDATE warehouses SET {', '.join(fields)} WHERE warehouse_id = :wid"), params)
    g.db.commit()

    row = g.db.execute(
        text("SELECT warehouse_id, warehouse_code, warehouse_name, address, is_active, created_at FROM warehouses WHERE warehouse_id = :wid"),
        {"wid": warehouse_id},
    ).fetchone()
    return jsonify({
        "warehouse_id": row.warehouse_id, "warehouse_code": row.warehouse_code, "warehouse_name": row.warehouse_name,
        "address": row.address, "is_active": row.is_active, "created_at": row.created_at.isoformat() if row.created_at else None,
    })


# ── Zones ─────────────────────────────────────────────────────────────────────

@admin_bp.route("/zones", methods=["GET"])
@require_auth
@with_db
def list_zones():
    warehouse_id = request.args.get("warehouse_id", type=int)
    if warehouse_id:
        rows = g.db.execute(
            text("SELECT zone_id, warehouse_id, zone_code, zone_name, zone_type, is_active FROM zones WHERE warehouse_id = :wid ORDER BY zone_id"),
            {"wid": warehouse_id},
        ).fetchall()
    else:
        rows = g.db.execute(text("SELECT zone_id, warehouse_id, zone_code, zone_name, zone_type, is_active FROM zones ORDER BY zone_id")).fetchall()

    return jsonify({
        "zones": [{"zone_id": z.zone_id, "warehouse_id": z.warehouse_id, "zone_code": z.zone_code,
                    "zone_name": z.zone_name, "zone_type": z.zone_type, "is_active": z.is_active} for z in rows]
    })


@admin_bp.route("/zones", methods=["POST"])
@require_auth
@require_role("ADMIN", "MANAGER")
@with_db
def create_zone():
    data = request.get_json()
    if not data or not data.get("warehouse_id") or not data.get("zone_code") or not data.get("zone_name") or not data.get("zone_type"):
        return jsonify({"error": "warehouse_id, zone_code, zone_name, and zone_type are required"}), 400

    if data["zone_type"] not in VALID_ZONE_TYPES:
        return jsonify({"error": f"zone_type must be one of: {', '.join(VALID_ZONE_TYPES)}"}), 400

    dup = g.db.execute(
        text("SELECT 1 FROM zones WHERE warehouse_id = :wid AND zone_code = :code"),
        {"wid": data["warehouse_id"], "code": data["zone_code"]},
    ).fetchone()
    if dup:
        return jsonify({"error": f"Duplicate zone_code '{data['zone_code']}' in warehouse {data['warehouse_id']}"}), 400

    result = g.db.execute(
        text("INSERT INTO zones (warehouse_id, zone_code, zone_name, zone_type) VALUES (:wid, :code, :name, :type) RETURNING zone_id, warehouse_id, zone_code, zone_name, zone_type, is_active"),
        {"wid": data["warehouse_id"], "code": data["zone_code"], "name": data["zone_name"], "type": data["zone_type"]},
    )
    row = result.fetchone()
    g.db.commit()
    return jsonify({"zone_id": row.zone_id, "warehouse_id": row.warehouse_id, "zone_code": row.zone_code,
                    "zone_name": row.zone_name, "zone_type": row.zone_type, "is_active": row.is_active}), 201


@admin_bp.route("/zones/<int:zone_id>", methods=["PUT"])
@require_auth
@require_role("ADMIN", "MANAGER")
@with_db
def update_zone(zone_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    if "zone_type" in data and data["zone_type"] not in VALID_ZONE_TYPES:
        return jsonify({"error": f"zone_type must be one of: {', '.join(VALID_ZONE_TYPES)}"}), 400

    existing = g.db.execute(text("SELECT zone_id FROM zones WHERE zone_id = :zid"), {"zid": zone_id}).fetchone()
    if not existing:
        return jsonify({"error": "Zone not found"}), 404

    fields, params = [], {"zid": zone_id}
    for col in ("zone_code", "zone_name", "zone_type", "is_active"):
        if col in data:
            fields.append(f"{col} = :{col}")
            params[col] = data[col]

    if not fields:
        return jsonify({"error": "No fields to update"}), 400

    g.db.execute(text(f"UPDATE zones SET {', '.join(fields)} WHERE zone_id = :zid"), params)
    g.db.commit()

    row = g.db.execute(
        text("SELECT zone_id, warehouse_id, zone_code, zone_name, zone_type, is_active FROM zones WHERE zone_id = :zid"),
        {"zid": zone_id},
    ).fetchone()
    return jsonify({"zone_id": row.zone_id, "warehouse_id": row.warehouse_id, "zone_code": row.zone_code,
                    "zone_name": row.zone_name, "zone_type": row.zone_type, "is_active": row.is_active})


# ── Bins ──────────────────────────────────────────────────────────────────────

@admin_bp.route("/bins", methods=["GET"])
@require_auth
@with_db
def list_bins():
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
    rows = g.db.execute(
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


@admin_bp.route("/bins/<int:bin_id>", methods=["GET"])
@require_auth
@with_db
def get_bin(bin_id):
    b = g.db.execute(
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

    inv_rows = g.db.execute(
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


@admin_bp.route("/bins", methods=["POST"])
@require_auth
@require_role("ADMIN", "MANAGER")
@with_db
def create_bin():
    data = request.get_json()
    if not data or not data.get("zone_id") or not data.get("warehouse_id") or not data.get("bin_code") or not data.get("bin_barcode") or not data.get("bin_type"):
        return jsonify({"error": "zone_id, warehouse_id, bin_code, bin_barcode, and bin_type are required"}), 400

    if data["bin_type"] not in VALID_BIN_TYPES:
        return jsonify({"error": f"bin_type must be one of: {', '.join(VALID_BIN_TYPES)}"}), 400

    dup = g.db.execute(
        text("SELECT 1 FROM bins WHERE warehouse_id = :wid AND bin_code = :code"),
        {"wid": data["warehouse_id"], "code": data["bin_code"]},
    ).fetchone()
    if dup:
        return jsonify({"error": f"Duplicate bin_code '{data['bin_code']}' in warehouse {data['warehouse_id']}"}), 400

    result = g.db.execute(
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
    g.db.commit()
    return jsonify({
        "bin_id": row.bin_id, "zone_id": row.zone_id, "warehouse_id": row.warehouse_id,
        "bin_code": row.bin_code, "bin_barcode": row.bin_barcode, "bin_type": row.bin_type,
        "aisle": row.aisle, "row_num": row.row_num, "level_num": row.level_num,
        "position_num": row.position_num, "pick_sequence": row.pick_sequence,
        "putaway_sequence": row.putaway_sequence, "is_active": row.is_active,
    }), 201


@admin_bp.route("/bins/<int:bin_id>", methods=["PUT"])
@require_auth
@require_role("ADMIN", "MANAGER")
@with_db
def update_bin(bin_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    if "bin_type" in data and data["bin_type"] not in VALID_BIN_TYPES:
        return jsonify({"error": f"bin_type must be one of: {', '.join(VALID_BIN_TYPES)}"}), 400

    existing = g.db.execute(text("SELECT bin_id FROM bins WHERE bin_id = :bid"), {"bid": bin_id}).fetchone()
    if not existing:
        return jsonify({"error": "Bin not found"}), 404

    fields, params = [], {"bid": bin_id}
    for col in ("bin_code", "bin_barcode", "bin_type", "aisle", "row_num", "level_num", "position_num", "pick_sequence", "putaway_sequence", "is_active", "zone_id"):
        if col in data:
            fields.append(f"{col} = :{col}")
            params[col] = data[col]

    if not fields:
        return jsonify({"error": "No fields to update"}), 400

    g.db.execute(text(f"UPDATE bins SET {', '.join(fields)} WHERE bin_id = :bid"), params)
    g.db.commit()

    row = g.db.execute(
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
