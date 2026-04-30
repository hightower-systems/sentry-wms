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
    """Stage the connector row inside the per-test SQLAlchemy
    transaction so the outer rollback cleans it up. A raw
    ``conn.commit()`` here would escape the fixture's transaction
    and leak rows across test modules; in a full-suite run that
    surfaces as cross-module pollution (later tests see leftover
    integration_events with cursor=0 subscriptions)."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO connectors (connector_id, display_name) "
        "VALUES (%s, %s) ON CONFLICT (connector_id) DO NOTHING",
        ("test-conn-webhook", "test connector"),
    )
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
        cur.execute(
            "INSERT INTO webhook_subscriptions_tombstones "
            "(subscription_id, delivery_url_at_delete, connector_id, deleted_by) "
            "VALUES (%s, %s, %s, %s) RETURNING tombstone_id",
            (str(uuid.uuid4()), delivery_url, "test-conn-webhook", 1),
        )
        tombstone_id = cur.fetchone()[0]
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


def _create_one(client, auth_headers, **overrides) -> dict:
    body = {
        "connector_id": "test-conn-webhook",
        "display_name": overrides.pop("display_name", "list-fixture"),
        "delivery_url": overrides.pop(
            "delivery_url", f"https://example.com/{uuid.uuid4()}"
        ),
    }
    body.update(overrides)
    resp = client.post("/api/admin/webhooks", json=body, headers=auth_headers)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


class TestList:
    def test_list_returns_created_subscription(self, client, auth_headers):
        created = _create_one(client, auth_headers, display_name="visible")
        resp = client.get("/api/admin/webhooks", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert "webhooks" in body
        match = next(
            (w for w in body["webhooks"]
             if w["subscription_id"] == created["subscription_id"]),
            None,
        )
        assert match is not None
        assert match["display_name"] == "visible"
        assert match["status"] == "active"
        assert "secret" not in match
        assert "secret_ciphertext" not in match
        assert match["stats"]["attempts_24h"] == 0
        assert match["stats"]["success_rate_24h"] is None

    def test_list_unauthenticated_returns_401(self, client):
        resp = client.get("/api/admin/webhooks")
        assert resp.status_code == 401

    def test_list_24h_stats_reflect_delivery_rows(self, client, auth_headers):
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO integration_events "
            "(event_type, event_version, aggregate_type, aggregate_id, "
            " aggregate_external_id, warehouse_id, source_txn_id, payload) "
            "VALUES ('test.stats', 1, 'agg', %s, %s, 1, %s, '{}'::jsonb) "
            "RETURNING event_id",
            (
                abs(hash(uuid.uuid4())) % (10**9),
                str(uuid.uuid4()),
                str(uuid.uuid4()),
            ),
        )
        event_id = cur.fetchone()[0]
        for status in ("succeeded", "succeeded", "failed", "dlq"):
            cur.execute(
                "INSERT INTO webhook_deliveries "
                "(subscription_id, event_id, attempt_number, status, "
                " scheduled_at, attempted_at, completed_at, secret_generation) "
                "VALUES (%s, %s, 1, %s, NOW(), NOW(), NOW(), 1)",
                (sub_id, event_id, status),
            )
        cur.execute(
            "INSERT INTO webhook_deliveries "
            "(subscription_id, event_id, attempt_number, status, "
            " scheduled_at, secret_generation) "
            "VALUES (%s, %s, 1, 'pending', NOW(), 1)",
            (sub_id, event_id),
        )
        cur.close()

        resp = client.get(
            f"/api/admin/webhooks/{sub_id}", headers=auth_headers
        )
        body = resp.get_json()
        stats = body["stats"]
        assert stats["attempts_24h"] == 4
        assert stats["succeeded_24h"] == 2
        assert stats["failed_24h"] == 1
        assert stats["dlq_24h"] == 1
        assert stats["success_rate_24h"] == 0.5
        assert stats["pending_count"] == 1


class TestDetail:
    def test_detail_returns_matching_row(self, client, auth_headers):
        created = _create_one(
            client, auth_headers, display_name="detail-target"
        )
        resp = client.get(
            f"/api/admin/webhooks/{created['subscription_id']}",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["subscription_id"] == created["subscription_id"]
        assert body["display_name"] == "detail-target"
        assert "secret" not in body

    def test_detail_unknown_uuid_returns_404(self, client, auth_headers):
        random_id = str(uuid.uuid4())
        resp = client.get(
            f"/api/admin/webhooks/{random_id}", headers=auth_headers
        )
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "subscription_not_found"

    def test_detail_invalid_uuid_returns_400(self, client, auth_headers):
        resp = client.get(
            "/api/admin/webhooks/not-a-uuid", headers=auth_headers
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_subscription_id"

    def test_detail_unauthenticated_returns_401(self, client):
        resp = client.get(f"/api/admin/webhooks/{uuid.uuid4()}")
        assert resp.status_code == 401


class TestRotateSecret:
    def _secrets_for(self, subscription_id: str):
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT generation, secret_ciphertext, expires_at "
            "FROM webhook_secrets WHERE subscription_id = %s "
            "ORDER BY generation",
            (subscription_id,),
        )
        rows = cur.fetchall()
        cur.close()
        return rows

    def test_rotate_returns_new_plaintext_and_demotes_prior_primary(
        self, client, auth_headers
    ):
        from services.webhook_dispatcher import signing as dispatcher_signing

        dispatcher_signing._fernet_cache = None  # noqa: SLF001
        created = _create_one(client, auth_headers, display_name="rotate-target")
        sub_id = created["subscription_id"]
        original_plaintext = created["secret"].encode("utf-8")

        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/rotate-secret",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["subscription_id"] == sub_id
        assert body["secret_generation"] == 1
        assert isinstance(body["secret"], str) and len(body["secret"]) >= 32
        new_plaintext = body["secret"].encode("utf-8")
        assert new_plaintext != original_plaintext

        rows = self._secrets_for(sub_id)
        assert len(rows) == 2
        gen1, gen2 = rows
        assert gen1[0] == 1 and gen1[2] is None  # new primary, no expiry
        assert gen2[0] == 2 and gen2[2] is not None  # demoted, expires_at set

        # The new gen=1 ciphertext decrypts to the plaintext returned
        # in the response; the demoted gen=2 ciphertext decrypts to
        # the original plaintext from create.
        gen1_decrypted = dispatcher_signing._get_fernet().decrypt(  # noqa: SLF001
            bytes(gen1[1])
        )
        gen2_decrypted = dispatcher_signing._get_fernet().decrypt(  # noqa: SLF001
            bytes(gen2[1])
        )
        assert gen1_decrypted == new_plaintext
        assert gen2_decrypted == original_plaintext

    def test_double_rotate_keeps_only_two_rows(self, client, auth_headers):
        created = _create_one(client, auth_headers, display_name="double-rotate")
        sub_id = created["subscription_id"]

        first = client.post(
            f"/api/admin/webhooks/{sub_id}/rotate-secret",
            headers=auth_headers,
        )
        assert first.status_code == 200
        first_secret = first.get_json()["secret"].encode("utf-8")

        second = client.post(
            f"/api/admin/webhooks/{sub_id}/rotate-secret",
            headers=auth_headers,
        )
        assert second.status_code == 200
        second_secret = second.get_json()["secret"].encode("utf-8")

        rows = self._secrets_for(sub_id)
        assert len(rows) == 2
        assert rows[0][0] == 1 and rows[1][0] == 2

        # The very-first plaintext (from create) is gone; gen=2 now
        # holds the prior rotation's plaintext, gen=1 the latest.
        from services.webhook_dispatcher import signing as dispatcher_signing

        gen1_decrypted = dispatcher_signing._get_fernet().decrypt(  # noqa: SLF001
            bytes(rows[0][1])
        )
        gen2_decrypted = dispatcher_signing._get_fernet().decrypt(  # noqa: SLF001
            bytes(rows[1][1])
        )
        assert gen1_decrypted == second_secret
        assert gen2_decrypted == first_secret

    def test_rotate_unknown_uuid_returns_404(self, client, auth_headers):
        resp = client.post(
            f"/api/admin/webhooks/{uuid.uuid4()}/rotate-secret",
            headers=auth_headers,
        )
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "subscription_not_found"

    def test_rotate_invalid_uuid_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/admin/webhooks/not-a-uuid/rotate-secret",
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_rotate_revoked_subscription_returns_400(self, client, auth_headers):
        created = _create_one(client, auth_headers, display_name="revoked")
        sub_id = created["subscription_id"]
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE webhook_subscriptions SET status = 'revoked' "
            "WHERE subscription_id = %s",
            (sub_id,),
        )
        cur.close()

        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/rotate-secret",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "cannot_rotate_revoked_subscription"

    def test_rotate_unauthenticated_returns_401(self, client):
        resp = client.post(
            f"/api/admin/webhooks/{uuid.uuid4()}/rotate-secret"
        )
        assert resp.status_code == 401

    def test_rotate_writes_audit_row_without_plaintext(self, client, auth_headers):
        created = _create_one(client, auth_headers, display_name="audit-rotate")
        sub_id = created["subscription_id"]
        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/rotate-secret",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        plaintext = resp.get_json()["secret"]

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT action_type, entity_type, details FROM audit_log "
            "WHERE action_type = 'WEBHOOK_SECRET_ROTATE' "
            "AND details->>'subscription_id' = %s",
            (sub_id,),
        )
        row = cur.fetchone()
        cur.close()
        assert row is not None
        action_type, entity_type, details = row
        assert action_type == "WEBHOOK_SECRET_ROTATE"
        assert entity_type == "WEBHOOK_SUBSCRIPTION"
        assert details["demoted_prior_primary"] is True
        assert plaintext not in str(details)


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
