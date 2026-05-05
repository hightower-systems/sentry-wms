"""v1.7.0 Pipe B inbound surface.

Endpoints:

    GET  /api/v1/inbound/mapping-schema   -- documentation aid (unauthed)
    POST /api/v1/inbound/sales_orders     -- v1.7 first resource

The four remaining resource endpoints (items / customers / vendors /
purchase_orders) land in subsequent commits and reuse the same
register_inbound_resource() helper below.

Per-request shape:
- @require_wms_token: validates X-WMS-Token, refuses cross-direction
  bridging (V-200 + v1.7 cross_direction_scope_violation), checks the
  resource is in the token's inbound_resources scope.
- 413 Payload Too Large at request boundary if Content-Length exceeds
  SENTRY_INBOUND_MAX_BODY_KB (16-4096 KB; default 256).
- 422 on Pydantic body validation failure (extra='forbid' rejects
  typos at the wire).
- handle_inbound() in services.inbound_service runs the 10-step flow
  and returns a HandlerResult; the wrapper serialises into Flask
  responses with the X-Sentry-Canonical-Model: DRAFT-v1 header on
  every response (success or failure).
"""

from flask import Blueprint, current_app, g, jsonify, make_response, request
from psycopg2.errors import IntegrityError as _PsycopgIntegrityError
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError as _SAIntegrityError

from middleware.auth_middleware import require_wms_token
from middleware.db import with_db
from schemas.inbound import InboundBody
from services.inbound_service import (
    HandlerError,
    HandlerOK,
    get_max_body_kb,
    handle_inbound,
)
from services.mapping_loader import MappingDocument
from services.rate_limit import limiter


inbound_bp = Blueprint("inbound", __name__)


# ----------------------------------------------------------------------
# GET /api/v1/inbound/mapping-schema (documentation aid; unauthed)
# ----------------------------------------------------------------------


@inbound_bp.route("/mapping-schema", methods=["GET"])
def mapping_schema():
    response = make_response(jsonify(build_mapping_schema()))
    response.headers["Cache-Control"] = "public, max-age=300"
    response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
    return response


def build_mapping_schema() -> dict:
    """Returns the JSON Schema for the mapping document. Used by the
    route above and by the schema-regeneration script
    (tools/scripts/regenerate-mapping-schema.py) so the wire-served
    version and the committed file at
    docs/api/mapping-document-schema.json cannot drift."""
    schema = MappingDocument.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "SentryWMS Inbound Mapping Document"
    return schema


# ----------------------------------------------------------------------
# POST handler shared by every resource route
# ----------------------------------------------------------------------


def _serialise(result) -> "Response":
    """Translate HandlerResult into a Flask response. Always carries the
    DRAFT-v1 header so consumers can detect the schema-stability stage
    on every response, including 4xx."""
    if isinstance(result, HandlerOK):
        body = jsonify(result.body)
        response = make_response(body, result.status_code)
    elif isinstance(result, HandlerError):
        body = jsonify(result.body)
        response = make_response(body, result.status_code)
        if result.headers:
            for k, v in result.headers.items():
                response.headers[k] = v
    else:
        raise RuntimeError(f"unknown handler result: {result!r}")
    response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
    return response


def _resource_post(resource_key: str):
    """Returns a Flask handler for POST /api/v1/inbound/<resource_key>.

    Body-size cap, Pydantic validation, transaction commit/rollback,
    and the DRAFT-v1 header live here so each per-resource route below
    is just a one-liner registering the right URL and Flask endpoint
    name (Flask endpoint name is what V170_INBOUND_RESOURCE_BY_ENDPOINT
    in the decorator dispatches on)."""

    def handler():
        cap_bytes = get_max_body_kb() * 1024
        if request.content_length is not None and request.content_length > cap_bytes:
            response = make_response(
                jsonify({
                    "error_kind": "body_too_large",
                    "max_body_kb": get_max_body_kb(),
                }),
                413,
            )
            response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
            return response

        try:
            body = InboundBody.model_validate(request.get_json(silent=False))
        except ValidationError as exc:
            response = make_response(
                jsonify({
                    "error_kind": "validation_error",
                    "details": exc.errors(include_url=False),
                }),
                422,
            )
            response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
            return response

        registry = current_app.config.get("MAPPING_REGISTRY")
        try:
            result = handle_inbound(
                db=g.db,
                resource_key=resource_key,
                body=body.model_dump(),
                token=g.current_token,
                registry=registry,
                source_txn_id=getattr(g, "source_txn_id", None),
            )
        except _SAIntegrityError as exc:
            g.db.rollback()
            response = make_response(
                jsonify({
                    "error_kind": "canonical_constraint_violation",
                    "message": _trim(str(exc.orig if exc.orig else exc)),
                }),
                422,
            )
            response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
            return response
        except _PsycopgIntegrityError as exc:
            g.db.rollback()
            response = make_response(
                jsonify({
                    "error_kind": "canonical_constraint_violation",
                    "message": _trim(str(exc)),
                }),
                422,
            )
            response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
            return response
        except Exception:
            g.db.rollback()
            raise

        if isinstance(result, HandlerOK):
            g.db.commit()
        else:
            g.db.rollback()
        return _serialise(result)

    return handler


def _trim(msg: str, n: int = 400) -> str:
    s = msg.replace("\n", " ").strip()
    return s if len(s) <= n else s[:n] + "..."


def register_inbound_resource(resource_key: str, endpoint_suffix: str) -> None:
    """Register POST /api/v1/inbound/<resource_key> with the wms_token
    decorator + per-token rate limit. Future per-resource commits add
    one line here per resource."""
    handler = _resource_post(resource_key)
    handler = with_db(handler)
    handler = require_wms_token(handler)
    handler = limiter.limit("100 per minute")(handler)
    inbound_bp.add_url_rule(
        f"/{resource_key}",
        endpoint=f"post_{endpoint_suffix}",
        view_func=handler,
        methods=["POST"],
    )


register_inbound_resource("sales_orders", "sales_orders")
register_inbound_resource("items", "items")
register_inbound_resource("customers", "customers")
