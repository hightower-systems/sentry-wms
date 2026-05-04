"""v1.7.0 Pipe B inbound surface.

Currently exposes a single read-only documentation endpoint:

    GET /api/v1/inbound/mapping-schema

Returns the JSON Schema for the mapping-document format the loader
consumes (db/mappings/<source_system>.yaml). Generated from the
Pydantic models in services.mapping_loader via model_json_schema()
so the on-disk schema (committed at docs/api/mapping-document-schema.json)
and the wire-served schema cannot drift.

The endpoint is intentionally unauthenticated: it is a documentation
aid for connector authors writing mapping docs offline. The schema
is safe to expose -- it describes the expected shape of inputs the
inbound endpoints will accept, nothing about runtime state. Cache
header set so consumer tooling can fetch once.

Per-resource POST endpoints (sales_orders, items, customers, vendors,
purchase_orders) land in subsequent commits and reuse this blueprint.
"""

from flask import Blueprint, jsonify, make_response

from services.mapping_loader import MappingDocument


inbound_bp = Blueprint("inbound", __name__)


@inbound_bp.route("/mapping-schema", methods=["GET"])
def mapping_schema():
    response = make_response(jsonify(build_mapping_schema()))
    response.headers["Cache-Control"] = "public, max-age=300"
    response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
    return response


def build_mapping_schema() -> dict:
    """Returns the JSON Schema for the mapping document. Used by the route
    above and by the schema-regeneration script (tools/scripts/regenerate-mapping-schema.py)
    so the wire-served version and the committed file at
    docs/api/mapping-document-schema.json cannot drift.
    """
    schema = MappingDocument.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "SentryWMS Inbound Mapping Document"
    return schema
