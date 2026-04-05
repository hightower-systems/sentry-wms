"""
Public warehouse endpoints (no auth required).
"""

from flask import Blueprint, jsonify
from sqlalchemy import text

from models.database import get_db

warehouses_bp = Blueprint("warehouses", __name__)


@warehouses_bp.route("/list")
def list_warehouses():
    db = next(get_db())
    try:
        rows = db.execute(
            text("SELECT warehouse_id, warehouse_name, warehouse_code FROM warehouses WHERE is_active = TRUE ORDER BY warehouse_name")
        ).fetchall()
        warehouses = [
            {"id": r.warehouse_id, "name": r.warehouse_name, "code": r.warehouse_code}
            for r in rows
        ]
        return jsonify({"warehouses": warehouses})
    finally:
        db.close()
