"""
Auth endpoints: login and token refresh.
"""

from flask import Blueprint, g, jsonify, request

from middleware.auth_middleware import require_auth
from models.database import get_db
from services.auth_service import authenticate_user, generate_token

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["POST"])
def login():
    data = request.get_json()
    if not data or not data.get("username") or not data.get("password"):
        return jsonify({"error": "Username and password are required"}), 400

    db = next(get_db())
    try:
        user = authenticate_user(db, data["username"], data["password"])
    finally:
        db.close()

    if not user:
        return jsonify({"error": "Invalid username or password"}), 401

    token = generate_token(user)
    return jsonify({"token": token, "user": user})


@auth_bp.route("/refresh", methods=["POST"])
@require_auth
def refresh():
    token = generate_token(g.current_user)
    return jsonify({"token": token})
