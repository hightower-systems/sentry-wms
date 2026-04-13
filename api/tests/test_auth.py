import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "test-secret")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import jwt
from datetime import datetime, timezone, timedelta

from db_test_context import get_raw_connection


class TestLogin:
    def test_login_success(self, client):
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert "token" in data, "Response should contain a token"
        assert "user" in data, "Response should contain user info"
        assert data["user"]["username"] == "admin"
        assert data["user"]["role"] == "ADMIN"

    def test_login_wrong_password(self, client):
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert resp.status_code == 401
        assert "error" in resp.get_json()

    def test_login_nonexistent_user(self, client):
        resp = client.post("/api/auth/login", json={"username": "nobody", "password": "pass"})
        assert resp.status_code == 401

    def test_login_missing_username(self, client):
        resp = client.post("/api/auth/login", json={"password": "admin"})
        assert resp.status_code == 400

    def test_login_missing_password(self, client):
        resp = client.post("/api/auth/login", json={"username": "admin"})
        assert resp.status_code == 400

    def test_login_empty_body(self, client):
        resp = client.post("/api/auth/login", json={})
        assert resp.status_code == 400


class TestRefresh:
    def test_refresh_with_valid_token(self, client, auth_headers):
        resp = client.post("/api/auth/refresh", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert "token" in data, "Refresh should return a new token"

    def test_refresh_without_token(self, client):
        resp = client.post("/api/auth/refresh")
        assert resp.status_code == 401

    def test_refresh_with_expired_token(self, client):
        expired_payload = {
            "user_id": 1,
            "username": "admin",
            "role": "ADMIN",
            "warehouse_id": 1,
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        }
        expired_token = jwt.encode(expired_payload, "test-secret", algorithm="HS256")
        resp = client.post("/api/auth/refresh", headers={"Authorization": f"Bearer {expired_token}"})
        assert resp.status_code == 401


class TestProtectedEndpoints:
    def test_protected_endpoint_without_token(self, client):
        resp = client.get("/api/lookup/item/100000000001")
        assert resp.status_code == 401

    def test_protected_endpoint_with_invalid_token(self, client):
        resp = client.get(
            "/api/lookup/item/100000000001",
            headers={"Authorization": "Bearer invalid.token.here"},
        )
        assert resp.status_code == 401


class TestWarehouseAuthorization:
    """Verify non-admin users can only access their assigned warehouses."""

    def _create_user_and_login(self, client, username, warehouse_id, warehouse_ids):
        conn = get_raw_connection()
        cur = conn.cursor()
        wids = "{" + ",".join(str(w) for w in warehouse_ids) + "}"
        cur.execute(
            """INSERT INTO users (username, password_hash, full_name, role,
                   warehouse_id, warehouse_ids, allowed_functions)
               VALUES (%s, '$2b$12$zDGRKFLmc6v/A4mVhxOzb.7uoW1ulnXn0AisK5uJ5iWk33vC2EpSK',
                       'Test User', 'PICKER', %s, %s, '{pick,receive,count}')""",
            (username, warehouse_id, wids),
        )
        cur.close()
        resp = client.post("/api/auth/login", json={"username": username, "password": "admin"})
        token = resp.get_json()["token"]
        return {"Authorization": f"Bearer {token}"}

    def test_jwt_includes_warehouse_ids(self, client):
        """Login token should contain warehouse_ids array."""
        headers = self._create_user_and_login(client, "wh_jwt_test", 1, [1, 2])
        token = headers["Authorization"].split(" ")[1]
        payload = jwt.decode(token, os.environ["JWT_SECRET"], algorithms=["HS256"])
        assert "warehouse_ids" in payload
        assert payload["warehouse_ids"] == [1, 2]

    def test_user_can_access_assigned_warehouse(self, client):
        """Non-admin accessing their assigned warehouse should succeed."""
        headers = self._create_user_and_login(client, "wh_ok_user", 1, [1])
        resp = client.get("/api/admin/dashboard?warehouse_id=1", headers=headers)
        assert resp.status_code == 200

    def test_user_blocked_from_other_warehouse(self, client):
        """Non-admin accessing an unassigned warehouse should get 403."""
        headers = self._create_user_and_login(client, "wh_blocked_user", 1, [1])
        resp = client.get("/api/admin/dashboard?warehouse_id=999", headers=headers)
        assert resp.status_code == 403
        assert "Access denied" in resp.get_json()["error"]

    def test_user_blocked_from_other_warehouse_post(self, client):
        """Non-admin POST with wrong warehouse_id in body should get 403."""
        headers = self._create_user_and_login(client, "wh_blocked_post", 1, [1])
        resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 999, "bin_ids": [1]},
            headers=headers,
        )
        assert resp.status_code == 403

    def test_admin_bypasses_warehouse_check(self, client, auth_headers):
        """Admin user can access any warehouse."""
        resp = client.get("/api/admin/dashboard?warehouse_id=999", headers=auth_headers)
        # 200 (empty data) not 403 - admin is never blocked by warehouse check
        assert resp.status_code == 200

    def test_request_without_warehouse_id_passes(self, client):
        """Requests with no warehouse_id in body or query should pass through."""
        headers = self._create_user_and_login(client, "wh_no_wid", 1, [1])
        resp = client.get("/api/picking/active-batch", headers=headers)
        assert resp.status_code == 200
