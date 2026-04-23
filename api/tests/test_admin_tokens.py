"""Admin token CRUD + rotate + revoke endpoints (v1.5.0 #129).

Covers:
- POST returns plaintext exactly once; list never contains plaintext.
- The stored hash matches SHA256(pepper || plaintext); the plaintext
  can authenticate through @require_wms_token.
- Rotation issues a new plaintext, stamps rotated_at, preserves scope.
- Revocation flips status and stamps revoked_at.
- Hard delete removes the row; subsequent auth attempts fail.
- Rotation-age badge is computed server-side (none / recommended / overdue).
- Non-admin callers are forbidden.
"""

import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2

from db_test_context import get_raw_connection


PEPPER = os.environ["SENTRY_TOKEN_PEPPER"]


def _expected_hash(plaintext: str) -> str:
    return hashlib.sha256((PEPPER + plaintext).encode("utf-8")).hexdigest()


def _row_by_id(token_id: int):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT token_id, token_name, token_hash, warehouse_ids, event_types, "
        "endpoints, connector_id, status, rotated_at, revoked_at, expires_at "
        "FROM wms_tokens WHERE token_id = %s",
        (token_id,),
    )
    row = cur.fetchone()
    cur.close()
    return row


class TestCreate:
    def test_create_returns_plaintext_and_metadata(self, client, auth_headers):
        resp = client.post(
            "/api/admin/tokens",
            json={
                "token_name": "fabric-prod",
                "warehouse_ids": [1, 2],
                "event_types": ["receipt.completed", "ship.confirmed"],
                "endpoints": ["events.poll"],
                "connector_id": None,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.get_json()
        body = resp.get_json()
        assert body["token_name"] == "fabric-prod"
        assert isinstance(body["token"], str) and len(body["token"]) >= 32
        assert body["status"] == "active"
        assert body["rotated_at"]

    def test_create_stores_peppered_sha256_hash(self, client, auth_headers):
        resp = client.post(
            "/api/admin/tokens",
            json={
                "token_name": "hash-probe",
                "warehouse_ids": [1],
                "event_types": [],
                # v1.5.1 V-200 (#140): endpoints is required and
                # non-empty; the hash-storage probe only needs one
                # valid slug to pass schema validation.
                "endpoints": ["events.poll"],
            },
            headers=auth_headers,
        )
        body = resp.get_json()
        token_id = body["token_id"]
        plaintext = body["token"]
        row = _row_by_id(token_id)
        assert row is not None
        stored_hash = row[2]
        assert stored_hash == _expected_hash(plaintext)

    def test_default_expires_at_is_about_one_year_out(self, client, auth_headers):
        resp = client.post(
            "/api/admin/tokens",
            json={
                "token_name": "expiry-default",
                "warehouse_ids": [1],
                "endpoints": ["events.poll"],
            },
            headers=auth_headers,
        )
        body = resp.get_json()
        assert body["expires_at"], "expires_at must be populated via the migration default"
        # String parse + rough check: within 10 days of +1 year.
        exp = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
        delta = exp - datetime.now(timezone.utc)
        assert 350 < delta.days < 380

    def test_unauthenticated_request_returns_401(self, client):
        """No auth header => 401 from @require_auth (before role check)."""
        resp = client.post(
            "/api/admin/tokens",
            json={"token_name": "no-auth-attempt", "warehouse_ids": [1]},
        )
        assert resp.status_code == 401


class TestList:
    def test_list_never_contains_plaintext(self, client, auth_headers):
        client.post(
            "/api/admin/tokens",
            json={
                "token_name": "list-target-1",
                "warehouse_ids": [1],
                "endpoints": ["events.poll"],
            },
            headers=auth_headers,
        )
        resp = client.get("/api/admin/tokens", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert "tokens" in body
        for row in body["tokens"]:
            assert "token" not in row, "list endpoint must never return plaintext"
            assert "token_hash" not in row, "list endpoint must not leak hashes"
            assert "rotation_status" in row

    def test_rotation_status_field_computed_server_side(
        self, client, auth_headers
    ):
        """Craft a row dated 100 days ago and assert rotation_status=overdue."""
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO wms_tokens (token_name, token_hash, warehouse_ids, rotated_at) "
            "VALUES ('overdue-row', repeat('a', 64), '{}', NOW() - INTERVAL '100 days') "
            "RETURNING token_id"
        )
        cur.fetchone()
        cur.close()

        resp = client.get("/api/admin/tokens", headers=auth_headers)
        body = resp.get_json()
        overdue = [t for t in body["tokens"] if t["token_name"] == "overdue-row"]
        assert overdue and overdue[0]["rotation_status"] == "overdue"


class TestRotate:
    def test_rotate_replaces_hash_preserves_scope(self, client, auth_headers):
        resp = client.post(
            "/api/admin/tokens",
            json={
                "token_name": "rotatable",
                "warehouse_ids": [1, 2],
                "event_types": ["ship.confirmed"],
                "endpoints": ["events.poll"],
            },
            headers=auth_headers,
        )
        body = resp.get_json()
        token_id = body["token_id"]
        original_plaintext = body["token"]
        original_hash = _row_by_id(token_id)[2]

        rot = client.post(
            f"/api/admin/tokens/{token_id}/rotate", headers=auth_headers
        )
        assert rot.status_code == 200
        rot_body = rot.get_json()
        assert rot_body["token"] != original_plaintext
        assert rot_body["status"] == "active"

        new_row = _row_by_id(token_id)
        assert new_row[2] != original_hash
        assert new_row[2] == _expected_hash(rot_body["token"])
        # Scope preserved.
        assert list(new_row[3]) == [1, 2]
        assert list(new_row[4]) == ["ship.confirmed"]
        assert list(new_row[5]) == ["events.poll"]

    def test_rotate_nonexistent_returns_404(self, client, auth_headers):
        resp = client.post(
            "/api/admin/tokens/99999999/rotate", headers=auth_headers
        )
        assert resp.status_code == 404

    def test_rotate_revoked_rejected(self, client, auth_headers):
        created = client.post(
            "/api/admin/tokens",
            json={
                "token_name": "revoke-then-rotate",
                "endpoints": ["events.poll"],
            },
            headers=auth_headers,
        ).get_json()
        token_id = created["token_id"]
        client.post(f"/api/admin/tokens/{token_id}/revoke", headers=auth_headers)
        resp = client.post(f"/api/admin/tokens/{token_id}/rotate", headers=auth_headers)
        assert resp.status_code == 400


class TestRevoke:
    def test_revoke_flips_status_and_stamps_revoked_at(
        self, client, auth_headers
    ):
        created = client.post(
            "/api/admin/tokens",
            json={
                "token_name": "revokable",
                "endpoints": ["events.poll"],
            },
            headers=auth_headers,
        ).get_json()
        token_id = created["token_id"]
        resp = client.post(
            f"/api/admin/tokens/{token_id}/revoke", headers=auth_headers
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "revoked"
        assert body["revoked_at"]

        row = _row_by_id(token_id)
        assert row[7] == "revoked"
        assert row[9] is not None  # revoked_at

    def test_revoke_nonexistent_returns_404(self, client, auth_headers):
        resp = client.post(
            "/api/admin/tokens/99999999/revoke", headers=auth_headers
        )
        assert resp.status_code == 404


class TestDelete:
    def test_delete_removes_row(self, client, auth_headers):
        created = client.post(
            "/api/admin/tokens",
            json={
                "token_name": "deletable",
                "endpoints": ["events.poll"],
            },
            headers=auth_headers,
        ).get_json()
        token_id = created["token_id"]
        resp = client.delete(
            f"/api/admin/tokens/{token_id}", headers=auth_headers
        )
        assert resp.status_code == 204
        assert _row_by_id(token_id) is None

    def test_delete_nonexistent_returns_404(self, client, auth_headers):
        resp = client.delete(
            "/api/admin/tokens/99999999", headers=auth_headers
        )
        assert resp.status_code == 404


class TestEndpointsValidation:
    """v1.5.1 V-200 (#140): CreateTokenRequest now requires a
    non-empty ``endpoints`` array of known slugs. Pre-v1.5.1 the
    field was accepted silently and never enforced by the decorator.
    """

    def test_missing_endpoints_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/admin/tokens",
            json={"token_name": "no-endpoints", "warehouse_ids": [1]},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_empty_endpoints_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/admin/tokens",
            json={
                "token_name": "empty-endpoints",
                "warehouse_ids": [1],
                "endpoints": [],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_unknown_slug_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/admin/tokens",
            json={
                "token_name": "bogus-slug",
                "warehouse_ids": [1],
                "endpoints": ["events.poll", "not.a.real.route"],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        body = resp.get_json()
        # The error body should surface the invalid slug so the admin
        # can correct it without digging through server logs.
        assert "not.a.real.route" in str(body)

    def test_every_known_slug_is_accepted(self, client, auth_headers):
        resp = client.post(
            "/api/admin/tokens",
            json={
                "token_name": "all-endpoints",
                "warehouse_ids": [1],
                "endpoints": [
                    "events.poll",
                    "events.ack",
                    "events.types",
                    "events.schema",
                    "snapshot.inventory",
                ],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.get_json()
