"""
Auth endpoints: login and token refresh.
"""

import time
from collections import defaultdict

from flask import Blueprint, g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_auth
from middleware.db import with_db
from services.auth_service import authenticate_user, generate_token, validate_password

ALL_FUNCTIONS = ["receive", "putaway", "pick", "pack", "ship", "count", "transfer"]

MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60  # 15 minutes

# Per-username tracking: {username: {"attempts": int, "locked_until": float}}
_login_attempts = defaultdict(lambda: {"attempts": 0, "locked_until": 0})

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["POST"])
@with_db
def login():
    data = request.get_json()
    if not data or not data.get("username") or not data.get("password"):
        return jsonify({"error": "Username and password are required"}), 400

    username = data["username"].lower().strip()
    tracker = _login_attempts[username]
    now = time.time()

    # Check lockout
    if tracker["locked_until"] > now:
        remaining = int(tracker["locked_until"] - now)
        minutes = remaining // 60
        seconds = remaining % 60
        return jsonify({
            "error": f"Account locked. Try again in {minutes}m {seconds}s",
        }), 429

    user = authenticate_user(g.db, data["username"], data["password"])

    if not user:
        tracker["attempts"] += 1
        if tracker["attempts"] >= MAX_LOGIN_ATTEMPTS:
            tracker["locked_until"] = now + LOCKOUT_SECONDS
            tracker["attempts"] = 0
            return jsonify({
                "error": "Too many failed attempts. Account locked for 15 minutes",
            }), 429
        remaining = MAX_LOGIN_ATTEMPTS - tracker["attempts"]
        return jsonify({
            "error": f"Invalid username or password ({remaining} attempts remaining)",
        }), 401

    # Successful login - reset tracker
    _login_attempts.pop(username, None)
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

    # Check packing toggle  -  filter out "pack" when packing is disabled
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
@with_db
def refresh():
    # Re-validate user exists and is active before issuing new token
    row = g.db.execute(
        text("""SELECT user_id, username, full_name, role, warehouse_id, warehouse_ids, is_active
               FROM users WHERE user_id = :uid"""),
        {"uid": g.current_user["user_id"]},
    ).fetchone()
    if not row or not row.is_active:
        return jsonify({"error": "Account disabled or deleted"}), 401

    user_dict = {
        "user_id": row.user_id,
        "username": row.username,
        "full_name": row.full_name,
        "role": row.role,
        "warehouse_id": row.warehouse_id,
        "warehouse_ids": list(row.warehouse_ids) if row.warehouse_ids else [],
    }
    token = generate_token(user_dict)
    return jsonify({"token": token})


@auth_bp.route("/change-password", methods=["POST"])
@require_auth
@with_db
def change_password():
    import bcrypt

    data = request.get_json()
    if not data or not data.get("current_password") or not data.get("new_password"):
        return jsonify({"error": "current_password and new_password are required"}), 400

    pw_error = validate_password(data["new_password"])
    if pw_error:
        return jsonify({"error": pw_error}), 400

    user_id = g.current_user["user_id"]
    row = g.db.execute(
        text("SELECT password_hash FROM users WHERE user_id = :uid"),
        {"uid": user_id},
    ).fetchone()

    if not row or not bcrypt.checkpw(data["current_password"].encode("utf-8"), row.password_hash.encode("utf-8")):
        return jsonify({"error": "Current password is incorrect"}), 401

    new_hash = bcrypt.hashpw(data["new_password"].encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    g.db.execute(
        text("UPDATE users SET password_hash = :pw, password_changed_at = NOW() WHERE user_id = :uid"),
        {"pw": new_hash, "uid": user_id},
    )
    g.db.commit()

    return jsonify({"message": "Password changed"})
