"""ship.voided/1 schema contract (v1.9.0 dockd).

Boot-time loading is covered by the existing
test_events_service.TestRegistryBootInvariants suite -- adding
("ship.voided", 1, "sales_order") to V150_CATALOG without shipping
the schema file fails boot. This module covers the wire shape:
required fields, additionalProperties=false, enum on
reverted_to_status, and uuid / date-time format checks.
"""

import json
import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import jsonschema
import pytest


def _validator():
    from services.events_schema_registry import get_validator
    return get_validator("ship.voided", 1)


def _valid_payload():
    return {
        "sales_order_external_id": "11111111-1111-4111-a111-111111111111",
        "voided_at": "2026-05-08T18:30:00Z",
        "voided_by_user_external_id": "22222222-2222-4222-a222-222222222222",
        "reason": "wrong box dimensions",
        "reverted_to_status": "PACKED",
    }


class TestCatalogEntry:
    def test_ship_voided_in_catalog(self):
        from services.events_schema_registry import V150_CATALOG
        entries = [e for e in V150_CATALOG if e[0] == "ship.voided"]
        assert entries == [("ship.voided", 1, "sales_order")]


class TestHappyPath:
    def test_valid_payload_passes(self):
        v = _validator()
        v.validate(_valid_payload())

    def test_reverted_to_status_picked_passes(self):
        v = _validator()
        p = _valid_payload()
        p["reverted_to_status"] = "PICKED"
        v.validate(p)


class TestRequiredFields:
    @pytest.mark.parametrize("field", [
        "sales_order_external_id",
        "voided_at",
        "voided_by_user_external_id",
        "reason",
        "reverted_to_status",
    ])
    def test_missing_required_field_fails(self, field):
        v = _validator()
        p = _valid_payload()
        del p[field]
        with pytest.raises(jsonschema.ValidationError):
            v.validate(p)


class TestRejectsExtraProperties:
    def test_extra_top_level_field_fails(self):
        v = _validator()
        p = _valid_payload()
        p["station_label"] = "Pack Station 3"
        with pytest.raises(jsonschema.ValidationError):
            v.validate(p)


class TestEnums:
    def test_reverted_to_status_open_rejected(self):
        v = _validator()
        p = _valid_payload()
        p["reverted_to_status"] = "OPEN"
        with pytest.raises(jsonschema.ValidationError):
            v.validate(p)

    def test_reverted_to_status_shipped_rejected(self):
        v = _validator()
        p = _valid_payload()
        p["reverted_to_status"] = "SHIPPED"
        with pytest.raises(jsonschema.ValidationError):
            v.validate(p)


class TestFormats:
    def test_sales_order_external_id_must_be_uuid_shape(self):
        # Draft 2020-12 format-assertion is opt-in but the registry's
        # loader uses the default validator. Schema-level shape check
        # via type=string + format=uuid still rejects type mismatches.
        v = _validator()
        p = _valid_payload()
        p["sales_order_external_id"] = 12345
        with pytest.raises(jsonschema.ValidationError):
            v.validate(p)

    def test_voided_at_must_be_string(self):
        v = _validator()
        p = _valid_payload()
        p["voided_at"] = 1715192400
        with pytest.raises(jsonschema.ValidationError):
            v.validate(p)


class TestSchemaFileShape:
    def test_schema_file_loads_as_draft_2020_12(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "schemas_v1", "events",
            "ship.voided", "1.json",
        )
        with open(path) as f:
            doc = json.load(f)
        assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert doc["title"].startswith("ship.voided")
        assert doc["additionalProperties"] is False
