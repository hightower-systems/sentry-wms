"""
Auth endpoints: login and token refresh.
"""

from flask import Blueprint, g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_auth
from middleware.db import with_db
from services.auth_service import authenticate_user, generate_token

ALL_FUNCTIONS = ["receive", "putaway", "pick", "pack", "ship", "count", "transfer"]

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["POST"])
@with_db
def login():
    data = request.get_json()
    if not data or not data.get("username") or not data.get("password"):
        return jsonify({"error": "Username and password are required"}), 400

    user = authenticate_user(g.db, data["username"], data["password"])

    if not user:
        return jsonify({"error": "Invalid username or password"}), 401

    token = generate_token(user)
    return jsonify({"token": token, "user": user})


@auth_bp.route("/me")
@require_auth
@with_db
def me():
    user_id = g.current_user["user_id"]
    row = g.db.execute(
        text("SELECT user_id, username, full_name, role, warehouse_id, allowed_functions FROM users WHERE user_id = :uid"),
        {"uid": user_id},
    ).fetchone()
    if not row:
        return jsonify({"error": "User not found"}), 404

    if row.role == "ADMIN":
        functions = list(ALL_FUNCTIONS)
    else:
        functions = list(row.allowed_functions) if row.allowed_functions else []

    # Check packing toggle — filter out "pack" when packing is disabled
    packing_row = g.db.execute(
        text("SELECT value FROM app_settings WHERE key = 'require_packing_before_shipping'")
    ).fetchone()
    require_packing = not packing_row or packing_row.value != "false"

    if not require_packing:
        functions = [f for f in functions if f != "pack"]

    return jsonify({
        "user_id": row.user_id,
        "username": row.username,
        "full_name": row.full_name,
        "role": row.role,
        "warehouse_id": row.warehouse_id,
        "allowed_functions": functions,
        "require_packing": require_packing,
    })


@auth_bp.route("/refresh", methods=["POST"])
@require_auth
def refresh():
    token = generate_token(g.current_user)
    return jsonify({"token": token})
