"""GET /api/v1/events/types + /schema/{type}/{version} contract (#126).

These endpoints expose the registry loaded at ``create_app`` time.
Registry-internal invariants (every catalog entry has a loadable
schema, unknown types raise KeyError from ``get_validator``) live in
test_events_service.py; this module covers the HTTP surface.
"""

import json
import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from _polling_helpers import insert_token
from services import token_cache


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


@pytest.fixture()
def scoped_token(seed_data):
    plaintext = f"registry-token-{uuid.uuid4()}"
    token_id = insert_token(
        plaintext,
        warehouse_ids=[1],
        event_types=["receipt.completed", "ship.confirmed"],
    )
    return {"plaintext": plaintext, "token_id": token_id}


class TestTypesEndpoint:
    def test_returns_all_seven_v150_event_types(self, client, scoped_token):
        resp = client.get(
            "/api/v1/events/types",
            headers={"X-WMS-Token": scoped_token["plaintext"]},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        names = {t["event_type"] for t in body["types"]}
        assert names == {
            "receipt.completed",
            "adjustment.applied",
            "transfer.completed",
            "pick.confirmed",
            "pack.confirmed",
            "ship.confirmed",
            "cycle_count.adjusted",
        }

    def test_each_entry_exposes_versions_and_aggregate_type(
        self, client, scoped_token
    ):
        resp = client.get(
            "/api/v1/events/types",
            headers={"X-WMS-Token": scoped_token["plaintext"]},
        )
        body = resp.get_json()
        by_name = {t["event_type"]: t for t in body["types"]}
        assert by_name["ship.confirmed"]["aggregate_type"] == "sales_order"
        assert by_name["ship.confirmed"]["versions"] == [1]
        assert by_name["receipt.completed"]["aggregate_type"] == "item_receipt"
        assert by_name["adjustment.applied"]["aggregate_type"] == "inventory_adjustment"

    def test_requires_token(self, client):
        resp = client.get("/api/v1/events/types")
        assert resp.status_code == 401


class TestSchemaEndpoint:
    def test_returns_raw_schema_body_with_schema_json_content_type(
        self, client, scoped_token
    ):
        resp = client.get(
            "/api/v1/events/schema/ship.confirmed/1",
            headers={"X-WMS-Token": scoped_token["plaintext"]},
        )
        assert resp.status_code == 200
        assert resp.mimetype == "application/schema+json"
        body = json.loads(resp.get_data())
        assert body["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert body["title"].startswith("ship.confirmed")

    def test_unknown_event_type_returns_404(self, client, scoped_token):
        resp = client.get(
            "/api/v1/events/schema/does.not.exist/1",
            headers={"X-WMS-Token": scoped_token["plaintext"]},
        )
        assert resp.status_code == 404

    def test_unknown_version_returns_404(self, client, scoped_token):
        resp = client.get(
            "/api/v1/events/schema/ship.confirmed/999",
            headers={"X-WMS-Token": scoped_token["plaintext"]},
        )
        assert resp.status_code == 404

    def test_requires_token(self, client):
        resp = client.get("/api/v1/events/schema/ship.confirmed/1")
        assert resp.status_code == 401

    def test_every_v150_event_type_has_a_loadable_schema(
        self, client, scoped_token
    ):
        """Boot-time loading is proven in test_events_service.py's
        registry-internal invariants; the HTTP surface test here
        verifies the same catalog lands on the wire. Pulls
        V150_CATALOG at test time so a future addition is
        automatically covered."""
        from services.events_schema_registry import V150_CATALOG

        for event_type, version, _aggregate_type in V150_CATALOG:
            resp = client.get(
                f"/api/v1/events/schema/{event_type}/{version}",
                headers={"X-WMS-Token": scoped_token["plaintext"]},
            )
            assert resp.status_code == 200, (
                f"{event_type} v{version} should be served by the endpoint"
            )
            body = json.loads(resp.get_data())
            assert body.get("$schema") == (
                "https://json-schema.org/draft/2020-12/schema"
            )
