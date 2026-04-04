import jwt
from datetime import datetime, timezone, timedelta


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
