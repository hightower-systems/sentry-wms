"""Request body validation decorator using pydantic v2."""

from functools import wraps

from flask import jsonify, request
from pydantic import ValidationError


def _safe_errors(exc):
    """Extract JSON-serializable error details from a pydantic ValidationError."""
    result = []
    for err in exc.errors():
        result.append({
            "type": err["type"],
            "loc": list(err["loc"]),
            "msg": err["msg"],
        })
    return result


# Keys the auth middleware reads from the request body (warehouse scoping)
# regardless of the route's schema. They must always pass the extras gate
# so the middleware-owned check in require_auth still works even when a
# schema has no warehouse_id field. V-033 will remove the need for this
# by teaching the middleware to read path args instead; until then we
# allowlist it at the decorator boundary.
_ALWAYS_ALLOWED_KEYS = frozenset({"warehouse_id"})


def _allowed_field_names(schema_class) -> set:
    """Return every name a caller may legitimately use for a schema field.

    Includes the canonical field name plus any alias or validation_alias.
    Used to detect mass-assignment-style extras at the decorator boundary.
    """
    names = set(_ALWAYS_ALLOWED_KEYS)
    for name, field in schema_class.model_fields.items():
        names.add(name)
        if field.alias:
            names.add(field.alias)
        if field.validation_alias and isinstance(field.validation_alias, str):
            names.add(field.validation_alias)
    return names


def validate_body(schema_class):
    """Validate the JSON request body against a pydantic model.

    On success, passes the validated model instance as ``validated`` kwarg.
    On failure, returns a 400 response with ``error: "validation_error"``
    and a ``details`` array of field-level errors.

    V-017: unknown top-level fields are rejected (extra="forbid" semantics
    applied at the decorator level so every request schema gets this by
    default without opting in). CSV-row schemas still use extra="ignore"
    internally because they consume vendor spreadsheets with exotic
    columns; those do not flow through validate_body.
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            # V-016: require application/json on request bodies so that a
            # future permissive parser slip (e.g. force=True) cannot let a
            # text/plain or form-encoded payload through. mimetype strips
            # any charset parameter so "application/json; charset=utf-8"
            # is still accepted.
            if request.method in ("POST", "PUT", "PATCH"):
                if request.mimetype != "application/json":
                    return jsonify({
                        "error": "unsupported_media_type",
                        "message": "Content-Type must be application/json",
                    }), 415
            raw = request.get_json(silent=True) or {}
            if isinstance(raw, dict):
                allowed = _allowed_field_names(schema_class)
                extras = [k for k in raw.keys() if k not in allowed]
                if extras:
                    return jsonify({
                        "error": "validation_error",
                        "details": [
                            {
                                "type": "extra_forbidden",
                                "loc": [k],
                                "msg": f"Unknown field: {k}",
                            }
                            for k in extras
                        ],
                    }), 400
            try:
                data = schema_class(**raw)
            except ValidationError as e:
                return jsonify({
                    "error": "validation_error",
                    "details": _safe_errors(e),
                }), 400
            return f(*args, validated=data, **kwargs)
        return wrapped
    return decorator
