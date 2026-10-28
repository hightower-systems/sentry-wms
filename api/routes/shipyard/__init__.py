"""Shipyard WMS API routes."""

import logging
from functools import wraps

from flask import Blueprint, g, jsonify
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from services.shipyard.errors import ShipyardServiceError

shipyard_bp = Blueprint("shipyard", __name__)
_LOGGER = logging.getLogger(__name__)


def require_shipyard_roles(*roles):
    """Allow the listed business roles, with ADMIN retaining full access."""

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            current_user = getattr(g, "current_user", {})
            role = current_user.get("role")
            if role == "ADMIN" or role in roles:
                return view(*args, **kwargs)
            return jsonify(
                {
                    "error": "role_forbidden",
                    "required_roles": list(roles),
                }
            ), 403

        return wrapped

    return decorator


@shipyard_bp.errorhandler(ShipyardServiceError)
def _service_error(exc):
    body = {
        "error": exc.code,
        "message": exc.message,
    }
    body.update(exc.details)
    return jsonify(body), exc.status_code


@shipyard_bp.errorhandler(IntegrityError)
def _integrity_error(exc):
    try:
        if g.db.is_active:
            g.db.rollback()
    except SQLAlchemyError:
        _LOGGER.warning("Failed to roll back shipyard integrity error", exc_info=True)
    diagnostic = getattr(getattr(exc, "orig", None), "diag", None)
    constraint = getattr(diagnostic, "constraint_name", None) if diagnostic else None
    body = {"error": "shipyard_conflict"}
    if constraint:
        body["constraint"] = constraint
    return jsonify(body), 409


@shipyard_bp.errorhandler(SQLAlchemyError)
def _database_error(exc):
    try:
        if g.db.is_active:
            g.db.rollback()
    except SQLAlchemyError:
        _LOGGER.warning("Failed to roll back shipyard database error", exc_info=True)
    _LOGGER.error(
        "Shipyard database operation failed",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return jsonify({"error": "shipyard_database_unavailable"}), 503


from routes.shipyard import (  # noqa: F401
    config,
    formal_outbound,
    inbound,
    outbound,
    receipts,
)
