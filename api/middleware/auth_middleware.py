"""
Route protection decorators for JWT authentication and role-based access.
"""

from functools import wraps

from flask import g, jsonify, request

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


def require_role(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if g.current_user["role"] not in roles:
                return jsonify({"error": "Forbidden"}), 403
            return f(*args, **kwargs)

        return decorated

    return decorator
