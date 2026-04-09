"""
User ORM model mapped to the existing users table.
"""

import bcrypt
from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.sql import func

from models.database import Base


class User(Base):
    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(100), nullable=False)
    role = Column(String(20), nullable=False, default="USER")
    warehouse_id = Column(Integer)
    warehouse_ids = Column(ARRAY(Integer), default=[])
    allowed_functions = Column(ARRAY(String), default=[])
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_login = Column(DateTime(timezone=True))

    def to_dict(self):
        return {
            "user_id": self.user_id,
            "username": self.username,
            "full_name": self.full_name,
            "role": self.role,
            "warehouse_id": self.warehouse_id,
            "warehouse_ids": list(self.warehouse_ids) if self.warehouse_ids else [],
            "allowed_functions": list(self.allowed_functions) if self.allowed_functions else [],
            "is_active": self.is_active,
        }

    def check_password(self, password):
        return bcrypt.checkpw(
            password.encode("utf-8"), self.password_hash.encode("utf-8")
        )
