"""v1.8.0 (#297): Productivity Dashboard tests.

Covers:
- /api/v1/dashboard/productivity:
    happy-path metric aggregation per event kind (units / unique_skus
    / orders);
    sort by total desc with tie-break on user_id asc;
    packing visibility honors require_packing_before_shipping;
    end < start -> 422; range > 90 days -> 422;
    cache hit avoids re-querying within 60s;
    empty audit_log returns empty users array.
- /api/v1/dashboard/preferences:
    GET returns schema defaults when no row exists;
    PUT upserts; second PUT replaces selectively;
    invalid chart_order key -> 422;
    user_id always derived from g.current_user (cannot be set in
    body via extra='forbid').
"""

import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db_test_context


def _query(sql, params=()):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        if cur.description is None:
            return None
        return cur.fetchall()
    finally:
        cur.close()


def _seed_audit(action_type, user_id, warehouse_id, details, entity_type="ITEM",
                entity_id=1, created_at=None):
    """Insert an audit_log row directly. The hash chain trigger
    computes prev_hash + row_hash automatically."""
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        if created_at is not None:
            cur.execute(
                "INSERT INTO audit_log "
                "(action_type, entity_type, entity_id, user_id, "
                " warehouse_id, details, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)",
                (action_type, entity_type, entity_id, user_id,
                 warehouse_id, json.dumps(details), created_at),
            )
        else:
            cur.execute(
                "INSERT INTO audit_log "
                "(action_type, entity_type, entity_id, user_id, "
                " warehouse_id, details) "
                "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
                (action_type, entity_type, entity_id, user_id,
                 warehouse_id, json.dumps(details)),
            )
    finally:
        cur.close()


@pytest.fixture(autouse=True)
def _clear_productivity_cache():
    from services.productivity_service import clear_cache
    clear_cache()
    yield
    clear_cache()


def _today_range():
    today = date.today()
    return today.isoformat(), today.isoformat()


# ----------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------


class TestProductivityAggregation:
    def test_picking_distinct_orders(self, client, auth_headers):
        # P11.1: picking metric is "distinct SOs picked", so two PICK
        # lines on the same SO collapse to one credit. alice picks two
        # different SOs (one of them across two lines); bob picks one.
        _seed_audit("PICK", "alice", 1,
                    {"quantity_picked": 5, "sku": "A", "item_id": 1},
                    entity_type="SO", entity_id=101)
        _seed_audit("PICK", "alice", 1,
                    {"quantity_picked": 3, "sku": "B", "item_id": 2},
                    entity_type="SO", entity_id=101)
        _seed_audit("PICK", "alice", 1,
                    {"quantity_picked": 2, "sku": "C", "item_id": 3},
                    entity_type="SO", entity_id=102)
        _seed_audit("PICK", "bob",   1,
                    {"quantity_picked": 7, "sku": "A", "item_id": 1},
                    entity_type="SO", entity_id=201)
        start, end = _today_range()
        resp = client.get(
            f"/api/v1/dashboard/productivity?start={start}&end={end}&warehouse_id=1",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        users_by_id = {u["user_id"]: u for u in body["users"]}
        assert users_by_id["alice"]["metrics"]["picking"] == 2
        assert users_by_id["bob"]["metrics"]["picking"] == 1
        # Sort by total desc; alice (2) > bob (1)
        assert [u["user_id"] for u in body["users"]] == ["alice", "bob"]

    def test_received_skus_distinct(self, client, auth_headers):
        # alice received SKU 1 twice (2 receipt events) but distinct
        # count is 1; bob received SKUs 1 + 2 (distinct = 2).
        _seed_audit("RECEIVE", "alice", 1, {"item_id": 1, "quantity": 3})
        _seed_audit("RECEIVE", "alice", 1, {"item_id": 1, "quantity": 2})
        _seed_audit("RECEIVE", "bob",   1, {"item_id": 1, "quantity": 5})
        _seed_audit("RECEIVE", "bob",   1, {"item_id": 2, "quantity": 1})
        start, end = _today_range()
        resp = client.get(
            f"/api/v1/dashboard/productivity?start={start}&end={end}&warehouse_id=1",
            headers=auth_headers,
        )
        body = resp.get_json()
        users = {u["user_id"]: u for u in body["users"]}
        assert users["alice"]["metrics"]["received_skus"] == 1
        assert users["bob"]["metrics"]["received_skus"] == 2

    def test_putaway_skus_distinct_via_entity_id(self, client, auth_headers):
        # PUTAWAY uses entity_id (the items.item_id) for DISTINCT SKU.
        _seed_audit("PUTAWAY", "alice", 1, {"quantity": 1},
                    entity_type="ITEM", entity_id=1)
        _seed_audit("PUTAWAY", "alice", 1, {"quantity": 1},
                    entity_type="ITEM", entity_id=1)
        _seed_audit("PUTAWAY", "alice", 1, {"quantity": 1},
                    entity_type="ITEM", entity_id=2)
        start, end = _today_range()
        resp = client.get(
            f"/api/v1/dashboard/productivity?start={start}&end={end}&warehouse_id=1",
            headers=auth_headers,
        )
        users = {u["user_id"]: u for u in resp.get_json()["users"]}
        assert users["alice"]["metrics"]["putaway_skus"] == 2

    def test_shipped_orders_count(self, client, auth_headers):
        _seed_audit("SHIP", "alice", 1, {"so_number": "SO-1"})
        _seed_audit("SHIP", "alice", 1, {"so_number": "SO-2"})
        _seed_audit("SHIP", "bob",   1, {"so_number": "SO-3"})
        start, end = _today_range()
        resp = client.get(
            f"/api/v1/dashboard/productivity?start={start}&end={end}&warehouse_id=1",
            headers=auth_headers,
        )
        users = {u["user_id"]: u for u in resp.get_json()["users"]}
        assert users["alice"]["metrics"]["shipped"] == 2
        assert users["bob"]["metrics"]["shipped"] == 1

    def test_packing_units_sum_when_required(self, client, auth_headers):
        # Packing visible by default (require_packing_before_shipping
        # default is TRUE).
        _seed_audit("PACK", "alice", 1, {"so_number": "SO-1", "total_items": 4})
        _seed_audit("PACK", "alice", 1, {"so_number": "SO-2", "total_items": 6})
        start, end = _today_range()
        resp = client.get(
            f"/api/v1/dashboard/productivity?start={start}&end={end}&warehouse_id=1",
            headers=auth_headers,
        )
        body = resp.get_json()
        assert "packing" in body["events_visible"]
        users = {u["user_id"]: u for u in body["users"]}
        assert users["alice"]["metrics"]["packing"] == 10

    def test_packing_hidden_when_setting_false(self, client, auth_headers):
        # Toggle the setting OFF for this test.
        from db_test_context import get_raw_connection
        conn = get_raw_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO app_settings (key, value) "
                "VALUES ('require_packing_before_shipping', 'false') "
                "ON CONFLICT (key) DO UPDATE SET value = 'false'"
            )
        finally:
            cur.close()
        _seed_audit("PACK", "alice", 1, {"so_number": "SO-1", "total_items": 4})
        start, end = _today_range()
        resp = client.get(
            f"/api/v1/dashboard/productivity?start={start}&end={end}&warehouse_id=1",
            headers=auth_headers,
        )
        body = resp.get_json()
        assert "packing" not in body["events_visible"]
        # If packing is hidden, alice's metrics should not include it.
        users = {u["user_id"]: u for u in body["users"]}
        if users:
            assert "packing" not in users["alice"]["metrics"]

    def test_empty_window_returns_empty_users(self, client, auth_headers):
        start = (date.today() - timedelta(days=10)).isoformat()
        end = (date.today() - timedelta(days=10)).isoformat()
        resp = client.get(
            f"/api/v1/dashboard/productivity?start={start}&end={end}&warehouse_id=1",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["users"] == []
        for slug in body["events_visible"]:
            assert body["totals_per_event"][slug] == 0


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------


class TestValidation:
    def test_end_before_start_returns_422(self, client, auth_headers):
        today = date.today()
        start = today.isoformat()
        end = (today - timedelta(days=1)).isoformat()
        resp = client.get(
            f"/api/v1/dashboard/productivity?start={start}&end={end}&warehouse_id=1",
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert resp.get_json()["error"] == "validation_error"

    def test_range_over_90_days_returns_422(self, client, auth_headers):
        today = date.today()
        start = (today - timedelta(days=120)).isoformat()
        end = today.isoformat()
        resp = client.get(
            f"/api/v1/dashboard/productivity?start={start}&end={end}&warehouse_id=1",
            headers=auth_headers,
        )
        assert resp.status_code == 422
        body = resp.get_json()
        assert body["error"] == "range_too_large"
        assert body["max_range_days"] == 90

    def test_missing_warehouse_id_returns_422(self, client, auth_headers):
        today = date.today()
        resp = client.get(
            f"/api/v1/dashboard/productivity?start={today}&end={today}",
            headers=auth_headers,
        )
        assert resp.status_code == 422


# ----------------------------------------------------------------------
# Cache
# ----------------------------------------------------------------------


class TestCache:
    def test_repeat_call_within_ttl_does_not_re_query(self, client, auth_headers):
        # Seed one row, run the query, mutate the row, run the same
        # query again. Cache should return the original payload (cache
        # hit), not the post-mutation count.
        # P11.1: picking is distinct-SO so we seed on a specific so_id.
        _seed_audit("PICK", "alice", 1,
                    {"quantity_picked": 5, "sku": "A", "item_id": 1},
                    entity_type="SO", entity_id=301)
        start, end = _today_range()
        url = f"/api/v1/dashboard/productivity?start={start}&end={end}&warehouse_id=1"
        first = client.get(url, headers=auth_headers).get_json()
        # Add a pick on a different SO; cache should NOT reflect this.
        _seed_audit("PICK", "alice", 1,
                    {"quantity_picked": 100, "sku": "A", "item_id": 1},
                    entity_type="SO", entity_id=302)
        second = client.get(url, headers=auth_headers).get_json()
        assert first["users"][0]["metrics"]["picking"] == 1
        assert second["users"][0]["metrics"]["picking"] == 1


# ----------------------------------------------------------------------
# Preferences
# ----------------------------------------------------------------------


class TestPreferences:
    def test_get_returns_defaults_when_no_row(self, client, auth_headers):
        resp = client.get(
            "/api/v1/dashboard/preferences", headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.get_json()
        # Default chart_order matches DASHBOARD_EVENTS catalog.
        assert body["chart_order"] == [
            "picking", "packing", "shipped", "received_skus", "putaway_skus",
        ]
        assert body["default_range"] == "today"
        assert body["default_view"] == "charts"

    def test_put_upserts_and_get_round_trips(self, client, auth_headers):
        put = client.put(
            "/api/v1/dashboard/preferences",
            json={
                "chart_order": ["packing", "picking", "shipped"],
                "default_range": "last_7d",
                "default_view": "table",
            },
            headers=auth_headers,
        )
        assert put.status_code == 200, put.get_json()
        body = put.get_json()
        assert body["chart_order"] == ["packing", "picking", "shipped"]
        assert body["default_range"] == "last_7d"
        assert body["default_view"] == "table"

        get = client.get(
            "/api/v1/dashboard/preferences", headers=auth_headers,
        )
        assert get.get_json()["chart_order"] == ["packing", "picking", "shipped"]

    def test_put_partial_keeps_other_fields(self, client, auth_headers):
        client.put(
            "/api/v1/dashboard/preferences",
            json={
                "chart_order": ["packing", "picking"],
                "default_range": "last_7d",
                "default_view": "table",
            },
            headers=auth_headers,
        )
        # Second PUT only changes default_view; other fields persist.
        client.put(
            "/api/v1/dashboard/preferences",
            json={"default_view": "charts"},
            headers=auth_headers,
        )
        body = client.get(
            "/api/v1/dashboard/preferences", headers=auth_headers,
        ).get_json()
        assert body["default_view"] == "charts"
        assert body["default_range"] == "last_7d"
        assert body["chart_order"] == ["packing", "picking"]

    def test_invalid_chart_order_key_returns_422(self, client, auth_headers):
        resp = client.put(
            "/api/v1/dashboard/preferences",
            json={"chart_order": ["picking", "totally_made_up"]},
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert resp.get_json()["error"] == "validation_error"

    def test_duplicate_chart_order_rejected(self, client, auth_headers):
        resp = client.put(
            "/api/v1/dashboard/preferences",
            json={"chart_order": ["picking", "picking"]},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_user_id_in_body_rejected_via_extra_forbid(
        self, client, auth_headers,
    ):
        # extra='forbid' on _PreferencesBody rejects any user_id
        # smuggled in the body; the endpoint always derives user_id
        # from g.current_user.
        resp = client.put(
            "/api/v1/dashboard/preferences",
            json={"user_id": 999, "default_view": "table"},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_empty_put_body_returns_422(self, client, auth_headers):
        resp = client.put(
            "/api/v1/dashboard/preferences",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert resp.get_json()["error"] == "no_fields_to_update"
