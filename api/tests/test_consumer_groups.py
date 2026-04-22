"""Consumer-group mode + POST /api/v1/events/ack + heartbeat throttling (#126).

Split out from test_polling.py so the polling-endpoint contract
(cursor / visibility / scope) stays decoupled from consumer-group
cursor state and the ack protocol. The heartbeat throttling test is
new for #126 -- the previous coverage only asserted the UPDATE path,
not the 30-second rate-cap (Decision T).
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

from _polling_helpers import hash_token, insert_event, insert_token, poll
from db_test_context import get_raw_connection
from routes import polling as polling_module
from services import token_cache


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


@pytest.fixture()
def scoped_token(seed_data):
    plaintext = f"cg-token-{uuid.uuid4()}"
    token_id = insert_token(
        plaintext,
        warehouse_ids=[1],
        event_types=["receipt.completed", "ship.confirmed"],
    )
    return {"plaintext": plaintext, "token_id": token_id}


def _setup_group(
    consumer_group_id,
    connector_id="test-connector",
    last_cursor=0,
    subscription=None,
):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO connectors (connector_id, display_name) VALUES (%s, 'Test') "
        "ON CONFLICT DO NOTHING",
        (connector_id,),
    )
    cur.execute(
        "INSERT INTO consumer_groups "
        "(consumer_group_id, connector_id, last_cursor, subscription) "
        "VALUES (%s, %s, %s, %s::jsonb)",
        (consumer_group_id, connector_id, last_cursor, json.dumps(subscription or {})),
    )
    cur.close()


def _group_field(consumer_group_id, column):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        f"SELECT {column} FROM consumer_groups WHERE consumer_group_id = %s",
        (consumer_group_id,),
    )
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


class TestConsumerGroupReadMode:
    def test_consumer_group_reads_last_cursor(self, client, scoped_token):
        e1 = insert_event(event_type="receipt.completed", warehouse_id=1)
        _setup_group("group-cursor-test", last_cursor=e1)
        e2 = insert_event(event_type="receipt.completed", warehouse_id=1)

        resp = poll(
            client, scoped_token["plaintext"], consumer_group="group-cursor-test"
        )
        body = resp.get_json()
        assert [e["event_id"] for e in body["events"]] == [e2]

    def test_consumer_group_not_found_returns_404(self, client, scoped_token):
        resp = poll(
            client, scoped_token["plaintext"], consumer_group="does-not-exist"
        )
        assert resp.status_code == 404

    def test_consumer_group_subscription_narrows(self, client, scoped_token):
        insert_event(event_type="receipt.completed", warehouse_id=1)
        ship = insert_event(event_type="ship.confirmed", warehouse_id=1)
        _setup_group(
            "group-sub-test",
            last_cursor=0,
            subscription={"event_types": ["ship.confirmed"]},
        )
        resp = poll(
            client, scoped_token["plaintext"], consumer_group="group-sub-test"
        )
        assert [e["event_id"] for e in resp.get_json()["events"]] == [ship]


class TestHeartbeatThrottling:
    """Decision T: last_heartbeat UPDATEs are capped at one per 30
    seconds per group via ``routes.polling._last_heartbeat_write``, an
    in-memory dict keyed on consumer_group_id with monotonic-clock
    timestamps. Clearing an entry simulates "30 seconds have elapsed"
    without a real sleep.

    We verify against the throttle dict rather than
    ``consumer_groups.last_heartbeat`` because the test fixture wraps
    every handler call in a single outer transaction, and
    ``NOW()`` / ``CURRENT_TIMESTAMP`` return the transaction-start
    time for every statement inside it. The DB column advances
    correctly in production; the throttle dict advances regardless.
    """

    def _reset_throttle(self, consumer_group_id=None):
        if consumer_group_id is None:
            polling_module._last_heartbeat_write.clear()
        else:
            polling_module._last_heartbeat_write.pop(consumer_group_id, None)

    def test_first_poll_writes_heartbeat_entry(self, client, scoped_token):
        _setup_group("hb-first", last_cursor=0)
        self._reset_throttle("hb-first")
        assert "hb-first" not in polling_module._last_heartbeat_write

        poll(client, scoped_token["plaintext"], consumer_group="hb-first")
        assert "hb-first" in polling_module._last_heartbeat_write, (
            "first poll must enter the group into the throttle dict"
        )

    def test_second_poll_within_window_does_not_update_timestamp(
        self, client, scoped_token
    ):
        _setup_group("hb-throttled", last_cursor=0)
        self._reset_throttle("hb-throttled")

        poll(client, scoped_token["plaintext"], consumer_group="hb-throttled")
        first_ts = polling_module._last_heartbeat_write["hb-throttled"]

        # No throttle reset; the second poll hits the 30s guard and
        # does NOT re-stamp (time.monotonic() - first_ts < 30s).
        poll(client, scoped_token["plaintext"], consumer_group="hb-throttled")
        second_ts = polling_module._last_heartbeat_write["hb-throttled"]

        assert first_ts == second_ts, (
            "second poll within the 30s throttle window must not re-stamp "
            "the throttle entry"
        )

    def test_poll_after_throttle_reset_re_enters_throttle_dict(
        self, client, scoped_token
    ):
        _setup_group("hb-reset", last_cursor=0)
        self._reset_throttle("hb-reset")

        poll(client, scoped_token["plaintext"], consumer_group="hb-reset")
        first_ts = polling_module._last_heartbeat_write["hb-reset"]

        # Simulate 30s elapsing by dropping this group's entry.
        self._reset_throttle("hb-reset")
        assert "hb-reset" not in polling_module._last_heartbeat_write

        poll(client, scoped_token["plaintext"], consumer_group="hb-reset")
        assert "hb-reset" in polling_module._last_heartbeat_write
        second_ts = polling_module._last_heartbeat_write["hb-reset"]
        assert second_ts > first_ts, (
            "poll after throttle reset must stamp a new monotonic timestamp"
        )


class TestAckCursor:
    def _ack(self, client, plaintext, consumer_group, cursor):
        return client.post(
            "/api/v1/events/ack",
            json={"consumer_group": consumer_group, "cursor": cursor},
            headers={"X-WMS-Token": plaintext},
        )

    def test_ack_advances_monotonic_cursor(self, client, scoped_token):
        _setup_group("ack-advance", last_cursor=10)
        resp = self._ack(client, scoped_token["plaintext"], "ack-advance", 25)
        assert resp.status_code == 200
        assert resp.get_json() == {
            "consumer_group": "ack-advance",
            "last_cursor": 25,
        }
        assert _group_field("ack-advance", "last_cursor") == 25

    def test_out_of_order_ack_is_noop(self, client, scoped_token):
        """Plan 2.4: an ack lower than the current stored cursor is a
        no-op via the UPDATE ... WHERE last_cursor <= :cursor clause."""
        _setup_group("ack-oow", last_cursor=50)
        resp = self._ack(client, scoped_token["plaintext"], "ack-oow", 10)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["last_cursor"] == 50
        assert _group_field("ack-oow", "last_cursor") == 50

    def test_ack_equal_cursor_is_idempotent(self, client, scoped_token):
        """Ack with the same value as the current cursor must succeed
        (the WHERE clause is <=, not <) so a retried ack after a
        client crash is idempotent."""
        _setup_group("ack-eq", last_cursor=42)
        resp = self._ack(client, scoped_token["plaintext"], "ack-eq", 42)
        assert resp.status_code == 200
        assert resp.get_json()["last_cursor"] == 42

    def test_ack_nonexistent_group_returns_404(self, client, scoped_token):
        resp = self._ack(
            client, scoped_token["plaintext"], "does-not-exist", 5
        )
        assert resp.status_code == 404

    def test_ack_cross_connector_returns_403(self, client, seed_data):
        """A token bound to connector A must not ack groups owned by
        connector B. Tokens without a connector_id may ack any group
        (legacy / admin shape)."""
        _setup_group(
            "ack-conn-b", connector_id="connector-b", last_cursor=0
        )
        plaintext = f"conn-a-token-{uuid.uuid4()}"
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO connectors (connector_id, display_name) VALUES ('connector-a', 'A') "
            "ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO wms_tokens (token_name, token_hash, connector_id, warehouse_ids, event_types) "
            "VALUES (%s, %s, 'connector-a', %s, %s)",
            (
                f"conn-a-{uuid.uuid4()}",
                hash_token(plaintext),
                [1],
                ["receipt.completed"],
            ),
        )
        cur.close()

        resp = self._ack(client, plaintext, "ack-conn-b", 5)
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "consumer_group_scope_violation"
        assert _group_field("ack-conn-b", "last_cursor") == 0

    def test_ack_missing_fields_returns_400(self, client, scoped_token):
        resp = client.post(
            "/api/v1/events/ack",
            json={"consumer_group": "ack-missing-cursor"},
            headers={"X-WMS-Token": scoped_token["plaintext"]},
        )
        assert resp.status_code == 400

    def test_ack_requires_token(self, client, seed_data):
        _setup_group("ack-noauth", last_cursor=0)
        resp = client.post(
            "/api/v1/events/ack",
            json={"consumer_group": "ack-noauth", "cursor": 1},
        )
        assert resp.status_code == 401
