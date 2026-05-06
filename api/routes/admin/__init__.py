"""
Admin CRUD endpoints for the web admin panel.
Covers warehouses, zones, bins, items, POs, SOs, users, audit log,
inventory overview, CSV import, and dashboard stats.
"""

import logging

from flask import Blueprint, g, jsonify
from sqlalchemy.exc import IntegrityError

admin_bp = Blueprint("admin", __name__)

VALID_ZONE_TYPES = ("RECEIVING", "STORAGE", "PICKING", "STAGING", "SHIPPING")
VALID_BIN_TYPES = ("Staging", "PickableStaging", "Pickable")
VALID_ROLES = ("ADMIN", "USER")


_LOGGER = logging.getLogger(__name__)


@admin_bp.errorhandler(IntegrityError)
def _admin_integrity_error(exc):
    """#211 defense-in-depth: any IntegrityError that escapes a
    handler's local try/except surfaces here as a structured 409
    instead of leaking as a generic 500. The constraint name
    (when psycopg2 exposes it via diag) goes in the body so
    operators can map the failure to the schema directly."""
    try:
        g.db.rollback()
    except Exception:  # noqa: BLE001
        pass
    constraint = None
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    if diag is not None:
        constraint = getattr(diag, "constraint_name", None)
    _LOGGER.warning(
        "admin IntegrityError surfaced as 409: constraint=%s detail=%s",
        constraint,
        getattr(getattr(exc, "orig", None), "diag", None)
        and getattr(exc.orig.diag, "message_detail", None),
    )
    body = {"error": "integrity_constraint_violation"}
    if constraint:
        body["constraint"] = constraint
    return jsonify(body), 409


from routes.admin import (  # noqa: E402, F401
    admin_connectors,
    admin_consumer_groups,
    admin_inbound,
    admin_items,
    admin_orders,
    admin_search,
    admin_tokens,
    admin_users,
    admin_warehouse,
    admin_webhooks,
)
