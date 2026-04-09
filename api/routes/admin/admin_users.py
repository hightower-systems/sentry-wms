"""Users, Audit Log, Dashboard Stats, Settings, and Cycle Counts endpoints."""

import math

import bcrypt
from flask import g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_auth, require_role
from middleware.db import with_db
from routes.admin import VALID_ROLES, admin_bp


# ── Users ─────────────────────────────────────────────────────────────────────

@admin_bp.route("/users", methods=["GET"])
@require_auth
@require_role("ADMIN", "MANAGER")
@with_db
def list_users():
    rows = g.db.execute(
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


@admin_bp.route("/users", methods=["POST"])
@require_auth
@require_role("ADMIN", "MANAGER")
@with_db
def create_user():
    data = request.get_json()
    if not data or not data.get("username") or not data.get("password") or not data.get("full_name") or not data.get("role"):
        return jsonify({"error": "username, password, full_name, and role are required"}), 400

    if data["role"] not in VALID_ROLES:
        return jsonify({"error": f"role must be one of: {', '.join(VALID_ROLES)}"}), 400

    dup = g.db.execute(text("SELECT 1 FROM users WHERE username = :u"), {"u": data["username"]}).fetchone()
    if dup:
        return jsonify({"error": f"Duplicate username: {data['username']}"}), 400

    pw_hash = bcrypt.hashpw(data["password"].encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    result = g.db.execute(
        text("""
            INSERT INTO users (username, password_hash, full_name, role, warehouse_id)
            VALUES (:u, :pw, :name, :role, :wid)
            RETURNING user_id, username, full_name, role, warehouse_id, is_active, created_at
        """),
        {"u": data["username"], "pw": pw_hash, "name": data["full_name"],
         "role": data["role"], "wid": data.get("warehouse_id")},
    )
    row = result.fetchone()
    g.db.commit()
    return jsonify({
        "user_id": row.user_id, "username": row.username, "full_name": row.full_name,
        "role": row.role, "warehouse_id": row.warehouse_id, "is_active": row.is_active,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }), 201


@admin_bp.route("/users/<int:user_id>", methods=["PUT"])
@require_auth
@require_role("ADMIN", "MANAGER")
@with_db
def update_user(user_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    if "role" in data and data["role"] not in VALID_ROLES:
        return jsonify({"error": f"role must be one of: {', '.join(VALID_ROLES)}"}), 400

    existing = g.db.execute(text("SELECT user_id FROM users WHERE user_id = :uid"), {"uid": user_id}).fetchone()
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

    g.db.execute(text(f"UPDATE users SET {', '.join(fields)} WHERE user_id = :uid"), params)
    g.db.commit()

    row = g.db.execute(
        text("SELECT user_id, username, full_name, role, warehouse_id, is_active, created_at, last_login FROM users WHERE user_id = :uid"),
        {"uid": user_id},
    ).fetchone()
    return jsonify({
        "user_id": row.user_id, "username": row.username, "full_name": row.full_name,
        "role": row.role, "warehouse_id": row.warehouse_id, "is_active": row.is_active,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_login": row.last_login.isoformat() if row.last_login else None,
    })


@admin_bp.route("/users/<int:user_id>", methods=["DELETE"])
@require_auth
@require_role("ADMIN", "MANAGER")
@with_db
def delete_user(user_id):
    existing = g.db.execute(text("SELECT user_id FROM users WHERE user_id = :uid"), {"uid": user_id}).fetchone()
    if not existing:
        return jsonify({"error": "User not found"}), 404

    if g.current_user["user_id"] == user_id:
        return jsonify({"error": "Cannot deactivate yourself"}), 400

    g.db.execute(text("UPDATE users SET is_active = FALSE WHERE user_id = :uid"), {"uid": user_id})
    g.db.commit()
    return jsonify({"message": "User deactivated"})


# ── Audit Log ─────────────────────────────────────────────────────────────────

@admin_bp.route("/audit-log", methods=["GET"])
@require_auth
@with_db
def list_audit_log():
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
    total = g.db.execute(text(f"SELECT COUNT(*) FROM audit_log {where_sql}"), params).scalar()
    pages = max(1, math.ceil(total / per_page))

    params["limit"] = per_page
    params["offset"] = (page - 1) * per_page
    rows = g.db.execute(
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


# ── Dashboard Stats ───────────────────────────────────────────────────────────

@admin_bp.route("/dashboard", methods=["GET"])
@require_auth
@with_db
def dashboard():
    warehouse_id = request.args.get("warehouse_id", type=int)
    wh_filter = "AND warehouse_id = :wid" if warehouse_id else ""
    wh_params = {"wid": warehouse_id} if warehouse_id else {}

    open_pos = g.db.execute(text(f"SELECT COUNT(*) FROM purchase_orders WHERE status IN ('OPEN', 'PARTIAL') {wh_filter}"), wh_params).scalar()

    pending_receipts = g.db.execute(
        text(f"SELECT COALESCE(SUM(pol.quantity_ordered - pol.quantity_received), 0) FROM purchase_order_lines pol JOIN purchase_orders po ON po.po_id = pol.po_id WHERE po.status IN ('OPEN', 'PARTIAL') {wh_filter.replace('warehouse_id', 'po.warehouse_id')}"),
        wh_params,
    ).scalar()

    items_awaiting_putaway = g.db.execute(
        text(f"SELECT COALESCE(SUM(inv.quantity_on_hand), 0) FROM inventory inv JOIN bins b ON b.bin_id = inv.bin_id WHERE b.bin_type = 'Staging' {wh_filter.replace('warehouse_id', 'inv.warehouse_id')}"),
        wh_params,
    ).scalar()

    open_sos = g.db.execute(text(f"SELECT COUNT(*) FROM sales_orders WHERE status = 'OPEN' {wh_filter}"), wh_params).scalar()
    ready_to_pick = g.db.execute(text(f"SELECT COUNT(*) FROM sales_orders WHERE status IN ('OPEN') {wh_filter}"), wh_params).scalar()
    in_picking = g.db.execute(text(f"SELECT COUNT(*) FROM sales_orders WHERE status = 'PICKING' {wh_filter}"), wh_params).scalar()
    # Toggle-aware pack/ship counts
    packing_row = g.db.execute(
        text("SELECT value FROM app_settings WHERE key = 'require_packing_before_shipping'")
    ).fetchone()
    require_packing = not packing_row or packing_row.value != "false"

    picked_count = g.db.execute(text(f"SELECT COUNT(*) FROM sales_orders WHERE status = 'PICKED' {wh_filter}"), wh_params).scalar()
    packed_count = g.db.execute(text(f"SELECT COUNT(*) FROM sales_orders WHERE status = 'PACKED' {wh_filter}"), wh_params).scalar()

    if require_packing:
        ready_to_pack = picked_count
        orders_packed = packed_count
        ready_to_ship = packed_count
    else:
        ready_to_pack = 0
        orders_packed = 0
        ready_to_ship = picked_count + packed_count

    total_skus = g.db.execute(text("SELECT COUNT(*) FROM items WHERE is_active = TRUE")).scalar()
    total_bins = g.db.execute(text(f"SELECT COUNT(*) FROM bins WHERE is_active = TRUE {wh_filter}"), wh_params).scalar()

    low_stock = g.db.execute(
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

    recent = g.db.execute(
        text(f"SELECT action_type, user_id, details, created_at FROM audit_log {('WHERE warehouse_id = :wid' if warehouse_id else '')} ORDER BY created_at DESC LIMIT 10"),
        wh_params,
    ).fetchall()

    # Short picks in last 7 days
    short_pick_count = g.db.execute(
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


# ── Settings ──────────────────────────────────────────────────────────────────

@admin_bp.route("/settings", methods=["GET"])
@require_auth
@with_db
def get_settings():
    rows = g.db.execute(text("SELECT id, key, value, updated_at FROM app_settings ORDER BY key")).fetchall()
    return jsonify({
        "settings": [
            {"id": r.id, "key": r.key, "value": r.value,
             "updated_at": r.updated_at.isoformat() if r.updated_at else None}
            for r in rows
        ]
    })


@admin_bp.route("/settings/<setting_key>", methods=["GET"])
@require_auth
@with_db
def get_setting(setting_key):
    row = g.db.execute(
        text("SELECT id, key, value FROM app_settings WHERE key = :key"),
        {"key": setting_key},
    ).fetchone()
    if not row:
        return jsonify({"error": "Setting not found"}), 404
    return jsonify({"key": row.key, "value": row.value})


@admin_bp.route("/settings", methods=["PUT"])
@require_auth
@require_role("ADMIN")
@with_db
def update_settings():
    data = request.get_json()
    if not data or not data.get("settings"):
        return jsonify({"error": "settings object is required"}), 400

    # Toggle protection: reject disabling packing when PACKED orders exist
    if data["settings"].get("require_packing_before_shipping") == "false":
        packed_count = g.db.execute(
            text("SELECT COUNT(*) FROM sales_orders WHERE status = 'PACKED'")
        ).scalar()
        if packed_count > 0:
            return jsonify({
                "error": f"Cannot disable packing. {packed_count} order{'s' if packed_count != 1 else ''} in PACKED status. Ship them before disabling."
            }), 400

    for key, value in data["settings"].items():
        g.db.execute(
            text(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (:key, :value, NOW())
                ON CONFLICT (key) DO UPDATE SET value = :value, updated_at = NOW()
                """
            ),
            {"key": key, "value": str(value)},
        )
    g.db.commit()
    return jsonify({"message": "Settings updated"})


# ── Cycle Counts (admin view) ────────────────────────────────────────────────

@admin_bp.route("/cycle-counts", methods=["GET"])
@require_auth
@with_db
def list_cycle_counts():
    rows = g.db.execute(
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
        lines = g.db.execute(
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
