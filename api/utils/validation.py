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


def validate_body(schema_class):
    """Validate the JSON request body against a pydantic model.

    On success, passes the validated model instance as ``validated`` kwarg.
    On failure, returns a 400 response with ``error: "validation_error"``
    and a ``details`` array of field-level errors.
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            try:
                data = schema_class(**(request.get_json(silent=True) or {}))
            except ValidationError as e:
                return jsonify({
                    "error": "validation_error",
                    "details": _safe_errors(e),
                }), 400
            return f(*args, validated=data, **kwargs)
        return wrapped
    return decorator
