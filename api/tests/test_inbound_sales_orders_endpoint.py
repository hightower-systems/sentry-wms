"""POST /api/v1/inbound/sales_orders end-to-end tests (v1.7.0).

Builds the MappingRegistry directly in-memory so tests don't depend on
boot_load() reading a tmp directory and writing audit rows -- that path
is covered by test_mapping_loader_boot.py. Each test owns its own
source_system label; cleanup wipes inbound_sales_orders +
cross_system_mappings + sales_orders + wms_tokens + the allowlist row
the test created. The cleanup runs in a finalizer so tests that fail
mid-flight still leave a clean slate for the session-scoped app fixture's
next boot.
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

import psycopg2
import yaml

from _wms_token_helpers import DATABASE_URL, delete_token, insert_token
import db_test_context
from services import token_cache
from services.mapping_loader import (
    LoadedMappingFile,
    MappingDocument,
    MappingRegistry,
)


def _query(sql, params=()):
    """Run a verifying SELECT against the test transaction's underlying
    connection. The Flask handler writes through g.db (same transaction);
    a separate psycopg2 connection wouldn't see those writes because the
    outer test transaction is held open by conftest's _db_transaction
    fixture and only rolls back at end-of-test."""
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        if cur.description is None:
            return None
        return cur.fetchall()
    finally:
        cur.close()


# ----------------------------------------------------------------------
# In-memory mapping registry helpers
# ----------------------------------------------------------------------


_BASE_MAPPING = """\
mapping_version: "1.0"
source_system: "{ss}"
version_compare: "{vc}"
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
"""


_LOOKUP_MAPPING = """\
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
      - canonical: "customer_id"
        source_path: "$.customer.id"
        type: "uuid"
        required: true
        cross_system_lookup:
          source_type: "customer"
"""


def _build_registry(app, ss: str, body_yaml: str) -> MappingDocument:
    """Parse `body_yaml`, register on app.config['MAPPING_REGISTRY'].
    No file IO, no boot_load, no audit_log writes."""
    parsed = yaml.safe_load(body_yaml)
    doc = MappingDocument.model_validate(parsed)
    registry = MappingRegistry()
    registry.register(LoadedMappingFile(
        document=doc, path=f"<test:{ss}>",
        sha256="0" * 64,
    ))
    app.config["MAPPING_REGISTRY"] = registry
    return doc


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _fresh_source(prefix="sotest"):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _insert_via_test_conn(ss, plaintext, **kw):
    """Insert wms_tokens + allowlist row via the TEST transaction's
    raw connection so both rows roll back at end-of-test. The shared
    insert_token helper uses an autocommit psycopg2 connection (durable
    inserts) which suits standalone token-decorator tests but leaks
    rows across the session here -- inbound endpoint tests insert MANY
    tokens per session, and a leaked allowlist row breaks the
    boot_load() cross-check the next time a session-scoped app fixture
    runs (e.g., subsequent test files). Inserting via the test conn
    keeps the rows test-scoped."""
    import hashlib
    from _wms_token_helpers import PEPPER, DEFAULT_TEST_ENDPOINTS

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
                kw.get("name", f"inbound-test-{uuid.uuid4().hex[:6]}"),
                token_hash, "active",
                kw.get("warehouse_ids", [1]),
                kw.get("event_types", []),
                kw.get("endpoints", []),
                ss,
                kw.get("inbound_resources", ["sales_orders"]),
                kw.get("mapping_override", False),
            ),
        )
        token_id = cur.fetchone()[0]
    finally:
        cur.close()
    return token_id


@pytest.fixture
def scenario(app):
    """Per-test scenario state. Allowlist + wms_tokens rows are inserted
    via the test's raw connection (db_test_context) so they roll back
    cleanly at end-of-test along with everything else. inbound writes
    flow through g.db (same transaction) and are rolled back too.
    No finalizer needed."""
    ss = _fresh_source()
    return {
        "ss": ss,
        "tokens": [],
        "canonical_external_ids": [],
    }


@pytest.fixture(autouse=True)
def _clear_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


def _make_token(ss, plaintext, **kw):
    """Wraps _insert_via_test_conn so wms_tokens + allowlist rows roll
    back at end-of-test. token_cache reads via _db.SessionLocal which
    conftest binds to the test conn -- so the just-inserted row is
    visible to the decorator without us having to invalidate the cache."""
    return _insert_via_test_conn(ss, plaintext, **kw)


def _post(client, plaintext, body):
    return client.post(
        "/api/v1/inbound/sales_orders",
        headers={"X-WMS-Token": plaintext, "Content-Type": "application/json"},
        data=json.dumps(body),
    )


def _setup_basic(app, scenario, plaintext, **token_kw):
    ss = scenario["ss"]
    _build_registry(app, ss, _BASE_MAPPING.format(ss=ss, vc="iso_timestamp"))
    token_id = _make_token(ss, plaintext, **token_kw)
    scenario["tokens"].append(token_id)
    return ss


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


class TestHappyPath:
    def test_first_post_creates_inbound_and_canonical(self, client, app, scenario):
        ss = _setup_basic(app, scenario, "happy-1")
        resp = _post(client, "happy-1", {
            "external_id": "SO-1",
            "external_version": "2026-05-04T10:00:00+00:00",
            "source_payload": {
                "orderNumber": "SO-1",
                "warehouseId": 1,
                "customer": {"name": "Acme"},
            },
        })
        assert resp.status_code == 201
        body = resp.get_json()
        scenario["canonical_external_ids"].append(body["canonical_id"])
        assert body["canonical_type"] == "sales_order"
        assert body["warning"].startswith("Canonical model is DRAFT")
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"

        rows = _query(
            "SELECT so_number, warehouse_id, customer_name, latest_inbound_id "
            "  FROM sales_orders WHERE external_id = %s",
            (body["canonical_id"],),
        )
        assert rows
        so_number, warehouse_id, customer_name, latest_inbound_id = rows[0]
        assert so_number == "SO-1"
        assert warehouse_id == 1
        assert customer_name == "Acme"
        assert latest_inbound_id == body["inbound_id"]

    def test_idempotent_repost_returns_200_without_double_writing(
        self, client, app, scenario
    ):
        ss = _setup_basic(app, scenario, "idem-1")
        payload = {
            "external_id": "SO-IDEM",
            "external_version": "2026-05-04T10:00:00+00:00",
            "source_payload": {
                "orderNumber": "SO-IDEM",
                "warehouseId": 1,
                "customer": {"name": "Acme"},
            },
        }
        r1 = _post(client, "idem-1", payload)
        r2 = _post(client, "idem-1", payload)
        scenario["canonical_external_ids"].append(r1.get_json()["canonical_id"])
        assert r1.status_code == 201
        assert r2.status_code == 200
        assert r2.get_json()["canonical_id"] == r1.get_json()["canonical_id"]
        assert r2.get_json()["inbound_id"] == r1.get_json()["inbound_id"]
        n = _query(
            "SELECT COUNT(*) FROM inbound_sales_orders "
            " WHERE source_system = %s AND external_id = 'SO-IDEM'",
            (ss,),
        )[0][0]
        assert n == 1

    def test_supersession_on_newer_version(self, client, app, scenario):
        ss = _setup_basic(app, scenario, "super-1")
        v1 = _post(client, "super-1", {
            "external_id": "SO-SUP",
            "external_version": "2026-05-04T10:00:00+00:00",
            "source_payload": {
                "orderNumber": "SO-SUP",
                "warehouseId": 1,
                "customer": {"name": "v1"},
            },
        })
        scenario["canonical_external_ids"].append(v1.get_json()["canonical_id"])
        v2 = _post(client, "super-1", {
            "external_id": "SO-SUP",
            "external_version": "2026-05-04T11:00:00+00:00",
            "source_payload": {
                "orderNumber": "SO-SUP",
                "warehouseId": 1,
                "customer": {"name": "v2"},
            },
        })
        assert v1.status_code == 201 and v2.status_code == 201
        assert v1.get_json()["canonical_id"] == v2.get_json()["canonical_id"]
        rows = _query(
            "SELECT external_version, status FROM inbound_sales_orders "
            " WHERE source_system = %s AND external_id = 'SO-SUP' "
            " ORDER BY received_at",
            (ss,),
        )
        cust = _query(
            "SELECT customer_name FROM sales_orders WHERE external_id = %s",
            (v1.get_json()["canonical_id"],),
        )[0][0]
        assert len(rows) == 2
        statuses = {ext_v: stat for ext_v, stat in rows}
        assert statuses["2026-05-04T10:00:00+00:00"] == "superseded"
        assert statuses["2026-05-04T11:00:00+00:00"] == "applied"
        assert cust == "v2"

    def test_stale_version_returns_409(self, client, app, scenario):
        ss = _setup_basic(app, scenario, "stale-1")
        r1 = _post(client, "stale-1", {
            "external_id": "SO-ST",
            "external_version": "2026-05-04T11:00:00+00:00",
            "source_payload": {
                "orderNumber": "SO-ST",
                "warehouseId": 1,
                "customer": {"name": "x"},
            },
        })
        scenario["canonical_external_ids"].append(r1.get_json()["canonical_id"])
        r_stale = _post(client, "stale-1", {
            "external_id": "SO-ST",
            "external_version": "2026-05-04T10:00:00+00:00",
            "source_payload": {
                "orderNumber": "SO-ST",
                "warehouseId": 1,
                "customer": {"name": "x"},
            },
        })
        assert r_stale.status_code == 409
        body = r_stale.get_json()
        assert body["error_kind"] == "stale_version"
        assert body["current_version"] == "2026-05-04T11:00:00+00:00"


# ----------------------------------------------------------------------
# Pydantic + body-cap
# ----------------------------------------------------------------------


class TestRequestValidation:
    def test_extra_field_returns_422(self, client, app, scenario):
        _setup_basic(app, scenario, "extra-1")
        resp = _post(client, "extra-1", {
            "external_id": "SO-1",
            "external_version": "v1",
            "source_payload": {},
            "evil_extra_field": "boom",
        })
        assert resp.status_code == 422
        assert resp.get_json()["error_kind"] == "validation_error"
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"

    def test_body_just_over_cap_returns_413(
        self, client, app, scenario, monkeypatch
    ):
        monkeypatch.setenv("SENTRY_INBOUND_MAX_BODY_KB", "16")
        _setup_basic(app, scenario, "size-1")
        blob = "x" * (17 * 1024)
        resp = _post(client, "size-1", {
            "external_id": "SO-BIG",
            "external_version": "v1",
            "source_payload": {"big": blob},
        })
        assert resp.status_code == 413
        assert resp.get_json()["error_kind"] == "body_too_large"
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"


# ----------------------------------------------------------------------
# Cross-system lookup miss
# ----------------------------------------------------------------------


class TestCrossSystemLookup:
    def test_required_lookup_miss_returns_409(self, client, app, scenario):
        ss = scenario["ss"]
        _build_registry(app, ss, _LOOKUP_MAPPING.format(ss=ss))
        token_id = _make_token(ss, "lookup-1")
        scenario["tokens"].append(token_id)
        resp = _post(client, "lookup-1", {
            "external_id": "SO-L",
            "external_version": "2026-05-04T10:00:00+00:00",
            "source_payload": {
                "orderNumber": "SO-L",
                "warehouseId": 1,
                "customer": {"id": "C-NOTFOUND"},
            },
        })
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["error_kind"] == "cross_system_lookup_miss"
        assert body["missing"]["source_type"] == "customer"
        assert body["missing"]["source_id"] == "C-NOTFOUND"
        assert body["missing"]["source_system"] == ss


# ----------------------------------------------------------------------
# Audit_log coverage
# ----------------------------------------------------------------------


class TestAuditLogCoverage:
    def test_accepted_post_writes_one_audit_row(self, client, app, scenario):
        ss = _setup_basic(app, scenario, "audit-1")
        r = _post(client, "audit-1", {
            "external_id": "SO-AUD",
            "external_version": "2026-05-04T10:00:00+00:00",
            "source_payload": {
                "orderNumber": "SO-AUD",
                "warehouseId": 1,
                "customer": {"name": "Acme"},
            },
        })
        scenario["canonical_external_ids"].append(r.get_json()["canonical_id"])
        inbound_id = r.get_json()["inbound_id"]
        rows = _query(
            "SELECT action_type, entity_type, details FROM audit_log "
            " WHERE entity_type='INBOUND_SALES_ORDER' AND entity_id = %s",
            (inbound_id,),
        )
        assert len(rows) == 1
        action, entity_type, details = rows[0]
        assert action == "CREATE"
        assert entity_type == "INBOUND_SALES_ORDER"
        assert details["source_system"] == ss
        assert details["external_id"] == "SO-AUD"
        assert "so_number" in details["field_set"]
        assert "warehouse_id" in details["field_set"]
        assert details["override_fields"] == []

    def test_idempotent_repost_writes_zero_additional_audit_rows(
        self, client, app, scenario
    ):
        _setup_basic(app, scenario, "audit-idem-1")
        payload = {
            "external_id": "SO-AUDI",
            "external_version": "2026-05-04T10:00:00+00:00",
            "source_payload": {
                "orderNumber": "SO-AUDI",
                "warehouseId": 1,
                "customer": {"name": "Acme"},
            },
        }
        r1 = _post(client, "audit-idem-1", payload)
        scenario["canonical_external_ids"].append(r1.get_json()["canonical_id"])
        r2 = _post(client, "audit-idem-1", payload)
        assert r2.status_code == 200
        inbound_id = r1.get_json()["inbound_id"]
        n = _query(
            "SELECT COUNT(*) FROM audit_log "
            " WHERE entity_type='INBOUND_SALES_ORDER' AND entity_id = %s",
            (inbound_id,),
        )[0][0]
        assert n == 1


# ----------------------------------------------------------------------
# cross_system_mappings autocreate
# ----------------------------------------------------------------------


class TestCrossSystemMappingsAutocreate:
    def test_first_post_inserts_mapping_row(self, client, app, scenario):
        ss = _setup_basic(app, scenario, "csm-auto-1")
        r = _post(client, "csm-auto-1", {
            "external_id": "SO-CSM",
            "external_version": "2026-05-04T10:00:00+00:00",
            "source_payload": {
                "orderNumber": "SO-CSM",
                "warehouseId": 1,
                "customer": {"name": "x"},
            },
        })
        scenario["canonical_external_ids"].append(r.get_json()["canonical_id"])
        rows = _query(
            "SELECT canonical_id FROM cross_system_mappings "
            " WHERE source_system = %s AND source_type = 'sales_order' "
            "   AND source_id = 'SO-CSM'",
            (ss,),
        )
        assert rows
        assert str(rows[0][0]) == r.get_json()["canonical_id"]


# ----------------------------------------------------------------------
# v1.8.0 (#285) optional sales_orders cost fields with per-field decimal
# bounds. order_total + customer_shipping_paid land via the same generic
# inbound contract; bounds enforce wire-level rejection so a connector
# author sees a clear 422 instead of silent rounding (excess scale) or
# a 500 (excess precision from the NUMERIC(12,2) column).
# ----------------------------------------------------------------------


_COST_MAPPING = """\
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
      - canonical: "order_total"
        source_path: "$.order.total"
        type: "decimal"
        max_digits: 12
        decimal_places: 2
        ge: "0"
        le: "9999999999.99"
      - canonical: "customer_shipping_paid"
        source_path: "$.order.shipping"
        type: "decimal"
        max_digits: 12
        decimal_places: 2
        ge: "0"
        le: "9999999999.99"
"""


def _setup_cost(app, scenario, plaintext, **token_kw):
    ss = scenario["ss"]
    _build_registry(app, ss, _COST_MAPPING.format(ss=ss))
    token_id = _make_token(ss, plaintext, **token_kw)
    scenario["tokens"].append(token_id)
    return ss


class TestOptionalSalesOrderFields:
    def test_both_fields_populate_canonical_columns(
        self, client, app, scenario,
    ):
        ss = _setup_cost(app, scenario, "cost-1")
        resp = _post(client, "cost-1", {
            "external_id": "SO-COST-1",
            "external_version": "2026-05-04T10:00:00+00:00",
            "source_payload": {
                "orderNumber": "SO-COST-1",
                "warehouseId": 1,
                "order": {"total": "123.45", "shipping": "9.99"},
            },
        })
        assert resp.status_code == 201, resp.get_json()
        scenario["canonical_external_ids"].append(resp.get_json()["canonical_id"])
        rows = _query(
            "SELECT order_total, customer_shipping_paid "
            "  FROM sales_orders WHERE external_id = %s",
            (resp.get_json()["canonical_id"],),
        )
        from decimal import Decimal
        assert rows[0][0] == Decimal("123.45")
        assert rows[0][1] == Decimal("9.99")

    def test_neither_field_present_leaves_columns_null(
        self, client, app, scenario,
    ):
        ss = _setup_cost(app, scenario, "cost-2")
        resp = _post(client, "cost-2", {
            "external_id": "SO-COST-2",
            "external_version": "2026-05-04T10:00:00+00:00",
            "source_payload": {
                "orderNumber": "SO-COST-2",
                "warehouseId": 1,
                "order": {},
            },
        })
        assert resp.status_code == 201, resp.get_json()
        scenario["canonical_external_ids"].append(resp.get_json()["canonical_id"])
        rows = _query(
            "SELECT order_total, customer_shipping_paid "
            "  FROM sales_orders WHERE external_id = %s",
            (resp.get_json()["canonical_id"],),
        )
        assert rows[0] == (None, None)

    def test_zero_distinguishable_from_null(self, client, app, scenario):
        ss = _setup_cost(app, scenario, "cost-3")
        resp = _post(client, "cost-3", {
            "external_id": "SO-COST-3",
            "external_version": "2026-05-04T10:00:00+00:00",
            "source_payload": {
                "orderNumber": "SO-COST-3",
                "warehouseId": 1,
                "order": {"total": "0", "shipping": "0.00"},
            },
        })
        assert resp.status_code == 201, resp.get_json()
        scenario["canonical_external_ids"].append(resp.get_json()["canonical_id"])
        rows = _query(
            "SELECT order_total, customer_shipping_paid "
            "  FROM sales_orders WHERE external_id = %s",
            (resp.get_json()["canonical_id"],),
        )
        from decimal import Decimal
        assert rows[0][0] == Decimal("0")
        assert rows[0][1] == Decimal("0")

    def test_excess_scale_rejected_422(self, client, app, scenario):
        ss = _setup_cost(app, scenario, "cost-4")
        resp = _post(client, "cost-4", {
            "external_id": "SO-COST-4",
            "external_version": "2026-05-04T10:00:00+00:00",
            "source_payload": {
                "orderNumber": "SO-COST-4",
                "warehouseId": 1,
                "order": {"total": "12.345"},
            },
        })
        assert resp.status_code == 422
        body = resp.get_json()
        assert body["error_kind"] == "mapping_apply_error"
        assert "order_total" in body["message"]
        assert "decimal place" in body["message"]

    def test_excess_precision_rejected_422(self, client, app, scenario):
        ss = _setup_cost(app, scenario, "cost-5")
        resp = _post(client, "cost-5", {
            "external_id": "SO-COST-5",
            "external_version": "2026-05-04T10:00:00+00:00",
            "source_payload": {
                "orderNumber": "SO-COST-5",
                "warehouseId": 1,
                "order": {"total": "12345678901234"},
            },
        })
        assert resp.status_code == 422
        body = resp.get_json()
        assert body["error_kind"] == "mapping_apply_error"
        assert "order_total" in body["message"]

    def test_negative_value_rejected_422(self, client, app, scenario):
        ss = _setup_cost(app, scenario, "cost-6")
        resp = _post(client, "cost-6", {
            "external_id": "SO-COST-6",
            "external_version": "2026-05-04T10:00:00+00:00",
            "source_payload": {
                "orderNumber": "SO-COST-6",
                "warehouseId": 1,
                "order": {"total": "-0.01"},
            },
        })
        assert resp.status_code == 422
        body = resp.get_json()
        assert body["error_kind"] == "mapping_apply_error"
        assert "order_total" in body["message"]
        assert "ge=" in body["message"]

    def test_above_le_rejected_422(self, client, app, scenario):
        # NUMERIC(12,2) saturates max_digits=12 and le=9999999999.99
        # at the same boundary; any value above the column's max trips
        # one of the two bound checks (whichever fires first). Both
        # are correct rejections; the contract is that the wire returns
        # 422 with order_total in the message.
        ss = _setup_cost(app, scenario, "cost-7")
        resp = _post(client, "cost-7", {
            "external_id": "SO-COST-7",
            "external_version": "2026-05-04T10:00:00+00:00",
            "source_payload": {
                "orderNumber": "SO-COST-7",
                "warehouseId": 1,
                "order": {"total": "10000000000.00"},
            },
        })
        assert resp.status_code == 422
        body = resp.get_json()
        assert body["error_kind"] == "mapping_apply_error"
        assert "order_total" in body["message"]
        assert ("le=" in body["message"]
                or "significant digit" in body["message"])
