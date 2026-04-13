"""
Authentication business logic: login, JWT generation, and token decoding.
"""

import os
from datetime import datetime, timezone, timedelta

import jwt

from models.user import User

JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET environment variable is required")
JWT_ALGORITHM = "HS256"
TOKEN_EXPIRY_HOURS = 24


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
    payload = {
        "user_id": user_dict["user_id"],
        "username": user_dict["username"],
        "role": user_dict["role"],
        "warehouse_id": user_dict["warehouse_id"],
        "warehouse_ids": user_dict.get("warehouse_ids", []),
        "exp": datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token):
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
