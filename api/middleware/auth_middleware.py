"""
Route protection decorators for JWT authentication and role-based access.
"""

from functools import wraps

from flask import g, jsonify, request
from sqlalchemy import text

from services.auth_service import decode_token
from services.cookie_auth import (
    AUTH_COOKIE_NAME,
    CSRF_PROTECTED_METHODS,
    csrf_token_matches,
)


def _extract_token():
    """Return (token, source) where source is 'header' or 'cookie', or (None, None)."""
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header.split(" ", 1)[1], "header"
    cookie_token = request.cookies.get(AUTH_COOKIE_NAME)
    if cookie_token:
        return cookie_token, "cookie"
    return None, None


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token, source = _extract_token()
        if not token:
            return jsonify({"error": "Unauthorized"}), 401

        # V-045: cookie-auth callers must prove they can read the CSRF cookie
        # on mutating requests (double-submit). Bearer-header callers are
        # exempt because bearer tokens don't auto-attach cross-origin.
        if source == "cookie" and request.method in CSRF_PROTECTED_METHODS:
            if not csrf_token_matches():
                return jsonify({"error": "CSRF token missing or invalid"}), 403

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
                    "SELECT role, is_active, warehouse_ids, password_changed_at "
                    "FROM users WHERE user_id = :uid"
                ),
                {"uid": payload["user_id"]},
            ).fetchone()
        finally:
            db.close()

        if not row or not row.is_active:
            return jsonify({"error": "Unauthorized"}), 401

        # Reject tokens issued before the last password change
        if row.password_changed_at and payload.get("iat"):
            changed_ts = int(row.password_changed_at.timestamp())
            if payload["iat"] < changed_ts:
                return jsonify({"error": "Token invalidated by password change"}), 401

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


def warehouse_scope_clause(column: str = "warehouse_id") -> tuple[str, dict]:
    """Return (SQL fragment, params) that scopes a query to the user's warehouses.

    Call this when building a SELECT whose existence must not leak across
    warehouse boundaries. For non-admin users, the fragment ``AND col = ANY(:_wscope)``
    is returned along with the matching parameter binding. For admins,
    an empty fragment and no params are returned (admins see all).

    Prefer this over ``check_warehouse_access`` when the concern is
    avoiding an existence oracle -- filtering in SQL means "does not
    exist" and "exists in a different warehouse" produce the same empty
    result set and therefore the same 404. See V-026.

    Args:
        column: SQL expression (with optional table alias) for the
                warehouse_id column, e.g. ``"po.warehouse_id"``.

    Returns:
        (fragment, params). Fragment is either an empty string or
        "AND <column> = ANY(:_wscope)". Params is either {} or
        {"_wscope": [warehouse_ids]}.
    """
    user = g.current_user
    if user.get("role") == "ADMIN":
        return "", {}
    allowed = list(user.get("warehouse_ids") or [])
    return f"AND {column} = ANY(:_wscope)", {"_wscope": allowed}


def require_role(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if g.current_user["role"] not in roles:
                return jsonify({"error": "Forbidden"}), 403
            return f(*args, **kwargs)

        return decorated

    return decorator
