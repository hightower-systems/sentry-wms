"""
Authentication business logic: login, JWT generation, and token decoding.
"""

import os
import uuid
from datetime import datetime, timezone, timedelta

import jwt

from models.user import User

JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET environment variable is required")
JWT_ALGORITHM = "HS256"
TOKEN_EXPIRY_HOURS = 8


def validate_password(password):
    """Return an error message if the password is invalid, or None if valid."""
    # Reject the literal string "admin" (case-insensitive, whitespace-stripped).
    # Covers "admin", "ADMIN", "Admin", "aDmIn", " admin ", "\tadmin\n", etc.
    # Checked before length so the error is specific instead of the generic
    # length message. Matters most after the v1.4.1 forced-password-change
    # flow, where the default seed credential is literally "admin" and we
    # must block users from "changing" back to it.
    if password.strip().lower() == "admin":
        return "Password cannot be 'admin'"
    if len(password) < 8:
        return "Password must be at least 8 characters"
    if not any(c.isalpha() for c in password):
        return "Password must contain at least one letter"
    if not any(c.isdigit() for c in password):
        return "Password must contain at least one digit"
    return None


def authenticate_user(db_session, username, password):
    user = (
        db_session.query(User)
        .filter(User.username == username, User.is_active == True)
        .first()
    )
    if not user or not user.check_password(password):
        return None

    user.last_login = datetime.now(timezone.utc)
    db_session.commit()
    return user.to_dict()


def generate_token(user_dict):
    now = datetime.now(timezone.utc)
    payload = {
        "user_id": user_dict["user_id"],
        "username": user_dict["username"],
        "role": user_dict["role"],
        "warehouse_id": user_dict["warehouse_id"],
        "warehouse_ids": user_dict.get("warehouse_ids", []),
        "iat": int(now.timestamp()),
        "jti": str(uuid.uuid4()),
        "exp": now + timedelta(hours=TOKEN_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token):
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
