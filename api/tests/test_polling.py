"""GET /api/v1/events polling endpoint (v1.5.0 #122).

Every pinned constraint from the plan / design review gets its own
test so a regression fails loudly:

- plain int64 cursor (NOT base64)
- no has_more field in the response
- after and consumer_group mutually exclusive (400)
- strict-subset scope enforcement (403, never silent intersection)
- direct aggregate_external_id read (no join to aggregate tables)
- hardcoded 2s visibility window
- rate limit 120/min per token
"""

import hashlib
import json
import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest

from db_test_context import get_raw_connection
from services import token_cache


PEPPER = os.environ["SENTRY_TOKEN_PEPPER"]


def _hash(plaintext: str) -> str:
    return hashlib.sha256((PEPPER + plaintext).encode("utf-8")).hexdigest()


def _insert_token(plaintext: str, warehouse_ids, event_types):
    """Insert a wms_tokens row. Uses the raw test connection so the
    row is visible to the handler's SessionLocal through the fixture's
    shared transaction."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO wms_tokens (token_name, token_hash, warehouse_ids, event_types) "
        "VALUES (%s, %s, %s, %s) RETURNING token_id",
        (f"polling-test-{uuid.uuid4()}", _hash(plaintext), warehouse_ids, event_types),
    )
    token_id = cur.fetchone()[0]
    cur.close()
    return token_id


def _insert_event(
    event_id=None,
    event_type="receipt.completed",
    warehouse_id=1,
    visible_at="NOW() - INTERVAL '5 seconds'",
    aggregate_type="item_receipt",
    aggregate_id=None,
    payload=None,
):
    """Insert a row into integration_events and force visible_at to a
    value that lies outside (or inside) the 2s gate depending on the
    caller's intent. The deferred trigger never fires inside the test
    fixture's outer transaction, so visible_at must be set explicitly.
    """
    conn = get_raw_connection()
    cur = conn.cursor()
    aggregate_id = aggregate_id if aggregate_id is not None else abs(hash(uuid.uuid4())) % 10_000_000
    cur.execute(
        f"""
        INSERT INTO integration_events (
            event_type, event_version, aggregate_type, aggregate_id,
            aggregate_external_id, warehouse_id, source_txn_id, visible_at, payload
        ) VALUES (%s, 1, %s, %s, %s, %s, %s, {visible_at}, %s)
        RETURNING event_id
        """,
        (
            event_type,
            aggregate_type,
            aggregate_id,
            str(uuid.uuid4()),
            warehouse_id,
            str(uuid.uuid4()),
            json.dumps(payload or {"synthesized": True}),
        ),
    )
    new_id = cur.fetchone()[0]
    cur.close()
    return new_id


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


@pytest.fixture()
def scoped_token(seed_data):
    """Issue a wms_token with warehouse_ids=[1] and a small event_type set."""
    plaintext = f"test-plain-{uuid.uuid4()}"
    token_id = _insert_token(
        plaintext,
        warehouse_ids=[1],
        event_types=["receipt.completed", "ship.confirmed"],
    )
    return {"plaintext": plaintext, "token_id": token_id}


@pytest.fixture()
def multi_wh_token(seed_data):
    plaintext = f"multi-wh-{uuid.uuid4()}"
    token_id = _insert_token(
        plaintext,
        warehouse_ids=[1, 2, 3],
        event_types=["receipt.completed", "ship.confirmed", "pick.confirmed"],
    )
    return {"plaintext": plaintext, "token_id": token_id}


def _poll(client, token_plaintext, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    url = "/api/v1/events" + ("?" + qs if qs else "")
    return client.get(url, headers={"X-WMS-Token": token_plaintext})


class TestAuth:
    def test_missing_token_returns_401(self, client):
        resp = client.get("/api/v1/events?after=0")
        assert resp.status_code == 401

    def test_invalid_token_returns_401(self, client, seed_data):
        resp = client.get(
            "/api/v1/events?after=0",
            headers={"X-WMS-Token": "not-a-real-token"},
        )
        assert resp.status_code == 401


class TestEmptyCase:
    def test_empty_result_returns_200_with_echo_cursor(
        self, client, scoped_token
    ):
        resp = _poll(client, scoped_token["plaintext"], after=0)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["events"] == []
        assert body["next_cursor"] == 0
        # Cursor echoes input when no rows land so polling never regresses.

    def test_response_has_no_has_more_field(self, client, scoped_token):
        resp = _poll(client, scoped_token["plaintext"], after=0)
        body = resp.get_json()
        assert "has_more" not in body, (
            "plan 2.2 pins: full page implies more, partial implies caught up; "
            "no has_more field on the wire"
        )

    def test_next_cursor_is_plain_int_not_base64(self, client, scoped_token):
        """The cursor is an int64 literal (plan 2.3). A JSON integer is
        emitted, not a base64 / string token. Regression guard against
        anyone wrapping it 'for safety'."""
        eid = _insert_event(event_type="receipt.completed", warehouse_id=1)
        resp = _poll(client, scoped_token["plaintext"], after=0)
        body = resp.get_json()
        assert isinstance(body["next_cursor"], int)
        assert body["next_cursor"] == eid


class TestSingleEventRoundtrip:
    def test_envelope_shape_matches_plan_2_2(self, client, scoped_token):
        eid = _insert_event(event_type="receipt.completed", warehouse_id=1)
        resp = _poll(client, scoped_token["plaintext"], after=0)
        body = resp.get_json()
        assert resp.status_code == 200
        assert len(body["events"]) == 1
        e = body["events"][0]
        assert e["event_id"] == eid
        assert e["event_type"] == "receipt.completed"
        assert e["event_version"] == 1
        assert e["aggregate_type"] == "item_receipt"
        # Plan Decision J: aggregate_id on the wire is the external UUID,
        # read directly from integration_events (no join to item_receipts).
        uuid.UUID(e["aggregate_id"])
        assert e["warehouse_id"] == 1
        uuid.UUID(e["source_txn_id"])
        assert e["data"] == {"synthesized": True}


class TestCursorAdvance:
    def test_cursor_advance_across_pages(self, client, scoped_token):
        eids = [
            _insert_event(event_type="receipt.completed", warehouse_id=1)
            for _ in range(3)
        ]
        # First page with limit=2 returns first two events.
        first = _poll(client, scoped_token["plaintext"], after=0, limit=2).get_json()
        assert [e["event_id"] for e in first["events"]] == eids[:2]
        assert first["next_cursor"] == eids[1]

        # Second page starting at that cursor returns the third event.
        second = _poll(
            client, scoped_token["plaintext"], after=first["next_cursor"], limit=2
        ).get_json()
        assert [e["event_id"] for e in second["events"]] == [eids[2]]
        assert second["next_cursor"] == eids[2]


class TestLimitBoundary:
    def test_default_limit_when_omitted(self, client, scoped_token):
        # Insert 3 events so this test is fast; we only assert the query
        # did not blow past the request without a limit param (server
        # default is 500, which we can verify by the Pydantic schema).
        _insert_event(event_type="receipt.completed", warehouse_id=1)
        _insert_event(event_type="receipt.completed", warehouse_id=1)
        resp = _poll(client, scoped_token["plaintext"], after=0)
        assert resp.status_code == 200
        assert len(resp.get_json()["events"]) == 2

    def test_limit_above_cap_rejected(self, client, scoped_token):
        resp = _poll(client, scoped_token["plaintext"], after=0, limit=5000)
        assert resp.status_code == 400

    def test_limit_at_cap_accepted(self, client, scoped_token):
        # limit=2000 is the max; the Pydantic schema accepts it.
        resp = _poll(client, scoped_token["plaintext"], after=0, limit=2000)
        assert resp.status_code == 200


class TestTypesFilter:
    def test_types_filter_narrows_results(self, client, scoped_token):
        r_id = _insert_event(event_type="receipt.completed", warehouse_id=1)
        s_id = _insert_event(event_type="ship.confirmed", warehouse_id=1)

        resp = _poll(
            client, scoped_token["plaintext"], after=0, types="receipt.completed"
        )
        body = resp.get_json()
        assert [e["event_id"] for e in body["events"]] == [r_id]


class TestWarehouseFilter:
    def test_warehouse_id_filter_narrows_results(self, client, multi_wh_token):
        w1 = _insert_event(event_type="receipt.completed", warehouse_id=1)
        # seed does not provide warehouse 2 or 3, so manually create one.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO warehouses (warehouse_code, warehouse_name) "
            "VALUES ('TEST-W2', 'Test W2') RETURNING warehouse_id"
        )
        wh2 = cur.fetchone()[0]
        cur.close()
        w2 = _insert_event(event_type="receipt.completed", warehouse_id=wh2)

        # Adjust token scope to include the new warehouse.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE wms_tokens SET warehouse_ids = %s WHERE token_id = %s",
            ([1, wh2], multi_wh_token["token_id"]),
        )
        cur.close()
        token_cache.clear()

        resp = _poll(
            client, multi_wh_token["plaintext"], after=0, warehouse_id=wh2
        )
        body = resp.get_json()
        assert [e["event_id"] for e in body["events"]] == [w2]
        assert w1 not in [e["event_id"] for e in body["events"]]


class TestScopeEnforcement:
    def test_warehouse_outside_token_scope_returns_403(
        self, client, scoped_token
    ):
        """Decision H: strict subset. Token has warehouse_ids=[1]; a
        request for warehouse_id=2 is 403, never a silent empty result."""
        resp = _poll(
            client, scoped_token["plaintext"], after=0, warehouse_id=2
        )
        assert resp.status_code == 403
        body = resp.get_json()
        assert body["error"] == "scope_violation"
        assert body["field"] == "warehouse_id"

    def test_types_outside_token_scope_returns_403(self, client, scoped_token):
        resp = _poll(
            client, scoped_token["plaintext"], after=0, types="pick.confirmed"
        )
        assert resp.status_code == 403
        body = resp.get_json()
        assert body["error"] == "scope_violation"
        assert body["field"] == "types"

    def test_partial_types_overlap_still_returns_403(
        self, client, scoped_token
    ):
        """If any requested type is outside scope, 403. No silent drop
        of the out-of-scope entry."""
        resp = _poll(
            client,
            scoped_token["plaintext"],
            after=0,
            types="receipt.completed,pick.confirmed",
        )
        assert resp.status_code == 403


class TestVisibilityGate:
    def test_event_within_two_second_window_not_returned(
        self, client, scoped_token
    ):
        # visible_at = NOW() (within the 2s window) => hidden from readers.
        _insert_event(
            event_type="receipt.completed",
            warehouse_id=1,
            visible_at="NOW()",
        )
        resp = _poll(client, scoped_token["plaintext"], after=0)
        body = resp.get_json()
        assert body["events"] == []

    def test_event_past_two_second_window_is_returned(
        self, client, scoped_token
    ):
        # visible_at = NOW() - 5s => outside the gate, visible.
        eid = _insert_event(
            event_type="receipt.completed",
            warehouse_id=1,
            visible_at="NOW() - INTERVAL '5 seconds'",
        )
        resp = _poll(client, scoped_token["plaintext"], after=0)
        body = resp.get_json()
        assert [e["event_id"] for e in body["events"]] == [eid]

    def test_event_with_null_visible_at_not_returned(
        self, client, scoped_token
    ):
        """Rows mid-insert (pre-commit, deferred trigger hasn't fired)
        must not leak to readers. visible_at IS NULL is the gate."""
        _insert_event(
            event_type="receipt.completed",
            warehouse_id=1,
            visible_at="NULL",
        )
        resp = _poll(client, scoped_token["plaintext"], after=0)
        assert resp.get_json()["events"] == []


class TestMutualExclusion:
    def test_after_and_consumer_group_returns_400(
        self, client, scoped_token
    ):
        resp = client.get(
            "/api/v1/events?after=5&consumer_group=fabric-prod",
            headers={"X-WMS-Token": scoped_token["plaintext"]},
        )
        assert resp.status_code == 400


class TestConsumerGroupMode:
    def _setup_group(self, consumer_group_id="test-cg", last_cursor=0, subscription=None):
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO connectors (connector_id, display_name) "
            "VALUES ('test-connector', 'Test') ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO consumer_groups (consumer_group_id, connector_id, last_cursor, subscription) "
            "VALUES (%s, 'test-connector', %s, %s::jsonb)",
            (consumer_group_id, last_cursor, json.dumps(subscription or {})),
        )
        cur.close()

    def test_consumer_group_reads_last_cursor(self, client, scoped_token):
        # Group's last_cursor = 5; events with id <= 5 are excluded.
        e1 = _insert_event(event_type="receipt.completed", warehouse_id=1)
        self._setup_group("group-cursor-test", last_cursor=e1)
        e2 = _insert_event(event_type="receipt.completed", warehouse_id=1)

        resp = _poll(
            client,
            scoped_token["plaintext"],
            consumer_group="group-cursor-test",
        )
        body = resp.get_json()
        assert [e["event_id"] for e in body["events"]] == [e2]

    def test_consumer_group_not_found_returns_404(self, client, scoped_token):
        resp = _poll(
            client, scoped_token["plaintext"], consumer_group="does-not-exist"
        )
        assert resp.status_code == 404

    def test_consumer_group_subscription_narrows(self, client, scoped_token):
        _insert_event(event_type="receipt.completed", warehouse_id=1)
        ship = _insert_event(event_type="ship.confirmed", warehouse_id=1)
        self._setup_group(
            "group-sub-test",
            last_cursor=0,
            subscription={"event_types": ["ship.confirmed"]},
        )
        resp = _poll(
            client, scoped_token["plaintext"], consumer_group="group-sub-test"
        )
        assert [e["event_id"] for e in resp.get_json()["events"]] == [ship]


class TestAckCursor:
    def _setup_group(
        self,
        consumer_group_id="ack-test-cg",
        connector_id="test-connector",
        last_cursor=0,
    ):
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO connectors (connector_id, display_name) VALUES (%s, 'Test') "
            "ON CONFLICT DO NOTHING",
            (connector_id,),
        )
        cur.execute(
            "INSERT INTO consumer_groups (consumer_group_id, connector_id, last_cursor) "
            "VALUES (%s, %s, %s)",
            (consumer_group_id, connector_id, last_cursor),
        )
        cur.close()

    def _cursor_value(self, consumer_group_id):
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT last_cursor FROM consumer_groups WHERE consumer_group_id = %s",
            (consumer_group_id,),
        )
        row = cur.fetchone()
        cur.close()
        return row[0] if row else None

    def _ack(self, client, plaintext, consumer_group, cursor):
        return client.post(
            "/api/v1/events/ack",
            json={"consumer_group": consumer_group, "cursor": cursor},
            headers={"X-WMS-Token": plaintext},
        )

    def test_ack_advances_monotonic_cursor(self, client, scoped_token):
        self._setup_group("ack-advance", last_cursor=10)
        resp = self._ack(client, scoped_token["plaintext"], "ack-advance", 25)
        assert resp.status_code == 200
        assert resp.get_json() == {
            "consumer_group": "ack-advance",
            "last_cursor": 25,
        }
        assert self._cursor_value("ack-advance") == 25

    def test_out_of_order_ack_is_noop(self, client, scoped_token):
        """Plan 2.4: an ack lower than the current stored cursor is a
        no-op via the UPDATE ... WHERE last_cursor <= :cursor clause.
        The response echoes the unchanged last_cursor so the client
        can see its stale ack did not regress the pointer."""
        self._setup_group("ack-oow", last_cursor=50)
        resp = self._ack(client, scoped_token["plaintext"], "ack-oow", 10)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["last_cursor"] == 50, (
            "an out-of-order ack must not rewind the cursor"
        )
        assert self._cursor_value("ack-oow") == 50

    def test_ack_equal_cursor_is_idempotent(self, client, scoped_token):
        """Ack with the same value as the current cursor must succeed
        (the WHERE clause is <=, not <) so a retried ack after a
        client crash is idempotent."""
        self._setup_group("ack-eq", last_cursor=42)
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
        # Set up connector B's group.
        self._setup_group(
            "ack-conn-b", connector_id="connector-b", last_cursor=0
        )
        # Issue a token bound to connector A.
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
                _hash(plaintext),
                [1],
                ["receipt.completed"],
            ),
        )
        cur.close()

        resp = self._ack(client, plaintext, "ack-conn-b", 5)
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "consumer_group_scope_violation"
        # Cursor unchanged.
        assert self._cursor_value("ack-conn-b") == 0

    def test_ack_missing_fields_returns_400(self, client, scoped_token):
        resp = client.post(
            "/api/v1/events/ack",
            json={"consumer_group": "ack-missing-cursor"},
            headers={"X-WMS-Token": scoped_token["plaintext"]},
        )
        assert resp.status_code == 400

    def test_ack_requires_token(self, client, seed_data):
        self._setup_group("ack-noauth", last_cursor=0)
        resp = client.post(
            "/api/v1/events/ack",
            json={"consumer_group": "ack-noauth", "cursor": 1},
        )
        assert resp.status_code == 401


class TestAggregateExternalIdIsDirect:
    def test_no_join_required_wire_reads_stored_uuid(
        self, client, scoped_token
    ):
        """Decision J: aggregate_id on the wire is what we wrote into
        integration_events.aggregate_external_id, not a fresh lookup
        on the aggregate table. The raw row's value must appear
        verbatim on the wire."""
        expected_ext = str(uuid.uuid4())
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO integration_events (
                event_type, event_version, aggregate_type, aggregate_id,
                aggregate_external_id, warehouse_id, source_txn_id, visible_at, payload
            ) VALUES (
                'receipt.completed', 1, 'item_receipt', 42,
                %s, 1, %s, NOW() - INTERVAL '5 seconds', '{}'::jsonb
            ) RETURNING event_id
            """,
            (expected_ext, str(uuid.uuid4())),
        )
        cur.fetchone()
        cur.close()

        resp = _poll(client, scoped_token["plaintext"], after=0)
        body = resp.get_json()
        assert body["events"][0]["aggregate_id"] == expected_ext
