"""
Admin CRUD endpoints for the web admin panel.
Covers warehouses, zones, bins, items, POs, SOs, users, audit log,
inventory overview, CSV import, and dashboard stats.
"""

from flask import Blueprint

admin_bp = Blueprint("admin", __name__)

VALID_ZONE_TYPES = ("RECEIVING", "STORAGE", "PICKING", "STAGING", "SHIPPING")
VALID_BIN_TYPES = ("Staging", "PickableStaging", "Pickable")
VALID_ROLES = ("ADMIN", "MANAGER", "PICKER", "RECEIVER", "PACKER")


def _paginate(query_base, count_base, params, page, per_page):
    """Add pagination to a query. Returns (rows, total, pages)."""
    total = None
    pages = 1
    offset = (page - 1) * per_page
    params["limit"] = per_page
    params["offset"] = offset
    return per_page, offset


from routes.admin import admin_warehouse, admin_items, admin_orders, admin_users  # noqa: E402, F401
