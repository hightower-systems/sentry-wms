"""Users, Audit Log, Dashboard Stats, Settings, Cycle Counts, and Adjustment Approval endpoints."""

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
@require_role("ADMIN")
@with_db
def list_users():
    rows = g.db.execute(
        text("SELECT user_id, username, full_name, role, warehouse_id, warehouse_ids, allowed_functions, is_active, created_at, last_login FROM users ORDER BY user_id")
    ).fetchall()
    return jsonify({
        "users": [
            {"user_id": r.user_id, "username": r.username, "full_name": r.full_name,
             "role": r.role, "warehouse_id": r.warehouse_id,
             "warehouse_ids": list(r.warehouse_ids) if r.warehouse_ids else [],
             "allowed_functions": list(r.allowed_functions) if r.allowed_functions else [],
             "is_active": r.is_active,
             "created_at": r.created_at.isoformat() if r.created_at else None,
             "last_login": r.last_login.isoformat() if r.last_login else None}
            for r in rows
        ]
    })


@admin_bp.route("/users", methods=["POST"])
@require_auth
@require_role("ADMIN")
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

    warehouse_ids = data.get("warehouse_ids", [])
    warehouse_id = warehouse_ids[0] if warehouse_ids else data.get("warehouse_id")
    allowed_functions = data.get("allowed_functions", [])

    pw_hash = bcrypt.hashpw(data["password"].encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    result = g.db.execute(
        text("""
            INSERT INTO users (username, password_hash, full_name, role, warehouse_id, warehouse_ids, allowed_functions)
            VALUES (:u, :pw, :name, :role, :wid, :wids, :funcs)
            RETURNING user_id, username, full_name, role, warehouse_id, warehouse_ids, allowed_functions, is_active, created_at
        """),
        {"u": data["username"], "pw": pw_hash, "name": data["full_name"],
         "role": data["role"], "wid": warehouse_id, "wids": warehouse_ids,
         "funcs": allowed_functions},
    )
    row = result.fetchone()
    g.db.commit()
    return jsonify({
        "user_id": row.user_id, "username": row.username, "full_name": row.full_name,
        "role": row.role, "warehouse_id": row.warehouse_id,
        "warehouse_ids": list(row.warehouse_ids) if row.warehouse_ids else [],
        "allowed_functions": list(row.allowed_functions) if row.allowed_functions else [],
        "is_active": row.is_active,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }), 201


@admin_bp.route("/users/<int:user_id>", methods=["PUT"])
@require_auth
@require_role("ADMIN")
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

    if "warehouse_ids" in data:
        fields.append("warehouse_ids = :warehouse_ids")
        params["warehouse_ids"] = data["warehouse_ids"]
        # Keep warehouse_id in sync (first warehouse)
        if data["warehouse_ids"]:
            fields.append("warehouse_id = :wid_sync")
            params["wid_sync"] = data["warehouse_ids"][0]

    if "allowed_functions" in data:
        fields.append("allowed_functions = :allowed_functions")
        params["allowed_functions"] = data["allowed_functions"]

    if "password" in data and data["password"]:
        pw_hash = bcrypt.hashpw(data["password"].encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        fields.append("password_hash = :pw_hash")
        params["pw_hash"] = pw_hash

    if not fields:
        return jsonify({"error": "No fields to update"}), 400

    g.db.execute(text(f"UPDATE users SET {', '.join(fields)} WHERE user_id = :uid"), params)
    g.db.commit()

    row = g.db.execute(
        text("SELECT user_id, username, full_name, role, warehouse_id, warehouse_ids, allowed_functions, is_active, created_at, last_login FROM users WHERE user_id = :uid"),
        {"uid": user_id},
    ).fetchone()
    return jsonify({
        "user_id": row.user_id, "username": row.username, "full_name": row.full_name,
        "role": row.role, "warehouse_id": row.warehouse_id,
        "warehouse_ids": list(row.warehouse_ids) if row.warehouse_ids else [],
        "allowed_functions": list(row.allowed_functions) if row.allowed_functions else [],
        "is_active": row.is_active,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_login": row.last_login.isoformat() if row.last_login else None,
    })


@admin_bp.route("/users/<int:user_id>", methods=["DELETE"])
@require_auth
@require_role("ADMIN")
@with_db
def delete_user(user_id):
    existing = g.db.execute(text("SELECT user_id FROM users WHERE user_id = :uid"), {"uid": user_id}).fetchone()
    if not existing:
        return jsonify({"error": "User not found"}), 404

    if g.current_user["user_id"] == user_id:
        return jsonify({"error": "Cannot delete yourself"}), 400

    g.db.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": user_id})
    g.db.commit()
    return jsonify({"message": "User deleted"})


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
        where_clauses.append("al.action_type = :action_type")
        params["action_type"] = action_type
    if user_id:
        where_clauses.append("al.user_id = :filter_user_id")
        params["filter_user_id"] = user_id
    if start_date:
        where_clauses.append("al.created_at >= :start_date")
        params["start_date"] = start_date
    if end_date:
        where_clauses.append("al.created_at <= :end_date")
        params["end_date"] = end_date

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    total = g.db.execute(text(f"SELECT COUNT(*) FROM audit_log al {where_sql}"), params).scalar()
    pages = max(1, math.ceil(total / per_page))

    params["limit"] = per_page
    params["offset"] = (page - 1) * per_page
    rows = g.db.execute(
        text(f"""
            SELECT al.log_id, al.action_type, al.entity_type, al.entity_id,
                   al.user_id, al.device_id, al.warehouse_id, al.details, al.created_at,
                   CASE al.entity_type
                       WHEN 'ITEM' THEN (SELECT sku FROM items WHERE item_id = al.entity_id)
                       WHEN 'SO' THEN (SELECT so_number FROM sales_orders WHERE so_id = al.entity_id)
                       WHEN 'PO' THEN (SELECT po_number FROM purchase_orders WHERE po_id = al.entity_id)
                       WHEN 'BIN' THEN (SELECT bin_code FROM bins WHERE bin_id = al.entity_id)
                       ELSE NULL
                   END AS entity_name,
                   w.warehouse_code
            FROM audit_log al
            LEFT JOIN warehouses w ON w.warehouse_id = al.warehouse_id
            {where_sql}
            ORDER BY al.created_at DESC LIMIT :limit OFFSET :offset
        """),
        params,
    ).fetchall()

    # Collect IDs from details to batch-resolve to human-readable names
    bin_ids, item_ids, so_ids, po_ids = set(), set(), set(), set()
    for r in rows:
        d = r.details if isinstance(r.details, dict) else {}
        for k, v in d.items():
            if not isinstance(v, int):
                continue
            if "bin" in k:
                bin_ids.add(v)
            elif "item" in k:
                item_ids.add(v)
            elif "so" in k:
                so_ids.add(v)
            elif "po" in k:
                po_ids.add(v)

    bin_map, item_map, so_map, po_map = {}, {}, {}, {}
    if bin_ids:
        for br in g.db.execute(text("SELECT bin_id, bin_code FROM bins WHERE bin_id = ANY(:ids)"), {"ids": list(bin_ids)}).fetchall():
            bin_map[br.bin_id] = br.bin_code
    if item_ids:
        for ir in g.db.execute(text("SELECT item_id, sku FROM items WHERE item_id = ANY(:ids)"), {"ids": list(item_ids)}).fetchall():
            item_map[ir.item_id] = ir.sku
    if so_ids:
        for sr in g.db.execute(text("SELECT so_id, so_number FROM sales_orders WHERE so_id = ANY(:ids)"), {"ids": list(so_ids)}).fetchall():
            so_map[sr.so_id] = sr.so_number
    if po_ids:
        for pr in g.db.execute(text("SELECT po_id, po_number FROM purchase_orders WHERE po_id = ANY(:ids)"), {"ids": list(po_ids)}).fetchall():
            po_map[pr.po_id] = pr.po_number

    def resolve_details(details):
        if not isinstance(details, dict):
            return details
        resolved = {}
        for k, v in details.items():
            if isinstance(v, int):
                if "bin" in k and v in bin_map:
                    resolved[k.replace("_id", "")] = bin_map[v]
                    continue
                elif "item" in k and v in item_map:
                    resolved[k.replace("_id", "")] = item_map[v]
                    continue
                elif "so" in k and v in so_map:
                    resolved[k.replace("_id", "")] = so_map[v]
                    continue
                elif "po" in k and v in po_map:
                    resolved[k.replace("_id", "")] = po_map[v]
                    continue
            resolved[k] = v
        return resolved

    return jsonify({
        "entries": [
            {"log_id": r.log_id, "action_type": r.action_type, "entity_type": r.entity_type,
             "entity_id": r.entity_id, "entity_name": r.entity_name,
             "username": r.user_id, "device_id": r.device_id,
             "warehouse_id": r.warehouse_id, "warehouse_code": r.warehouse_code,
             "details": resolve_details(r.details),
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

    # Pending adjustments count
    pending_adjustments = g.db.execute(
        text("SELECT COUNT(*) FROM inventory_adjustments WHERE status = 'PENDING'")
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
        "pending_adjustments": pending_adjustments,
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
                       ccl.expected_quantity, ccl.counted_quantity, ccl.unexpected,
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
                    "unexpected": l.unexpected,
                    "variance": l.variance,
                }
                for l in lines
            ],
        })

    return jsonify({"cycle_counts": counts})


# ── Inventory Adjustment Approval ────────────────────────────────────────────

@admin_bp.route("/adjustments/pending", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def list_pending_adjustments():
    """Return pending inventory adjustments grouped by cycle count."""
    rows = g.db.execute(
        text("""
            SELECT ia.adjustment_id, ia.item_id, ia.bin_id, ia.warehouse_id,
                   ia.quantity_change, ia.reason_code, ia.reason_detail,
                   ia.status, ia.adjusted_by, ia.adjusted_at, ia.cycle_count_id,
                   i.sku, i.item_name, b.bin_code
            FROM inventory_adjustments ia
            JOIN items i ON i.item_id = ia.item_id
            JOIN bins b ON b.bin_id = ia.bin_id
            WHERE ia.status = 'PENDING'
            ORDER BY ia.cycle_count_id, ia.adjustment_id
        """)
    ).fetchall()

    return jsonify({
        "adjustments": [
            {
                "adjustment_id": r.adjustment_id,
                "item_id": r.item_id,
                "bin_id": r.bin_id,
                "warehouse_id": r.warehouse_id,
                "quantity_change": r.quantity_change,
                "reason_code": r.reason_code,
                "reason_detail": r.reason_detail,
                "status": r.status,
                "adjusted_by": r.adjusted_by,
                "adjusted_at": r.adjusted_at.isoformat() if r.adjusted_at else None,
                "cycle_count_id": r.cycle_count_id,
                "sku": r.sku,
                "item_name": r.item_name,
                "bin_code": r.bin_code,
            }
            for r in rows
        ]
    })


@admin_bp.route("/adjustments/review", methods=["POST"])
@require_auth
@require_role("ADMIN")
@with_db
def review_adjustments():
    """Approve or reject individual adjustments. Approved adjustments update inventory."""
    data = request.get_json()
    if not data or not data.get("decisions"):
        return jsonify({"error": "decisions array is required"}), 400

    approved = 0
    rejected = 0

    for decision in data["decisions"]:
        adj_id = decision.get("adjustment_id")
        action = decision.get("action")  # 'approve' or 'reject'

        if not adj_id or action not in ("approve", "reject"):
            continue

        row = g.db.execute(
            text("SELECT adjustment_id, item_id, bin_id, warehouse_id, quantity_change, status FROM inventory_adjustments WHERE adjustment_id = :aid"),
            {"aid": adj_id},
        ).fetchone()

        if not row or row.status != "PENDING":
            continue

        if action == "approve":
            # Apply the inventory adjustment
            existing = g.db.execute(
                text("SELECT inventory_id, quantity_on_hand FROM inventory WHERE item_id = :iid AND bin_id = :bid"),
                {"iid": row.item_id, "bid": row.bin_id},
            ).fetchone()

            if existing:
                new_qty = max(0, existing.quantity_on_hand + row.quantity_change)
                g.db.execute(
                    text("UPDATE inventory SET quantity_on_hand = :qty, updated_at = NOW() WHERE inventory_id = :inv_id"),
                    {"qty": new_qty, "inv_id": existing.inventory_id},
                )
            elif row.quantity_change > 0:
                g.db.execute(
                    text("INSERT INTO inventory (item_id, bin_id, warehouse_id, quantity_on_hand) VALUES (:iid, :bid, :wid, :qty)"),
                    {"iid": row.item_id, "bid": row.bin_id, "wid": row.warehouse_id, "qty": row.quantity_change},
                )

            g.db.execute(
                text("UPDATE inventory_adjustments SET status = 'APPROVED' WHERE adjustment_id = :aid"),
                {"aid": adj_id},
            )
            approved += 1
        else:
            g.db.execute(
                text("UPDATE inventory_adjustments SET status = 'REJECTED' WHERE adjustment_id = :aid"),
                {"aid": adj_id},
            )
            rejected += 1

    g.db.commit()
    return jsonify({"approved": approved, "rejected": rejected})
