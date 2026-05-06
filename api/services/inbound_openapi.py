"""Generator for the v1.7.0 inbound OpenAPI 3.1 spec.

The spec describes the five POST endpoints + the documentation-aid
mapping-schema GET. Generated from the Pydantic body model
(schemas.inbound.InboundBody) and a hand-rolled response shape
catalog so the on-disk file at docs/api/inbound-openapi.yaml stays
in lock-step with the actual handlers.

Rolled by hand rather than via apispec because the surface is small
(5 paths + 1 body + ~10 response codes) and a custom generator
avoids pulling in another dependency. The
test_committed_inbound_openapi_matches_live parity test is the
regression net: changes to InboundBody or the per-resource config
that aren't reflected on disk fail CI with a pointer to the
regenerator.
"""

from typing import Any, Dict

from schemas.inbound import InboundBody
from services.inbound_service import _CONFIGS


_DRAFT_HEADER = {
    "X-Sentry-Canonical-Model": {
        "description": (
            "Always set to DRAFT-v1 in v1.7.0. Indicates the canonical model "
            "may break at v2.0 once NetSuite validation drives schema lock."
        ),
        "schema": {"type": "string", "enum": ["DRAFT-v1"]},
    }
}


_RETRY_AFTER_HEADER = {
    "Retry-After": {
        "description": (
            "Seconds to wait before retrying a 409 lock_held response. "
            "Pinned to 1 in v1.7.0."
        ),
        "schema": {"type": "integer"},
    }
}


def _success_201_schema(canonical_type: str) -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "inbound_id", "canonical_id", "canonical_type",
            "received_at", "warning",
        ],
        "properties": {
            "inbound_id": {"type": "integer"},
            "canonical_id": {"type": "string", "format": "uuid"},
            "canonical_type": {"type": "string", "enum": [canonical_type]},
            "received_at": {"type": "string", "format": "date-time"},
            "warning": {
                "type": "string",
                "description": "DRAFT-v1 stability notice; canonical model "
                               "may break at v2.0.",
            },
        },
    }


_ERROR_RESPONSES: Dict[str, Dict[str, Any]] = {
    "200": {
        "description": (
            "Idempotent re-POST of the exact same "
            "(source_system, external_id, external_version) triple. Body "
            "identical to the original 201 response. No second write, "
            "no audit row, no event emission."
        ),
        "headers": {**_DRAFT_HEADER},
    },
    "403": {
        "description": (
            "Scope violation. error_kind one of: "
            "cross_direction_scope_violation (token tried to cross the "
            "inbound / outbound boundary), inbound_resource_scope_violation "
            "(token's inbound_resources does not list the target), "
            "mapping_override_capability_required (body carried "
            "mapping_overrides but token lacks the capability flag)."
        ),
        "headers": {**_DRAFT_HEADER},
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "error_kind": {"type": "string"},
                        "error": {"type": "string"},
                        "message": {"type": "string"},
                    },
                },
            }
        },
    },
    "409_stale": {
        "code": "409",
        "description": (
            "stale_version: incoming external_version is older than the "
            "server's current applied row per the source's "
            "version_compare strategy."
        ),
        "headers": {**_DRAFT_HEADER},
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["error_kind", "current_version"],
                    "properties": {
                        "error_kind": {"type": "string", "enum": ["stale_version"]},
                        "current_version": {"type": "string"},
                        "current_received_at": {"type": "string", "format": "date-time"},
                        "message": {"type": "string"},
                    },
                },
            }
        },
    },
    "413": {
        "description": (
            "body_too_large: Content-Length exceeds "
            "SENTRY_INBOUND_MAX_BODY_KB (16-4096 KB; default 256)."
        ),
        "headers": {**_DRAFT_HEADER},
    },
    "422": {
        "description": (
            "validation_error (Pydantic strict body fail) or "
            "canonical_constraint_violation (NOT NULL column not covered "
            "by the source's mapping doc field set)."
        ),
        "headers": {**_DRAFT_HEADER},
    },
    "503": {
        "description": (
            "mapping_document_not_loaded: the boot cross-check between "
            "inbound_source_systems_allowlist and db/mappings/ was bypassed. "
            "Restart with the matching YAML in place."
        ),
        "headers": {**_DRAFT_HEADER},
    },
}


def _resource_response_block(canonical_type: str) -> Dict[str, Any]:
    """OpenAPI responses object for one inbound POST endpoint."""
    block = {
        "201": {
            "description": (
                "First-write of (source_system, external_id, "
                "external_version). Canonical row created, "
                "cross_system_mappings row inserted on first-receipt, "
                "audit_log entry written."
            ),
            "headers": {**_DRAFT_HEADER},
            "content": {
                "application/json": {
                    "schema": _success_201_schema(canonical_type),
                }
            },
        },
        "200": _ERROR_RESPONSES["200"],
        "403": _ERROR_RESPONSES["403"],
        "409": {
            "description": (
                "Three error_kinds: stale_version, lock_held (with "
                "Retry-After: 1 header), cross_system_lookup_miss."
            ),
            "headers": {**_DRAFT_HEADER, **_RETRY_AFTER_HEADER},
            "content": {
                "application/json": {
                    "schema": {
                        "oneOf": [
                            _ERROR_RESPONSES["409_stale"]["content"][
                                "application/json"
                            ]["schema"],
                            {
                                "type": "object",
                                "properties": {
                                    "error_kind": {
                                        "type": "string",
                                        "enum": ["lock_held"],
                                    },
                                    "message": {"type": "string"},
                                },
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "error_kind": {
                                        "type": "string",
                                        "enum": ["cross_system_lookup_miss"],
                                    },
                                    "missing": {
                                        "type": "object",
                                        "required": [
                                            "source_system",
                                            "source_type",
                                            "source_id",
                                        ],
                                        "properties": {
                                            "source_system": {"type": "string"},
                                            "source_type": {"type": "string"},
                                            "source_id": {"type": "string"},
                                        },
                                    },
                                    "message": {"type": "string"},
                                },
                            },
                        ]
                    }
                }
            },
        },
        "413": _ERROR_RESPONSES["413"],
        "422": _ERROR_RESPONSES["422"],
        "503": _ERROR_RESPONSES["503"],
    }
    return block


def _body_schema_ref() -> str:
    return "#/components/schemas/InboundBody"


def build_inbound_openapi() -> Dict[str, Any]:
    """Returns the full OpenAPI 3.1 document for the v1.7.0 inbound
    surface as a Python dict. The yaml.safe_dump output is what gets
    committed at docs/api/inbound-openapi.yaml."""
    body_schema = InboundBody.model_json_schema()

    paths: Dict[str, Any] = {}
    for resource_key, cfg in _CONFIGS.items():
        path = f"/api/v1/inbound/{resource_key}"
        paths[path] = {
            "post": {
                "summary": (
                    f"Upsert one external {cfg.canonical_type} into the "
                    f"canonical model"
                ),
                "description": (
                    "Idempotent on (source_system, external_id, "
                    "external_version). The source_system is bound to "
                    "the X-WMS-Token; admins set it at issuance and the "
                    "decorator enforces it on every call. v1.7 ships the "
                    "canonical model as DRAFT; X-Sentry-Canonical-Model: "
                    "DRAFT-v1 header on every response."
                ),
                "operationId": f"post_inbound_{resource_key}",
                "tags": ["inbound"],
                "security": [{"WmsToken": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": _body_schema_ref()},
                        }
                    },
                },
                "responses": _resource_response_block(cfg.canonical_type),
            }
        }
    paths["/api/v1/inbound/mapping-schema"] = {
        "get": {
            "summary": (
                "Fetch the JSON Schema for the mapping-document format"
            ),
            "description": (
                "Documentation aid; unauthenticated. Returns the JSON "
                "Schema (Draft 2020-12) consumers / tooling use to "
                "validate db/mappings/<source_system>.yaml offline. "
                "Generated from services.mapping_loader.MappingDocument; "
                "see docs/api/mapping-document-schema.json for the "
                "committed copy."
            ),
            "operationId": "get_inbound_mapping_schema",
            "tags": ["inbound"],
            "security": [],
            "responses": {
                "200": {
                    "description": "JSON Schema document.",
                    "headers": {**_DRAFT_HEADER},
                    "content": {
                        "application/json": {
                            "schema": {"type": "object"},
                        }
                    },
                }
            },
        }
    }

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "SentryWMS v1.7.0 Inbound (Pipe B)",
            "version": "1.7.0",
            "description": (
                "Inbound write surface for external systems and internal "
                "tools. v1.7 ships the canonical model as DRAFT; "
                "X-Sentry-Canonical-Model: DRAFT-v1 header on every "
                "response. Schema may break at v2.0 once NetSuite "
                "validation drives the canonical lock."
            ),
        },
        "tags": [{"name": "inbound", "description": "v1.7.0 Pipe B"}],
        "components": {
            "securitySchemes": {
                "WmsToken": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-WMS-Token",
                    "description": (
                        "Inbound token issued via the admin Tokens page. "
                        "Must carry source_system + non-empty "
                        "inbound_resources for this surface."
                    ),
                }
            },
            "schemas": {
                "InboundBody": body_schema,
            },
        },
        "paths": paths,
    }
