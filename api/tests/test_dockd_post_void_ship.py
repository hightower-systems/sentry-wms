"""POST /api/v1/dockd/orders/<so_number>/void-ship contract (v1.9.0 dockd #6).

Coverage:
- happy path: SHIPPED -> reverted_to_status (PICKED or PACKED), response
  shape, ship.voided/1 emitted with source_txn_id = idempotency_key,
  fulfillment row's voided_at / voided_by / void_reason populated,
  sales_order_lines roll back to pre_ship_status with quantity_shipped=0.
- legacy void: SHIPPED order created before mig 054 has
  pre_ship_status='PICKED' from the in-migration backfill -> revert
  target is PICKED.
- 409 not_shipped: SO is in any non-SHIPPED status (PICKED, PACKED, OPEN)
  -> 409 with current_status in details.
- 404 not_found: unknown so_number AND wrong-warehouse (no enumeration).
- 422 invalid_so_number for malformed path parameter.
- 422 invalid_body: missing reason / operator_username / idempotency_key,
  reason over 500 chars, non-UUID4 idempotency_key, extra field.
- 422 unknown_operator.
- Idempotency replay: same key + same body -> 200 with
  X-Idempotent-Replay: true; same key + different body -> 409.
- Cross-endpoint reuse: ship's idempotency_key reused on void -> 409.
- DRAFT-v1 header on every response.
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

from _wms_token_helpers import insert_token
from db_test_context import get_raw_connection
from services import token_cache


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


@pytest.fixture()
def dockd_token(seed_data):
    plaintext = f"dockd-void-test-{uuid.uuid4()}"
    token_id = insert_token(
        name="Pack Station 4",
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
    plaintext = f"dockd-void-wh99-{uuid.uuid4()}"
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


def _insert_so(status="SHIPPED", warehouse_id=1, so_number=None, **kwargs):
    conn = get_raw_connection()
    cur = conn.cursor()
    so_number = so_number or f"DOCKD-VOID-{uuid.uuid4().hex[:8]}"
    cur.execute(
        "INSERT INTO sales_orders ("
        " so_number, customer_name, status, warehouse_id, external_id,"
        " carrier, tracking_number, shipped_at"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING so_id",
        (so_number, "Cust", status, warehouse_id, str(uuid.uuid4()),
         kwargs.get("carrier"), kwargs.get("tracking_number"), kwargs.get("shipped_at")),
    )
    so_id = cur.fetchone()[0]
    cur.close()
    return so_id, so_number


def _insert_item():
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO items (sku, item_name, upc, external_id) "
        "VALUES (%s, %s, %s, %s) RETURNING item_id",
        (f"SKU-{uuid.uuid4().hex[:8]}", "Widget", "0123456789012", str(uuid.uuid4())),
    )
    item_id = cur.fetchone()[0]
    cur.close()
    return item_id


def _insert_so_line(so_id, item_id, qty=2, line_number=1, status="SHIPPED"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_order_lines "
        "(so_id, item_id, quantity_ordered, quantity_picked, quantity_packed, "
        " quantity_shipped, line_number, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (so_id, item_id, qty, qty, qty, qty if status == "SHIPPED" else 0, line_number, status),
    )
    cur.close()


def _insert_fulfillment(so_id, pre_ship_status="PICKED", status="SHIPPED",
                        shipped_by="mike"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO item_fulfillments "
        "(so_id, warehouse_id, tracking_number, carrier, ship_method, "
        " shipped_by, status, external_id, pre_ship_status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING fulfillment_id",
        (so_id, 1, "1Z999AA10123456784", "UPS", "GROUND",
         shipped_by, status, str(uuid.uuid4()), pre_ship_status),
    )
    fid = cur.fetchone()[0]
    cur.close()
    return fid


def _seed_shipped(pre_ship_status="PICKED"):
    _ensure_user("mike")
    item_id = _insert_item()
    so_id, so_number = _insert_so(status="SHIPPED", carrier="UPS",
                                  tracking_number="1Z999AA10123456784")
    _insert_so_line(so_id, item_id, qty=2, status="SHIPPED")
    fid = _insert_fulfillment(so_id, pre_ship_status=pre_ship_status)
    return so_id, so_number, fid


def _void_body(idempotency_key=None, **overrides):
    body = {
        "reason": "wrong box dimensions",
        "operator_username": "mike",
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
    }
    body.update(overrides)
    return body


def _post_void(client, token, so_number, body):
    return client.post(
        f"/api/v1/dockd/orders/{so_number}/void-ship",
        json=body,
        headers={"X-WMS-Token": token},
    )


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


class TestHappyPath:
    def test_shipped_to_picked(self, client, dockd_token):
        so_id, so_number, fid = _seed_shipped(pre_ship_status="PICKED")
        key = str(uuid.uuid4())
        resp = _post_void(client, dockd_token["plaintext"], so_number,
                          _void_body(idempotency_key=key))
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["status"] == "PICKED"
        assert body["voided_at"].endswith("Z")
        assert body["audit_log_id"] is not None
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"

    def test_shipped_to_packed(self, client, dockd_token):
        so_id, so_number, fid = _seed_shipped(pre_ship_status="PACKED")
        resp = _post_void(client, dockd_token["plaintext"], so_number, _void_body())
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "PACKED"

    def test_so_status_reverts_and_ship_fields_clear(self, client, dockd_token):
        so_id, so_number, fid = _seed_shipped()
        _post_void(client, dockd_token["plaintext"], so_number, _void_body())
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT status, tracking_number, carrier, shipped_at "
            "FROM sales_orders WHERE so_id = %s",
            (so_id,),
        )
        row = cur.fetchone()
        cur.close()
        assert row[0] == "PICKED"
        assert row[1] is None
        assert row[2] is None
        assert row[3] is None

    def test_fulfillment_marked_voided_with_attribution(self, client, dockd_token):
        so_id, so_number, fid = _seed_shipped()
        _post_void(client, dockd_token["plaintext"], so_number,
                   _void_body(reason="wrong box"))
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT status, voided_at, voided_by, void_reason "
            "FROM item_fulfillments WHERE fulfillment_id = %s",
            (fid,),
        )
        row = cur.fetchone()
        cur.close()
        assert row[0] == "VOIDED"
        assert row[1] is not None
        assert row[2] == "mike"
        assert row[3] == "wrong box"

    def test_sales_order_lines_roll_back(self, client, dockd_token):
        so_id, so_number, fid = _seed_shipped(pre_ship_status="PICKED")
        _post_void(client, dockd_token["plaintext"], so_number, _void_body())
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT quantity_shipped, status FROM sales_order_lines WHERE so_id = %s",
            (so_id,),
        )
        rows = cur.fetchall()
        cur.close()
        assert len(rows) == 1
        assert rows[0][0] == 0
        assert rows[0][1] == "PICKED"

    def test_ship_voided_event_emitted_with_idempotency_key_as_source_txn_id(
        self, client, dockd_token
    ):
        so_id, so_number, fid = _seed_shipped()
        key = str(uuid.uuid4())
        _post_void(client, dockd_token["plaintext"], so_number,
                   _void_body(idempotency_key=key))
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT event_type, source_txn_id::text, payload "
            "FROM integration_events "
            "WHERE aggregate_id = %s AND event_type = 'ship.voided' "
            "ORDER BY event_id DESC LIMIT 1",
            (so_id,),
        )
        row = cur.fetchone()
        cur.close()
        assert row[0] == "ship.voided"
        assert row[1] == key
        # payload is jsonb -> psycopg2 returns as dict
        payload = row[2]
        assert payload["reason"] == "wrong box dimensions"
        assert payload["reverted_to_status"] == "PICKED"


class TestLegacyVoid:
    def test_pre_migration_ship_with_backfilled_picked(self, client, dockd_token):
        # Mig 054 backfilled pre_ship_status='PICKED' for legacy SHIPPED
        # rows. The void route reads that column and reverts to PICKED.
        so_id, so_number, fid = _seed_shipped(pre_ship_status="PICKED")
        resp = _post_void(client, dockd_token["plaintext"], so_number, _void_body())
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "PICKED"


# ----------------------------------------------------------------------
# Status guard
# ----------------------------------------------------------------------


class TestNotShipped:
    @pytest.mark.parametrize("status", ["PICKED", "PACKED", "OPEN"])
    def test_non_shipped_returns_409(self, client, dockd_token, status):
        _ensure_user()
        item_id = _insert_item()
        so_id, so_number = _insert_so(status=status)
        _insert_so_line(so_id, item_id, status=status)
        resp = _post_void(client, dockd_token["plaintext"], so_number, _void_body())
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["error_kind"] == "not_shipped"
        assert body["details"]["current_status"] == status


# ----------------------------------------------------------------------
# Scope + path
# ----------------------------------------------------------------------


class TestScopeAndPath:
    def test_unknown_so_returns_404(self, client, dockd_token):
        _ensure_user()
        resp = _post_void(client, dockd_token["plaintext"], "NEVER", _void_body())
        assert resp.status_code == 404
        assert resp.get_json()["error_kind"] == "not_found"

    def test_other_warehouse_returns_404(self, client, dockd_token_other_warehouse):
        _ensure_user()
        so_id, so_number, fid = _seed_shipped()
        resp = _post_void(client, dockd_token_other_warehouse["plaintext"],
                          so_number, _void_body())
        assert resp.status_code == 404

    def test_invalid_so_number_returns_422(self, client, dockd_token):
        resp = _post_void(client, dockd_token["plaintext"], "has spaces", _void_body())
        assert resp.status_code == 422
        assert resp.get_json()["error_kind"] == "invalid_so_number"


# ----------------------------------------------------------------------
# Body validation
# ----------------------------------------------------------------------


class TestBodyValidation:
    def test_missing_reason_returns_422(self, client, dockd_token):
        body = _void_body()
        del body["reason"]
        resp = _post_void(client, dockd_token["plaintext"], "SO-X", body)
        assert resp.status_code == 422
        assert resp.get_json()["error_kind"] == "invalid_body"

    def test_missing_operator_username_returns_422(self, client, dockd_token):
        body = _void_body()
        del body["operator_username"]
        resp = _post_void(client, dockd_token["plaintext"], "SO-X", body)
        assert resp.status_code == 422

    def test_missing_idempotency_key_returns_422(self, client, dockd_token):
        body = _void_body()
        del body["idempotency_key"]
        resp = _post_void(client, dockd_token["plaintext"], "SO-X", body)
        assert resp.status_code == 422

    def test_reason_too_long_returns_422(self, client, dockd_token):
        body = _void_body(reason="A" * 501)
        resp = _post_void(client, dockd_token["plaintext"], "SO-X", body)
        assert resp.status_code == 422

    def test_extra_field_returns_422(self, client, dockd_token):
        body = _void_body()
        body["station_label"] = "spoofed"
        resp = _post_void(client, dockd_token["plaintext"], "SO-X", body)
        assert resp.status_code == 422

    def test_non_uuid4_idempotency_key_returns_422(self, client, dockd_token):
        body = _void_body()
        body["idempotency_key"] = "not-a-uuid"
        resp = _post_void(client, dockd_token["plaintext"], "SO-X", body)
        assert resp.status_code == 422
        assert "idempotency_key" in resp.get_json()["details"]["field"]


class TestUnknownOperator:
    def test_unknown_operator_returns_422(self, client, dockd_token):
        so_id, so_number, fid = _seed_shipped()
        body = _void_body(operator_username=f"never-{uuid.uuid4().hex[:8]}")
        resp = _post_void(client, dockd_token["plaintext"], so_number, body)
        assert resp.status_code == 422
        assert resp.get_json()["error_kind"] == "unknown_operator"


# ----------------------------------------------------------------------
# Idempotency
# ----------------------------------------------------------------------


class TestIdempotencyReplay:
    def test_same_key_same_body_replays(self, client, dockd_token):
        so_id, so_number, fid = _seed_shipped()
        key = str(uuid.uuid4())
        first = _post_void(client, dockd_token["plaintext"], so_number,
                           _void_body(idempotency_key=key))
        assert first.status_code == 200
        first_body = first.get_json()

        second = _post_void(client, dockd_token["plaintext"], so_number,
                            _void_body(idempotency_key=key))
        assert second.status_code == 200
        assert second.headers.get("X-Idempotent-Replay") == "true"
        assert second.get_json() == first_body

    def test_replay_does_not_reemit_event(self, client, dockd_token):
        so_id, so_number, fid = _seed_shipped()
        key = str(uuid.uuid4())
        _post_void(client, dockd_token["plaintext"], so_number,
                   _void_body(idempotency_key=key))
        _post_void(client, dockd_token["plaintext"], so_number,
                   _void_body(idempotency_key=key))
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM integration_events "
            "WHERE aggregate_id = %s AND event_type = 'ship.voided'",
            (so_id,),
        )
        count = cur.fetchone()[0]
        cur.close()
        assert count == 1


class TestIdempotencyMismatch:
    def test_same_key_different_body_returns_409(self, client, dockd_token):
        so_id, so_number, fid = _seed_shipped()
        key = str(uuid.uuid4())
        first = _post_void(client, dockd_token["plaintext"], so_number,
                           _void_body(idempotency_key=key, reason="A"))
        assert first.status_code == 200
        second = _post_void(client, dockd_token["plaintext"], so_number,
                            _void_body(idempotency_key=key, reason="B"))
        assert second.status_code == 409
        assert second.get_json()["error_kind"] == "idempotency_key_reused_with_different_body"


class TestCrossEndpointReuse:
    def test_ship_key_reused_on_void_returns_409(self, client, dockd_token):
        # Use a key that was first claimed by /ship; sending the same key
        # to /void-ship must 409 because the cached endpoint differs.
        _ensure_user()
        item_id = _insert_item()
        so_id, so_number = _insert_so(status="PICKED")
        _insert_so_line(so_id, item_id, status="PICKED")

        # First, ship via the same token + key.
        ship_key = str(uuid.uuid4())
        ship_body = {
            "tracking": "1Z999AA10123456784",
            "carrier": "UPS",
            "ship_method": "UPS Ground",
            "operator_username": "mike",
            "manual_link": False,
            "idempotency_key": ship_key,
        }
        # Disable packing-required so PICKED ships.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO app_settings (key, value) VALUES "
            "('require_packing_before_shipping','false') "
            "ON CONFLICT (key) DO UPDATE SET value = 'false'"
        )
        cur.close()

        ship_resp = client.post(
            f"/api/v1/dockd/orders/{so_number}/ship",
            json=ship_body,
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        assert ship_resp.status_code == 200

        # Now try /void-ship with the same key but a different body.
        void_resp = _post_void(client, dockd_token["plaintext"], so_number,
                               _void_body(idempotency_key=ship_key))
        assert void_resp.status_code == 409
        assert void_resp.get_json()["error_kind"] == "idempotency_key_reused_with_different_body"


# ----------------------------------------------------------------------
# DRAFT header
# ----------------------------------------------------------------------


class TestDraftHeader:
    def test_404_carries_draft_header(self, client, dockd_token):
        _ensure_user()
        resp = _post_void(client, dockd_token["plaintext"], "NEVER", _void_body())
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"

    def test_409_carries_draft_header(self, client, dockd_token):
        _ensure_user()
        item_id = _insert_item()
        so_id, so_number = _insert_so(status="PACKED")
        _insert_so_line(so_id, item_id, status="PACKED")
        resp = _post_void(client, dockd_token["plaintext"], so_number, _void_body())
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"

    def test_422_carries_draft_header(self, client, dockd_token):
        body = _void_body()
        del body["reason"]
        resp = _post_void(client, dockd_token["plaintext"], "SO-X", body)
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"
