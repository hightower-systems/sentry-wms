"""Tests for GET /api/admin/webhook-errors (#204)."""

import uuid

from db_test_context import get_raw_connection


def _seed_subscription(display_name="errors-test", connector_id="fabric"):
    """Insert a subscription row and return its UUID. Connector
    'fabric' is created on demand if it does not exist; deferred
    so this helper is callable from inside a test cleanly."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO connectors (connector_id, display_name) VALUES (%s, %s) "
        "ON CONFLICT (connector_id) DO NOTHING",
        (connector_id, "Fabric (test)"),
    )
    sid = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO webhook_subscriptions
            (subscription_id, connector_id, display_name, delivery_url,
             subscription_filter)
        VALUES (%s, %s, %s, %s, '{}'::jsonb)
        """,
        (str(sid), connector_id, display_name, "https://example.test/hook"),
    )
    cur.close()
    return str(sid)


def _seed_delivery(subscription_id, status, error_kind, error_detail=None,
                   http_status=None, event_id=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO webhook_deliveries
            (subscription_id, event_id, attempt_number, status,
             scheduled_at, http_status, error_kind, error_detail,
             secret_generation, completed_at)
        VALUES (%s, %s, 1, %s, NOW(), %s, %s, %s, 1, NOW())
        RETURNING delivery_id
        """,
        (subscription_id, event_id, status, http_status, error_kind, error_detail),
    )
    did = cur.fetchone()[0]
    cur.close()
    return int(did)


class TestWebhookErrorsBasic:
    def test_returns_empty_when_no_failures(self, client, auth_headers):
        # Seed a subscription with no deliveries.
        sid = _seed_subscription("errors-empty")
        resp = client.get(
            f"/api/admin/webhook-errors?subscription_id={sid}",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] == 0
        assert data["deliveries"] == []
        # Catalog kinds list always travels with the response so the
        # admin UI can populate the filter dropdown without a second
        # round-trip.
        assert "ssrf_rejected" in data["error_kinds"]
        assert "5xx" in data["error_kinds"]

    def test_returns_failure_with_catalog_join(self, client, auth_headers):
        sid = _seed_subscription("errors-5xx")
        from services.webhook_dispatcher import error_catalog
        catalog_short = error_catalog.get_short_message("5xx")
        did = _seed_delivery(
            sid, status="dlq", error_kind="5xx",
            error_detail=catalog_short, http_status=500,
        )
        resp = client.get(
            f"/api/admin/webhook-errors?subscription_id={sid}",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] == 1
        row = data["deliveries"][0]
        assert row["delivery_id"] == did
        assert row["error_kind"] == "5xx"
        assert row["short_message"] == catalog_short
        assert row["description"] == error_catalog.get_entry("5xx")["description"]
        assert row["triage_hint"] == error_catalog.get_entry("5xx")["triage_hint"]
        assert row["http_status"] == 500
        assert row["subscription_display_name"] == "errors-5xx"


class TestWebhookErrorsFilters:
    def test_filter_by_error_kind(self, client, auth_headers):
        sid = _seed_subscription("errors-filter-kind")
        _seed_delivery(sid, status="dlq", error_kind="5xx", event_id=10)
        _seed_delivery(sid, status="dlq", error_kind="timeout", event_id=11)
        resp = client.get(
            f"/api/admin/webhook-errors?subscription_id={sid}&error_kind=timeout",
            headers=auth_headers,
        )
        rows = resp.get_json()["deliveries"]
        assert len(rows) == 1
        assert rows[0]["error_kind"] == "timeout"

    def test_invalid_subscription_id_rejected(self, client, auth_headers):
        resp = client.get(
            "/api/admin/webhook-errors?subscription_id=not-a-uuid",
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_invalid_pagination_rejected(self, client, auth_headers):
        resp = client.get(
            "/api/admin/webhook-errors?limit=0",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        resp = client.get(
            "/api/admin/webhook-errors?limit=10000",
            headers=auth_headers,
        )
        assert resp.status_code == 400


class TestWebhookErrorsSucceededExcluded:
    def test_succeeded_rows_not_returned(self, client, auth_headers):
        sid = _seed_subscription("errors-success-excluded")
        _seed_delivery(sid, status="succeeded", error_kind=None,
                       http_status=200, event_id=20)
        _seed_delivery(sid, status="failed", error_kind="5xx",
                       http_status=503, event_id=21)
        resp = client.get(
            f"/api/admin/webhook-errors?subscription_id={sid}",
            headers=auth_headers,
        )
        rows = resp.get_json()["deliveries"]
        # Only the failed row should land; succeeded is filtered server-side.
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"


class TestWebhookErrorsAuth:
    def test_unauthenticated_rejected(self, client):
        resp = client.get("/api/admin/webhook-errors")
        assert resp.status_code == 401
