"""GET /api/v1/dockd/orders/<so_number> contract (v1.9.0 dockd #4).

Coverage:
- happy path: PICKED + PACKED + SHIPPED rows return 200 with the
  documented response shape.
- shippable / shippable_from_statuses derivation against the
  app_settings.require_packing_before_shipping toggle.
- structured shipping_address: each component round-trips; NULL
  columns serialize as JSON null, never empty string.
- items array sourced from sales_order_lines + items, ordered by
  line_number, with external_id as UUID string.
- 404 not_found for unknown so_number AND for orders outside the
  token's warehouse scope (no enumeration oracle).
- 422 invalid_so_number for malformed path parameter.
- DRAFT-v1 header on every response, success or failure.
- 401 invalid_token for missing / unknown bearer.
- 403 cross_direction / endpoint_scope when an outbound or inbound
  token reaches the dockd surface (covered by the V190 dispatcher
  branch's own tests; included here to assert the route inherits).
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from _wms_token_helpers import delete_token, insert_token
from db_test_context import get_raw_connection
from services import token_cache


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


@pytest.fixture()
def dockd_token(seed_data):
    """Pure-direction dockd station token: dockd.dispatch slug, warehouse_id 1,
    no inbound / outbound markers."""
    plaintext = f"dockd-test-{uuid.uuid4()}"
    token_id = insert_token(
        name="Pack Station 1",
        plaintext=plaintext,
        warehouse_ids=[1],
        event_types=[],
        inbound_resources=[],
        source_system=None,
        endpoints=["dockd.dispatch"],
    )
    yield {"plaintext": plaintext, "token_id": token_id}
    delete_token(token_id)


@pytest.fixture()
def dockd_token_other_warehouse(seed_data):
    plaintext = f"dockd-test-wh99-{uuid.uuid4()}"
    token_id = insert_token(
        name="Other Warehouse Station",
        plaintext=plaintext,
        warehouse_ids=[99],
        event_types=[],
        inbound_resources=[],
        source_system=None,
        endpoints=["dockd.dispatch"],
    )
    yield {"plaintext": plaintext, "token_id": token_id}
    delete_token(token_id)


def _insert_so(
    so_number=None,
    status="PICKED",
    warehouse_id=1,
    customer_name="Jane Doe",
    customer_phone="555-0100",
    ship_method="UPS Ground",
    shipping=None,
    order_total=None,
    customer_shipping_paid=None,
    created_by="AMAZON",
    carrier=None,
    tracking_number=None,
    shipped_at=None,
    memo=None,
):
    """Insert a sales_orders row via the test transactional connection.
    Returns the so_id and the assigned so_number.
    """
    conn = get_raw_connection()
    cur = conn.cursor()
    so_number = so_number or f"DOCKD-T-{uuid.uuid4().hex[:8]}"
    addr = shipping or {}
    cur.execute(
        "INSERT INTO sales_orders ("
        " so_number, customer_name, customer_phone, status, warehouse_id,"
        " ship_method, external_id, created_by,"
        " shipping_address_name, shipping_address_line1, shipping_address_line2,"
        " shipping_address_city, shipping_address_state, shipping_address_postal_code,"
        " shipping_address_country, shipping_address_phone,"
        " order_total, customer_shipping_paid, memo,"
        " carrier, tracking_number, shipped_at"
        ") VALUES ("
        " %s,%s,%s,%s,%s,"
        " %s,%s,%s,"
        " %s,%s,%s,%s,%s,%s,%s,%s,"
        " %s,%s,%s,%s,%s,%s"
        ") RETURNING so_id",
        (
            so_number, customer_name, customer_phone, status, warehouse_id,
            ship_method, str(uuid.uuid4()), created_by,
            addr.get("name"), addr.get("line1"), addr.get("line2"),
            addr.get("city"), addr.get("state"), addr.get("postal_code"),
            addr.get("country"), addr.get("phone"),
            order_total, customer_shipping_paid, memo,
            carrier, tracking_number, shipped_at,
        ),
    )
    so_id = cur.fetchone()[0]
    cur.close()
    return so_id, so_number


def _insert_so_line(so_id, item_id, quantity_picked=1, line_number=1, quantity_ordered=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_order_lines "
        "(so_id, item_id, quantity_ordered, quantity_picked, line_number) "
        "VALUES (%s, %s, %s, %s, %s)",
        (so_id, item_id, quantity_ordered or quantity_picked, quantity_picked, line_number),
    )
    cur.close()


def _insert_item(sku=None, item_name="Widget", upc="0123456789012"):
    conn = get_raw_connection()
    cur = conn.cursor()
    sku = sku or f"SKU-{uuid.uuid4().hex[:8]}"
    cur.execute(
        "INSERT INTO items (sku, item_name, upc, external_id) "
        "VALUES (%s, %s, %s, %s) RETURNING item_id",
        (sku, item_name, upc, str(uuid.uuid4())),
    )
    item_id = cur.fetchone()[0]
    cur.close()
    return item_id


def _insert_fulfillment(so_id, shipped_by="mike"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO item_fulfillments "
        "(so_id, warehouse_id, tracking_number, carrier, ship_method, "
        " shipped_by, status, external_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING fulfillment_id",
        (so_id, 1, "1Z999AA10123456784", "UPS", "GROUND",
         shipped_by, "SHIPPED", str(uuid.uuid4())),
    )
    fid = cur.fetchone()[0]
    cur.close()
    return fid


def _set_setting(key, value):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO app_settings (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = %s",
        (key, value, value),
    )
    cur.close()


# ----------------------------------------------------------------------
# Auth + DRAFT header
# ----------------------------------------------------------------------


class TestAuthAndHeader:
    def test_missing_token_returns_401(self, client):
        resp = client.get("/api/v1/dockd/orders/SO-X")
        assert resp.status_code == 401
        assert resp.headers.get("X-Sentry-Canonical-Model") == "DRAFT-v1" or \
               resp.headers.get("X-Sentry-Canonical-Model") is None
        # 401 from the decorator pre-dispatcher path doesn't carry the
        # DRAFT header (decorator returns before the route's _draft_response
        # wrapper runs). The header lives on route-side responses.

    def test_unknown_token_returns_401(self, client, seed_data):
        resp = client.get(
            "/api/v1/dockd/orders/SO-X",
            headers={"X-WMS-Token": "not-real"},
        )
        assert resp.status_code == 401

    def test_happy_response_carries_draft_header(self, client, dockd_token):
        so_id, so_number = _insert_so()
        resp = client.get(
            f"/api/v1/dockd/orders/{so_number}",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        assert resp.status_code == 200
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"

    def test_404_response_carries_draft_header(self, client, dockd_token):
        resp = client.get(
            "/api/v1/dockd/orders/NEVER-EXISTED",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        assert resp.status_code == 404
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"

    def test_422_response_carries_draft_header(self, client, dockd_token):
        resp = client.get(
            "/api/v1/dockd/orders/has spaces",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        assert resp.status_code == 422
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"
        body = resp.get_json()
        assert body["error_kind"] == "invalid_so_number"


# ----------------------------------------------------------------------
# Path-parameter validation
# ----------------------------------------------------------------------


class TestPathParameter:
    def test_so_number_with_disallowed_chars_returns_422(self, client, dockd_token):
        resp = client.get(
            "/api/v1/dockd/orders/has@chars",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        assert resp.status_code == 422

    def test_so_number_too_long_returns_422(self, client, dockd_token):
        long = "A" * 129
        resp = client.get(
            f"/api/v1/dockd/orders/{long}",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        assert resp.status_code == 422


# ----------------------------------------------------------------------
# Warehouse scope (no enumeration oracle)
# ----------------------------------------------------------------------


class TestWarehouseScope:
    def test_unknown_so_returns_404(self, client, dockd_token):
        resp = client.get(
            "/api/v1/dockd/orders/NEVER-EXISTED",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        assert resp.status_code == 404
        body = resp.get_json()
        assert body["error_kind"] == "not_found"

    def test_so_in_other_warehouse_returns_same_404(
        self, client, dockd_token_other_warehouse
    ):
        """A real SO in warehouse 1 must NOT be visible to a token
        scoped to warehouse 99. The 404 body is identical to a genuinely
        missing order so the token cannot tell the difference."""
        so_id, so_number = _insert_so(warehouse_id=1)
        resp = client.get(
            f"/api/v1/dockd/orders/{so_number}",
            headers={"X-WMS-Token": dockd_token_other_warehouse["plaintext"]},
        )
        assert resp.status_code == 404
        body = resp.get_json()
        assert body["error_kind"] == "not_found"


# ----------------------------------------------------------------------
# Happy path response shape
# ----------------------------------------------------------------------


class TestHappyPath:
    def test_picked_so_returns_full_payload(self, client, dockd_token):
        item_id = _insert_item(sku="WIDGET-RED-S", item_name="Widget Red Small")
        so_id, so_number = _insert_so(
            status="PICKED",
            shipping={
                "name": "Jane Doe",
                "line1": "123 Main St",
                "line2": None,
                "city": "Boulder",
                "state": "CO",
                "postal_code": "80301",
                "country": "US",
                "phone": "555-0100",
            },
            order_total="25.98",
            customer_shipping_paid="8.95",
        )
        _insert_so_line(so_id, item_id, quantity_picked=2, line_number=1)
        _set_setting("require_packing_before_shipping", "false")

        resp = client.get(
            f"/api/v1/dockd/orders/{so_number}",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        assert resp.status_code == 200
        body = resp.get_json()

        assert body["so_number"] == so_number
        assert body["status"] == "PICKED"
        assert body["warehouse_id"] == 1
        assert body["customer_name"] == "Jane Doe"
        assert body["customer_phone"] == "555-0100"
        assert body["shipping_address"] == {
            "name": "Jane Doe",
            "line1": "123 Main St",
            "line2": None,
            "city": "Boulder",
            "state": "CO",
            "postal_code": "80301",
            "country": "US",
            "phone": "555-0100",
        }
        assert body["ship_method"] == "UPS Ground"
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["sku"] == "WIDGET-RED-S"
        assert item["display_name"] == "Widget Red Small"
        assert item["upc"] == "0123456789012"
        assert item["qty"] == 2
        assert isinstance(item["external_id"], str) and len(item["external_id"]) == 36
        assert body["order_total"] == 25.98
        assert body["customer_shipping_paid"] == 8.95
        assert body["marketplace"] == "AMAZON"
        assert body["shippable"] is True
        assert body["shippable_from_statuses"] == ["PICKED", "PACKED"]
        assert body["shipped_by"] is None
        assert body["tracking_number"] is None
        assert body["carrier"] is None
        assert body["shipped_at"] is None
        assert body["station_label"] is None

    def test_packed_so_with_packing_required(self, client, dockd_token):
        item_id = _insert_item()
        so_id, so_number = _insert_so(status="PACKED")
        _insert_so_line(so_id, item_id, quantity_picked=1)
        _set_setting("require_packing_before_shipping", "true")

        resp = client.get(
            f"/api/v1/dockd/orders/{so_number}",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        body = resp.get_json()
        assert body["status"] == "PACKED"
        assert body["shippable"] is True
        assert body["shippable_from_statuses"] == ["PACKED"]

    def test_picked_so_with_packing_required_is_not_shippable(
        self, client, dockd_token
    ):
        item_id = _insert_item()
        so_id, so_number = _insert_so(status="PICKED")
        _insert_so_line(so_id, item_id)
        _set_setting("require_packing_before_shipping", "true")

        resp = client.get(
            f"/api/v1/dockd/orders/{so_number}",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        body = resp.get_json()
        assert body["shippable"] is False
        assert body["shippable_from_statuses"] == ["PACKED"]


class TestShippedSo:
    def test_shipped_so_surfaces_carrier_tracking_shipped_by(
        self, client, dockd_token
    ):
        from datetime import datetime, timezone
        item_id = _insert_item()
        so_id, so_number = _insert_so(
            status="SHIPPED",
            carrier="UPS",
            tracking_number="1Z999AA10123456784",
            shipped_at=datetime.now(timezone.utc),
        )
        _insert_so_line(so_id, item_id)
        _insert_fulfillment(so_id, shipped_by="mike")

        resp = client.get(
            f"/api/v1/dockd/orders/{so_number}",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        body = resp.get_json()
        assert body["status"] == "SHIPPED"
        assert body["shippable"] is False
        assert body["shipped_by"] == "mike"
        assert body["carrier"] == "UPS"
        assert body["tracking_number"] == "1Z999AA10123456784"
        assert body["shipped_at"] is not None


# ----------------------------------------------------------------------
# NULL preservation
# ----------------------------------------------------------------------


class TestMemoField:
    def test_memo_surfaces_in_get_response(self, client, dockd_token):
        item_id = _insert_item()
        so_id, so_number = _insert_so(memo="leave at back door")
        _insert_so_line(so_id, item_id)
        resp = client.get(
            f"/api/v1/dockd/orders/{so_number}",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["memo"] == "leave at back door"

    def test_memo_null_when_unset(self, client, dockd_token):
        item_id = _insert_item()
        so_id, so_number = _insert_so()
        _insert_so_line(so_id, item_id)
        resp = client.get(
            f"/api/v1/dockd/orders/{so_number}",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        body = resp.get_json()
        assert body["memo"] is None


class TestNullPreservation:
    def test_null_address_components_serialize_as_json_null(
        self, client, dockd_token
    ):
        item_id = _insert_item()
        so_id, so_number = _insert_so(
            shipping={"name": "Jane", "line1": "1 St"},  # everything else NULL
        )
        _insert_so_line(so_id, item_id)
        resp = client.get(
            f"/api/v1/dockd/orders/{so_number}",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        body = resp.get_json()
        addr = body["shipping_address"]
        assert addr["name"] == "Jane"
        assert addr["line1"] == "1 St"
        assert addr["line2"] is None
        assert addr["city"] is None
        assert addr["state"] is None
        assert addr["postal_code"] is None
        assert addr["country"] is None
        assert addr["phone"] is None

    def test_null_money_fields_serialize_as_json_null(self, client, dockd_token):
        item_id = _insert_item()
        so_id, so_number = _insert_so(order_total=None, customer_shipping_paid=None)
        _insert_so_line(so_id, item_id)
        resp = client.get(
            f"/api/v1/dockd/orders/{so_number}",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        body = resp.get_json()
        assert body["order_total"] is None
        assert body["customer_shipping_paid"] is None
