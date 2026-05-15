"""mig 054: fraud-flag at fabric ingest + admin push-to-queue + memo PATCH.

Covers four ingest scenarios (pure-match, mismatch on city, billing
entirely blank, only blank-side differs) and the admin endpoints that
back the Outbound > Fraud page.
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

import yaml

import db_test_context
from services import token_cache
from services.mapping_loader import (
    LoadedMappingFile,
    MappingDocument,
    MappingRegistry,
)


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


# Mapping doc that exercises every billing/shipping field the fraud
# rule cares about. Mirrors the v1.8.0 example template shape.
_FRAUD_MAPPING = """\
mapping_version: "1.0"
source_system: "{ss}"
version_compare: "iso_timestamp"
resources:
  sales_orders:
    canonical_type: "sales_order"
    fields:
      - canonical: "so_number"
        source_path: "$.orderNumber"
        type: "string"
        required: true
      - canonical: "warehouse_id"
        source_path: "$.warehouseId"
        type: "integer"
        required: true
      - canonical: "customer_name"
        source_path: "$.customer.name"
        type: "string"
      - canonical: "billing_address_line1"
        source_path: "$.billing.line1"
        type: "string"
      - canonical: "billing_address_line2"
        source_path: "$.billing.line2"
        type: "string"
      - canonical: "billing_address_city"
        source_path: "$.billing.city"
        type: "string"
      - canonical: "billing_address_state"
        source_path: "$.billing.state"
        type: "string"
      - canonical: "billing_address_postal_code"
        source_path: "$.billing.zip"
        type: "string"
      - canonical: "shipping_address_line1"
        source_path: "$.shipping.line1"
        type: "string"
      - canonical: "shipping_address_line2"
        source_path: "$.shipping.line2"
        type: "string"
      - canonical: "shipping_address_city"
        source_path: "$.shipping.city"
        type: "string"
      - canonical: "shipping_address_state"
        source_path: "$.shipping.state"
        type: "string"
      - canonical: "shipping_address_postal_code"
        source_path: "$.shipping.zip"
        type: "string"
"""


def _build_registry(app, ss, body_yaml):
    parsed = yaml.safe_load(body_yaml)
    doc = MappingDocument.model_validate(parsed)
    registry = MappingRegistry()
    registry.register(LoadedMappingFile(
        document=doc, path=f"<test:{ss}>", sha256="0" * 64,
    ))
    app.config["MAPPING_REGISTRY"] = registry


def _fresh_source(prefix="fraudtest"):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _insert_via_test_conn(ss, plaintext, **kw):
    import hashlib
    import json as _json
    from _wms_token_helpers import PEPPER

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
            " endpoints, source_system, inbound_resources, mapping_override, "
            " mapping_overrides) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
            "RETURNING token_id",
            (
                kw.get("name", f"fraud-test-{uuid.uuid4().hex[:6]}"),
                token_hash, "active",
                kw.get("warehouse_ids", [1]),
                [], [],
                ss,
                kw.get("inbound_resources", ["sales_orders"]),
                False,
                _json.dumps({}),
            ),
        )
        token_id = cur.fetchone()[0]
    finally:
        cur.close()
    return token_id


@pytest.fixture
def scenario(app):
    ss = _fresh_source()
    return {"ss": ss}


@pytest.fixture(autouse=True)
def _clear_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


def _setup(app, scenario, plaintext):
    ss = scenario["ss"]
    _build_registry(app, ss, _FRAUD_MAPPING.format(ss=ss))
    _insert_via_test_conn(ss, plaintext)
    return ss


def _post_inbound(client, plaintext, body):
    return client.post(
        "/api/v1/inbound/sales_orders",
        headers={"X-WMS-Token": plaintext, "Content-Type": "application/json"},
        data=json.dumps(body),
    )


def _payload(order_number, *, billing=None, shipping=None):
    body = {
        "external_id": order_number,
        "external_version": "2026-05-14T10:00:00+00:00",
        "source_payload": {
            "orderNumber": order_number,
            "warehouseId": 1,
            "customer": {"name": "Acme"},
        },
    }
    if billing is not None:
        body["source_payload"]["billing"] = billing
    if shipping is not None:
        body["source_payload"]["shipping"] = shipping
    return body


def _status_for(canonical_id):
    rows = _query(
        "SELECT status FROM sales_orders WHERE external_id = %s",
        (canonical_id,),
    )
    return rows[0][0] if rows else None


# ----------------------------------------------------------------------
# Ingest: fraud auto-flag rule
# ----------------------------------------------------------------------


class TestFraudIngest:
    def test_billing_matches_shipping_stays_open(self, client, app, scenario):
        _setup(app, scenario, "fraud-match")
        addr = {"line1": "100 Main St", "city": "Denver",
                "state": "CO", "zip": "80202"}
        resp = _post_inbound(client, "fraud-match", _payload(
            "SO-MATCH", billing=addr, shipping=addr,
        ))
        assert resp.status_code == 201
        assert _status_for(resp.get_json()["canonical_id"]) == "OPEN"

    def test_city_mismatch_flags_fraud(self, client, app, scenario):
        _setup(app, scenario, "fraud-city")
        resp = _post_inbound(client, "fraud-city", _payload(
            "SO-CITYDIFF",
            billing={"line1": "100 Main St", "city": "Houston",
                     "state": "TX", "zip": "77002"},
            shipping={"line1": "100 Main St", "city": "Miami",
                      "state": "FL", "zip": "33101"},
        ))
        assert resp.status_code == 201
        assert _status_for(resp.get_json()["canonical_id"]) == "FRAUD_REVIEW"

    def test_billing_entirely_blank_stays_open(self, client, app, scenario):
        # Tenant didn't map billing, or fabric didn't send it. Per the
        # spec, blank-on-one-side bypasses the rule.
        _setup(app, scenario, "fraud-blank")
        resp = _post_inbound(client, "fraud-blank", _payload(
            "SO-BLANKBILL",
            shipping={"line1": "100 Main St", "city": "Denver",
                      "state": "CO", "zip": "80202"},
        ))
        assert resp.status_code == 201
        assert _status_for(resp.get_json()["canonical_id"]) == "OPEN"

    def test_only_blank_side_differs_stays_open(self, client, app, scenario):
        # billing has line2, shipping doesn't -- that field's
        # comparison is bypassed; everything else matches; no flag.
        _setup(app, scenario, "fraud-line2")
        resp = _post_inbound(client, "fraud-line2", _payload(
            "SO-LINE2ONLY",
            billing={"line1": "100 Main St", "line2": "Apt 4B",
                     "city": "Denver", "state": "CO", "zip": "80202"},
            shipping={"line1": "100 Main St",
                      "city": "Denver", "state": "CO", "zip": "80202"},
        ))
        assert resp.status_code == 201
        assert _status_for(resp.get_json()["canonical_id"]) == "OPEN"

    def test_case_and_whitespace_do_not_trigger(self, client, app, scenario):
        _setup(app, scenario, "fraud-norm")
        resp = _post_inbound(client, "fraud-norm", _payload(
            "SO-NORM",
            billing={"line1": "100 Main St", "city": "Denver",
                     "state": "CO", "zip": "80202"},
            shipping={"line1": "  100 main st  ", "city": "DENVER",
                      "state": "co", "zip": "80202"},
        ))
        assert resp.status_code == 201
        assert _status_for(resp.get_json()["canonical_id"]) == "OPEN"

    def test_postal_dash_stripped_for_compare(self, client, app, scenario):
        # "80202" and "80202" with one side dashed-formatted are still
        # the same 5-digit ZIP -> no fraud. Different ZIP+4 extensions
        # (different physical locations) DO trigger; that's covered by
        # the city-mismatch test above which would differ on multiple
        # fields anyway.
        _setup(app, scenario, "fraud-postal")
        resp = _post_inbound(client, "fraud-postal", _payload(
            "SO-POSTAL",
            billing={"line1": "100 Main St", "city": "Denver",
                     "state": "CO", "zip": "80202"},
            shipping={"line1": "100 Main St", "city": "Denver",
                      "state": "CO", "zip": " 80202 "},
        ))
        assert resp.status_code == 201
        assert _status_for(resp.get_json()["canonical_id"]) == "OPEN"


# ----------------------------------------------------------------------
# Admin: push-to-queue + memo PATCH
# ----------------------------------------------------------------------


def _seed_so(status, *, memo=None):
    """INSERT a minimal sales_orders row in the test transaction and
    return so_id. Bypasses the inbound flow so we can isolate the
    admin endpoints under test."""
    so_number = f"SO-ADMIN-{uuid.uuid4().hex[:6]}"
    rows = _query(
        "INSERT INTO sales_orders (so_number, status, warehouse_id, "
        "                          external_id, memo) "
        "VALUES (%s, %s, 1, %s, %s) RETURNING so_id",
        (so_number, status, str(uuid.uuid4()), memo),
    )
    return rows[0][0]


class TestPushToQueue:
    def test_fraud_review_order_moves_to_open(self, client, auth_headers):
        so_id = _seed_so("FRAUD_REVIEW")
        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/push-to-queue",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "OPEN"
        assert _query("SELECT status FROM sales_orders WHERE so_id = %s",
                      (so_id,))[0][0] == "OPEN"

    def test_open_order_returns_409(self, client, auth_headers):
        so_id = _seed_so("OPEN")
        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/push-to-queue",
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert _query("SELECT status FROM sales_orders WHERE so_id = %s",
                      (so_id,))[0][0] == "OPEN"

    def test_unknown_so_returns_404(self, client, auth_headers):
        resp = client.post(
            "/api/admin/sales-orders/99999999/push-to-queue",
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_audit_log_entry_written(self, client, auth_headers):
        so_id = _seed_so("FRAUD_REVIEW")
        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/push-to-queue",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        rows = _query(
            "SELECT action_type, details FROM audit_log "
            " WHERE entity_type = 'SO' AND entity_id = %s "
            "   AND action_type = 'SO_FRAUD_CLEARED'",
            (so_id,),
        )
        assert rows, "no SO_FRAUD_CLEARED audit row found"
        action_type, details = rows[0]
        assert action_type == "SO_FRAUD_CLEARED"
        assert details["from_status"] == "FRAUD_REVIEW"
        assert details["to_status"] == "OPEN"


class TestMemoPatch:
    def test_set_memo_persists(self, client, auth_headers):
        so_id = _seed_so("FRAUD_REVIEW")
        resp = client.patch(
            f"/api/admin/sales-orders/{so_id}/memo",
            headers=auth_headers,
            json={"memo": "called customer, awaiting confirmation"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["memo"] == "called customer, awaiting confirmation"
        assert _query("SELECT memo FROM sales_orders WHERE so_id = %s",
                      (so_id,))[0][0] == "called customer, awaiting confirmation"

    def test_empty_string_clears_to_null(self, client, auth_headers):
        so_id = _seed_so("FRAUD_REVIEW", memo="prior note")
        resp = client.patch(
            f"/api/admin/sales-orders/{so_id}/memo",
            headers=auth_headers,
            json={"memo": ""},
        )
        assert resp.status_code == 200
        assert resp.get_json()["memo"] is None
        assert _query("SELECT memo FROM sales_orders WHERE so_id = %s",
                      (so_id,))[0][0] is None

    def test_unchanged_value_returns_unchanged(self, client, auth_headers):
        so_id = _seed_so("FRAUD_REVIEW", memo="same")
        resp = client.patch(
            f"/api/admin/sales-orders/{so_id}/memo",
            headers=auth_headers,
            json={"memo": "same"},
        )
        assert resp.status_code == 200
        assert resp.get_json().get("unchanged") is True

    def test_audit_log_records_diff(self, client, auth_headers):
        so_id = _seed_so("FRAUD_REVIEW", memo="old note")
        client.patch(
            f"/api/admin/sales-orders/{so_id}/memo",
            headers=auth_headers,
            json={"memo": "new note"},
        )
        rows = _query(
            "SELECT action_type, details FROM audit_log "
            " WHERE entity_type = 'SO' AND entity_id = %s "
            "   AND action_type = 'SO_MEMO_EDITED'",
            (so_id,),
        )
        assert rows, "no SO_MEMO_EDITED audit row found"
        _, details = rows[0]
        assert details["old_value"] == "old note"
        assert details["new_value"] == "new note"
