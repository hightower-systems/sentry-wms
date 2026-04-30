"""Audit-log coverage guardrail for admin webhook mutations.

The manifest below enumerates every mutating endpoint under
``/api/admin/webhooks`` with the expected audit_log action_type.
Two assertions enforce coverage:

  1. Every Flask url_map rule under ``/api/admin/webhooks*`` whose
     methods include POST, PATCH, or DELETE has a corresponding
     manifest entry. A new endpoint added without a manifest entry
     fails the test; the contributor either adds it or explicitly
     registers it as exempt (action=None) with a justification
     comment alongside.

  2. Each manifest entry's happy-path call produces an audit_log
     row with the expected action_type. The test exercises each
     endpoint end-to-end through the Flask test client.

The manifest is the central inventory: a future contributor adding
a mutation has to either add the audit_log write or explain the
exemption explicitly here.
"""

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
    monkeypatch.setenv("SENTRY_ALLOW_INTERNAL_WEBHOOKS", "true")
    yield


@pytest.fixture(autouse=True)
def _ensure_test_connector():
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO connectors (connector_id, display_name) "
        "VALUES (%s, %s) ON CONFLICT (connector_id) DO NOTHING",
        ("audit-cov-conn", "audit coverage test connector"),
    )
    cur.close()


# Manifest of (rule_path, method) -> expected action_type. None
# means "this endpoint is intentionally not audited" and requires
# a comment justifying why on the same line.
AUDIT_MANIFEST: dict = {
    ("/api/admin/webhooks", "POST"): "WEBHOOK_SUBSCRIPTION_CREATE",
    ("/api/admin/webhooks/<subscription_id>", "PATCH"): "WEBHOOK_SUBSCRIPTION_UPDATE",
    ("/api/admin/webhooks/<subscription_id>", "DELETE"): "WEBHOOK_SUBSCRIPTION_DELETE_SOFT",
    ("/api/admin/webhooks/<subscription_id>/rotate-secret", "POST"): "WEBHOOK_SECRET_ROTATE",
    (
        "/api/admin/webhooks/<subscription_id>/replay/<int:delivery_id>",
        "POST",
    ): "WEBHOOK_DELIVERY_REPLAY_SINGLE",
    (
        "/api/admin/webhooks/<subscription_id>/replay-batch",
        "POST",
    ): "WEBHOOK_DELIVERY_REPLAY_BATCH",
}


# DELETE with ?purge=true reuses the same Flask rule as the soft
# delete path; the action_type differs by query param. Tracked as a
# secondary entry that the manifest enumeration check ignores but
# the exercise loop hits.
AUDIT_MANIFEST_QUERY_VARIANTS = {
    (
        "/api/admin/webhooks/<subscription_id>",
        "DELETE",
        "purge=true",
    ): "WEBHOOK_SUBSCRIPTION_DELETE_HARD",
}


def _create_subscription(client, auth_headers) -> str:
    resp = client.post(
        "/api/admin/webhooks",
        json={
            "connector_id": "audit-cov-conn",
            "display_name": "audit-coverage",
            "delivery_url": f"https://example.com/audit-{uuid.uuid4()}",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["subscription_id"]


def _seed_replayable_delivery(sub_id: str) -> int:
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO integration_events "
        "(event_type, event_version, aggregate_type, aggregate_id, "
        " aggregate_external_id, warehouse_id, source_txn_id, payload) "
        "VALUES ('test.audit', 1, 'agg', %s, %s, 1, %s, '{}'::jsonb) "
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
        "VALUES (%s, %s, 8, 'dlq', NOW(), NOW(), NOW(), 1) "
        "RETURNING delivery_id",
        (sub_id, event_id),
    )
    delivery_id = cur.fetchone()[0]
    cur.close()
    return delivery_id


def _audit_action_exists_for(action_type: str, sub_id: str) -> bool:
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM audit_log "
        " WHERE action_type = %s "
        "   AND details->>'subscription_id' = %s "
        " LIMIT 1",
        (action_type, sub_id),
    )
    row = cur.fetchone()
    cur.close()
    return row is not None


def test_manifest_covers_every_mutating_webhook_route(client):
    """Flask url_map enumeration: every rule under /api/admin/webhooks
    with POST / PATCH / DELETE methods must appear in
    AUDIT_MANIFEST. A new endpoint without a manifest entry fails
    here so the contributor cannot ship it without an audit
    decision."""
    found: set[tuple[str, str]] = set()
    for rule in client.application.url_map.iter_rules():
        if not rule.rule.startswith("/api/admin/webhooks"):
            continue
        for method in rule.methods or ():
            if method in ("POST", "PATCH", "DELETE"):
                found.add((rule.rule, method))

    missing = found - set(AUDIT_MANIFEST.keys())
    assert not missing, (
        f"the following webhook admin mutations are not registered in "
        f"AUDIT_MANIFEST: {sorted(missing)}. Add an entry mapping "
        f"each to its WEBHOOK_* action_type, or to None with a "
        f"justification comment if it is intentionally unaudited."
    )

    stale = set(AUDIT_MANIFEST.keys()) - found
    assert not stale, (
        f"AUDIT_MANIFEST has entries for routes that no longer "
        f"exist: {sorted(stale)}. Drop the stale entries."
    )


def test_create_endpoint_writes_audit_row(client, auth_headers):
    sub_id = _create_subscription(client, auth_headers)
    assert _audit_action_exists_for("WEBHOOK_SUBSCRIPTION_CREATE", sub_id)


def test_patch_endpoint_writes_audit_row(client, auth_headers):
    sub_id = _create_subscription(client, auth_headers)
    resp = client.patch(
        f"/api/admin/webhooks/{sub_id}",
        json={"display_name": "audited-patch"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert _audit_action_exists_for("WEBHOOK_SUBSCRIPTION_UPDATE", sub_id)


def test_soft_delete_endpoint_writes_audit_row(client, auth_headers):
    sub_id = _create_subscription(client, auth_headers)
    resp = client.delete(
        f"/api/admin/webhooks/{sub_id}", headers=auth_headers
    )
    assert resp.status_code == 200
    assert _audit_action_exists_for(
        "WEBHOOK_SUBSCRIPTION_DELETE_SOFT", sub_id
    )


def test_hard_delete_endpoint_writes_audit_row(client, auth_headers):
    sub_id = _create_subscription(client, auth_headers)
    resp = client.delete(
        f"/api/admin/webhooks/{sub_id}?purge=true",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert _audit_action_exists_for(
        "WEBHOOK_SUBSCRIPTION_DELETE_HARD", sub_id
    )


def test_rotate_secret_endpoint_writes_audit_row(client, auth_headers):
    sub_id = _create_subscription(client, auth_headers)
    resp = client.post(
        f"/api/admin/webhooks/{sub_id}/rotate-secret",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert _audit_action_exists_for("WEBHOOK_SECRET_ROTATE", sub_id)


def test_replay_single_endpoint_writes_audit_row(client, auth_headers):
    sub_id = _create_subscription(client, auth_headers)
    delivery_id = _seed_replayable_delivery(sub_id)
    resp = client.post(
        f"/api/admin/webhooks/{sub_id}/replay/{delivery_id}",
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert _audit_action_exists_for(
        "WEBHOOK_DELIVERY_REPLAY_SINGLE", sub_id
    )


def test_replay_batch_endpoint_writes_audit_row(client, auth_headers):
    sub_id = _create_subscription(client, auth_headers)
    resp = client.post(
        f"/api/admin/webhooks/{sub_id}/replay-batch",
        json={"filter": {}},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert _audit_action_exists_for(
        "WEBHOOK_DELIVERY_REPLAY_BATCH", sub_id
    )


def test_empty_patch_body_does_not_write_audit_row(client, auth_headers):
    """The PATCH endpoint deliberately skips the audit write when
    the request mutates nothing. This test pins that behavior so
    a future change cannot silently start writing audit rows on
    no-op PATCHes (which would pollute the trail when the admin
    UI's Save button is clicked without changes)."""
    sub_id = _create_subscription(client, auth_headers)
    resp = client.patch(
        f"/api/admin/webhooks/{sub_id}", json={}, headers=auth_headers
    )
    assert resp.status_code == 200
    assert not _audit_action_exists_for(
        "WEBHOOK_SUBSCRIPTION_UPDATE", sub_id
    )
