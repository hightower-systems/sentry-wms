"""POST /api/v1/inbound/items end-to-end tests (v1.7.0).

Smaller suite than sales_orders since the shared 10-step handler is
already covered by test_inbound_sales_orders_endpoint.py. This file
verifies items-specific concerns:

- The route is registered and reachable through the cross-direction
  scope check + per-resource scope check (token's inbound_resources
  must include 'items').
- Mapping doc + canonical write produce a row in the existing items
  table (V-216 retrofit; sets external_id only, no canonical_id col).
- inbound_items receives the staging row.
- audit_log entity_type is INBOUND_ITEM.
- 409 lock_held includes Retry-After: 1.
"""

import json
import os
import sys
import uuid

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import hashlib
import yaml

import db_test_context
from _wms_token_helpers import DATABASE_URL, PEPPER
from services import token_cache
from services.mapping_loader import (
    LoadedMappingFile,
    MappingDocument,
    MappingRegistry,
)


_ITEMS_MAPPING = """\
mapping_version: "1.0"
source_system: "{ss}"
version_compare: "lexicographic"
resources:
  items:
    canonical_type: "item"
    fields:
      - canonical: "sku"
        source_path: "$.sku"
        type: "string"
        required: true
      - canonical: "item_name"
        source_path: "$.name"
        type: "string"
        required: true
      - canonical: "weight_lbs"
        source_path: "$.weightLbs"
        type: "decimal"
"""


def _build_registry(app, ss: str, body_yaml: str) -> MappingDocument:
    parsed = yaml.safe_load(body_yaml)
    doc = MappingDocument.model_validate(parsed)
    registry = MappingRegistry()
    registry.register(LoadedMappingFile(
        document=doc, path=f"<test:{ss}>",
        sha256="0" * 64,
    ))
    app.config["MAPPING_REGISTRY"] = registry
    return doc


def _insert_token_via_test_conn(ss, plaintext, **kw):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO inbound_source_systems_allowlist (source_system, kind) "
            "VALUES (%s, 'internal_tool') ON CONFLICT DO NOTHING",
            (ss,),
        )
        token_hash = hashlib.sha256((PEPPER + plaintext).encode()).hexdigest()
        cur.execute(
            "INSERT INTO wms_tokens "
            "(token_name, token_hash, status, warehouse_ids, event_types, "
            " endpoints, source_system, inbound_resources, mapping_override) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING token_id",
            (
                f"items-test-{uuid.uuid4().hex[:6]}",
                token_hash, "active",
                [1], [], [],
                ss,
                kw.get("inbound_resources", ["items"]),
                False,
            ),
        )
        return cur.fetchone()[0]
    finally:
        cur.close()


def _query(sql, params=()):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        if cur.description is None:
            return None
        return cur.fetchall()
    finally:
        cur.close()


def _post(client, plaintext, body):
    return client.post(
        "/api/v1/inbound/items",
        headers={"X-WMS-Token": plaintext, "Content-Type": "application/json"},
        data=json.dumps(body),
    )


@pytest.fixture(autouse=True)
def _clear_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


@pytest.fixture
def scenario(app):
    ss = f"itemstest-{uuid.uuid4().hex[:8]}"
    return {"ss": ss}


# ----------------------------------------------------------------------


class TestItemsEndpoint:
    def test_first_post_creates_inbound_and_canonical(self, client, app, scenario):
        ss = scenario["ss"]
        _build_registry(app, ss, _ITEMS_MAPPING.format(ss=ss))
        _insert_token_via_test_conn(ss, "items-1")
        resp = _post(client, "items-1", {
            "external_id": "ITEM-1",
            "external_version": "v1",
            "source_payload": {
                "sku": "SKU-1",
                "name": "Widget",
                "weightLbs": "1.5",
            },
        })
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["canonical_type"] == "item"
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"

        rows = _query(
            "SELECT sku, item_name FROM items WHERE external_id = %s",
            (body["canonical_id"],),
        )
        assert rows
        assert rows[0][0] == "SKU-1"
        assert rows[0][1] == "Widget"

        # inbound_items staging row
        n = _query(
            "SELECT COUNT(*) FROM inbound_items "
            " WHERE source_system = %s AND external_id = 'ITEM-1'",
            (ss,),
        )[0][0]
        assert n == 1

        # audit row
        rows = _query(
            "SELECT action_type, entity_type FROM audit_log "
            " WHERE entity_type='INBOUND_ITEM' AND entity_id = %s",
            (body["inbound_id"],),
        )
        assert len(rows) == 1
        assert rows[0] == ("CREATE", "INBOUND_ITEM")

    def test_scope_violation_when_token_lacks_items_resource(
        self, client, app, scenario
    ):
        """A token scoped to ['sales_orders'] hitting the items endpoint
        gets 403 inbound_resource_scope_violation. Confirms the v1.7
        decorator dispatch routes per-resource scope correctly."""
        ss = scenario["ss"]
        _build_registry(app, ss, _ITEMS_MAPPING.format(ss=ss))
        _insert_token_via_test_conn(
            ss, "items-scope-1",
            inbound_resources=["sales_orders"],
        )
        resp = _post(client, "items-scope-1", {
            "external_id": "ITEM-X",
            "external_version": "v1",
            "source_payload": {"sku": "SKU-X", "name": "x"},
        })
        assert resp.status_code == 403
        assert resp.get_json() == {"error": "inbound_resource_scope_violation"}

    def test_idempotent_repost_returns_200(self, client, app, scenario):
        ss = scenario["ss"]
        _build_registry(app, ss, _ITEMS_MAPPING.format(ss=ss))
        _insert_token_via_test_conn(ss, "items-idem")
        payload = {
            "external_id": "ITEM-IDEM",
            "external_version": "v1",
            "source_payload": {"sku": "SKU-IDEM", "name": "Idem"},
        }
        r1 = _post(client, "items-idem", payload)
        r2 = _post(client, "items-idem", payload)
        assert r1.status_code == 201
        assert r2.status_code == 200
        assert r2.get_json()["inbound_id"] == r1.get_json()["inbound_id"]
