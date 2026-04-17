import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "test-secret")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import jwt
from datetime import datetime, timezone, timedelta

from db_test_context import get_raw_connection


def _reset_lockout():
    """Clear login attempt tracking between tests."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM login_attempts")
    cur.close()


class TestLogin:
    def test_login_success(self, client):
        _reset_lockout()
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert "token" in data, "Response should contain a token"
        assert "user" in data, "Response should contain user info"
        assert data["user"]["username"] == "admin"
        assert data["user"]["role"] == "ADMIN"

    def test_login_wrong_password(self, client):
        _reset_lockout()
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


class TestLoginLockout:
    """Verify account lockout after failed login attempts."""

    def test_lockout_after_five_failures(self, client):
        _reset_lockout()
        for i in range(5):
            resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
            if i < 4:
                assert resp.status_code == 401
        # 5th attempt triggers lockout
        assert resp.status_code == 429
        assert "locked" in resp.get_json()["error"].lower()

    def test_locked_account_rejects_correct_password(self, client):
        _reset_lockout()
        for _ in range(5):
            client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        # Even correct password is rejected during lockout
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        assert resp.status_code == 429

    def test_successful_login_resets_attempts(self, client):
        _reset_lockout()
        # 4 failures (not locked yet)
        for _ in range(4):
            client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        # Successful login resets counter
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        assert resp.status_code == 200
        # 4 more failures should still not lock (counter was reset)
        for _ in range(4):
            resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
            assert resp.status_code == 401

    def test_attempts_remaining_not_shown(self, client):
        _reset_lockout()
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        error_msg = resp.get_json()["error"]
        assert "Invalid username or password" in error_msg
        assert "remaining" not in error_msg

    def test_ip_lockout_accumulates_across_usernames(self, client):
        """V-023: lockout is IP-scoped. 5 failures from one IP triggers
        the IP's lockout regardless of which username was targeted
        (attacker cannot spread the attempts across usernames to evade
        throttling)."""
        _reset_lockout()
        # Mix usernames from a single IP -- total across them hits the
        # IP-level threshold.
        for uname in ["admin", "nobody", "x", "y", "z"]:
            resp = client.post(
                "/api/auth/login", json={"username": uname, "password": "wrong"}
            )
        assert resp.status_code == 429
        # Correct admin password from the same IP is still blocked.
        resp = client.post(
            "/api/auth/login", json={"username": "admin", "password": "admin"}
        )
        assert resp.status_code == 429

    def test_ip_lockout_does_not_block_other_ips(self, client):
        """V-023: attacker locking themselves out from IP A must NOT
        prevent the real user from logging in at IP B. This is the
        core V-023 fix: pre-fix the lockout was per-username, so an
        attacker could DoS any account by knowing its name."""
        _reset_lockout()
        # Attacker from IP A exhausts the lockout.
        for _ in range(5):
            client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "wrong"},
                environ_base={"REMOTE_ADDR": "203.0.113.99"},
            )
        # Real admin at a different IP: correct password still works.
        resp = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin"},
            environ_base={"REMOTE_ADDR": "203.0.113.42"},
        )
        assert resp.status_code == 200
        # Meanwhile the attacker IP is still locked.
        resp = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin"},
            environ_base={"REMOTE_ADDR": "203.0.113.99"},
        )
        assert resp.status_code == 429


class TestWarehouseAuthorization:
    """Verify non-admin users can only access their assigned warehouses."""

    def _create_user_and_login(self, client, username, warehouse_id, warehouse_ids):
        _reset_lockout()
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
        """Non-admin accessing their assigned warehouse should pass the auth check."""
        headers = self._create_user_and_login(client, "wh_ok_user", 1, [1])
        # Use a non-admin POST endpoint that carries warehouse_id. Business logic may
        # return 404 (bin not found) but should never return 403 for a valid warehouse.
        resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 1, "bin_ids": [99999]},
            headers=headers,
        )
        assert resp.status_code != 403

    def test_user_blocked_from_other_warehouse(self, client):
        """Non-admin accessing an unassigned warehouse should get 403."""
        headers = self._create_user_and_login(client, "wh_blocked_user", 1, [1])
        resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 999, "bin_ids": [1]},
            headers=headers,
        )
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


class TestChangePassword:
    """L2: self-service password change."""

    def test_change_password_success(self, client, auth_headers):
        _reset_lockout()
        resp = client.post("/api/auth/change-password", json={
            "current_password": "admin",
            "new_password": "newadmin1",
        }, headers=auth_headers)
        assert resp.status_code == 200

        # Verify login with new password
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "newadmin1"})
        assert resp.status_code == 200

    def test_change_password_wrong_current(self, client, auth_headers):
        resp = client.post("/api/auth/change-password", json={
            "current_password": "wrongpassword1",
            "new_password": "newadmin1",
        }, headers=auth_headers)
        assert resp.status_code == 403
        assert "incorrect" in resp.get_json()["error"].lower()

    def test_change_password_weak_new(self, client, auth_headers):
        resp = client.post("/api/auth/change-password", json={
            "current_password": "admin",
            "new_password": "short",
        }, headers=auth_headers)
        assert resp.status_code == 400

    def test_change_password_requires_auth(self, client):
        resp = client.post("/api/auth/change-password", json={
            "current_password": "admin",
            "new_password": "newadmin1",
        })
        assert resp.status_code == 401


class TestJwtClaims:
    """L10: verify iat and jti are present in tokens."""

    def test_token_contains_iat_and_jti(self, client):
        _reset_lockout()
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        token = resp.get_json()["token"]
        payload = jwt.decode(token, os.environ["JWT_SECRET"], algorithms=["HS256"])
        assert "iat" in payload
        assert "jti" in payload
        assert isinstance(payload["iat"], int)
        assert len(payload["jti"]) == 36  # UUID format

    def test_each_token_has_unique_jti(self, client):
        _reset_lockout()
        resp1 = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        resp2 = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        p1 = jwt.decode(resp1.get_json()["token"], os.environ["JWT_SECRET"], algorithms=["HS256"])
        p2 = jwt.decode(resp2.get_json()["token"], os.environ["JWT_SECRET"], algorithms=["HS256"])
        assert p1["jti"] != p2["jti"]


class TestTokenInvalidation:
    """M1: old tokens rejected after password change."""

    def test_old_token_rejected_after_password_change(self, client, auth_headers):
        """A token with iat before password_changed_at should be rejected."""
        # Craft a token with iat 10 seconds in the past
        old_payload = {
            "user_id": 1,
            "username": "admin",
            "role": "ADMIN",
            "warehouse_id": 1,
            "warehouse_ids": [],
            "iat": int(datetime.now(timezone.utc).timestamp()) - 10,
            "jti": "old-token-id",
            "exp": datetime.now(timezone.utc) + timedelta(hours=8),
        }
        old_token = jwt.encode(old_payload, os.environ["JWT_SECRET"], algorithm="HS256")
        old_headers = {"Authorization": f"Bearer {old_token}"}

        # Verify token works before password change
        resp = client.get("/api/auth/me", headers=old_headers)
        assert resp.status_code == 200

        # Change password via admin endpoint
        client.put(
            "/api/admin/users/1",
            json={"password": "newpassword1"},
            headers=auth_headers,
        )

        # Old token should now be rejected
        resp = client.get("/api/auth/me", headers=old_headers)
        assert resp.status_code == 401
        assert "password change" in resp.get_json()["error"].lower()

    def test_new_token_works_after_password_change(self, client, auth_headers):
        _reset_lockout()
        # Change password
        client.put(
            "/api/admin/users/1",
            json={"password": "newpassword2"},
            headers=auth_headers,
        )

        # Login with new password gets a working token
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "newpassword2"})
        assert resp.status_code == 200
        new_headers = {"Authorization": f"Bearer {resp.get_json()['token']}"}

        resp = client.get("/api/auth/me", headers=new_headers)
        assert resp.status_code == 200
