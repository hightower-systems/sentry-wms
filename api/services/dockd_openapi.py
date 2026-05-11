"""Generator for the v1.9.0 dockd OpenAPI 3.1 spec.

The spec describes the three /api/v1/dockd/* endpoints. Generated
from the Pydantic body models (schemas.dockd.ShipBody, VoidShipBody)
and a hand-rolled response shape catalog so the on-disk file at
docs/api/dockd-openapi.yaml stays in lock-step with the actual
handlers.

Rolled by hand rather than via apispec for the same reasons as
services.inbound_openapi: the surface is small (3 paths + 2 bodies +
~10 response codes) and a custom generator avoids pulling another
dependency. The test_committed_dockd_openapi_matches_live parity
test is the regression net.
"""

from typing import Any, Dict

from schemas.dockd import ShipBody, VoidShipBody


_DRAFT_HEADER = {
    "X-Sentry-Canonical-Model": {
        "description": (
            "Always set to DRAFT-v1 in v1.9.0. Indicates the dockd "
            "contract may break at v2.0 alongside the inbound canonical "
            "model lock."
        ),
        "schema": {"type": "string", "enum": ["DRAFT-v1"]},
    }
}


_REPLAY_HEADER = {
    "X-Idempotent-Replay": {
        "description": (
            "Set to 'true' when the response is a cached replay of a "
            "prior successful POST with the same idempotency_key. The "
            "handler did not re-execute the ship / void logic."
        ),
        "schema": {"type": "string", "enum": ["true"]},
    }
}


_ERROR_BODY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "required": ["error_kind", "message"],
    "properties": {
        "error_kind": {"type": "string"},
        "message": {"type": "string"},
        "details": {"type": "object"},
    },
}


def _err_response(description: str, with_retry_after: bool = False) -> Dict[str, Any]:
    headers = dict(_DRAFT_HEADER)
    if with_retry_after:
        headers["Retry-After"] = {
            "description": "Suggested seconds before retry.",
            "schema": {"type": "integer"},
        }
    return {
        "description": description,
        "headers": headers,
        "content": {
            "application/json": {"schema": _ERROR_BODY_SCHEMA},
        },
    }


_ADDRESS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "name", "line1", "line2", "city", "state",
        "postal_code", "country", "phone",
    ],
    "properties": {
        "name":        {"type": ["string", "null"]},
        "line1":       {"type": ["string", "null"]},
        "line2":       {"type": ["string", "null"]},
        "city":        {"type": ["string", "null"]},
        "state":       {"type": ["string", "null"]},
        "postal_code": {"type": ["string", "null"]},
        "country":     {"type": ["string", "null"]},
        "phone":       {"type": ["string", "null"]},
    },
}


_GET_ORDER_ITEM_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["external_id", "sku", "display_name", "upc", "qty"],
    "properties": {
        "external_id":  {"type": "string", "format": "uuid"},
        "sku":          {"type": "string"},
        "display_name": {"type": "string"},
        "upc":          {"type": ["string", "null"]},
        "qty":          {"type": "integer"},
    },
}


_GET_ORDER_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "so_number", "external_id", "status", "warehouse_id",
        "customer_name", "customer_phone", "memo", "shipping_address",
        "ship_method", "items", "order_total", "customer_shipping_paid",
        "marketplace", "order_date", "ff_created_at", "shippable",
        "shippable_from_statuses", "shipped_by", "tracking_number",
        "carrier", "shipped_at", "station_label",
    ],
    "properties": {
        "so_number":               {"type": "string"},
        "external_id":             {"type": "string", "format": "uuid"},
        "status":                  {"type": "string"},
        "warehouse_id":            {"type": "integer"},
        "customer_name":           {"type": ["string", "null"]},
        "customer_phone":          {"type": ["string", "null"]},
        "memo":                    {"type": ["string", "null"]},
        "shipping_address":        _ADDRESS_SCHEMA,
        "ship_method":             {"type": ["string", "null"]},
        "items": {
            "type": "array",
            "items": _GET_ORDER_ITEM_SCHEMA,
        },
        "order_total":             {"type": ["number", "null"]},
        "customer_shipping_paid":  {"type": ["number", "null"]},
        "marketplace":             {"type": ["string", "null"]},
        "order_date":              {"type": ["string", "null"], "format": "date-time"},
        "ff_created_at":           {"type": ["string", "null"], "format": "date-time"},
        "shippable":               {"type": "boolean"},
        "shippable_from_statuses": {
            "type": "array",
            "items": {"type": "string"},
        },
        "shipped_by":              {"type": ["string", "null"]},
        "tracking_number":         {"type": ["string", "null"]},
        "carrier":                 {"type": ["string", "null"]},
        "shipped_at":              {"type": ["string", "null"], "format": "date-time"},
        "station_label":           {"type": ["string", "null"]},
    },
}


_SHIP_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "tracking", "shipped_at", "fulfillment_id", "audit_log_id"],
    "properties": {
        "status":         {"type": "string", "enum": ["SHIPPED"]},
        "tracking":       {"type": "string"},
        "shipped_at":     {"type": "string", "format": "date-time"},
        "fulfillment_id": {"type": "integer"},
        "audit_log_id":   {"type": "integer"},
    },
}


_VOID_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "voided_at", "audit_log_id"],
    "properties": {
        "status":       {"type": "string", "enum": ["PICKED", "PACKED"]},
        "voided_at":    {"type": "string", "format": "date-time"},
        "audit_log_id": {"type": "integer"},
    },
}


def _ok_response(description: str, schema: Dict[str, Any], with_replay: bool = False) -> Dict[str, Any]:
    headers = dict(_DRAFT_HEADER)
    if with_replay:
        headers.update(_REPLAY_HEADER)
    return {
        "description": description,
        "headers": headers,
        "content": {
            "application/json": {"schema": schema},
        },
    }


def build_dockd_openapi() -> Dict[str, Any]:
    """Returns the full OpenAPI 3.1 document for the v1.9.0 dockd
    surface as a Python dict. yaml.safe_dump output is what gets
    committed at docs/api/dockd-openapi.yaml."""
    ship_body = ShipBody.model_json_schema()
    void_body = VoidShipBody.model_json_schema()

    paths: Dict[str, Any] = {
        "/api/v1/dockd/orders/{so_number}": {
            "get": {
                "summary": "Load an order for a dockd station",
                "description": (
                    "The load-on-scan call. Returns the order shape "
                    "dockd's UI needs to render either the 'ready to "
                    "ship' or the 'already shipped, want to void?' "
                    "branch. Warehouse scope is enforced at SELECT "
                    "time; an order outside the token's warehouse_ids "
                    "returns 404 not_found, identical to a genuinely "
                    "missing order."
                ),
                "operationId": "get_dockd_order",
                "tags": ["dockd"],
                "security": [{"WmsToken": []}],
                "parameters": [
                    {
                        "name": "so_number",
                        "in": "path",
                        "required": True,
                        "schema": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                            "pattern": r"^[A-Za-z0-9_\-#.]+$",
                        },
                    }
                ],
                "responses": {
                    "200": _ok_response("Order detail.", _GET_ORDER_RESPONSE_SCHEMA),
                    "401": _err_response("Missing / invalid X-WMS-Token."),
                    "403": _err_response(
                        "Scope violation. error_kind one of "
                        "cross_direction_scope_violation, "
                        "endpoint_scope_violation."
                    ),
                    "404": _err_response(
                        "Order not found OR outside the token's warehouse "
                        "scope. The two cases share a body to prevent "
                        "enumeration via 404-vs-403 inference."
                    ),
                    "422": _err_response(
                        "Path-parameter validation failure (so_number "
                        "regex / length)."
                    ),
                    "429": _err_response("Per-token rate limit exceeded."),
                },
            }
        },
        "/api/v1/dockd/orders/{so_number}/ship": {
            "post": {
                "summary": "Record a successful ship",
                "description": (
                    "Updates sales_orders to SHIPPED, inserts an "
                    "item_fulfillments row, writes audit, emits "
                    "ship.confirmed/1 to the outbox. HTTP-layer "
                    "idempotency on (token_id, idempotency_key); 72h "
                    "cache window. Replays return 200 with header "
                    "X-Idempotent-Replay: true and do not re-execute. "
                    "source_txn_id on the outbox row equals the "
                    "request's idempotency_key, tying outbox dedup to "
                    "HTTP dedup."
                ),
                "operationId": "post_dockd_ship",
                "tags": ["dockd"],
                "security": [{"WmsToken": []}],
                "parameters": [
                    {
                        "name": "so_number",
                        "in": "path",
                        "required": True,
                        "schema": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                            "pattern": r"^[A-Za-z0-9_\-#.]+$",
                        },
                    }
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ShipBody"},
                        }
                    },
                },
                "responses": {
                    "200": _ok_response(
                        "Ship recorded OR cached replay (replay carries "
                        "X-Idempotent-Replay: true).",
                        _SHIP_RESPONSE_SCHEMA,
                        with_replay=True,
                    ),
                    "401": _err_response("Missing / invalid X-WMS-Token."),
                    "403": _err_response("Scope violation."),
                    "404": _err_response("Order not found OR outside warehouse scope."),
                    "409": _err_response(
                        "Conflict. error_kind one of: already_shipped "
                        "(SO is already SHIPPED, details carry "
                        "existing_tracking / carrier / shipped_at / "
                        "shipped_by), idempotency_key_reused_with_different_body."
                    ),
                    "410": _err_response(
                        "not_in_shippable_status. Details carry "
                        "current_status and allowed_statuses."
                    ),
                    "413": _err_response("body_too_large; SENTRY_DOCKD_MAX_BODY_KB."),
                    "422": _err_response(
                        "invalid_body (Pydantic), invalid_so_number "
                        "(path regex), or unknown_operator "
                        "(operator_username does not resolve)."
                    ),
                    "429": _err_response("Per-token rate limit exceeded."),
                    "503": _err_response(
                        "idempotency_lock_timeout. A concurrent ship "
                        "with the same key blocked longer than the 5s "
                        "lock_timeout. dockd should back off >=250ms "
                        "and retry with the same key.",
                        with_retry_after=True,
                    ),
                },
            }
        },
        "/api/v1/dockd/orders/{so_number}/void-ship": {
            "post": {
                "summary": "Reverse a previously-successful ship",
                "description": (
                    "Reverts sales_orders to its pre_ship_status (PICKED "
                    "or PACKED) and clears tracking / carrier / "
                    "shipped_at, marks the SHIPPED item_fulfillments row "
                    "VOIDED with operator + reason + timestamp, rolls "
                    "back sales_order_lines.quantity_shipped + status "
                    "for re-ship symmetry, writes audit, emits "
                    "ship.voided/1 with source_txn_id = idempotency_key. "
                    "Same idempotency contract as POST /ship."
                ),
                "operationId": "post_dockd_void_ship",
                "tags": ["dockd"],
                "security": [{"WmsToken": []}],
                "parameters": [
                    {
                        "name": "so_number",
                        "in": "path",
                        "required": True,
                        "schema": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                            "pattern": r"^[A-Za-z0-9_\-#.]+$",
                        },
                    }
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/VoidShipBody"},
                        }
                    },
                },
                "responses": {
                    "200": _ok_response(
                        "Void recorded OR cached replay.",
                        _VOID_RESPONSE_SCHEMA,
                        with_replay=True,
                    ),
                    "401": _err_response("Missing / invalid X-WMS-Token."),
                    "403": _err_response("Scope violation."),
                    "404": _err_response("Order not found OR outside warehouse scope."),
                    "409": _err_response(
                        "Conflict. error_kind one of: not_shipped (SO is "
                        "not SHIPPED), "
                        "idempotency_key_reused_with_different_body."
                    ),
                    "413": _err_response("body_too_large."),
                    "422": _err_response(
                        "invalid_body, invalid_so_number, or "
                        "unknown_operator."
                    ),
                    "429": _err_response("Per-token rate limit exceeded."),
                    "503": _err_response(
                        "idempotency_lock_timeout.", with_retry_after=True,
                    ),
                },
            }
        },
    }

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "SentryWMS v1.9.0 Dockd",
            "version": "1.9.0",
            "description": (
                "Per-station shipping API for the dockd integration. "
                "Three endpoints: GET /orders/<so_number> for load-on-"
                "scan, POST /ship to record a ship, POST /void-ship to "
                "reverse one. v1.9 ships the contract as DRAFT; "
                "X-Sentry-Canonical-Model: DRAFT-v1 header on every "
                "response."
            ),
        },
        "tags": [{"name": "dockd", "description": "v1.9.0 dockd shipping surface"}],
        "components": {
            "securitySchemes": {
                "WmsToken": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-WMS-Token",
                    "description": (
                        "Per-station bearer token. Must carry "
                        "endpoints=['dockd.dispatch'] and MUST NOT "
                        "carry source_system / inbound_resources / "
                        "event_types (mixed-direction tokens are "
                        "rejected at the dispatcher with "
                        "cross_direction_scope_violation)."
                    ),
                }
            },
            "schemas": {
                "ShipBody": ship_body,
                "VoidShipBody": void_body,
            },
        },
        "paths": paths,
    }
