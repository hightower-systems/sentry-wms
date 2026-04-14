"""
Warehouse endpoints (authenticated).
"""

from flask import Blueprint, g, jsonify
from sqlalchemy import text

from middleware.auth_middleware import require_auth
from middleware.db import with_db

warehouses_bp = Blueprint("warehouses", __name__)


@warehouses_bp.route("/list")
@require_auth
@with_db
def list_warehouses():
    rows = g.db.execute(
        text("SELECT warehouse_id, warehouse_name, warehouse_code FROM warehouses WHERE is_active = TRUE ORDER BY warehouse_name")
    ).fetchall()
    warehouses = [
        {"id": r.warehouse_id, "name": r.warehouse_name, "code": r.warehouse_code}
        for r in rows
    ]
    return jsonify({"warehouses": warehouses})
