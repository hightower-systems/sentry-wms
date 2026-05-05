"""POST /api/v1/inbound/purchase_orders end-to-end tests (v1.7.0).

Last inbound POST endpoint. purchase_orders is an existing
warehouse-floor table (V-216 retrofit) with NO updated_at column --
the same shape as sales_orders. The handler's
has_updated_at_col=False branch is exercised here on top of the
shared 10-step flow already covered by the sales_orders tests.

This file also covers the cross-resource line item shape (PO line
items are stored in canonical_payload JSONB, never written to
purchase_order_lines in v1.7 -- forensic chain via JSONB only).
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


_PO_MAPPING = """\
mapping_version: "1.0"
source_system: "{ss}"
version_compare: "lexicographic"
resources:
  purchase_orders:
    canonical_type: "purchase_order"
    fields:
      - canonical: "po_number"
        source_path: "$.poNumber"
        type: "string"
        required: true
      - canonical: "warehouse_id"
        source_path: "$.warehouseId"
        type: "integer"
        required: true
      - canonical: "vendor_name"
        source_path: "$.vendor.name"
        type: "string"
    line_items:
      source_path: "$.lineItems"
      canonical_path: "lines"
      fields:
        - canonical: "sku"
          source_path: "$.sku"
          type: "string"
        - canonical: "qty"
          source_path: "$.quantity"
          type: "integer"
"""


def _load(app, ss: str, body_yaml: str) -> MappingDocument:
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
                f"po-test-{uuid.uuid4().hex[:6]}",
                token_hash, "active",
                [1], [], [], ss,
                kw.get("inbound_resources", ["purchase_orders"]),
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
        "/api/v1/inbound/purchase_orders",
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
    return {"ss": f"potest-{uuid.uuid4().hex[:8]}"}


# ----------------------------------------------------------------------


class TestPurchaseOrdersEndpoint:
    def test_first_post_creates_canonical_with_no_updated_at(
        self, client, app, scenario
    ):
        """purchase_orders predates the updated_at convention -- the
        handler's has_updated_at_col=False branch must skip the
        updated_at maintenance on subsequent UPDATEs (covered by the
        supersession test below) and the INSERT must succeed without
        referencing the column."""
        ss = scenario["ss"]
        _load(app, ss, _PO_MAPPING.format(ss=ss))
        _insert_token_via_test_conn(ss, "po-1")
        resp = _post(client, "po-1", {
            "external_id": "PO-1",
            "external_version": "v1",
            "source_payload": {
                "poNumber": "PO-1",
                "warehouseId": 1,
                "vendor": {"name": "ACME"},
                "lineItems": [
                    {"sku": "A", "quantity": 1},
                    {"sku": "B", "quantity": 2},
                ],
            },
        })
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["canonical_type"] == "purchase_order"

        rows = _query(
            "SELECT po_number, warehouse_id, vendor_name, latest_inbound_id "
            "  FROM purchase_orders WHERE external_id = %s",
            (body["canonical_id"],),
        )
        assert rows
        po_number, warehouse_id, vendor_name, latest_inbound_id = rows[0]
        assert po_number == "PO-1"
        assert warehouse_id == 1
        assert vendor_name == "ACME"
        assert latest_inbound_id == body["inbound_id"]

    def test_line_items_land_in_canonical_payload_only(
        self, client, app, scenario
    ):
        """v1.7 first-pass: line_items resolved by the loader and stored
        in inbound_purchase_orders.canonical_payload JSONB; NOT written
        to the canonical purchase_order_lines table. Forensic chain
        preserved via JSONB; line table sync lands at v1.8+ once a
        real consumer demonstrates the shape."""
        ss = scenario["ss"]
        _load(app, ss, _PO_MAPPING.format(ss=ss))
        _insert_token_via_test_conn(ss, "po-lines")
        resp = _post(client, "po-lines", {
            "external_id": "PO-LINES",
            "external_version": "v1",
            "source_payload": {
                "poNumber": "PO-LINES",
                "warehouseId": 1,
                "vendor": {"name": "ACME"},
                "lineItems": [
                    {"sku": "ABC-001", "quantity": 5},
                    {"sku": "ABC-002", "quantity": 10},
                ],
            },
        })
        assert resp.status_code == 201
        inbound_id = resp.get_json()["inbound_id"]

        # canonical_payload JSONB carries the resolved lines.
        rows = _query(
            "SELECT canonical_payload FROM inbound_purchase_orders "
            " WHERE inbound_id = %s",
            (inbound_id,),
        )
        assert rows
        cp = rows[0][0]
        assert cp.get("lines") == [
            {"sku": "ABC-001", "qty": 5},
            {"sku": "ABC-002", "qty": 10},
        ]

        # purchase_order_lines untouched (no mass-write of inbound lines
        # to the canonical line table in v1.7).
        po_id_rows = _query(
            "SELECT po_id FROM purchase_orders WHERE external_id = %s",
            (resp.get_json()["canonical_id"],),
        )
        assert po_id_rows
        n = _query(
            "SELECT COUNT(*) FROM purchase_order_lines WHERE po_id = %s",
            (po_id_rows[0][0],),
        )[0][0]
        assert n == 0

    def test_supersession_runs_without_updated_at(self, client, app, scenario):
        """v1 then v2 with the same external_id; v2 UPDATEs the canonical
        row. Verifies the handler does not try to SET updated_at = NOW()
        on a table that doesn't have the column."""
        ss = scenario["ss"]
        _load(app, ss, _PO_MAPPING.format(ss=ss))
        _insert_token_via_test_conn(ss, "po-sup")
        v1 = _post(client, "po-sup", {
            "external_id": "PO-SUP",
            "external_version": "v1",
            "source_payload": {
                "poNumber": "PO-SUP",
                "warehouseId": 1,
                "vendor": {"name": "v1"},
            },
        })
        v2 = _post(client, "po-sup", {
            "external_id": "PO-SUP",
            "external_version": "v2",
            "source_payload": {
                "poNumber": "PO-SUP",
                "warehouseId": 1,
                "vendor": {"name": "v2"},
            },
        })
        assert v1.status_code == 201
        assert v2.status_code == 201
        assert v1.get_json()["canonical_id"] == v2.get_json()["canonical_id"]

        rows = _query(
            "SELECT vendor_name FROM purchase_orders WHERE external_id = %s",
            (v1.get_json()["canonical_id"],),
        )
        assert rows[0][0] == "v2"
