#!/usr/bin/env python3
"""Create idempotent local shipyard demo users.

This script is intentionally development/test-only. It refuses to run when
FLASK_ENV is production and must never be wired into production migrations.
"""

from __future__ import annotations

import os
import sys
import uuid

import bcrypt
from sqlalchemy import create_engine, text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from constants import (  # noqa: E402
    ROLE_ADMIN,
    ROLE_OUTBOUND,
    ROLE_RECEIVER,
    ROLE_SHIPYARD_MANAGER,
    ROLE_VIEWER,
    USER_ROLE_TEMPLATES,
)
from services.auth_service import validate_password  # noqa: E402


DEMO_USERS = (
    ("demo_shipyard_admin", "船厂演示管理员", ROLE_ADMIN),
    ("demo_shipyard_manager", "船厂演示主管", ROLE_SHIPYARD_MANAGER),
    ("demo_shipyard_receiver", "船厂演示收货员", ROLE_RECEIVER),
    ("demo_shipyard_outbound", "船厂演示出库员", ROLE_OUTBOUND),
    ("demo_shipyard_viewer", "船厂演示审计员", ROLE_VIEWER),
)


def _warehouse_ids() -> list[int]:
    raw = os.getenv("SHIPYARD_DEMO_WAREHOUSE_IDS", "1")
    try:
        warehouse_ids = [
            int(value.strip()) for value in raw.split(",") if value.strip()
        ]
    except ValueError as exc:
        raise SystemExit(
            "SHIPYARD_DEMO_WAREHOUSE_IDS must be comma-separated integers"
        ) from exc
    if not warehouse_ids:
        raise SystemExit("At least one demo warehouse ID is required")
    return warehouse_ids


def _password() -> str:
    password = os.getenv("SHIPYARD_DEMO_PASSWORD", "ShipyardDemo2026!")
    error = validate_password(password)
    if error:
        raise SystemExit(f"Invalid SHIPYARD_DEMO_PASSWORD: {error}")
    return password


def seed() -> None:
    if os.getenv("FLASK_ENV", "").lower() == "production":
        raise SystemExit("Refusing to create demo users in production")
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")

    warehouse_ids = _warehouse_ids()
    password_hash = bcrypt.hashpw(
        _password().encode("utf-8"),
        bcrypt.gensalt(),
    ).decode("utf-8")
    engine = create_engine(database_url)
    with engine.begin() as db:
        admin_user_id = None
        for username, full_name, role in DEMO_USERS:
            template = USER_ROLE_TEMPLATES[role]
            user_id = db.execute(
                text(
                    """
                    INSERT INTO users
                        (username, password_hash, full_name, role,
                         warehouse_id, warehouse_ids, allowed_functions,
                         is_active, must_change_password, external_id)
                    VALUES
                        (:username, :password_hash, :full_name, :role,
                         :warehouse_id, :warehouse_ids, :functions,
                         TRUE, FALSE, :external_id)
                    ON CONFLICT (username) DO UPDATE
                       SET password_hash = EXCLUDED.password_hash,
                           full_name = EXCLUDED.full_name,
                           role = EXCLUDED.role,
                           warehouse_id = EXCLUDED.warehouse_id,
                           warehouse_ids = EXCLUDED.warehouse_ids,
                           allowed_functions = EXCLUDED.allowed_functions,
                           is_active = TRUE,
                           must_change_password = FALSE
                    RETURNING user_id
                    """
                ),
                {
                    "username": username,
                    "password_hash": password_hash,
                    "full_name": full_name,
                    "role": role,
                    "warehouse_id": warehouse_ids[0],
                    "warehouse_ids": warehouse_ids,
                    "functions": list(template["functions"]),
                    "external_id": str(uuid.uuid4()),
                },
            ).scalar_one()
            if role == ROLE_ADMIN:
                admin_user_id = user_id

            db.execute(
                text("DELETE FROM user_page_permissions WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            if role != ROLE_ADMIN:
                db.execute(
                    text(
                        """
                        INSERT INTO user_page_permissions
                            (user_id, page_key, granted_by)
                        VALUES
                            (:user_id, :page_key, :granted_by)
                        """
                    ),
                    [
                        {
                            "user_id": user_id,
                            "page_key": page_key,
                            "granted_by": admin_user_id,
                        }
                        for page_key in template["page_keys"]
                    ],
                )
            print(f"upserted {username} ({role})")

    engine.dispose()


if __name__ == "__main__":
    seed()
