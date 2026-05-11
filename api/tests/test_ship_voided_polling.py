"""Polling-pipe coverage for ship.voided/1 (v1.9.0 dockd #9).

The dockd void route emits ship.voided onto the integration_events
outbox; consumers receive it via GET /api/v1/events. The polling
endpoint filters by the caller token's event_types array. This test
file confirms two things:

1. A token scoped to ship.voided sees the events on the wire.
2. A token NOT scoped to ship.voided gets the rows filtered out
   server-side (no leak across scope boundaries).

Both behaviors are existing polling-endpoint contract; this file pins
them specifically for ship.voided so the v1.9 deploy has a regression
net for the operator-side scope update documented at
docs/runbooks/fabric-token-add-ship-voided.md.
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
from services import token_cache


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


@pytest.fixture()
def fabric_like_token(seed_data):
    """Mirrors the production Fabric polling token's scope after the
    v1.9 ship.voided rollout: receipt.completed (existing) + ship.voided
    (new). Confirms the runbook's UPDATE produces a token that actually
    receives the new events."""
    plaintext = f"fabric-like-{uuid.uuid4()}"
    token_id = insert_token(
        plaintext,
        warehouse_ids=[1],
        event_types=["receipt.completed", "ship.confirmed", "ship.voided"],
    )
    return {"plaintext": plaintext, "token_id": token_id}


@pytest.fixture()
def pre_v1_9_token(seed_data):
    """The same token's pre-rollout scope: ship.confirmed but NOT
    ship.voided. Confirms the polling endpoint filters ship.voided
    out for this token."""
    plaintext = f"pre-v1-9-{uuid.uuid4()}"
    token_id = insert_token(
        plaintext,
        warehouse_ids=[1],
        event_types=["receipt.completed", "ship.confirmed"],
    )
    return {"plaintext": plaintext, "token_id": token_id}


class TestShipVoidedReachesScopedConsumer:
    def test_ship_voided_event_delivered(self, client, fabric_like_token):
        event_id = insert_event(
            event_type="ship.voided",
            warehouse_id=1,
            aggregate_type="sales_order",
            payload={
                "sales_order_external_id": str(uuid.uuid4()),
                "voided_at": "2026-05-08T12:00:00Z",
                "voided_by_user_external_id": str(uuid.uuid4()),
                "reason": "wrong box dimensions",
                "reverted_to_status": "PICKED",
            },
        )
        resp = poll(client, fabric_like_token["plaintext"], after=0)
        assert resp.status_code == 200
        body = resp.get_json()
        types = {e["event_type"] for e in body["events"]}
        assert "ship.voided" in types
        # The event_id round-trips so consumers can ack via cursor.
        ids = {e["event_id"] for e in body["events"]}
        assert event_id in ids

    def test_ship_voided_payload_carries_required_fields(
        self, client, fabric_like_token
    ):
        payload = {
            "sales_order_external_id": str(uuid.uuid4()),
            "voided_at": "2026-05-08T12:00:00Z",
            "voided_by_user_external_id": str(uuid.uuid4()),
            "reason": "label voided in ShipRush",
            "reverted_to_status": "PACKED",
        }
        insert_event(
            event_type="ship.voided",
            warehouse_id=1,
            aggregate_type="sales_order",
            payload=payload,
        )
        resp = poll(client, fabric_like_token["plaintext"], after=0)
        events = resp.get_json()["events"]
        voided = [e for e in events if e["event_type"] == "ship.voided"][0]
        assert voided["data"] == payload


class TestShipVoidedFilteredFromUnscopedConsumer:
    def test_pre_v1_9_token_does_not_see_ship_voided(self, client, pre_v1_9_token):
        """A token whose event_types does NOT include ship.voided gets
        the row filtered out at the polling endpoint, even though the
        row is committed on the outbox. The operator runbook for adding
        ship.voided to the Fabric token is the path to widen the scope."""
        insert_event(
            event_type="ship.voided",
            warehouse_id=1,
            aggregate_type="sales_order",
            payload={
                "sales_order_external_id": str(uuid.uuid4()),
                "voided_at": "2026-05-08T12:00:00Z",
                "voided_by_user_external_id": str(uuid.uuid4()),
                "reason": "test",
                "reverted_to_status": "PICKED",
            },
        )
        resp = poll(client, pre_v1_9_token["plaintext"], after=0)
        assert resp.status_code == 200
        types = {e["event_type"] for e in resp.get_json()["events"]}
        assert "ship.voided" not in types

    def test_pre_v1_9_token_still_sees_ship_confirmed(self, client, pre_v1_9_token):
        """Sanity check: the same token still sees the events that ARE
        in scope. Catches a regression where adding ship.voided to the
        catalog might have broken ship.confirmed delivery."""
        insert_event(event_type="ship.confirmed", warehouse_id=1)
        resp = poll(client, pre_v1_9_token["plaintext"], after=0)
        types = {e["event_type"] for e in resp.get_json()["events"]}
        assert "ship.confirmed" in types
