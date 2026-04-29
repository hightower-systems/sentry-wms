"""Admin webhook subscription create endpoint (v1.6.0 #185)."""

import os
import sys
import uuid

os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")
os.environ.setdefault(
    "SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8="
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from db_test_context import get_raw_connection


@pytest.fixture(autouse=True)
def _ensure_internal_webhook_opt_out(monkeypatch):
    """The admin endpoint runs the SSRF guard; example.com etc.
    resolve to public addresses but the existing test fixtures
    use https://example.invalid which fails DNS. Enable the
    dev/CI opt-out so create-path tests can supply any URL.
    Tests that target the SSRF reject path explicitly clear the
    opt-out at the test level."""
    monkeypatch.setenv("SENTRY_ALLOW_INTERNAL_WEBHOOKS", "true")
    yield


@pytest.fixture(autouse=True)
def _ensure_test_connector():
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO connectors (connector_id, display_name) "
        "VALUES (%s, %s) ON CONFLICT (connector_id) DO NOTHING",
        ("test-conn-webhook", "test connector"),
    )
    conn.commit()
    cur.close()


def _row_by_id(subscription_id: str):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT subscription_id, connector_id, display_name, delivery_url, "
        "subscription_filter, rate_limit_per_second, pending_ceiling, "
        "dlq_ceiling, status FROM webhook_subscriptions "
        "WHERE subscription_id = %s",
        (subscription_id,),
    )
    row = cur.fetchone()
    cur.close()
    return row


def _secret_row(subscription_id: str):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT generation, secret_ciphertext FROM webhook_secrets "
        "WHERE subscription_id = %s ORDER BY generation",
        (subscription_id,),
    )
    rows = cur.fetchall()
    cur.close()
    return rows


class TestCreateHappyPath:
    def test_201_returns_plaintext_secret_and_metadata(self, client, auth_headers):
        resp = client.post(
            "/api/admin/webhooks",
            json={
                "connector_id": "test-conn-webhook",
                "display_name": "fabric staging",
                "delivery_url": "https://example.com/hooks/wms",
                "subscription_filter": {"event_types": ["receipt.completed"]},
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.get_json()
        body = resp.get_json()
        assert body["connector_id"] == "test-conn-webhook"
        assert body["status"] == "active"
        assert body["rate_limit_per_second"] == 50  # default
        assert body["pending_ceiling"] == 10_000
        assert body["dlq_ceiling"] == 1_000
        assert body["secret_generation"] == 1
        assert isinstance(body["secret"], str) and len(body["secret"]) >= 32
        assert "subscription_id" in body

        row = _row_by_id(body["subscription_id"])
        assert row is not None
        assert row[1] == "test-conn-webhook"
        assert row[2] == "fabric staging"
        assert row[3] == "https://example.com/hooks/wms"
        assert row[5] == 50
        assert row[8] == "active"

        secrets_rows = _secret_row(body["subscription_id"])
        assert len(secrets_rows) == 1
        assert secrets_rows[0][0] == 1  # generation
        assert secrets_rows[0][1] is not None  # ciphertext present

    def test_secret_round_trip_decrypts_to_returned_plaintext(
        self, client, auth_headers
    ):
        from services.webhook_dispatcher import signing as dispatcher_signing

        dispatcher_signing._fernet_cache = None  # noqa: SLF001
        resp = client.post(
            "/api/admin/webhooks",
            json={
                "connector_id": "test-conn-webhook",
                "display_name": "round-trip",
                "delivery_url": "https://example.com/rt",
            },
            headers=auth_headers,
        )
        body = resp.get_json()
        plaintext = body["secret"].encode("utf-8")
        rows = _secret_row(body["subscription_id"])
        ciphertext = bytes(rows[0][1])
        decrypted = dispatcher_signing._get_fernet().decrypt(ciphertext)  # noqa: SLF001
        assert decrypted == plaintext


class TestStrictBody:
    def test_unknown_field_rejected(self, client, auth_headers):
        resp = client.post(
            "/api/admin/webhooks",
            json={
                "connector_id": "test-conn-webhook",
                "display_name": "x",
                "delivery_url": "https://example.com/x",
                "bogus_field": 42,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400, resp.get_json()


class TestValidationFailures:
    def test_missing_connector_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/admin/webhooks",
            json={
                "connector_id": "no-such-connector",
                "display_name": "x",
                "delivery_url": "https://example.com/x",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "connector_not_found"

    def test_unknown_event_types_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/admin/webhooks",
            json={
                "connector_id": "test-conn-webhook",
                "display_name": "x",
                "delivery_url": "https://example.com/x",
                "subscription_filter": {"event_types": ["does.not.exist"]},
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"] == "unknown_event_types"
        assert "does.not.exist" in body["unknown"]

    def test_missing_warehouse_ids_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/admin/webhooks",
            json={
                "connector_id": "test-conn-webhook",
                "display_name": "x",
                "delivery_url": "https://example.com/x",
                "subscription_filter": {"warehouse_ids": [9999999]},
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"] == "unknown_warehouse_ids"
        assert 9999999 in body["missing"]

    def test_http_url_rejected_without_opt_out(
        self, client, auth_headers, monkeypatch
    ):
        monkeypatch.delenv("SENTRY_ALLOW_HTTP_WEBHOOKS", raising=False)
        resp = client.post(
            "/api/admin/webhooks",
            json={
                "connector_id": "test-conn-webhook",
                "display_name": "http",
                "delivery_url": "http://example.com/x",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "https_required"

    def test_http_url_allowed_with_opt_out(
        self, client, auth_headers, monkeypatch
    ):
        monkeypatch.setenv("SENTRY_ALLOW_HTTP_WEBHOOKS", "true")
        resp = client.post(
            "/api/admin/webhooks",
            json={
                "connector_id": "test-conn-webhook",
                "display_name": "http-ok",
                "delivery_url": "http://example.com/x",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201

    def test_pending_ceiling_above_hard_cap_rejected(
        self, client, auth_headers, monkeypatch
    ):
        monkeypatch.setenv("DISPATCHER_MAX_PENDING_HARD_CAP", "1000")
        resp = client.post(
            "/api/admin/webhooks",
            json={
                "connector_id": "test-conn-webhook",
                "display_name": "x",
                "delivery_url": "https://example.com/x",
                "pending_ceiling": 9999,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "pending_ceiling_above_hard_cap"

    def test_dlq_ceiling_above_hard_cap_rejected(
        self, client, auth_headers, monkeypatch
    ):
        monkeypatch.setenv("DISPATCHER_MAX_DLQ_HARD_CAP", "100")
        resp = client.post(
            "/api/admin/webhooks",
            json={
                "connector_id": "test-conn-webhook",
                "display_name": "x",
                "delivery_url": "https://example.com/x",
                "dlq_ceiling": 999,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "dlq_ceiling_above_hard_cap"

    def test_private_url_rejected_at_admin_time(
        self, client, auth_headers, monkeypatch
    ):
        monkeypatch.delenv("SENTRY_ALLOW_INTERNAL_WEBHOOKS", raising=False)
        resp = client.post(
            "/api/admin/webhooks",
            json={
                "connector_id": "test-conn-webhook",
                "display_name": "private",
                "delivery_url": "https://127.0.0.1/hook",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "private_destination"


class TestAuthorization:
    def test_unauthenticated_returns_401(self, client):
        resp = client.post(
            "/api/admin/webhooks",
            json={
                "connector_id": "test-conn-webhook",
                "display_name": "x",
                "delivery_url": "https://example.com/x",
            },
        )
        assert resp.status_code == 401


class TestUrlReuseTombstone:
    def _seed_tombstone(self, delivery_url: str) -> int:
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute("SELECT subscription_id FROM webhook_subscriptions LIMIT 1")
        cur.execute(
            "INSERT INTO webhook_subscriptions_tombstones "
            "(subscription_id, delivery_url_at_delete, connector_id, deleted_by) "
            "VALUES (%s, %s, %s, %s) RETURNING tombstone_id",
            (str(uuid.uuid4()), delivery_url, "test-conn-webhook", 1),
        )
        tombstone_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        return tombstone_id

    def test_reuse_without_acknowledge_returns_409(self, client, auth_headers):
        url = f"https://example.com/reuse-{uuid.uuid4()}"
        tombstone_id = self._seed_tombstone(url)

        resp = client.post(
            "/api/admin/webhooks",
            json={
                "connector_id": "test-conn-webhook",
                "display_name": "x",
                "delivery_url": url,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 409, resp.get_json()
        body = resp.get_json()
        assert body["error"] == "url_reuse_tombstone"
        assert body["tombstone_id"] == tombstone_id
        assert resp.headers.get("X-Sentry-URL-Reuse-Tombstone") == str(tombstone_id)

    def test_reuse_with_acknowledge_creates_and_marks_tombstone(
        self, client, auth_headers
    ):
        url = f"https://example.com/reuse-ack-{uuid.uuid4()}"
        tombstone_id = self._seed_tombstone(url)

        resp = client.post(
            "/api/admin/webhooks",
            json={
                "connector_id": "test-conn-webhook",
                "display_name": "x",
                "delivery_url": url,
                "acknowledge_url_reuse": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT acknowledged_at, acknowledged_by FROM "
            "webhook_subscriptions_tombstones WHERE tombstone_id = %s",
            (tombstone_id,),
        )
        ack_at, ack_by = cur.fetchone()
        cur.close()
        assert ack_at is not None
        assert ack_by == 1


class TestAuditLog:
    def test_create_writes_audit_row(self, client, auth_headers):
        resp = client.post(
            "/api/admin/webhooks",
            json={
                "connector_id": "test-conn-webhook",
                "display_name": "audit-probe",
                "delivery_url": f"https://example.com/audit-{uuid.uuid4()}",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        sub_id = resp.get_json()["subscription_id"]

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT action_type, entity_type, details FROM audit_log "
            "WHERE action_type = 'WEBHOOK_SUBSCRIPTION_CREATE' "
            "AND details->>'subscription_id' = %s",
            (sub_id,),
        )
        row = cur.fetchone()
        cur.close()
        assert row is not None
        action_type, entity_type, details = row
        assert action_type == "WEBHOOK_SUBSCRIPTION_CREATE"
        assert entity_type == "WEBHOOK_SUBSCRIPTION"
        assert details["display_name"] == "audit-probe"
        assert "secret" not in details
        assert "secret_ciphertext" not in details
