"""POST /api/v1/dockd/orders/<so_number>/ship contract (v1.9.0 dockd #5).

Coverage:
- happy path: PICKED -> SHIPPED, response shape, ship.confirmed/1
  emitted with source_txn_id = idempotency_key, dockd_idempotency
  row populated.
- packing-required toggle: PACKED accepted, PICKED rejected with
  410 not_in_shippable_status when packing is required.
- already-SHIPPED: 409 already_shipped with existing tracking.
- 404 not_found for unknown so_number AND wrong warehouse (no
  enumeration oracle).
- 422 invalid_so_number for malformed path parameter.
- Pydantic body: 422 invalid_body for missing required, extra
  property, invalid UUID4 idempotency_key, length overflow.
- 422 unknown_operator when operator_username is not a Sentry
  users row.
- Idempotency replay: same key + same body -> 200 with
  X-Idempotent-Replay: true and the cached response.
- Idempotency mismatch: same key + different body -> 409
  idempotency_key_reused_with_different_body.
- Body cap: 413 when content-length exceeds the dockd cap.
- DRAFT-v1 header on every response.
"""

import os
import sys
import uuid
import json

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from _wms_token_helpers import insert_token
from db_test_context import get_raw_connection
from services import token_cache


# IMPORTANT: do NOT delete_token() in fixture teardown. The route INSERTs
# into dockd_idempotency (FK to wms_tokens) within the test's outer
# transaction, which holds a share lock on the wms_tokens row. A
# separate autocommit connection running DELETE FROM wms_tokens at
# fixture teardown blocks on that share lock; the outer transaction's
# rollback (which would release the lock) runs later in the LIFO
# teardown order. Session-start TRUNCATE in conftest is the cleanup
# path; within-session accumulation is harmless because each test
# inserts a unique plaintext + hash.


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


@pytest.fixture()
def dockd_token(seed_data):
    plaintext = f"dockd-ship-test-{uuid.uuid4()}"
    token_id = insert_token(
        name="Pack Station 3",
        plaintext=plaintext,
        warehouse_ids=[1],
        event_types=[],
        inbound_resources=[],
        source_system=None,
        endpoints=["dockd.dispatch"],
    )
    return {"plaintext": plaintext, "token_id": token_id}


@pytest.fixture()
def dockd_token_other_warehouse(seed_data):
    plaintext = f"dockd-ship-wh99-{uuid.uuid4()}"
    token_id = insert_token(
        name="Other WH",
        plaintext=plaintext,
        warehouse_ids=[99],
        event_types=[],
        inbound_resources=[],
        source_system=None,
        endpoints=["dockd.dispatch"],
    )
    return {"plaintext": plaintext, "token_id": token_id}


def _ensure_user(username="mike"):
    """Insert a users row if the username isn't already in the seed data."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE username = %s", (username,))
    row = cur.fetchone()
    if row is not None:
        cur.close()
        return row[0]
    cur.execute(
        "INSERT INTO users (username, password_hash, full_name, role, external_id) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING user_id",
        (username,
         "$2b$12$placeholderHashForTests000000000000000000000000000000",
         username.title(), "USER", str(uuid.uuid4())),
    )
    user_id = cur.fetchone()[0]
    cur.close()
    return user_id


def _insert_so(status="PICKED", warehouse_id=1, so_number=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    so_number = so_number or f"DOCKD-SHIP-{uuid.uuid4().hex[:8]}"
    cur.execute(
        "INSERT INTO sales_orders ("
        " so_number, customer_name, status, warehouse_id, external_id"
        ") VALUES (%s, %s, %s, %s, %s) RETURNING so_id",
        (so_number, "Cust", status, warehouse_id, str(uuid.uuid4())),
    )
    so_id = cur.fetchone()[0]
    cur.close()
    return so_id, so_number


def _insert_item(sku=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    sku = sku or f"SKU-{uuid.uuid4().hex[:8]}"
    cur.execute(
        "INSERT INTO items (sku, item_name, upc, external_id) "
        "VALUES (%s, %s, %s, %s) RETURNING item_id",
        (sku, "Widget", "0123456789012", str(uuid.uuid4())),
    )
    item_id = cur.fetchone()[0]
    cur.close()
    return item_id


def _insert_so_line(so_id, item_id, qty=1, line_number=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_order_lines "
        "(so_id, item_id, quantity_ordered, quantity_picked, quantity_packed, line_number) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (so_id, item_id, qty, qty, qty, line_number),
    )
    cur.close()


def _set_setting(key, value):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO app_settings (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = %s",
        (key, value, value),
    )
    cur.close()


def _seed_shippable(status="PICKED"):
    _ensure_user("mike")
    _set_setting("require_packing_before_shipping", "false")
    item_id = _insert_item()
    so_id, so_number = _insert_so(status=status)
    _insert_so_line(so_id, item_id, qty=2, line_number=1)
    return so_id, so_number


def _ship_body(idempotency_key=None, **overrides):
    body = {
        "tracking": "1Z999AA10123456784",
        "carrier": "UPS",
        "ship_method": "UPS Ground",
        "operator_username": "mike",
        "shipping_cost": "12.45",
        "weight": 2.5,
        "dims": {"l": 12, "w": 8, "h": 6},
        "manual_link": False,
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
    }
    body.update(overrides)
    return body


def _post_ship(client, token, so_number, body):
    return client.post(
        f"/api/v1/dockd/orders/{so_number}/ship",
        json=body,
        headers={"X-WMS-Token": token},
    )


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


class TestHappyPath:
    def test_picked_to_shipped(self, client, dockd_token):
        so_id, so_number = _seed_shippable(status="PICKED")
        key = str(uuid.uuid4())
        resp = _post_ship(client, dockd_token["plaintext"], so_number, _ship_body(idempotency_key=key))
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "SHIPPED"
        assert body["tracking"] == "1Z999AA10123456784"
        assert body["fulfillment_id"] is not None
        assert body["audit_log_id"] is not None
        assert body["shipped_at"].endswith("Z")
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"

    def test_so_status_flipped_to_shipped(self, client, dockd_token):
        so_id, so_number = _seed_shippable(status="PICKED")
        _post_ship(client, dockd_token["plaintext"], so_number, _ship_body())
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT status, tracking_number, carrier FROM sales_orders WHERE so_id = %s",
            (so_id,),
        )
        row = cur.fetchone()
        cur.close()
        assert row[0] == "SHIPPED"
        assert row[1] == "1Z999AA10123456784"
        assert row[2] == "UPS"

    def test_fulfillment_carries_pre_ship_status_and_shipping_cost(
        self, client, dockd_token
    ):
        so_id, so_number = _seed_shippable(status="PICKED")
        _post_ship(client, dockd_token["plaintext"], so_number, _ship_body())
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT pre_ship_status, shipping_cost, shipped_by FROM item_fulfillments "
            "WHERE so_id = %s",
            (so_id,),
        )
        row = cur.fetchone()
        cur.close()
        assert row[0] == "PICKED"
        assert float(row[1]) == 12.45
        assert row[2] == "mike"

    def test_idempotency_row_cached(self, client, dockd_token):
        so_id, so_number = _seed_shippable()
        key = str(uuid.uuid4())
        _post_ship(client, dockd_token["plaintext"], so_number, _ship_body(idempotency_key=key))
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT endpoint, so_number, response_status, response_body IS NOT NULL "
            "FROM dockd_idempotency WHERE token_id = %s AND idempotency_key = %s",
            (dockd_token["token_id"], key),
        )
        row = cur.fetchone()
        cur.close()
        assert row[0] == "ship"
        assert row[1] == so_number
        assert row[2] == 200
        assert row[3] is True

    def test_ship_confirmed_emitted_with_idempotency_key_as_source_txn_id(
        self, client, dockd_token
    ):
        so_id, so_number = _seed_shippable()
        key = str(uuid.uuid4())
        _post_ship(client, dockd_token["plaintext"], so_number, _ship_body(idempotency_key=key))
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT event_type, source_txn_id::text "
            "FROM integration_events "
            "WHERE aggregate_id = %s AND event_type = 'ship.confirmed' "
            "ORDER BY event_id DESC LIMIT 1",
            (so_id,),
        )
        row = cur.fetchone()
        cur.close()
        assert row[0] == "ship.confirmed"
        assert row[1] == key


# ----------------------------------------------------------------------
# Status gate
# ----------------------------------------------------------------------


class TestStatusGate:
    def test_packed_with_packing_required(self, client, dockd_token):
        _ensure_user()
        _set_setting("require_packing_before_shipping", "true")
        item_id = _insert_item()
        so_id, so_number = _insert_so(status="PACKED")
        _insert_so_line(so_id, item_id)
        resp = _post_ship(client, dockd_token["plaintext"], so_number, _ship_body())
        assert resp.status_code == 200

    def test_picked_with_packing_required_returns_410(self, client, dockd_token):
        _ensure_user()
        _set_setting("require_packing_before_shipping", "true")
        item_id = _insert_item()
        so_id, so_number = _insert_so(status="PICKED")
        _insert_so_line(so_id, item_id)
        resp = _post_ship(client, dockd_token["plaintext"], so_number, _ship_body())
        assert resp.status_code == 410
        body = resp.get_json()
        assert body["error_kind"] == "not_in_shippable_status"
        assert body["details"]["current_status"] == "PICKED"
        assert body["details"]["allowed_statuses"] == ["PACKED"]

    def test_open_status_returns_410(self, client, dockd_token):
        _ensure_user()
        _set_setting("require_packing_before_shipping", "false")
        item_id = _insert_item()
        so_id, so_number = _insert_so(status="OPEN")
        _insert_so_line(so_id, item_id)
        resp = _post_ship(client, dockd_token["plaintext"], so_number, _ship_body())
        assert resp.status_code == 410
        assert resp.get_json()["details"]["current_status"] == "OPEN"


class TestAlreadyShipped:
    def test_already_shipped_returns_409_with_existing_tracking(
        self, client, dockd_token
    ):
        _ensure_user()
        _set_setting("require_packing_before_shipping", "false")
        item_id = _insert_item()
        so_id, so_number = _insert_so(status="PICKED")
        _insert_so_line(so_id, item_id)
        # First ship.
        first = _post_ship(client, dockd_token["plaintext"], so_number, _ship_body())
        assert first.status_code == 200
        # Second ship attempt with a fresh idempotency_key.
        second = _post_ship(
            client, dockd_token["plaintext"], so_number,
            _ship_body(idempotency_key=str(uuid.uuid4())),
        )
        assert second.status_code == 409
        body = second.get_json()
        assert body["error_kind"] == "already_shipped"
        assert body["details"]["existing_tracking"] == "1Z999AA10123456784"
        assert body["details"]["carrier"] == "UPS"
        assert body["details"]["shipped_by"] == "mike"


# ----------------------------------------------------------------------
# Warehouse scope + path validation
# ----------------------------------------------------------------------


class TestScopeAnd404:
    def test_unknown_so_returns_404(self, client, dockd_token):
        _ensure_user()
        resp = _post_ship(client, dockd_token["plaintext"], "NEVER-SHIPPED", _ship_body())
        assert resp.status_code == 404
        assert resp.get_json()["error_kind"] == "not_found"

    def test_other_warehouse_returns_404(
        self, client, dockd_token_other_warehouse
    ):
        _ensure_user()
        so_id, so_number = _seed_shippable()  # warehouse 1
        resp = _post_ship(
            client, dockd_token_other_warehouse["plaintext"], so_number, _ship_body(),
        )
        assert resp.status_code == 404


class TestPathValidation:
    def test_invalid_so_number_returns_422(self, client, dockd_token):
        resp = _post_ship(client, dockd_token["plaintext"], "has spaces", _ship_body())
        assert resp.status_code == 422
        assert resp.get_json()["error_kind"] == "invalid_so_number"


# ----------------------------------------------------------------------
# Body validation
# ----------------------------------------------------------------------


class TestBodyValidation:
    def test_missing_tracking_returns_422(self, client, dockd_token):
        body = _ship_body()
        del body["tracking"]
        resp = _post_ship(client, dockd_token["plaintext"], "SO-X", body)
        assert resp.status_code == 422
        assert resp.get_json()["error_kind"] == "invalid_body"

    def test_extra_field_returns_422(self, client, dockd_token):
        body = _ship_body()
        body["station_label"] = "spoofed"
        resp = _post_ship(client, dockd_token["plaintext"], "SO-X", body)
        assert resp.status_code == 422
        assert resp.get_json()["error_kind"] == "invalid_body"

    def test_non_uuid4_idempotency_key_returns_422(self, client, dockd_token):
        body = _ship_body()
        body["idempotency_key"] = "not-a-uuid"
        resp = _post_ship(client, dockd_token["plaintext"], "SO-X", body)
        assert resp.status_code == 422
        body_resp = resp.get_json()
        assert body_resp["error_kind"] == "invalid_body"
        assert "idempotency_key" in body_resp["details"]["field"]

    def test_uuid_v1_idempotency_key_returns_422(self, client, dockd_token):
        # uuid.uuid1 produces a v1 UUID; UUID4 type should reject it.
        body = _ship_body()
        body["idempotency_key"] = str(uuid.uuid1())
        resp = _post_ship(client, dockd_token["plaintext"], "SO-X", body)
        assert resp.status_code == 422

    def test_tracking_too_long_returns_422(self, client, dockd_token):
        body = _ship_body(tracking="A" * 101)
        resp = _post_ship(client, dockd_token["plaintext"], "SO-X", body)
        assert resp.status_code == 422


class TestUnknownOperator:
    def test_unknown_operator_returns_422(self, client, dockd_token):
        so_id, so_number = _seed_shippable()
        body = _ship_body(operator_username=f"never-{uuid.uuid4().hex[:8]}")
        resp = _post_ship(client, dockd_token["plaintext"], so_number, body)
        assert resp.status_code == 422
        body_resp = resp.get_json()
        assert body_resp["error_kind"] == "unknown_operator"


# ----------------------------------------------------------------------
# Idempotency
# ----------------------------------------------------------------------


class TestIdempotencyReplay:
    def test_same_key_same_body_replays_cached(self, client, dockd_token):
        so_id, so_number = _seed_shippable()
        key = str(uuid.uuid4())
        first = _post_ship(client, dockd_token["plaintext"], so_number, _ship_body(idempotency_key=key))
        assert first.status_code == 200
        first_body = first.get_json()

        second = _post_ship(client, dockd_token["plaintext"], so_number, _ship_body(idempotency_key=key))
        assert second.status_code == 200
        assert second.headers.get("X-Idempotent-Replay") == "true"
        assert second.get_json() == first_body

    def test_replay_does_not_reemit_event(self, client, dockd_token):
        so_id, so_number = _seed_shippable()
        key = str(uuid.uuid4())
        _post_ship(client, dockd_token["plaintext"], so_number, _ship_body(idempotency_key=key))
        _post_ship(client, dockd_token["plaintext"], so_number, _ship_body(idempotency_key=key))
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM integration_events "
            "WHERE aggregate_id = %s AND event_type = 'ship.confirmed'",
            (so_id,),
        )
        count = cur.fetchone()[0]
        cur.close()
        assert count == 1


class TestIdempotencyMismatch:
    def test_same_key_different_body_returns_409(self, client, dockd_token):
        so_id, so_number = _seed_shippable()
        key = str(uuid.uuid4())
        first = _post_ship(client, dockd_token["plaintext"], so_number, _ship_body(idempotency_key=key))
        assert first.status_code == 200
        # Different tracking number -> different body hash.
        second = _post_ship(
            client, dockd_token["plaintext"], so_number,
            _ship_body(idempotency_key=key, tracking="9X9XXXXXXXXXX"),
        )
        assert second.status_code == 409
        assert second.get_json()["error_kind"] == "idempotency_key_reused_with_different_body"


# ----------------------------------------------------------------------
# DRAFT header + body cap
# ----------------------------------------------------------------------


class TestDraftHeader:
    def test_404_carries_draft_header(self, client, dockd_token):
        _ensure_user()
        resp = _post_ship(client, dockd_token["plaintext"], "NEVER", _ship_body())
        assert resp.status_code == 404
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"

    def test_422_carries_draft_header(self, client, dockd_token):
        body = _ship_body()
        del body["tracking"]
        resp = _post_ship(client, dockd_token["plaintext"], "SO-X", body)
        assert resp.status_code == 422
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"


class TestBodyCap:
    def test_oversize_content_length_returns_413(self, client, dockd_token, monkeypatch):
        monkeypatch.setenv("SENTRY_DOCKD_MAX_BODY_KB", "16")
        # Build a payload guaranteed to exceed 16 KB once Pydantic
        # parses it. Tracking up to 100 chars; pad ship_method via
        # comments? extra='forbid' so no padding fields. Easiest: send
        # a content-length header that overshoots. Flask test_client
        # computes content-length from the JSON body length, so make
        # the body itself big by inflating ship_method (50 char cap).
        # Better: simulate over-cap by sending tracking with surrounding
        # whitespace would still fit. Use raw data to set content-length
        # explicitly.
        oversize = "{" + ('"x":"' + ("a" * 17_000) + '"') + "}"
        resp = client.post(
            "/api/v1/dockd/orders/SO-X/ship",
            data=oversize,
            content_type="application/json",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        assert resp.status_code == 413
        assert resp.get_json()["error_kind"] == "body_too_large"
