"""GET /api/v1/events polling endpoint contract (v1.5.0 #122 / #126).

Every pinned wire-contract invariant gets its own test:

- plain int64 cursor (NOT base64)
- no has_more field in the response
- after and consumer_group mutually exclusive (400)
- strict-subset scope enforcement (403, never silent intersection)
- direct aggregate_external_id read (no join to aggregate tables)
- hardcoded 2s visibility window
- rate limit 120/min per token (not tested here; decorator-level
  coverage lives in test_wms_token_auth.py)

Consumer-group mode and the ack endpoint live in test_consumer_groups.py.
Types + raw-schema endpoints live in test_events_schema_registry.py.
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

from _polling_helpers import insert_event, insert_token, poll
from db_test_context import get_raw_connection
from services import token_cache


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


@pytest.fixture()
def scoped_token(seed_data):
    plaintext = f"test-plain-{uuid.uuid4()}"
    token_id = insert_token(
        plaintext,
        warehouse_ids=[1],
        event_types=["receipt.completed", "ship.confirmed"],
    )
    return {"plaintext": plaintext, "token_id": token_id}


@pytest.fixture()
def multi_wh_token(seed_data):
    plaintext = f"multi-wh-{uuid.uuid4()}"
    token_id = insert_token(
        plaintext,
        warehouse_ids=[1, 2, 3],
        event_types=["receipt.completed", "ship.confirmed", "pick.confirmed"],
    )
    return {"plaintext": plaintext, "token_id": token_id}


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
        resp = poll(client, scoped_token["plaintext"], after=0)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["events"] == []
        assert body["next_cursor"] == 0
        # Cursor echoes input when no rows land so polling never regresses.

    def test_response_has_no_has_more_field(self, client, scoped_token):
        resp = poll(client, scoped_token["plaintext"], after=0)
        body = resp.get_json()
        assert "has_more" not in body, (
            "plan 2.2 pins: full page implies more, partial implies caught up; "
            "no has_more field on the wire"
        )

    def test_next_cursor_is_plain_int_not_base64(self, client, scoped_token):
        """The cursor is an int64 literal (plan 2.3). A JSON integer is
        emitted, not a base64 / string token. Regression guard against
        anyone wrapping it 'for safety'."""
        eid = insert_event(event_type="receipt.completed", warehouse_id=1)
        resp = poll(client, scoped_token["plaintext"], after=0)
        body = resp.get_json()
        assert isinstance(body["next_cursor"], int)
        assert body["next_cursor"] == eid


class TestSingleEventRoundtrip:
    def test_envelope_shape_matches_plan_2_2(self, client, scoped_token):
        eid = insert_event(event_type="receipt.completed", warehouse_id=1)
        resp = poll(client, scoped_token["plaintext"], after=0)
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
            insert_event(event_type="receipt.completed", warehouse_id=1)
            for _ in range(3)
        ]
        first = poll(client, scoped_token["plaintext"], after=0, limit=2).get_json()
        assert [e["event_id"] for e in first["events"]] == eids[:2]
        assert first["next_cursor"] == eids[1]

        second = poll(
            client, scoped_token["plaintext"], after=first["next_cursor"], limit=2
        ).get_json()
        assert [e["event_id"] for e in second["events"]] == [eids[2]]
        assert second["next_cursor"] == eids[2]


class TestLimitBoundary:
    def test_default_limit_when_omitted(self, client, scoped_token):
        insert_event(event_type="receipt.completed", warehouse_id=1)
        insert_event(event_type="receipt.completed", warehouse_id=1)
        resp = poll(client, scoped_token["plaintext"], after=0)
        assert resp.status_code == 200
        assert len(resp.get_json()["events"]) == 2

    def test_limit_above_cap_rejected(self, client, scoped_token):
        resp = poll(client, scoped_token["plaintext"], after=0, limit=5000)
        assert resp.status_code == 400

    def test_limit_at_cap_accepted(self, client, scoped_token):
        resp = poll(client, scoped_token["plaintext"], after=0, limit=2000)
        assert resp.status_code == 200


class TestTypesFilter:
    def test_types_filter_narrows_results(self, client, scoped_token):
        r_id = insert_event(event_type="receipt.completed", warehouse_id=1)
        s_id = insert_event(event_type="ship.confirmed", warehouse_id=1)

        resp = poll(
            client, scoped_token["plaintext"], after=0, types="receipt.completed"
        )
        body = resp.get_json()
        assert [e["event_id"] for e in body["events"]] == [r_id]


class TestWarehouseFilter:
    def test_warehouse_id_filter_narrows_results(self, client, multi_wh_token):
        w1 = insert_event(event_type="receipt.completed", warehouse_id=1)
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO warehouses (warehouse_code, warehouse_name) "
            "VALUES ('TEST-W2', 'Test W2') RETURNING warehouse_id"
        )
        wh2 = cur.fetchone()[0]
        cur.close()
        w2 = insert_event(event_type="receipt.completed", warehouse_id=wh2)

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE wms_tokens SET warehouse_ids = %s WHERE token_id = %s",
            ([1, wh2], multi_wh_token["token_id"]),
        )
        cur.close()
        token_cache.clear()

        resp = poll(
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
        resp = poll(
            client, scoped_token["plaintext"], after=0, warehouse_id=2
        )
        assert resp.status_code == 403
        body = resp.get_json()
        assert body["error"] == "scope_violation"
        assert body["field"] == "warehouse_id"

    def test_types_outside_token_scope_returns_403(self, client, scoped_token):
        resp = poll(
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
        resp = poll(
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
        insert_event(
            event_type="receipt.completed",
            warehouse_id=1,
            visible_at="NOW()",
        )
        resp = poll(client, scoped_token["plaintext"], after=0)
        body = resp.get_json()
        assert body["events"] == []

    def test_event_past_two_second_window_is_returned(
        self, client, scoped_token
    ):
        eid = insert_event(
            event_type="receipt.completed",
            warehouse_id=1,
            visible_at="NOW() - INTERVAL '5 seconds'",
        )
        resp = poll(client, scoped_token["plaintext"], after=0)
        body = resp.get_json()
        assert [e["event_id"] for e in body["events"]] == [eid]

    def test_event_with_null_visible_at_not_returned(
        self, client, scoped_token
    ):
        """Rows mid-insert (pre-commit, deferred trigger hasn't fired)
        must not leak to readers. visible_at IS NULL is the gate."""
        insert_event(
            event_type="receipt.completed",
            warehouse_id=1,
            visible_at="NULL",
        )
        resp = poll(client, scoped_token["plaintext"], after=0)
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

        resp = poll(client, scoped_token["plaintext"], after=0)
        body = resp.get_json()
        assert body["events"][0]["aggregate_id"] == expected_ext
