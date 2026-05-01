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
        from services.webhook_dispatcher.url_normalize import (
            canonicalize_delivery_url,
        )

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO webhook_subscriptions_tombstones "
            "(subscription_id, delivery_url_at_delete, "
            "delivery_url_canonical, connector_id, deleted_by) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING tombstone_id",
            (
                str(uuid.uuid4()),
                delivery_url,
                canonicalize_delivery_url(delivery_url),
                "test-conn-webhook",
                1,
            ),
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

    @pytest.mark.parametrize(
        "variant_suffix",
        [
            # Casing on host segment.
            ("CASE", lambda raw: raw.replace("example.com", "EXAMPLE.com")),
            # Mixed scheme casing.
            ("SCHEME", lambda raw: raw.replace("https://", "HTTPS://")),
            # Default port noise.
            ("PORT", lambda raw: raw.replace("example.com", "example.com:443")),
            # Trailing slash on a non-root path.
            ("SLASH", lambda raw: raw + "/"),
            # Fragment that should be stripped.
            ("FRAGMENT", lambda raw: raw + "#fragment"),
        ],
    )
    def test_canonicalization_variants_still_trip_gate(
        self, client, auth_headers, variant_suffix
    ):
        """#218: a one-character casing / port / fragment / trailing-slash
        mutation must not bypass the URL-reuse acknowledgement step."""
        label, mutator = variant_suffix
        unique = uuid.uuid4().hex[:8]
        seeded_url = f"https://example.com/hook-{unique}"
        self._seed_tombstone(seeded_url)

        mutated = mutator(seeded_url)
        resp = client.post(
            "/api/admin/webhooks",
            json={
                "connector_id": "test-conn-webhook",
                "display_name": f"variant-{label}",
                "delivery_url": mutated,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 409, (label, mutated, resp.get_json())
        assert resp.get_json()["error"] == "url_reuse_tombstone"

    def test_canonical_column_populated_on_hard_delete_tombstone(
        self, client, auth_headers
    ):
        """#218: hard-delete writes both raw delivery_url_at_delete and
        the canonical column so the gate can match variants."""
        url = f"https://Example.COM:443/hook-{uuid.uuid4().hex[:8]}/"
        created = client.post(
            "/api/admin/webhooks",
            json={
                "connector_id": "test-conn-webhook",
                "display_name": "to-be-purged",
                "delivery_url": url,
            },
            headers=auth_headers,
        ).get_json()

        sub_id = created["subscription_id"]
        purge = client.delete(
            f"/api/admin/webhooks/{sub_id}?purge=true", headers=auth_headers
        )
        assert purge.status_code == 200, purge.get_json()

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT delivery_url_at_delete, delivery_url_canonical "
            "FROM webhook_subscriptions_tombstones "
            "WHERE tombstone_id = %s",
            (purge.get_json()["tombstone_id"],),
        )
        raw, canonical = cur.fetchone()
        cur.close()
        # Raw is the URL the admin originally typed; canonical is
        # lowercased / port-stripped / trailing-slash-collapsed.
        assert raw == url
        assert canonical == f"https://example.com/hook-{url.split('-')[-1].rstrip('/')}"


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


class TestPatchUpdate:
    def _publishes(self, monkeypatch):
        """Replace the dispatcher's pubsub publisher with a recorder
        so the test can assert exactly which events fire on each
        PATCH. Avoids relying on a live Redis to verify the routing
        decisions, which is what the endpoint owns."""
        from services.webhook_dispatcher import wake as wake_module

        captured = []

        def fake_publish(redis_url, subscription_id, event):
            captured.append({"subscription_id": subscription_id, "event": event})

        monkeypatch.setattr(
            wake_module, "publish_subscription_event", fake_publish
        )
        # Re-import the alias used by the route module so the patch
        # routes through the recorder instead of the real publisher.
        from routes.admin import admin_webhooks as route_module

        monkeypatch.setattr(
            route_module, "dispatcher_wake", wake_module
        )
        return captured

    def test_empty_body_is_no_op(self, client, auth_headers, monkeypatch):
        captured = self._publishes(monkeypatch)
        created = _create_one(client, auth_headers, display_name="empty-patch")
        sub_id = created["subscription_id"]
        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}", json={}, headers=auth_headers
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["display_name"] == "empty-patch"
        assert captured == []

    def test_display_name_updates_without_pubsub(
        self, client, auth_headers, monkeypatch
    ):
        captured = self._publishes(monkeypatch)
        created = _create_one(client, auth_headers, display_name="old")
        sub_id = created["subscription_id"]
        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={"display_name": "new"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["display_name"] == "new"
        assert captured == []

    def test_delivery_url_change_publishes_delivery_url_changed(
        self, client, auth_headers, monkeypatch
    ):
        captured = self._publishes(monkeypatch)
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        new_url = f"https://example.com/changed-{uuid.uuid4()}"
        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={"delivery_url": new_url},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["delivery_url"] == new_url
        assert any(
            c["event"] == "delivery_url_changed" for c in captured
        )

    def test_patch_to_tombstoned_url_returns_409(
        self, client, auth_headers, monkeypatch
    ):
        """#219: a PATCH that switches delivery_url to a previously-
        tombstoned URL must trip the same gate the POST handler
        runs. Without this check, create-then-PATCH bypasses the
        URL-reuse acknowledgement step entirely."""
        captured = self._publishes(monkeypatch)
        # Seed a tombstone for the URL we will PATCH to.
        tombstoned = f"https://example.com/tombstoned-{uuid.uuid4().hex[:8]}"
        seeder = TestUrlReuseTombstone()
        tombstone_id = seeder._seed_tombstone(tombstoned)

        created = _create_one(client, auth_headers, display_name="patch-victim")
        sub_id = created["subscription_id"]

        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={"delivery_url": tombstoned},
            headers=auth_headers,
        )
        assert resp.status_code == 409, resp.get_json()
        body = resp.get_json()
        assert body["error"] == "url_reuse_tombstone"
        assert body["tombstone_id"] == tombstone_id
        # No pubsub fires when the gate refuses; the row was not
        # mutated, so no peer state needs invalidation.
        assert captured == []

    def test_patch_with_acknowledge_url_reuse_clears_tombstone(
        self, client, auth_headers, monkeypatch
    ):
        """#219: PATCH accepts the same acknowledge_url_reuse opt-in
        as the POST handler. The tombstone is acknowledged in the
        same transaction as the URL change."""
        captured = self._publishes(monkeypatch)
        tombstoned = f"https://example.com/ack-{uuid.uuid4().hex[:8]}"
        seeder = TestUrlReuseTombstone()
        tombstone_id = seeder._seed_tombstone(tombstoned)

        created = _create_one(client, auth_headers, display_name="patch-ack")
        sub_id = created["subscription_id"]

        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={
                "delivery_url": tombstoned,
                "acknowledge_url_reuse": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["delivery_url"] == tombstoned
        assert any(c["event"] == "delivery_url_changed" for c in captured)

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT acknowledged_at, acknowledged_by "
            "FROM webhook_subscriptions_tombstones WHERE tombstone_id = %s",
            (tombstone_id,),
        )
        ack_at, ack_by = cur.fetchone()
        cur.close()
        assert ack_at is not None
        assert ack_by == 1

    def test_patch_canonicalization_variant_still_trips_gate(
        self, client, auth_headers, monkeypatch
    ):
        """#218 + #219: a one-character casing / port mutation on the
        PATCH side must still match the canonical key."""
        self._publishes(monkeypatch)
        seeded = f"https://example.com/canon-{uuid.uuid4().hex[:8]}"
        seeder = TestUrlReuseTombstone()
        seeder._seed_tombstone(seeded)

        created = _create_one(client, auth_headers, display_name="patch-canon")
        sub_id = created["subscription_id"]

        # Mutate: uppercase host + add default port.
        mutated = seeded.replace(
            "example.com", "EXAMPLE.com:443"
        )
        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={"delivery_url": mutated},
            headers=auth_headers,
        )
        assert resp.status_code == 409, resp.get_json()
        assert resp.get_json()["error"] == "url_reuse_tombstone"

    def test_patch_to_same_url_does_not_run_gate(
        self, client, auth_headers, monkeypatch
    ):
        """A no-op delivery_url PATCH (passing the current value
        verbatim) must not consult the tombstone table; the URL
        is unchanged and an unrelated tombstone for the same URL
        from a prior subscription would be a false positive."""
        self._publishes(monkeypatch)
        url = f"https://example.com/idem-{uuid.uuid4().hex[:8]}"
        created = _create_one(
            client, auth_headers, display_name="patch-idem", delivery_url=url
        )
        sub_id = created["subscription_id"]

        # No tombstone seeded; PATCH passes the same URL.
        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={"delivery_url": url},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()

    def test_rate_limit_change_publishes_rate_limit_changed(
        self, client, auth_headers, monkeypatch
    ):
        captured = self._publishes(monkeypatch)
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={"rate_limit_per_second": 25},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["rate_limit_per_second"] == 25
        assert any(c["event"] == "rate_limit_changed" for c in captured)

    def test_pause_then_resume_emits_paused_then_resumed(
        self, client, auth_headers, monkeypatch
    ):
        captured = self._publishes(monkeypatch)
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]

        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={"status": "paused"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "paused"
        assert body["pause_reason"] == "manual"
        assert captured[-1]["event"] == "paused"

        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={"status": "active"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "active"
        assert body["pause_reason"] is None
        assert captured[-1]["event"] == "resumed"

    def test_multi_field_patch_publishes_each_event_kind(
        self, client, auth_headers, monkeypatch
    ):
        captured = self._publishes(monkeypatch)
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        new_url = f"https://example.com/multi-{uuid.uuid4()}"
        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={
                "delivery_url": new_url,
                "rate_limit_per_second": 7,
                "status": "paused",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        events = {c["event"] for c in captured}
        assert "delivery_url_changed" in events
        assert "rate_limit_changed" in events
        assert "paused" in events

    def test_status_revoked_rejected(self, client, auth_headers, monkeypatch):
        self._publishes(monkeypatch)
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={"status": "revoked"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_revoked_subscription_cannot_be_modified(
        self, client, auth_headers, monkeypatch
    ):
        self._publishes(monkeypatch)
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE webhook_subscriptions SET status = 'revoked' "
            "WHERE subscription_id = %s",
            (sub_id,),
        )
        cur.close()
        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={"status": "active"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "cannot_modify_revoked_subscription"

    def test_unknown_field_rejected(self, client, auth_headers, monkeypatch):
        self._publishes(monkeypatch)
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={"bogus": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_https_required_on_url_change(
        self, client, auth_headers, monkeypatch
    ):
        self._publishes(monkeypatch)
        monkeypatch.delenv("SENTRY_ALLOW_HTTP_WEBHOOKS", raising=False)
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={"delivery_url": "http://example.com/x"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "https_required"

    def test_private_url_rejected_on_url_change(
        self, client, auth_headers, monkeypatch
    ):
        self._publishes(monkeypatch)
        monkeypatch.delenv("SENTRY_ALLOW_INTERNAL_WEBHOOKS", raising=False)
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={"delivery_url": "https://127.0.0.1/x"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "private_destination"

    def test_unknown_event_types_rejected(
        self, client, auth_headers, monkeypatch
    ):
        self._publishes(monkeypatch)
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={"subscription_filter": {"event_types": ["does.not.exist"]}},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "unknown_event_types"

    def test_ceiling_above_hard_cap_rejected(
        self, client, auth_headers, monkeypatch
    ):
        self._publishes(monkeypatch)
        # Create first under the default hard cap; lower the cap
        # only for the PATCH so the create-time validation is not
        # tripped by the same setting.
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        monkeypatch.setenv("DISPATCHER_MAX_PENDING_HARD_CAP", "1000")
        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={"pending_ceiling": 99999},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_unknown_uuid_returns_404(self, client, auth_headers, monkeypatch):
        self._publishes(monkeypatch)
        resp = client.patch(
            f"/api/admin/webhooks/{uuid.uuid4()}",
            json={"display_name": "x"},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_invalid_uuid_returns_400(self, client, auth_headers, monkeypatch):
        self._publishes(monkeypatch)
        resp = client.patch(
            "/api/admin/webhooks/not-a-uuid",
            json={"display_name": "x"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_unauthenticated_returns_401(self, client):
        resp = client.patch(
            f"/api/admin/webhooks/{uuid.uuid4()}", json={"display_name": "x"}
        )
        assert resp.status_code == 401

    def test_audit_row_records_diff(self, client, auth_headers, monkeypatch):
        self._publishes(monkeypatch)
        created = _create_one(client, auth_headers, display_name="audit-old")
        sub_id = created["subscription_id"]
        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={"display_name": "audit-new", "rate_limit_per_second": 9},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT details FROM audit_log "
            "WHERE action_type = 'WEBHOOK_SUBSCRIPTION_UPDATE' "
            "AND details->>'subscription_id' = %s "
            "ORDER BY log_id DESC LIMIT 1",
            (sub_id,),
        )
        details = cur.fetchone()[0]
        cur.close()
        diff = details["diff"]
        assert "display_name" in diff
        assert diff["display_name"]["before"] == "audit-old"
        assert diff["display_name"]["after"] == "audit-new"
        assert "rate_limit_per_second" in diff
        # Fields not mutated do not appear in the diff.
        assert "delivery_url" not in diff
        assert "pending_ceiling" not in diff

    def _latest_audit_events(self, sub_id: str) -> list:
        """Helper for the #216 events-field tests. Pulls the most
        recent WEBHOOK_SUBSCRIPTION_UPDATE row's events list."""
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT details->'events' FROM audit_log "
            "WHERE action_type = 'WEBHOOK_SUBSCRIPTION_UPDATE' "
            "AND details->>'subscription_id' = %s "
            "ORDER BY log_id DESC LIMIT 1",
            (sub_id,),
        )
        row = cur.fetchone()
        cur.close()
        return row[0] if row else None

    def test_audit_row_events_field_status_paused(
        self, client, auth_headers, monkeypatch,
    ):
        """#216: PATCH that flips status to paused records the
        published kind in details.events so audit triage can name
        what changed without re-deriving from the diff."""
        self._publishes(monkeypatch)
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={"status": "paused"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert self._latest_audit_events(sub_id) == ["paused"]

    def test_audit_row_events_field_rate_limit_change(
        self, client, auth_headers, monkeypatch,
    ):
        """#216: rate_limit_per_second change records
        rate_limit_changed."""
        self._publishes(monkeypatch)
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={"rate_limit_per_second": 7},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert self._latest_audit_events(sub_id) == ["rate_limit_changed"]

    def test_audit_row_events_field_multi_kind(
        self, client, auth_headers, monkeypatch,
    ):
        """#216: a PATCH that flips status AND rate limit in one
        body records both kinds in the order the publisher walks
        them."""
        self._publishes(monkeypatch)
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={"status": "paused", "rate_limit_per_second": 7},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        events = self._latest_audit_events(sub_id)
        # Walk order in the handler: rate_limit_per_second is checked
        # before status; both kinds present, both consistent with the
        # pubsub publish call sites.
        assert set(events) == {"paused", "rate_limit_changed"}

    def test_audit_row_events_field_empty_when_diff_unpublished(
        self, client, auth_headers, monkeypatch,
    ):
        """#216: a real diff that does not publish a pubsub event
        (pending_ceiling change only) still gets an audit row;
        details.events is an empty list. Distinct from the no-diff
        case which short-circuits before any audit row is written."""
        self._publishes(monkeypatch)
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        resp = client.patch(
            f"/api/admin/webhooks/{sub_id}",
            json={"pending_ceiling": 7777},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert self._latest_audit_events(sub_id) == []


class TestStats:
    def _seed_event(self, event_type="test.stats", warehouse_id=1):
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO integration_events "
            "(event_type, event_version, aggregate_type, aggregate_id, "
            " aggregate_external_id, warehouse_id, source_txn_id, payload) "
            "VALUES (%s, 1, 'agg', %s, %s, %s, %s, '{}'::jsonb) "
            "RETURNING event_id",
            (
                event_type,
                abs(hash(uuid.uuid4())) % (10**9),
                str(uuid.uuid4()),
                warehouse_id,
                str(uuid.uuid4()),
            ),
        )
        eid = cur.fetchone()[0]
        cur.close()
        return eid

    def _seed_delivery(
        self,
        sub_id: str,
        event_id: int,
        status: str = "succeeded",
        response_time_ms: int = None,
        error_kind: str = None,
    ):
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO webhook_deliveries "
            "(subscription_id, event_id, attempt_number, status, "
            " scheduled_at, attempted_at, completed_at, "
            " response_time_ms, error_kind, secret_generation) "
            "VALUES (%s, %s, %s, %s, NOW(), NOW(), NOW(), %s, %s, 1) "
            "RETURNING delivery_id",
            (
                sub_id,
                event_id,
                8 if status == "dlq" else 1,
                status,
                response_time_ms,
                error_kind,
            ),
        )
        did = cur.fetchone()[0]
        cur.close()
        return did

    def _clear_stats_cache(self):
        from routes.admin.admin_webhooks import _STATS_CACHE

        _STATS_CACHE.clear()

    def test_empty_subscription_returns_zero_rollups(
        self, client, auth_headers
    ):
        self._clear_stats_cache()
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        resp = client.get(
            f"/api/admin/webhooks/{sub_id}/stats", headers=auth_headers
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["window"] == "24h"
        assert body["attempts_total"] == 0
        assert body["success_rate"] is None
        assert body["response_time_ms"]["p50"] is None
        assert body["top_error_kinds"] == []
        assert body["current_lag"] == 0

    def test_rollups_reflect_seeded_deliveries(self, client, auth_headers):
        self._clear_stats_cache()
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        event_id = self._seed_event()
        self._seed_delivery(sub_id, event_id, "succeeded", response_time_ms=10)
        self._seed_delivery(sub_id, event_id, "succeeded", response_time_ms=50)
        self._seed_delivery(sub_id, event_id, "succeeded", response_time_ms=200)
        self._seed_delivery(sub_id, event_id, "failed", error_kind="5xx")
        self._seed_delivery(sub_id, event_id, "dlq", error_kind="connection")

        resp = client.get(
            f"/api/admin/webhooks/{sub_id}/stats", headers=auth_headers
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["attempts_total"] == 5
        assert body["succeeded"] == 3
        assert body["failed"] == 1
        assert body["dlq"] == 1
        assert body["success_rate"] == pytest.approx(0.6)
        # Three samples (10, 50, 200): p50 ~ 50, p95 close to 200, p99 close to 200.
        assert body["response_time_ms"]["p50"] == pytest.approx(50, rel=0.05)
        assert body["response_time_ms"]["p95"] is not None
        assert body["response_time_ms"]["p99"] is not None

    def test_top_error_kinds_capped_and_ordered(self, client, auth_headers):
        self._clear_stats_cache()
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        event_id = self._seed_event()
        # Seed seven distinct error_kinds with different counts.
        kinds = [
            ("connection", 5),
            ("5xx", 4),
            ("4xx", 3),
            ("timeout", 2),
            ("tls", 1),
            ("ssrf_rejected", 1),
            ("unknown", 1),
        ]
        for kind, n in kinds:
            for _ in range(n):
                self._seed_delivery(
                    sub_id, event_id, "failed", error_kind=kind
                )

        resp = client.get(
            f"/api/admin/webhooks/{sub_id}/stats", headers=auth_headers
        )
        assert resp.status_code == 200
        top = resp.get_json()["top_error_kinds"]
        assert len(top) == 5
        assert [t["kind"] for t in top[:4]] == ["connection", "5xx", "4xx", "timeout"]
        assert all(top[i]["count"] >= top[i + 1]["count"] for i in range(4))

    def test_current_lag_reflects_uncovered_events(
        self, client, auth_headers
    ):
        self._clear_stats_cache()
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        # Seed three events past the cursor (which is 0 by default).
        for _ in range(3):
            self._seed_event()

        resp = client.get(
            f"/api/admin/webhooks/{sub_id}/stats", headers=auth_headers
        )
        body = resp.get_json()
        assert body["current_lag"] >= 3

    def test_cache_returns_same_generated_at_within_ttl(
        self, client, auth_headers
    ):
        self._clear_stats_cache()
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]

        first = client.get(
            f"/api/admin/webhooks/{sub_id}/stats", headers=auth_headers
        ).get_json()
        second = client.get(
            f"/api/admin/webhooks/{sub_id}/stats", headers=auth_headers
        ).get_json()
        assert first["generated_at"] == second["generated_at"]

    def test_cache_key_distinguishes_window(self, client, auth_headers):
        self._clear_stats_cache()
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]

        a = client.get(
            f"/api/admin/webhooks/{sub_id}/stats?window=1h",
            headers=auth_headers,
        ).get_json()
        b = client.get(
            f"/api/admin/webhooks/{sub_id}/stats?window=24h",
            headers=auth_headers,
        ).get_json()
        assert a["window"] == "1h"
        assert b["window"] == "24h"

    def test_invalid_window_rejected(self, client, auth_headers):
        self._clear_stats_cache()
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        resp = client.get(
            f"/api/admin/webhooks/{sub_id}/stats?window=banana",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_window"

    def test_unknown_uuid_returns_404(self, client, auth_headers):
        self._clear_stats_cache()
        resp = client.get(
            f"/api/admin/webhooks/{uuid.uuid4()}/stats",
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_invalid_uuid_returns_400(self, client, auth_headers):
        self._clear_stats_cache()
        resp = client.get(
            "/api/admin/webhooks/not-a-uuid/stats", headers=auth_headers
        )
        assert resp.status_code == 400

    def test_unauthenticated_returns_401(self, client):
        resp = client.get(f"/api/admin/webhooks/{uuid.uuid4()}/stats")
        assert resp.status_code == 401


class TestReplayBatch:
    def _seed_event(self, event_type="test.batch", warehouse_id=1):
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO integration_events "
            "(event_type, event_version, aggregate_type, aggregate_id, "
            " aggregate_external_id, warehouse_id, source_txn_id, payload) "
            "VALUES (%s, 1, 'agg', %s, %s, %s, %s, '{}'::jsonb) "
            "RETURNING event_id",
            (
                event_type,
                abs(hash(uuid.uuid4())) % (10**9),
                str(uuid.uuid4()),
                warehouse_id,
                str(uuid.uuid4()),
            ),
        )
        event_id = cur.fetchone()[0]
        cur.close()
        return event_id

    def _seed_delivery(
        self, sub_id: str, event_id: int, status: str = "dlq"
    ):
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO webhook_deliveries "
            "(subscription_id, event_id, attempt_number, status, "
            " scheduled_at, attempted_at, completed_at, secret_generation) "
            "VALUES (%s, %s, %s, %s, NOW(), NOW(), NOW(), 1) "
            "RETURNING delivery_id",
            (sub_id, event_id, 8 if status == "dlq" else 1, status),
        )
        delivery_id = cur.fetchone()[0]
        cur.close()
        return delivery_id

    def _pending_count(self, sub_id: str):
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM webhook_deliveries "
            "WHERE subscription_id = %s AND status = 'pending'",
            (sub_id,),
        )
        n = cur.fetchone()[0]
        cur.close()
        return n

    def test_batch_replay_inserts_pending_rows_and_writes_audit(
        self, client, auth_headers
    ):
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        event_id = self._seed_event()
        for _ in range(3):
            self._seed_delivery(sub_id, event_id, "dlq")

        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/replay-batch",
            json={"filter": {"status": "dlq"}},
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.get_json()
        body = resp.get_json()
        assert body["impact_count"] == 3
        assert body["replayed_count"] == 3
        assert self._pending_count(sub_id) == 3

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT details FROM audit_log "
            "WHERE action_type = 'WEBHOOK_DELIVERY_REPLAY_BATCH' "
            "AND details->>'subscription_id' = %s",
            (sub_id,),
        )
        row = cur.fetchone()
        cur.close()
        assert row is not None
        details = row[0]
        assert details["impact_count"] == 3
        assert details["acknowledge_large_replay"] is False

    def test_filter_event_type_narrows(self, client, auth_headers):
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        match_event = self._seed_event(event_type="want.this")
        skip_event = self._seed_event(event_type="skip.this")
        for _ in range(2):
            self._seed_delivery(sub_id, match_event, "dlq")
        self._seed_delivery(sub_id, skip_event, "dlq")

        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/replay-batch",
            json={"filter": {"status": "dlq", "event_type": "want.this"}},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        assert resp.get_json()["impact_count"] == 2

    def test_filter_warehouse_id_narrows(self, client, auth_headers):
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        match_event = self._seed_event(warehouse_id=1)
        skip_event = self._seed_event(warehouse_id=2)
        self._seed_delivery(sub_id, match_event, "dlq")
        self._seed_delivery(sub_id, skip_event, "dlq")

        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/replay-batch",
            json={"filter": {"warehouse_id": 1}},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        assert resp.get_json()["impact_count"] == 1

    def test_zero_impact_writes_audit_and_returns_201(
        self, client, auth_headers
    ):
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/replay-batch",
            json={"filter": {}},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        assert resp.get_json()["impact_count"] == 0
        # No new pending rows.
        assert self._pending_count(sub_id) == 0

    def test_hard_cap_refuses_without_acknowledgement(
        self, client, auth_headers, monkeypatch
    ):
        monkeypatch.setenv("DISPATCHER_REPLAY_BATCH_HARD_CAP", "2")
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        event_id = self._seed_event()
        for _ in range(3):
            self._seed_delivery(sub_id, event_id, "dlq")

        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/replay-batch",
            json={"filter": {"status": "dlq"}},
            headers=auth_headers,
        )
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["error"] == "batch_size_above_hard_cap"
        assert body["impact_count"] == 3
        assert body["hard_cap"] == 2
        # No insert, no audit.
        assert self._pending_count(sub_id) == 0

    def test_hard_cap_bypassed_with_acknowledgement(
        self, client, auth_headers, monkeypatch
    ):
        monkeypatch.setenv("DISPATCHER_REPLAY_BATCH_HARD_CAP", "2")
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        event_id = self._seed_event()
        for _ in range(3):
            self._seed_delivery(sub_id, event_id, "dlq")

        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/replay-batch",
            json={
                "filter": {"status": "dlq"},
                "acknowledge_large_replay": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        assert resp.get_json()["impact_count"] == 3
        assert self._pending_count(sub_id) == 3

    def test_throttle_returns_429_within_window(self, client, auth_headers):
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]

        first = client.post(
            f"/api/admin/webhooks/{sub_id}/replay-batch",
            json={"filter": {}},
            headers=auth_headers,
        )
        assert first.status_code == 201

        second = client.post(
            f"/api/admin/webhooks/{sub_id}/replay-batch",
            json={"filter": {}},
            headers=auth_headers,
        )
        assert second.status_code == 429
        body = second.get_json()
        assert body["error"] == "replay_batch_throttled"
        assert body["seconds_until_retry"] >= 1

    def test_revoked_subscription_rejected(self, client, auth_headers):
        created = _create_one(client, auth_headers)
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
            f"/api/admin/webhooks/{sub_id}/replay-batch",
            json={"filter": {}},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "cannot_replay_to_revoked_subscription"

    def test_unknown_filter_key_rejected(self, client, auth_headers):
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/replay-batch",
            json={"filter": {"bogus": "x"}},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_unknown_status_value_rejected(self, client, auth_headers):
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/replay-batch",
            json={"filter": {"status": "pending"}},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_unknown_subscription_returns_404(self, client, auth_headers):
        resp = client.post(
            f"/api/admin/webhooks/{uuid.uuid4()}/replay-batch",
            json={"filter": {}},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_invalid_uuid_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/admin/webhooks/not-a-uuid/replay-batch",
            json={"filter": {}},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_unauthenticated_returns_401(self, client):
        resp = client.post(
            f"/api/admin/webhooks/{uuid.uuid4()}/replay-batch",
            json={"filter": {}},
        )
        assert resp.status_code == 401

    def _bulk_seed_deliveries(self, sub_id: str, event_id: int, count: int):
        """Bulk INSERT helper for ceiling tests. Single statement is
        much faster than per-row INSERTs when the row count is in
        the low hundreds; the schema CHECK on pending_ceiling
        (BETWEEN 100 AND 100000) forces us to seed at least 101
        rows for any test that wants to overshoot the ceiling."""
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO webhook_deliveries "
            "(subscription_id, event_id, attempt_number, status, "
            " scheduled_at, attempted_at, completed_at, secret_generation) "
            "SELECT %s, %s, 8, 'dlq', NOW(), NOW(), NOW(), 1 "
            "FROM generate_series(1, %s)",
            (sub_id, event_id, count),
        )
        cur.close()

    def test_replay_refused_when_would_exceed_pending_ceiling(
        self, client, auth_headers
    ):
        """#222: a replay-batch that would push pending+in_flight
        past the subscription's pending_ceiling is refused with 409
        BEFORE the INSERT lands. The auto-pause path only fires
        after a delivery attempt; without a pre-INSERT check the
        batch could overshoot the ceiling silently."""
        # Schema CHECK enforces pending_ceiling BETWEEN 100 AND
        # 100000, so seed 101 dlq rows against a ceiling of 100 to
        # trip the gate by exactly one.
        body = {
            "connector_id": "test-conn-webhook",
            "display_name": "ceiling-victim",
            "delivery_url": f"https://example.com/{uuid.uuid4()}",
            "pending_ceiling": 100,
            "dlq_ceiling": 200,
        }
        sub_id = client.post(
            "/api/admin/webhooks", json=body, headers=auth_headers
        ).get_json()["subscription_id"]

        event_id = self._seed_event()
        self._bulk_seed_deliveries(sub_id, event_id, 101)

        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/replay-batch",
            json={"filter": {"status": "dlq"}},
            headers=auth_headers,
        )
        assert resp.status_code == 409, resp.get_json()
        body_out = resp.get_json()
        assert body_out["error"] == "replay_would_exceed_pending_ceiling"
        assert body_out["current_pending"] == 0
        assert body_out["impact_count"] == 101
        assert body_out["pending_ceiling"] == 100
        assert body_out["gap"] == 1

        # The INSERT did not land.
        assert self._pending_count(sub_id) == 0

    def test_replay_within_ceiling_proceeds(self, client, auth_headers):
        """An impact that fits inside the remaining pending budget
        replays normally."""
        body = {
            "connector_id": "test-conn-webhook",
            "display_name": "ceiling-fits",
            "delivery_url": f"https://example.com/{uuid.uuid4()}",
            "pending_ceiling": 200,
            "dlq_ceiling": 200,
        }
        sub_id = client.post(
            "/api/admin/webhooks", json=body, headers=auth_headers
        ).get_json()["subscription_id"]

        event_id = self._seed_event()
        for _ in range(3):
            self._seed_delivery(sub_id, event_id, "dlq")
        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/replay-batch",
            json={"filter": {"status": "dlq"}},
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.get_json()
        assert resp.get_json()["replayed_count"] == 3

    def test_replay_acknowledge_large_replay_does_not_waive_ceiling(
        self, client, auth_headers
    ):
        """The acknowledge_large_replay flag covers the per-batch
        hard cap; the pending_ceiling is independent and not
        waivable. A request with the flag set must still be 409'd
        when the ceiling would be crossed."""
        body = {
            "connector_id": "test-conn-webhook",
            "display_name": "ack-no-waive",
            "delivery_url": f"https://example.com/{uuid.uuid4()}",
            "pending_ceiling": 100,
            "dlq_ceiling": 200,
        }
        sub_id = client.post(
            "/api/admin/webhooks", json=body, headers=auth_headers
        ).get_json()["subscription_id"]

        event_id = self._seed_event()
        self._bulk_seed_deliveries(sub_id, event_id, 101)

        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/replay-batch",
            json={
                "filter": {"status": "dlq"},
                "acknowledge_large_replay": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "replay_would_exceed_pending_ceiling"

    def test_replay_batch_handler_holds_for_update_on_subscription_row(self):
        """#223: the replay-batch handler must hold SELECT FOR
        UPDATE on the subscription row through the throttle SELECT
        and the pre-INSERT pending_ceiling check. Source-level
        sentinel: a refactor that drops the lock surfaces here.

        Reading the file rather than driving real concurrency
        because the test client routes every request through the
        same SQLAlchemy connection (conftest wraps the suite in a
        single transaction), so two threaded requests serialize at
        the connection level and never expose the row-lock
        behavior. The lock-acquisition shape itself is exercised
        by test_replay_batch_for_update_blocks_concurrent_session
        below."""
        handler_path = os.path.join(
            os.path.dirname(__file__), "..", "routes", "admin",
            "admin_webhooks.py",
        )
        with open(handler_path) as f:
            source = f.read()
        # Locate the replay_batch function and check that its body
        # contains a SELECT against webhook_subscriptions with FOR
        # UPDATE. A refactor that drops the lock would surface
        # here.
        anchor = "def replay_batch("
        start = source.index(anchor)
        # End at the next top-level def or admin_bp.route to
        # bound the function body inspection.
        end = source.find("\n@admin_bp.route", start + len(anchor))
        assert end > start, "could not bound replay_batch source"
        body = source[start:end]
        assert "FROM webhook_subscriptions" in body
        assert "FOR UPDATE" in body, (
            "replay_batch must hold SELECT FOR UPDATE on the "
            "subscription row to close the throttle TOCTOU race"
        )

    def _seed_global_throttle_audit_rows(self, count: int):
        """Insert N WEBHOOK_DELIVERY_REPLAY_BATCH audit_log rows
        across distinct fake subscription_ids so the global
        throttle bucket counts them but the per-subscription
        throttle does not match any real subscription. Inserts
        through the test transaction's raw connection so the rows
        are visible to the handler under test and roll back at
        teardown (no cross-test pollution)."""
        import json as _json

        conn = get_raw_connection()
        cur = conn.cursor()
        for _ in range(count):
            cur.execute(
                "INSERT INTO audit_log "
                "(action_type, entity_type, entity_id, user_id, "
                " warehouse_id, details) "
                "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
                (
                    "WEBHOOK_DELIVERY_REPLAY_BATCH",
                    "WEBHOOK_SUBSCRIPTION",
                    0,
                    "seed",
                    None,
                    _json.dumps({"subscription_id": str(uuid.uuid4())}),
                ),
            )
        cur.close()

    def test_global_throttle_refuses_after_budget_consumed(
        self, client, auth_headers, monkeypatch
    ):
        """#224: once the rolling global budget of N
        WEBHOOK_DELIVERY_REPLAY_BATCH rows lands, a fresh request
        from any subscription is refused 429
        replay_batch_global_throttled. Closes the fan-out path
        where a compromised admin distributes batches across many
        subscriptions to bypass the per-subscription 60s bucket."""
        # Tighten the global budget for a fast test; 2 rows in
        # any subscription saturates the bucket.
        monkeypatch.setenv("DISPATCHER_REPLAY_BATCH_GLOBAL_BUDGET", "2")
        monkeypatch.setenv("DISPATCHER_REPLAY_BATCH_GLOBAL_WINDOW_S", "300")

        # Pre-fill the bucket with 2 audit rows attributed to other
        # subscriptions so the per-subscription throttle on this
        # subscription is clear.
        self._seed_global_throttle_audit_rows(2)

        created = _create_one(client, auth_headers, display_name="global-throttle")
        sub_id = created["subscription_id"]

        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/replay-batch",
            json={"filter": {"status": "dlq"}},
            headers=auth_headers,
        )
        assert resp.status_code == 429, resp.get_json()
        body = resp.get_json()
        assert body["error"] == "replay_batch_global_throttled"
        assert body["global_count"] >= 2
        assert body["global_budget"] == 2

    def test_global_throttle_lets_through_when_under_budget(
        self, client, auth_headers, monkeypatch
    ):
        """The global bucket only refuses once it is FULL; an
        empty / under-budget bucket still accepts."""
        monkeypatch.setenv("DISPATCHER_REPLAY_BATCH_GLOBAL_BUDGET", "5")
        monkeypatch.setenv("DISPATCHER_REPLAY_BATCH_GLOBAL_WINDOW_S", "300")

        created = _create_one(client, auth_headers, display_name="global-fits")
        sub_id = created["subscription_id"]
        event_id = self._seed_event()
        self._seed_delivery(sub_id, event_id, "dlq")

        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/replay-batch",
            json={"filter": {"status": "dlq"}},
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.get_json()

    def test_global_throttle_counts_only_recent_window(
        self, client, auth_headers, monkeypatch
    ):
        """The bucket is rolling: a row outside the window does
        not count. Insert one audit row with a backdated
        created_at older than the configured window and confirm
        the new request is admitted (audit_log is append-only;
        UPDATE is forbidden by V-025, so we INSERT with the
        timestamp pre-set)."""
        import json as _json

        monkeypatch.setenv("DISPATCHER_REPLAY_BATCH_GLOBAL_BUDGET", "1")
        monkeypatch.setenv("DISPATCHER_REPLAY_BATCH_GLOBAL_WINDOW_S", "5")

        # Insert one audit row dated 60s ago. audit_log_chain_hash
        # honors NEW.created_at, so an explicit backdated value
        # survives the trigger.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO audit_log "
            "(action_type, entity_type, entity_id, user_id, "
            " warehouse_id, details, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, "
            "        NOW() - INTERVAL '60 seconds')",
            (
                "WEBHOOK_DELIVERY_REPLAY_BATCH",
                "WEBHOOK_SUBSCRIPTION",
                0,
                "seed",
                None,
                _json.dumps({"subscription_id": str(uuid.uuid4())}),
            ),
        )
        cur.close()

        created = _create_one(client, auth_headers, display_name="rolling")
        sub_id = created["subscription_id"]
        event_id = self._seed_event()
        self._seed_delivery(sub_id, event_id, "dlq")

        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/replay-batch",
            json={"filter": {"status": "dlq"}},
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.get_json()

    def test_cursor_unchanged_after_batch(self, client, auth_headers):
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        event_id = self._seed_event()
        self._seed_delivery(sub_id, event_id, "dlq")

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT last_delivered_event_id FROM webhook_subscriptions "
            "WHERE subscription_id = %s",
            (sub_id,),
        )
        before = cur.fetchone()[0]
        cur.close()

        client.post(
            f"/api/admin/webhooks/{sub_id}/replay-batch",
            json={"filter": {}},
            headers=auth_headers,
        )

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT last_delivered_event_id FROM webhook_subscriptions "
            "WHERE subscription_id = %s",
            (sub_id,),
        )
        after = cur.fetchone()[0]
        cur.close()
        assert before == after


class TestReplaySingle:
    def _seed_event_and_delivery(self, sub_id: str, status: str = "dlq"):
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO integration_events "
            "(event_type, event_version, aggregate_type, aggregate_id, "
            " aggregate_external_id, warehouse_id, source_txn_id, payload) "
            "VALUES ('test.replay', 1, 'agg', %s, %s, 1, %s, '{}'::jsonb) "
            "RETURNING event_id",
            (
                abs(hash(uuid.uuid4())) % (10**9),
                str(uuid.uuid4()),
                str(uuid.uuid4()),
            ),
        )
        event_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO webhook_deliveries "
            "(subscription_id, event_id, attempt_number, status, "
            " scheduled_at, attempted_at, completed_at, secret_generation) "
            "VALUES (%s, %s, %s, %s, NOW(), NOW(), NOW(), 1) "
            "RETURNING delivery_id",
            (sub_id, event_id, 8 if status == "dlq" else 1, status),
        )
        delivery_id = cur.fetchone()[0]
        cur.close()
        return event_id, delivery_id

    def _delivery_count(self, sub_id: str, status: str = None):
        conn = get_raw_connection()
        cur = conn.cursor()
        if status is None:
            cur.execute(
                "SELECT COUNT(*) FROM webhook_deliveries WHERE subscription_id = %s",
                (sub_id,),
            )
        else:
            cur.execute(
                "SELECT COUNT(*) FROM webhook_deliveries "
                "WHERE subscription_id = %s AND status = %s",
                (sub_id, status),
            )
        n = cur.fetchone()[0]
        cur.close()
        return n

    def test_replay_dlq_creates_pending_row(self, client, auth_headers):
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        event_id, delivery_id = self._seed_event_and_delivery(sub_id, "dlq")

        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/replay/{delivery_id}",
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["original_delivery_id"] == delivery_id
        assert isinstance(body["replayed_delivery_id"], int)
        assert body["replayed_delivery_id"] != delivery_id

        assert self._delivery_count(sub_id, "pending") == 1
        assert self._delivery_count(sub_id, "dlq") == 1

    def test_replay_does_not_advance_cursor(self, client, auth_headers):
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        _, delivery_id = self._seed_event_and_delivery(sub_id, "dlq")

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT last_delivered_event_id FROM webhook_subscriptions "
            "WHERE subscription_id = %s",
            (sub_id,),
        )
        before = cur.fetchone()[0]
        cur.close()

        client.post(
            f"/api/admin/webhooks/{sub_id}/replay/{delivery_id}",
            headers=auth_headers,
        )

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT last_delivered_event_id FROM webhook_subscriptions "
            "WHERE subscription_id = %s",
            (sub_id,),
        )
        after = cur.fetchone()[0]
        cur.close()
        assert before == after

    def test_replay_failed_or_succeeded_also_creates_pending(
        self, client, auth_headers
    ):
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        _, failed_id = self._seed_event_and_delivery(sub_id, "failed")
        _, success_id = self._seed_event_and_delivery(sub_id, "succeeded")

        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/replay/{failed_id}",
            headers=auth_headers,
        )
        assert resp.status_code == 201
        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/replay/{success_id}",
            headers=auth_headers,
        )
        assert resp.status_code == 201

        assert self._delivery_count(sub_id, "pending") == 2

    def test_double_replay_creates_two_pending_rows(
        self, client, auth_headers
    ):
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        _, delivery_id = self._seed_event_and_delivery(sub_id, "dlq")

        client.post(
            f"/api/admin/webhooks/{sub_id}/replay/{delivery_id}",
            headers=auth_headers,
        )
        client.post(
            f"/api/admin/webhooks/{sub_id}/replay/{delivery_id}",
            headers=auth_headers,
        )
        assert self._delivery_count(sub_id, "pending") == 2

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE action_type = 'WEBHOOK_DELIVERY_REPLAY_SINGLE' "
            "AND details->>'subscription_id' = %s",
            (sub_id,),
        )
        audit_count = cur.fetchone()[0]
        cur.close()
        assert audit_count == 2

    def test_url_tampering_rejected(self, client, auth_headers):
        # Create two subscriptions; replay across them must fail.
        a = _create_one(client, auth_headers, display_name="a")
        b = _create_one(client, auth_headers, display_name="b")
        _, delivery_id = self._seed_event_and_delivery(
            a["subscription_id"], "dlq"
        )

        resp = client.post(
            f"/api/admin/webhooks/{b['subscription_id']}/replay/{delivery_id}",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "delivery_subscription_mismatch"

    def test_replay_to_revoked_rejected(self, client, auth_headers):
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        _, delivery_id = self._seed_event_and_delivery(sub_id, "dlq")
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE webhook_subscriptions SET status = 'revoked' "
            "WHERE subscription_id = %s",
            (sub_id,),
        )
        cur.close()

        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/replay/{delivery_id}",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "cannot_replay_to_revoked_subscription"

    def test_unknown_subscription_returns_404(self, client, auth_headers):
        resp = client.post(
            f"/api/admin/webhooks/{uuid.uuid4()}/replay/1",
            headers=auth_headers,
        )
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "subscription_not_found"

    def test_unknown_delivery_returns_404(self, client, auth_headers):
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        resp = client.post(
            f"/api/admin/webhooks/{sub_id}/replay/99999999",
            headers=auth_headers,
        )
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "delivery_not_found"

    def test_invalid_uuid_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/admin/webhooks/not-a-uuid/replay/1", headers=auth_headers
        )
        assert resp.status_code == 400

    def test_unauthenticated_returns_401(self, client):
        resp = client.post(f"/api/admin/webhooks/{uuid.uuid4()}/replay/1")
        assert resp.status_code == 401


class TestDlqViewer:
    def _seed_event(self):
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO integration_events "
            "(event_type, event_version, aggregate_type, aggregate_id, "
            " aggregate_external_id, warehouse_id, source_txn_id, payload) "
            "VALUES ('test.dlq', 1, 'agg', %s, %s, 1, %s, '{}'::jsonb) "
            "RETURNING event_id, aggregate_external_id, source_txn_id",
            (
                abs(hash(uuid.uuid4())) % (10**9),
                str(uuid.uuid4()),
                str(uuid.uuid4()),
            ),
        )
        row = cur.fetchone()
        cur.close()
        return row[0], row[1], row[2]

    def _seed_delivery(self, sub_id: str, event_id: int, status: str):
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO webhook_deliveries "
            "(subscription_id, event_id, attempt_number, status, "
            " scheduled_at, attempted_at, completed_at, http_status, "
            " error_kind, error_detail, secret_generation) "
            "VALUES (%s, %s, %s, %s, NOW(), NOW(), NOW(), %s, %s, %s, 1) "
            "RETURNING delivery_id",
            (
                sub_id,
                event_id,
                8 if status == "dlq" else 1,
                status,
                500 if status in ("dlq", "failed") else 200,
                "5xx" if status in ("dlq", "failed") else None,
                "consumer 500" if status in ("dlq", "failed") else None,
            ),
        )
        delivery_id = cur.fetchone()[0]
        cur.close()
        return delivery_id

    def test_empty_dlq_returns_zero_total(self, client, auth_headers):
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        resp = client.get(
            f"/api/admin/webhooks/{sub_id}/dlq", headers=auth_headers
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["total"] == 0
        assert body["deliveries"] == []
        assert body["limit"] == 50
        assert body["offset"] == 0

    def test_dlq_returns_rows_newest_first_with_event_context(
        self, client, auth_headers
    ):
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        event_id, ext_id, txn_id = self._seed_event()
        first_id = self._seed_delivery(sub_id, event_id, "dlq")
        second_id = self._seed_delivery(sub_id, event_id, "dlq")

        resp = client.get(
            f"/api/admin/webhooks/{sub_id}/dlq", headers=auth_headers
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["total"] == 2
        ids = [d["delivery_id"] for d in body["deliveries"]]
        # Stable secondary order: delivery_id DESC on identical
        # completed_at ties.
        assert ids == [second_id, first_id]

        first_payload = body["deliveries"][0]
        assert first_payload["event"]["event_type"] == "test.dlq"
        assert first_payload["event"]["aggregate_external_id"] == str(ext_id)
        assert first_payload["event"]["source_txn_id"] == str(txn_id)
        assert first_payload["http_status"] == 500
        assert first_payload["error_kind"] == "5xx"

    def test_non_dlq_rows_excluded(self, client, auth_headers):
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        event_id, _, _ = self._seed_event()
        self._seed_delivery(sub_id, event_id, "failed")
        self._seed_delivery(sub_id, event_id, "succeeded")
        kept = self._seed_delivery(sub_id, event_id, "dlq")

        resp = client.get(
            f"/api/admin/webhooks/{sub_id}/dlq", headers=auth_headers
        )
        body = resp.get_json()
        assert body["total"] == 1
        assert [d["delivery_id"] for d in body["deliveries"]] == [kept]

    def test_pagination_limit_and_offset(self, client, auth_headers):
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        event_id, _, _ = self._seed_event()
        ids = [self._seed_delivery(sub_id, event_id, "dlq") for _ in range(5)]
        ids_desc = list(reversed(ids))

        resp = client.get(
            f"/api/admin/webhooks/{sub_id}/dlq?limit=2&offset=0",
            headers=auth_headers,
        )
        page1 = [d["delivery_id"] for d in resp.get_json()["deliveries"]]
        assert page1 == ids_desc[:2]

        resp = client.get(
            f"/api/admin/webhooks/{sub_id}/dlq?limit=2&offset=2",
            headers=auth_headers,
        )
        page2 = [d["delivery_id"] for d in resp.get_json()["deliveries"]]
        assert page2 == ids_desc[2:4]

    def test_pagination_invalid_inputs_rejected(self, client, auth_headers):
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]

        for bad in ("limit=0", "limit=501", "offset=-1", "limit=abc"):
            resp = client.get(
                f"/api/admin/webhooks/{sub_id}/dlq?{bad}",
                headers=auth_headers,
            )
            assert resp.status_code == 400, bad

    def test_unknown_uuid_returns_404(self, client, auth_headers):
        resp = client.get(
            f"/api/admin/webhooks/{uuid.uuid4()}/dlq", headers=auth_headers
        )
        assert resp.status_code == 404

    def test_invalid_uuid_returns_400(self, client, auth_headers):
        resp = client.get(
            "/api/admin/webhooks/not-a-uuid/dlq", headers=auth_headers
        )
        assert resp.status_code == 400

    def test_unauthenticated_returns_401(self, client):
        resp = client.get(f"/api/admin/webhooks/{uuid.uuid4()}/dlq")
        assert resp.status_code == 401


class TestDelete:
    def _publishes(self, monkeypatch):
        from services.webhook_dispatcher import wake as wake_module

        captured = []

        def fake_publish(redis_url, subscription_id, event):
            captured.append({"subscription_id": subscription_id, "event": event})

        monkeypatch.setattr(wake_module, "publish_subscription_event", fake_publish)
        from routes.admin import admin_webhooks as route_module

        monkeypatch.setattr(route_module, "dispatcher_wake", wake_module)
        return captured

    def _subscription_status(self, sub_id: str):
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT status, pause_reason FROM webhook_subscriptions "
            "WHERE subscription_id = %s",
            (sub_id,),
        )
        row = cur.fetchone()
        cur.close()
        return row

    def test_soft_delete_flips_status_and_publishes_deleted(
        self, client, auth_headers, monkeypatch
    ):
        captured = self._publishes(monkeypatch)
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]

        resp = client.delete(
            f"/api/admin/webhooks/{sub_id}", headers=auth_headers
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["purged"] is False
        assert body["status"] == "revoked"

        status, pause_reason = self._subscription_status(sub_id)
        assert status == "revoked"
        assert pause_reason is None
        assert any(c["event"] == "deleted" for c in captured)

    def test_soft_delete_idempotent_on_revoked(
        self, client, auth_headers, monkeypatch
    ):
        captured = self._publishes(monkeypatch)
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        client.delete(f"/api/admin/webhooks/{sub_id}", headers=auth_headers)
        first_count = len(captured)

        resp = client.delete(
            f"/api/admin/webhooks/{sub_id}", headers=auth_headers
        )
        assert resp.status_code == 200
        # Second call must not publish a duplicate or write a
        # second audit row.
        assert len(captured) == first_count

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE action_type = 'WEBHOOK_SUBSCRIPTION_DELETE_SOFT' "
            "AND details->>'subscription_id' = %s",
            (sub_id,),
        )
        soft_count = cur.fetchone()[0]
        cur.close()
        assert soft_count == 1

    def test_hard_delete_removes_row_writes_tombstone_publishes_deleted(
        self, client, auth_headers, monkeypatch
    ):
        captured = self._publishes(monkeypatch)
        url = f"https://example.com/hard-{uuid.uuid4()}"
        created = _create_one(client, auth_headers, delivery_url=url)
        sub_id = created["subscription_id"]

        resp = client.delete(
            f"/api/admin/webhooks/{sub_id}?purge=true", headers=auth_headers
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["purged"] is True
        tombstone_id = body["tombstone_id"]
        assert isinstance(tombstone_id, int)

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM webhook_subscriptions WHERE subscription_id = %s",
            (sub_id,),
        )
        assert cur.fetchone() is None

        cur.execute(
            "SELECT delivery_url_at_delete, acknowledged_at "
            "FROM webhook_subscriptions_tombstones WHERE tombstone_id = %s",
            (tombstone_id,),
        )
        ts_row = cur.fetchone()
        assert ts_row is not None
        assert ts_row[0] == url
        assert ts_row[1] is None  # tombstone fresh, not acknowledged

        # webhook_secrets cascades on the FK; the row(s) are gone.
        cur.execute(
            "SELECT COUNT(*) FROM webhook_secrets WHERE subscription_id = %s",
            (sub_id,),
        )
        assert cur.fetchone()[0] == 0
        cur.close()

        assert any(c["event"] == "deleted" for c in captured)

    def test_hard_delete_cascades_terminal_deliveries(
        self, client, auth_headers, monkeypatch
    ):
        """#211: terminal deliveries (succeeded / failed / dlq) must
        not block ?purge=true. Pre-fix the FK ON DELETE RESTRICT
        on webhook_deliveries.subscription_id leaked an unhandled
        IntegrityError as a generic 500 once the subscription had
        any terminal history. Post-fix the purge cascades through
        webhook_deliveries before deleting the subscription row."""
        captured = self._publishes(monkeypatch)
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]

        # Seed an integration_events row + three terminal delivery
        # rows on the subscription so the cascade has work to do.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO integration_events "
            "(event_type, event_version, aggregate_type, aggregate_id, "
            " aggregate_external_id, warehouse_id, source_txn_id, payload) "
            "VALUES ('test.terminal', 1, 'agg', %s, %s, 1, %s, '{}'::jsonb) "
            "RETURNING event_id",
            (
                abs(hash(uuid.uuid4())) % (10**9),
                str(uuid.uuid4()),
                str(uuid.uuid4()),
            ),
        )
        event_id = cur.fetchone()[0]
        for status in ("succeeded", "failed", "dlq"):
            cur.execute(
                "INSERT INTO webhook_deliveries "
                "(subscription_id, event_id, attempt_number, status, "
                " scheduled_at, completed_at, secret_generation) "
                "VALUES (%s, %s, 1, %s, NOW(), NOW(), 1)",
                (sub_id, event_id, status),
            )
        cur.close()

        resp = client.delete(
            f"/api/admin/webhooks/{sub_id}?purge=true", headers=auth_headers
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["purged"] is True

        # Subscription gone; deliveries cascaded; tombstone present.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM webhook_subscriptions WHERE subscription_id = %s",
            (sub_id,),
        )
        assert cur.fetchone() is None
        cur.execute(
            "SELECT COUNT(*) FROM webhook_deliveries WHERE subscription_id = %s",
            (sub_id,),
        )
        assert cur.fetchone()[0] == 0
        cur.close()

        # Audit row records how many delivery rows were cascaded so
        # the trail captures the destructive blast radius.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT details->>'cascaded_deliveries' "
            "  FROM audit_log "
            " WHERE action_type = 'WEBHOOK_SUBSCRIPTION_DELETE_HARD' "
            "   AND details->>'subscription_id' = %s "
            " ORDER BY log_id DESC LIMIT 1",
            (sub_id,),
        )
        cascaded = cur.fetchone()[0]
        assert cascaded == "3"
        cur.close()

        assert any(c["event"] == "deleted" for c in captured)

    def test_hard_delete_blocked_by_live_deliveries(
        self, client, auth_headers, monkeypatch
    ):
        captured = self._publishes(monkeypatch)
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO integration_events "
            "(event_type, event_version, aggregate_type, aggregate_id, "
            " aggregate_external_id, warehouse_id, source_txn_id, payload) "
            "VALUES ('test.live', 1, 'agg', %s, %s, 1, %s, '{}'::jsonb) "
            "RETURNING event_id",
            (
                abs(hash(uuid.uuid4())) % (10**9),
                str(uuid.uuid4()),
                str(uuid.uuid4()),
            ),
        )
        event_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO webhook_deliveries "
            "(subscription_id, event_id, attempt_number, status, "
            " scheduled_at, secret_generation) "
            "VALUES (%s, %s, 1, 'pending', NOW(), 1)",
            (sub_id, event_id),
        )
        cur.close()

        resp = client.delete(
            f"/api/admin/webhooks/{sub_id}?purge=true", headers=auth_headers
        )
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["error"] == "live_deliveries_block_hard_delete"
        assert body["live_count"] >= 1

        # Row still exists; nothing was published.
        assert self._subscription_status(sub_id)[0] == "active"
        assert captured == []

    def test_hard_delete_tombstone_triggers_url_reuse_gate_on_recreate(
        self, client, auth_headers, monkeypatch
    ):
        self._publishes(monkeypatch)
        url = f"https://example.com/reuse-after-purge-{uuid.uuid4()}"
        created = _create_one(client, auth_headers, delivery_url=url)
        sub_id = created["subscription_id"]
        client.delete(
            f"/api/admin/webhooks/{sub_id}?purge=true", headers=auth_headers
        )

        # Re-create with the same URL: the URL-reuse gate fires.
        resp = client.post(
            "/api/admin/webhooks",
            json={
                "connector_id": "test-conn-webhook",
                "display_name": "reuse",
                "delivery_url": url,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "url_reuse_tombstone"

    def test_hard_delete_writes_audit_row(
        self, client, auth_headers, monkeypatch
    ):
        self._publishes(monkeypatch)
        created = _create_one(client, auth_headers)
        sub_id = created["subscription_id"]
        client.delete(
            f"/api/admin/webhooks/{sub_id}?purge=true", headers=auth_headers
        )

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT details FROM audit_log "
            "WHERE action_type = 'WEBHOOK_SUBSCRIPTION_DELETE_HARD' "
            "AND details->>'subscription_id' = %s",
            (sub_id,),
        )
        row = cur.fetchone()
        cur.close()
        assert row is not None
        details = row[0]
        assert details["status_before"] == "active"
        assert "tombstone_id" in details

    def test_unknown_uuid_returns_404(self, client, auth_headers, monkeypatch):
        self._publishes(monkeypatch)
        resp = client.delete(
            f"/api/admin/webhooks/{uuid.uuid4()}", headers=auth_headers
        )
        assert resp.status_code == 404

    def test_invalid_uuid_returns_400(self, client, auth_headers, monkeypatch):
        self._publishes(monkeypatch)
        resp = client.delete(
            "/api/admin/webhooks/not-a-uuid", headers=auth_headers
        )
        assert resp.status_code == 400

    def test_unauthenticated_returns_401(self, client):
        resp = client.delete(f"/api/admin/webhooks/{uuid.uuid4()}")
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


class TestAdminIntegrityErrorHandler:
    """#211 defense-in-depth: any IntegrityError that escapes a
    handler's local try/except surfaces as a structured 409 from
    the blueprint-level errorhandler, not as a generic 500. The
    purge cascade fix above closes the documented case; this test
    exercises the global handler directly so a future endpoint
    that forgets local handling cannot regress to 500."""

    def test_handler_returns_409_with_constraint_name(self):
        from flask import Flask
        from sqlalchemy.exc import IntegrityError

        from routes.admin import admin_bp, _admin_integrity_error

        app = Flask(__name__)
        app.register_blueprint(admin_bp, url_prefix="/api/admin")

        # Synthesize a psycopg2-shaped IntegrityError carrying the
        # constraint_name diag attribute the handler reads. Using
        # SimpleNamespace keeps the test independent of psycopg2
        # internals; the handler only inspects exc.orig.diag.
        from types import SimpleNamespace

        diag = SimpleNamespace(
            constraint_name="webhook_deliveries_subscription_id_fkey",
            message_detail="Key is still referenced.",
        )
        orig = SimpleNamespace(diag=diag)
        exc = IntegrityError("statement", {}, orig)

        with app.test_request_context("/api/admin/probe"):
            from flask import g as flask_g

            class _NoopDb:
                def rollback(self):
                    return None

            flask_g.db = _NoopDb()
            resp, status = _admin_integrity_error(exc)

        assert status == 409
        body = resp.get_json()
        assert body["error"] == "integrity_constraint_violation"
        assert body["constraint"] == "webhook_deliveries_subscription_id_fkey"

    def test_handler_omits_constraint_when_diag_unavailable(self):
        from flask import Flask
        from sqlalchemy.exc import IntegrityError

        from routes.admin import admin_bp, _admin_integrity_error

        app = Flask(__name__)
        app.register_blueprint(admin_bp, url_prefix="/api/admin")

        # No diag attribute at all (older psycopg2 wrappers, custom
        # mocks). Handler must still return 409 with no constraint
        # field, not raise.
        exc = IntegrityError("statement", {}, Exception("opaque"))

        with app.test_request_context("/api/admin/probe"):
            from flask import g as flask_g

            class _NoopDb:
                def rollback(self):
                    return None

            flask_g.db = _NoopDb()
            resp, status = _admin_integrity_error(exc)

        assert status == 409
        body = resp.get_json()
        assert body["error"] == "integrity_constraint_violation"
        assert "constraint" not in body


class TestAdminWebhooksRateLimit:
    """#214: per-admin rate limit on webhook CRUD endpoints. 60/min
    per user_id; the 61st mutating request inside a fresh window
    returns 429. Read endpoints are not limited."""

    def test_create_burst_returns_429_after_budget(self, client, auth_headers):
        # 60/minute budget per user_id. 61 sequential creates must
        # hit the limit on the last call. Clear limiter first so a
        # leftover bucket from a prior test does not skew the count.
        from services.rate_limit import limiter
        limiter._storage.reset()  # noqa: SLF001

        statuses = []
        for i in range(61):
            resp = client.post(
                "/api/admin/webhooks",
                json={
                    "connector_id": "test-conn-webhook",
                    "display_name": f"rate-limit-{i}",
                    "delivery_url": f"https://example.com/{uuid.uuid4()}",
                },
                headers=auth_headers,
            )
            statuses.append(resp.status_code)

        # The first 60 are accepted; the 61st is 429.
        assert statuses[-1] == 429, (
            f"61st request must be rate-limited; got status {statuses[-1]} "
            f"with prior counts: 201={statuses.count(201)}, "
            f"429={statuses.count(429)}"
        )
        # Sanity: at least the first call succeeded (proves the route
        # itself is not generally broken; only the limit is firing).
        assert statuses[0] == 201, (
            f"first request must succeed; got {statuses[0]}"
        )

    def test_get_endpoints_not_rate_limited(self, client, auth_headers):
        """Read endpoints are not abuse vectors; 100 sequential GETs
        must not produce a 429."""
        from services.rate_limit import limiter
        limiter._storage.reset()  # noqa: SLF001

        statuses = set()
        for _ in range(100):
            resp = client.get("/api/admin/webhooks", headers=auth_headers)
            statuses.add(resp.status_code)
        assert 429 not in statuses, (
            f"GET /api/admin/webhooks must not be rate-limited; got {statuses}"
        )
