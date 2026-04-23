"""Admin CRUD for v1.5.0 connectors + consumer_groups (#125)."""

import os
import sys

os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db_test_context import get_raw_connection


def _delete_all_cg_and_connectors():
    conn = get_raw_connection()
    cur = conn.cursor()
    # v1.5.1 V-207 (#148): tombstones must be cleared too so tests
    # that reuse a previously-deleted consumer_group_id don't 409
    # on the replay guard.
    cur.execute("DELETE FROM consumer_groups_tombstones")
    cur.execute("DELETE FROM consumer_groups")
    cur.execute("DELETE FROM connectors")
    cur.close()


class TestConnectorRegistry:
    def test_create_and_list(self, client, auth_headers):
        _delete_all_cg_and_connectors()
        resp = client.post(
            "/api/admin/connector-registry",
            json={"connector_id": "fabric", "display_name": "Fabric Prod"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["connector_id"] == "fabric"
        assert body["display_name"] == "Fabric Prod"

        listing = client.get("/api/admin/connector-registry", headers=auth_headers)
        names = {c["connector_id"] for c in listing.get_json()["connectors"]}
        assert "fabric" in names

    def test_duplicate_connector_id_returns_409(self, client, auth_headers):
        _delete_all_cg_and_connectors()
        payload = {"connector_id": "dup", "display_name": "First"}
        client.post("/api/admin/connector-registry", json=payload, headers=auth_headers)
        resp = client.post(
            "/api/admin/connector-registry",
            json={"connector_id": "dup", "display_name": "Second"},
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "duplicate_connector_id"

    def test_unauthenticated_returns_401(self, client):
        resp = client.post(
            "/api/admin/connector-registry",
            json={"connector_id": "nope", "display_name": "no auth"},
        )
        assert resp.status_code == 401


class TestConsumerGroupCreate:
    def _seed_connector(self, client, auth_headers, connector_id="fabric"):
        client.post(
            "/api/admin/connector-registry",
            json={"connector_id": connector_id, "display_name": "Seed"},
            headers=auth_headers,
        )

    def test_create_returns_group_with_defaults(self, client, auth_headers):
        _delete_all_cg_and_connectors()
        self._seed_connector(client, auth_headers)
        resp = client.post(
            "/api/admin/consumer-groups",
            json={
                "consumer_group_id": "fabric-prod-main",
                "connector_id": "fabric",
                "subscription": {"event_types": ["ship.confirmed"]},
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.get_json()
        body = resp.get_json()
        assert body["consumer_group_id"] == "fabric-prod-main"
        assert body["connector_id"] == "fabric"
        assert body["last_cursor"] == 0
        assert body["subscription"] == {"event_types": ["ship.confirmed"]}

    def test_create_duplicate_returns_409(self, client, auth_headers):
        _delete_all_cg_and_connectors()
        self._seed_connector(client, auth_headers)
        payload = {
            "consumer_group_id": "dup-cg",
            "connector_id": "fabric",
        }
        client.post("/api/admin/consumer-groups", json=payload, headers=auth_headers)
        resp = client.post(
            "/api/admin/consumer-groups", json=payload, headers=auth_headers
        )
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "duplicate_consumer_group_id"

    def test_create_unknown_connector_returns_400(self, client, auth_headers):
        _delete_all_cg_and_connectors()
        resp = client.post(
            "/api/admin/consumer-groups",
            json={
                "consumer_group_id": "orphan-cg",
                "connector_id": "does-not-exist",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "unknown_connector_id"

    def test_default_subscription_is_empty_dict(self, client, auth_headers):
        _delete_all_cg_and_connectors()
        self._seed_connector(client, auth_headers)
        resp = client.post(
            "/api/admin/consumer-groups",
            json={"consumer_group_id": "no-sub", "connector_id": "fabric"},
            headers=auth_headers,
        )
        assert resp.get_json()["subscription"] == {}


class TestConsumerGroupList:
    def test_list_returns_all_groups(self, client, auth_headers):
        _delete_all_cg_and_connectors()
        client.post(
            "/api/admin/connector-registry",
            json={"connector_id": "listy", "display_name": "Lister"},
            headers=auth_headers,
        )
        for cg in ("a", "b", "c"):
            client.post(
                "/api/admin/consumer-groups",
                json={"consumer_group_id": cg, "connector_id": "listy"},
                headers=auth_headers,
            )
        resp = client.get("/api/admin/consumer-groups", headers=auth_headers)
        body = resp.get_json()
        ids = {g["consumer_group_id"] for g in body["consumer_groups"]}
        assert {"a", "b", "c"} <= ids


class TestConsumerGroupUpdate:
    def _setup(self, client, auth_headers):
        _delete_all_cg_and_connectors()
        client.post(
            "/api/admin/connector-registry",
            json={"connector_id": "upd", "display_name": "Upd"},
            headers=auth_headers,
        )
        client.post(
            "/api/admin/consumer-groups",
            json={"consumer_group_id": "upd-cg", "connector_id": "upd"},
            headers=auth_headers,
        )

    def test_patch_subscription_updates_row(self, client, auth_headers):
        self._setup(client, auth_headers)
        resp = client.patch(
            "/api/admin/consumer-groups/upd-cg",
            json={"subscription": {"warehouse_ids": [1, 2]}},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["subscription"] == {"warehouse_ids": [1, 2]}

    def test_patch_unknown_group_returns_404(self, client, auth_headers):
        resp = client.patch(
            "/api/admin/consumer-groups/ghost",
            json={"subscription": {}},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_patch_empty_body_returns_400(self, client, auth_headers):
        self._setup(client, auth_headers)
        resp = client.patch(
            "/api/admin/consumer-groups/upd-cg",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 400


class TestSubscriptionValidation:
    """v1.5.1 V-204 (#145): subscription is strict-typed. Unknown
    keys and wrong-typed values fail 400 at the admin endpoint
    instead of silently persisting and crashing the next poll."""

    def _seed(self, client, auth_headers):
        _delete_all_cg_and_connectors()
        client.post(
            "/api/admin/connector-registry",
            json={"connector_id": "sv", "display_name": "Sub-Validate"},
            headers=auth_headers,
        )

    def test_create_rejects_string_where_array_expected(self, client, auth_headers):
        self._seed(client, auth_headers)
        resp = client.post(
            "/api/admin/consumer-groups",
            json={
                "consumer_group_id": "sv-bad-string",
                "connector_id": "sv",
                "subscription": {"warehouse_ids": "abc"},
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_create_rejects_unknown_subscription_keys(self, client, auth_headers):
        self._seed(client, auth_headers)
        resp = client.post(
            "/api/admin/consumer-groups",
            json={
                "consumer_group_id": "sv-extra-key",
                "connector_id": "sv",
                "subscription": {
                    "event_types": ["ship.confirmed"],
                    "__proto__": "malicious",
                },
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_create_rejects_non_integer_warehouse_id(self, client, auth_headers):
        self._seed(client, auth_headers)
        resp = client.post(
            "/api/admin/consumer-groups",
            json={
                "consumer_group_id": "sv-bad-int",
                "connector_id": "sv",
                "subscription": {"warehouse_ids": ["one", "two"]},
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_create_accepts_valid_subscription(self, client, auth_headers):
        self._seed(client, auth_headers)
        resp = client.post(
            "/api/admin/consumer-groups",
            json={
                "consumer_group_id": "sv-ok",
                "connector_id": "sv",
                "subscription": {
                    "event_types": ["ship.confirmed"],
                    "warehouse_ids": [1, 2],
                },
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.get_json()
        assert resp.get_json()["subscription"] == {
            "event_types": ["ship.confirmed"],
            "warehouse_ids": [1, 2],
        }

    def test_patch_rejects_malformed_subscription(self, client, auth_headers):
        self._seed(client, auth_headers)
        client.post(
            "/api/admin/consumer-groups",
            json={"consumer_group_id": "sv-patch", "connector_id": "sv"},
            headers=auth_headers,
        )
        resp = client.patch(
            "/api/admin/consumer-groups/sv-patch",
            json={"subscription": {"event_types": 42}},
            headers=auth_headers,
        )
        assert resp.status_code == 400


class TestConsumerGroupDelete:
    def test_delete_removes_row(self, client, auth_headers):
        _delete_all_cg_and_connectors()
        _delete_all_tombstones()
        client.post(
            "/api/admin/connector-registry",
            json={"connector_id": "del", "display_name": "Del"},
            headers=auth_headers,
        )
        client.post(
            "/api/admin/consumer-groups",
            json={"consumer_group_id": "del-cg", "connector_id": "del"},
            headers=auth_headers,
        )
        resp = client.delete(
            "/api/admin/consumer-groups/del-cg", headers=auth_headers
        )
        assert resp.status_code == 204

        # Not visible in list after delete.
        listing = client.get("/api/admin/consumer-groups", headers=auth_headers)
        ids = {g["consumer_group_id"] for g in listing.get_json()["consumer_groups"]}
        assert "del-cg" not in ids

    def test_delete_unknown_returns_404(self, client, auth_headers):
        resp = client.delete(
            "/api/admin/consumer-groups/does-not-exist", headers=auth_headers
        )
        assert resp.status_code == 404


def _delete_all_tombstones():
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM consumer_groups_tombstones")
    cur.close()


def _advance_cursor_directly(cgid, new_cursor):
    """Simulate cursor advance without going through the ack
    endpoint, so V-207 tests do not depend on V-202 behaviour."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE consumer_groups SET last_cursor = %s WHERE consumer_group_id = %s",
        (new_cursor, cgid),
    )
    cur.close()


def _tombstone_for(cgid):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT consumer_group_id, last_cursor_at_delete, connector_id, "
        "deleted_by "
        "  FROM consumer_groups_tombstones "
        " WHERE consumer_group_id = %s",
        (cgid,),
    )
    row = cur.fetchone()
    cur.close()
    return row


class TestReplayGuard:
    """v1.5.1 V-207 (#148): deleting then recreating a consumer_group
    under the same id resets last_cursor=0 and replays every event.
    v1.5.1 records a tombstone on DELETE and refuses the recreate
    with 409 replay_would_skip_history unless the admin sends
    acknowledge_replay=true.
    """

    def _seed(self, client, auth_headers, cgid="replay-cg", cursor=123):
        _delete_all_cg_and_connectors()
        _delete_all_tombstones()
        client.post(
            "/api/admin/connector-registry",
            json={"connector_id": "replay-c", "display_name": "Replay"},
            headers=auth_headers,
        )
        client.post(
            "/api/admin/consumer-groups",
            json={"consumer_group_id": cgid, "connector_id": "replay-c"},
            headers=auth_headers,
        )
        _advance_cursor_directly(cgid, cursor)

    def test_delete_records_tombstone_with_last_cursor(
        self, client, auth_headers
    ):
        self._seed(client, auth_headers, cgid="tomb-target", cursor=99)
        resp = client.delete(
            "/api/admin/consumer-groups/tomb-target", headers=auth_headers
        )
        assert resp.status_code == 204
        row = _tombstone_for("tomb-target")
        assert row is not None
        assert row[0] == "tomb-target"
        assert row[1] == 99
        assert row[2] == "replay-c"

    def test_recreate_without_ack_returns_409(self, client, auth_headers):
        self._seed(client, auth_headers, cgid="ack-required", cursor=500)
        client.delete(
            "/api/admin/consumer-groups/ack-required", headers=auth_headers
        )
        resp = client.post(
            "/api/admin/consumer-groups",
            json={
                "consumer_group_id": "ack-required",
                "connector_id": "replay-c",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["error"] == "replay_would_skip_history"
        assert body["last_cursor_at_delete"] == 500

    def test_recreate_with_ack_succeeds_and_clears_tombstone(
        self, client, auth_headers
    ):
        self._seed(client, auth_headers, cgid="ack-ok", cursor=250)
        client.delete(
            "/api/admin/consumer-groups/ack-ok", headers=auth_headers
        )
        resp = client.post(
            "/api/admin/consumer-groups",
            json={
                "consumer_group_id": "ack-ok",
                "connector_id": "replay-c",
                "acknowledge_replay": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.get_json()
        # Tombstone cleared so a fresh DELETE later starts a new
        # tombstone cycle cleanly.
        assert _tombstone_for("ack-ok") is None

    def test_recreate_with_fresh_id_is_unaffected(
        self, client, auth_headers
    ):
        """A tombstone must only gate the EXACT id it was created
        for; a different new id sails through with no 409."""
        self._seed(client, auth_headers, cgid="one-tomb", cursor=10)
        client.delete(
            "/api/admin/consumer-groups/one-tomb", headers=auth_headers
        )
        resp = client.post(
            "/api/admin/consumer-groups",
            json={
                "consumer_group_id": "totally-different",
                "connector_id": "replay-c",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201

    def test_repeated_delete_refreshes_tombstone_cursor(
        self, client, auth_headers
    ):
        """delete -> recreate (ack) -> advance -> delete again: the
        second tombstone reflects the newest cursor, not the first."""
        self._seed(client, auth_headers, cgid="repeat-tomb", cursor=77)
        client.delete(
            "/api/admin/consumer-groups/repeat-tomb", headers=auth_headers
        )
        client.post(
            "/api/admin/consumer-groups",
            json={
                "consumer_group_id": "repeat-tomb",
                "connector_id": "replay-c",
                "acknowledge_replay": True,
            },
            headers=auth_headers,
        )
        _advance_cursor_directly("repeat-tomb", 400)
        client.delete(
            "/api/admin/consumer-groups/repeat-tomb", headers=auth_headers
        )
        row = _tombstone_for("repeat-tomb")
        assert row is not None
        assert row[1] == 400
