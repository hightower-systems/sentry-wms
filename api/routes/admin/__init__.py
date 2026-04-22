"""
Admin CRUD endpoints for the web admin panel.
Covers warehouses, zones, bins, items, POs, SOs, users, audit log,
inventory overview, CSV import, and dashboard stats.
"""

from flask import Blueprint

admin_bp = Blueprint("admin", __name__)

VALID_ZONE_TYPES = ("RECEIVING", "STORAGE", "PICKING", "STAGING", "SHIPPING")
VALID_BIN_TYPES = ("Staging", "PickableStaging", "Pickable")
VALID_ROLES = ("ADMIN", "USER")


from routes.admin import (  # noqa: E402, F401
    admin_connectors,
    admin_items,
    admin_orders,
    admin_tokens,
    admin_users,
    admin_warehouse,
)
