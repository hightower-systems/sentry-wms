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


from routes.admin import admin_warehouse, admin_items, admin_orders, admin_users, admin_connectors  # noqa: E402, F401
