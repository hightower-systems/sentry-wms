"""
Route protection decorators for JWT authentication and role-based access.
"""

from functools import wraps

from flask import g, jsonify, request
from sqlalchemy import text

from services.auth_service import decode_token


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return jsonify({"error": "Unauthorized"}), 401

        token = auth_header.split(" ", 1)[1]
        payload = decode_token(token)
        if payload is None:
            return jsonify({"error": "Token expired"}), 401

        # Verify the user is still active and refresh role/warehouse_ids from DB.
        # This ensures that deactivated accounts and role/warehouse changes take
        # effect immediately rather than waiting for the JWT to expire.
        import models.database as _db
        db = _db.SessionLocal()
        try:
            row = db.execute(
                text(
                    "SELECT role, is_active, warehouse_ids "
                    "FROM users WHERE user_id = :uid"
                ),
                {"uid": payload["user_id"]},
            ).fetchone()
        finally:
            db.close()

        if not row or not row.is_active:
            return jsonify({"error": "Unauthorized"}), 401

        # Overwrite JWT claims with live DB values so downstream role/warehouse
        # checks always reflect the current state.
        payload["role"] = row.role
        payload["warehouse_ids"] = list(row.warehouse_ids) if row.warehouse_ids else []

        g.current_user = payload

        # Warehouse authorization: non-admin users can only access assigned warehouses
        if payload.get("role") != "ADMIN":
            allowed = payload.get("warehouse_ids") or []
            req_wid = None
            if request.is_json:
                body = request.get_json(silent=True)
                if body:
                    req_wid = body.get("warehouse_id")
            if req_wid is None:
                req_wid = request.args.get("warehouse_id", type=int)
            if req_wid is not None and int(req_wid) not in allowed:
                return jsonify({"error": "Access denied for this warehouse"}), 403

        return f(*args, **kwargs)

    return decorated


def check_warehouse_access(warehouse_id):
    """Check if the current user has access to the given warehouse.

    Call after loading a resource to verify the user is authorized
    for that resource's warehouse. Returns (False, response) if denied,
    (True, None) if allowed.
    """
    user = g.current_user
    if user.get("role") == "ADMIN":
        return True, None
    allowed = user.get("warehouse_ids") or []
    if warehouse_id is not None and int(warehouse_id) not in allowed:
        return False, (jsonify({"error": "Access denied for this warehouse"}), 403)
    return True, None


def require_role(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if g.current_user["role"] not in roles:
                return jsonify({"error": "Forbidden"}), 403
            return f(*args, **kwargs)

        return decorated

    return decorator
