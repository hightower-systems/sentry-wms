"""End-to-end integration tests for the v1.9.0 dockd surface.

The per-route test files (test_dockd_get_order, test_dockd_post_ship,
test_dockd_post_void_ship) cover individual endpoint contracts. This
file covers the cross-cutting integration value:

- Full lifecycle: GET sees PICKED -> POST /ship -> GET sees SHIPPED ->
  POST /void-ship -> GET sees PICKED again -> re-ship succeeds. Verifies
  state across the three routes is consistent.
- Audit chain integrity (mig 047 verify_audit_log_chain()) after a
  ship + void + re-ship cycle. Catches missing or out-of-order audit
  rows that would silently break the hash chain.
- Outbox event ordering: integration_events carries ship.confirmed ->
  ship.voided -> ship.confirmed in monotonic event_id order with
  source_txn_id matching each request's idempotency_key.
- Idempotency-race indirect coverage: pre-insert a fully-populated
  dockd_idempotency row to simulate a peer-committed scenario, then
  re-issue with the same key + same body (replay) and with a different
  body (409). The sentinel ON CONFLICT branch in routes/dockd.py
  exercises the same code path under real concurrency; this test
  proves the post-conflict logic is correct without needing real
  threads.
- Two-station already_shipped: a second dockd token in the same
  warehouse hitting /ship after a first station has already shipped
  -> 409 already_shipped with the first station's tracking. The race
  is serialized by SELECT...FOR UPDATE; the result-after-serialization
  is what dockd's UI renders to the second operator.
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

from _wms_token_helpers import insert_token
from db_test_context import get_raw_connection
from services import token_cache


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


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


def _set_setting(key, value):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO app_settings (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = %s",
        (key, value, value),
    )
    cur.close()


def _insert_so(status="PICKED", warehouse_id=1, so_number=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    so_number = so_number or f"DOCKD-E2E-{uuid.uuid4().hex[:8]}"
    cur.execute(
        "INSERT INTO sales_orders ("
        " so_number, customer_name, customer_phone, status, warehouse_id,"
        " ship_method, external_id, created_by"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING so_id",
        (so_number, "Jane Doe", "555-0100", status, warehouse_id,
         "UPS Ground", str(uuid.uuid4()), "AMAZON"),
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
        (f"SKU-{uuid.uuid4().hex[:8]}", "Widget Red Small",
         "0123456789012", str(uuid.uuid4())),
    )
    item_id = cur.fetchone()[0]
    cur.close()
    return item_id


def _insert_so_line(so_id, item_id, qty=2):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_order_lines "
        "(so_id, item_id, quantity_ordered, quantity_picked, quantity_packed, "
        " line_number, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (so_id, item_id, qty, qty, qty, 1, "PICKED"),
    )
    cur.close()


def _verify_audit_chain():
    """Returns the log_id where the chain is broken, or None when intact."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT verify_audit_log_chain()")
    (broken,) = cur.fetchone()
    cur.close()
    return broken


@pytest.fixture()
def dockd_token(seed_data):
    plaintext = f"dockd-e2e-{uuid.uuid4()}"
    token_id = insert_token(
        name="Pack Station 1",
        plaintext=plaintext,
        warehouse_ids=[1],
        event_types=[],
        inbound_resources=[],
        source_system=None,
        endpoints=["dockd.dispatch"],
    )
    return {"plaintext": plaintext, "token_id": token_id, "name": "Pack Station 1"}


@pytest.fixture()
def dockd_token_station2(seed_data):
    plaintext = f"dockd-e2e-s2-{uuid.uuid4()}"
    token_id = insert_token(
        name="Pack Station 2",
        plaintext=plaintext,
        warehouse_ids=[1],
        event_types=[],
        inbound_resources=[],
        source_system=None,
        endpoints=["dockd.dispatch"],
    )
    return {"plaintext": plaintext, "token_id": token_id, "name": "Pack Station 2"}


def _ship_body(idempotency_key=None, **overrides):
    body = {
        "tracking": "1Z999AA10123456784",
        "carrier": "UPS",
        "ship_method": "UPS Ground",
        "operator_username": "mike",
        "manual_link": False,
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
    }
    body.update(overrides)
    return body


def _void_body(idempotency_key=None, **overrides):
    body = {
        "reason": "wrong box dimensions",
        "operator_username": "mike",
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
    }
    body.update(overrides)
    return body


# ----------------------------------------------------------------------
# Full lifecycle
# ----------------------------------------------------------------------


class TestFullLifecycle:
    def test_get_ship_get_void_get_reship(self, client, dockd_token):
        """GET PICKED -> POST /ship -> GET SHIPPED -> POST /void-ship ->
        GET PICKED -> POST /ship (fresh idempotency_key) succeeds."""
        _ensure_user("mike")
        _set_setting("require_packing_before_shipping", "false")
        item_id = _insert_item()
        so_id, so_number = _insert_so(status="PICKED")
        _insert_so_line(so_id, item_id, qty=2)
        token = dockd_token["plaintext"]

        # 1. GET sees PICKED + shippable=true.
        r = client.get(
            f"/api/v1/dockd/orders/{so_number}",
            headers={"X-WMS-Token": token},
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body["status"] == "PICKED"
        assert body["shippable"] is True
        assert len(body["items"]) == 1
        assert body["items"][0]["qty"] == 2

        # 2. POST /ship.
        ship_r = client.post(
            f"/api/v1/dockd/orders/{so_number}/ship",
            json=_ship_body(),
            headers={"X-WMS-Token": token},
        )
        assert ship_r.status_code == 200
        assert ship_r.get_json()["status"] == "SHIPPED"

        # 3. GET sees SHIPPED + ship-state fields populated.
        r = client.get(
            f"/api/v1/dockd/orders/{so_number}",
            headers={"X-WMS-Token": token},
        )
        body = r.get_json()
        assert body["status"] == "SHIPPED"
        assert body["shippable"] is False
        assert body["tracking_number"] == "1Z999AA10123456784"
        assert body["carrier"] == "UPS"
        assert body["shipped_by"] == "mike"

        # 4. POST /void-ship.
        void_r = client.post(
            f"/api/v1/dockd/orders/{so_number}/void-ship",
            json=_void_body(),
            headers={"X-WMS-Token": token},
        )
        assert void_r.status_code == 200
        assert void_r.get_json()["status"] == "PICKED"

        # 5. GET sees PICKED + shippable=true again, ship-state cleared.
        r = client.get(
            f"/api/v1/dockd/orders/{so_number}",
            headers={"X-WMS-Token": token},
        )
        body = r.get_json()
        assert body["status"] == "PICKED"
        assert body["shippable"] is True
        assert body["tracking_number"] is None
        assert body["carrier"] is None
        assert body["shipped_at"] is None

        # 6. Re-ship with a fresh idempotency_key succeeds.
        reship_r = client.post(
            f"/api/v1/dockd/orders/{so_number}/ship",
            json=_ship_body(tracking="1Z999AA10987654321"),
            headers={"X-WMS-Token": token},
        )
        assert reship_r.status_code == 200
        assert reship_r.get_json()["tracking"] == "1Z999AA10987654321"

    def test_lifecycle_emits_three_outbox_events_in_order(self, client, dockd_token):
        _ensure_user("mike")
        _set_setting("require_packing_before_shipping", "false")
        item_id = _insert_item()
        so_id, so_number = _insert_so(status="PICKED")
        _insert_so_line(so_id, item_id)
        token = dockd_token["plaintext"]

        ship1_key = str(uuid.uuid4())
        void_key = str(uuid.uuid4())
        ship2_key = str(uuid.uuid4())

        client.post(
            f"/api/v1/dockd/orders/{so_number}/ship",
            json=_ship_body(idempotency_key=ship1_key),
            headers={"X-WMS-Token": token},
        )
        client.post(
            f"/api/v1/dockd/orders/{so_number}/void-ship",
            json=_void_body(idempotency_key=void_key),
            headers={"X-WMS-Token": token},
        )
        client.post(
            f"/api/v1/dockd/orders/{so_number}/ship",
            json=_ship_body(idempotency_key=ship2_key),
            headers={"X-WMS-Token": token},
        )

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT event_type, source_txn_id::text "
            "FROM integration_events "
            "WHERE aggregate_id = %s "
            "ORDER BY event_id ASC",
            (so_id,),
        )
        rows = cur.fetchall()
        cur.close()
        assert len(rows) == 3
        assert rows[0] == ("ship.confirmed", ship1_key)
        assert rows[1] == ("ship.voided", void_key)
        assert rows[2] == ("ship.confirmed", ship2_key)


# ----------------------------------------------------------------------
# Audit chain integrity
# ----------------------------------------------------------------------


class TestAuditChainIntact:
    def test_lifecycle_keeps_chain_intact(self, client, dockd_token):
        _ensure_user("mike")
        _set_setting("require_packing_before_shipping", "false")
        item_id = _insert_item()
        so_id, so_number = _insert_so(status="PICKED")
        _insert_so_line(so_id, item_id)
        token = dockd_token["plaintext"]

        client.post(
            f"/api/v1/dockd/orders/{so_number}/ship",
            json=_ship_body(),
            headers={"X-WMS-Token": token},
        )
        client.post(
            f"/api/v1/dockd/orders/{so_number}/void-ship",
            json=_void_body(),
            headers={"X-WMS-Token": token},
        )
        client.post(
            f"/api/v1/dockd/orders/{so_number}/ship",
            json=_ship_body(tracking="1Z999AA10987654321"),
            headers={"X-WMS-Token": token},
        )

        broken_at = _verify_audit_chain()
        assert broken_at is None, (
            f"audit_log hash chain broken at log_id={broken_at}"
        )

    def test_each_action_lands_one_audit_row(self, client, dockd_token):
        _ensure_user("mike")
        _set_setting("require_packing_before_shipping", "false")
        item_id = _insert_item()
        so_id, so_number = _insert_so(status="PICKED")
        _insert_so_line(so_id, item_id)
        token = dockd_token["plaintext"]

        client.post(
            f"/api/v1/dockd/orders/{so_number}/ship",
            json=_ship_body(),
            headers={"X-WMS-Token": token},
        )
        client.post(
            f"/api/v1/dockd/orders/{so_number}/void-ship",
            json=_void_body(),
            headers={"X-WMS-Token": token},
        )
        client.post(
            f"/api/v1/dockd/orders/{so_number}/ship",
            json=_ship_body(tracking="1Z999AA10987654321"),
            headers={"X-WMS-Token": token},
        )

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT action_type FROM audit_log "
            "WHERE entity_type = 'SO' AND entity_id = %s "
            "ORDER BY log_id ASC",
            (so_id,),
        )
        actions = [r[0] for r in cur.fetchall()]
        cur.close()
        assert actions == ["SHIP", "SHIP_VOID", "SHIP"]


# ----------------------------------------------------------------------
# Idempotency race - peer-committed branch
# ----------------------------------------------------------------------


class TestIdempotencyPeerCommittedBranch:
    """Pre-insert a fully-populated dockd_idempotency row with a known
    body hash + cached response, then issue a request with the same key.
    This exercises the same code path the sentinel ON CONFLICT branch
    lands on under real concurrency: peer committed, current request
    finds DO NOTHING + a populated response_body, replays the cached
    body. The test cannot easily simulate true thread concurrency under
    the conftest's transactional fixture, but the post-conflict logic
    is fully covered here."""

    def test_pre_committed_peer_returns_replay(self, client, dockd_token):
        _ensure_user("mike")
        _set_setting("require_packing_before_shipping", "false")
        item_id = _insert_item()
        so_id, so_number = _insert_so(status="PICKED")
        _insert_so_line(so_id, item_id)
        token_id = dockd_token["token_id"]

        # Compute the body hash the same way the route does: parse via
        # Pydantic so optional fields fill in as None, dump in JSON mode
        # to normalize UUID / Decimal, then run the dockd_service helper.
        from schemas.dockd import ShipBody
        from services.dockd_service import canonical_body_sha256
        body = _ship_body()
        body_hash = canonical_body_sha256(
            ShipBody.model_validate(body).model_dump(mode="json")
        )

        cached_response = {
            "status": "SHIPPED",
            "tracking": body["tracking"],
            "shipped_at": "2026-05-08T12:00:00Z",
            "fulfillment_id": 999999,
            "audit_log_id": 999999,
        }
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO dockd_idempotency "
            "(token_id, idempotency_key, endpoint, so_number, "
            " request_body_sha256, response_body, response_status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (token_id, body["idempotency_key"], "ship", so_number,
             body_hash, json.dumps(cached_response), 200),
        )
        cur.close()

        # Now issue the actual request. The warm-cache short-circuit
        # finds the populated row and replays.
        r = client.post(
            f"/api/v1/dockd/orders/{so_number}/ship",
            json=body,
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        assert r.status_code == 200
        assert r.headers.get("X-Idempotent-Replay") == "true"
        assert r.get_json() == cached_response

    def test_pre_committed_peer_with_different_body_returns_409(
        self, client, dockd_token
    ):
        _ensure_user("mike")
        _set_setting("require_packing_before_shipping", "false")
        item_id = _insert_item()
        so_id, so_number = _insert_so(status="PICKED")
        _insert_so_line(so_id, item_id)
        token_id = dockd_token["token_id"]

        # Pre-insert a sentinel row with a body hash that does NOT match
        # what the upcoming request will hash to.
        conn = get_raw_connection()
        cur = conn.cursor()
        key = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO dockd_idempotency "
            "(token_id, idempotency_key, endpoint, so_number, "
            " request_body_sha256, response_body, response_status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (token_id, key, "ship", so_number, "0" * 64, '{"x":1}', 200),
        )
        cur.close()

        body = _ship_body(idempotency_key=key, tracking="DIFFERENT")
        r = client.post(
            f"/api/v1/dockd/orders/{so_number}/ship",
            json=body,
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        assert r.status_code == 409
        assert r.get_json()["error_kind"] == "idempotency_key_reused_with_different_body"


# ----------------------------------------------------------------------
# Two-station already_shipped (concurrent ship race serialized)
# ----------------------------------------------------------------------


class TestTwoStationsSerializedRace:
    def test_second_station_sees_already_shipped(
        self, client, dockd_token, dockd_token_station2
    ):
        """Both tokens scoped to warehouse 1. Station 1 ships; Station 2
        attempts the same SO with a different idempotency_key. The
        SELECT...FOR UPDATE serializes; Station 2 reads SHIPPED on lock
        release and returns 409 already_shipped with Station 1's
        tracking + operator. The dockd UI uses this to render
        'already shipped by Mike at Pack Station 1; void to retry?'."""
        _ensure_user("mike")
        _set_setting("require_packing_before_shipping", "false")
        item_id = _insert_item()
        so_id, so_number = _insert_so(status="PICKED")
        _insert_so_line(so_id, item_id)

        # Station 1 ships.
        first = client.post(
            f"/api/v1/dockd/orders/{so_number}/ship",
            json=_ship_body(),
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        assert first.status_code == 200

        # Station 2 attempts the same SO. Different token, fresh
        # idempotency_key (the cache is per-token so it cannot replay).
        second = client.post(
            f"/api/v1/dockd/orders/{so_number}/ship",
            json=_ship_body(idempotency_key=str(uuid.uuid4()),
                            tracking="1Z999AA20999999999"),
            headers={"X-WMS-Token": dockd_token_station2["plaintext"]},
        )
        assert second.status_code == 409
        body = second.get_json()
        assert body["error_kind"] == "already_shipped"
        # The first station's tracking is in the details, not the
        # second station's request body.
        assert body["details"]["existing_tracking"] == "1Z999AA10123456784"
        assert body["details"]["shipped_by"] == "mike"
