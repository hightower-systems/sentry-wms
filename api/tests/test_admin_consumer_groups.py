"""Admin CRUD for v1.5.0 connectors + consumer_groups (#125)."""

import os
import sys

os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db_test_context import get_raw_connection


def _delete_all_cg_and_connectors():
    conn = get_raw_connection()
    cur = conn.cursor()
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


class TestConsumerGroupDelete:
    def test_delete_removes_row(self, client, auth_headers):
        _delete_all_cg_and_connectors()
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
