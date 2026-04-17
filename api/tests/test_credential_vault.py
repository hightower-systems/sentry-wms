"""Tests for the credential vault and connector admin endpoints.

Covers:
- Encrypt/decrypt round-trip
- API responses never contain plaintext credential values
- Credential scoping per warehouse
- Credential deletion
- Admin role enforcement (non-admin gets 403)
- Connection test endpoint with example connector
- Missing encryption key raises at startup
"""

import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from db_test_context import get_raw_connection
from services.credential_vault import _encrypt, _decrypt, _get_fernet


def _ensure_table():
    """Create the connector_credentials table if it doesn't exist."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS connector_credentials (
            id SERIAL PRIMARY KEY,
            connector_name VARCHAR(64) NOT NULL,
            warehouse_id INT NOT NULL,
            credential_key VARCHAR(128) NOT NULL,
            encrypted_value TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(connector_name, warehouse_id, credential_key)
        )
    """)
    cur.close()


def _register_example():
    """Register the example connector in the global registry."""
    from connectors import registry
    from connectors.example import ExampleConnector
    try:
        registry.get("example")
    except KeyError:
        registry.register("example", ExampleConnector)


# ---------------------------------------------------------------------------
# Encryption unit tests
# ---------------------------------------------------------------------------


class TestEncryption:
    def test_round_trip(self):
        """Encrypting then decrypting returns the original value."""
        original = "my-secret-api-key-12345"
        encrypted = _encrypt(original)
        assert encrypted != original
        assert _decrypt(encrypted) == original

    def test_different_ciphertexts(self):
        """Same plaintext produces different ciphertexts (Fernet uses random IV)."""
        a = _encrypt("same-value")
        b = _encrypt("same-value")
        assert a != b
        assert _decrypt(a) == _decrypt(b)

    def test_requires_key_env_var(self, monkeypatch):
        """_get_fernet raises RuntimeError when SENTRY_ENCRYPTION_KEY is unset.

        The vault deliberately refuses to auto-generate a key: doing so would
        rotate the key on every process restart (silently orphaning existing
        ciphertexts) and risk logging the key value. Operators must set it.
        """
        import services.credential_vault as vault
        monkeypatch.setattr(vault, "_fernet", None)
        monkeypatch.delenv("SENTRY_ENCRYPTION_KEY", raising=False)
        with pytest.raises(RuntimeError, match="SENTRY_ENCRYPTION_KEY"):
            vault._get_fernet()


# ---------------------------------------------------------------------------
# Vault integration tests via API
# ---------------------------------------------------------------------------


class TestCredentialEndpoints:
    @pytest.fixture(autouse=True)
    def _setup(self):
        _ensure_table()
        _register_example()

    def test_save_credentials(self, client, auth_headers):
        resp = client.post(
            "/api/admin/connectors/example/credentials",
            json={"warehouse_id": 1, "credentials": {"api_key": "secret123", "base_url": "https://api.test.com"}},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["message"] == "Credentials saved"
        assert "api_key" in data["keys"]
        assert "base_url" in data["keys"]

    def test_get_credentials_masked(self, client, auth_headers):
        """GET response must show masked values, never plaintext."""
        # Store first
        client.post(
            "/api/admin/connectors/example/credentials",
            json={"warehouse_id": 1, "credentials": {"api_key": "super-secret"}},
            headers=auth_headers,
        )
        # Retrieve
        resp = client.get(
            "/api/admin/connectors/example/credentials?warehouse_id=1",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        creds = data["credentials"]
        assert len(creds) > 0
        for cred in creds:
            assert cred["value"] == "****"
            assert "secret" not in str(cred)

    def test_credential_scoping(self, client, auth_headers):
        """Credentials for warehouse 1 must not appear in warehouse 2 query."""
        client.post(
            "/api/admin/connectors/example/credentials",
            json={"warehouse_id": 1, "credentials": {"api_key": "wh1-key"}},
            headers=auth_headers,
        )

        # Query for warehouse 2 -- should be empty
        resp = client.get(
            "/api/admin/connectors/example/credentials?warehouse_id=2",
            headers=auth_headers,
        )
        data = resp.get_json()
        assert data["credentials"] == []

    def test_delete_credentials(self, client, auth_headers):
        """DELETE removes all credentials for a connector+warehouse."""
        client.post(
            "/api/admin/connectors/example/credentials",
            json={"warehouse_id": 1, "credentials": {"api_key": "to-delete"}},
            headers=auth_headers,
        )

        resp = client.delete(
            "/api/admin/connectors/example/credentials",
            json={"warehouse_id": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["message"] == "Credentials deleted"

        # Verify gone
        resp = client.get(
            "/api/admin/connectors/example/credentials?warehouse_id=1",
            headers=auth_headers,
        )
        assert resp.get_json()["credentials"] == []

    def test_test_connection(self, client, auth_headers):
        """Test connection endpoint calls connector.test_connection()."""
        resp = client.post(
            "/api/admin/connectors/example/test",
            json={"warehouse_id": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["connected"] is True
        assert "message" in data

    def test_connector_not_found(self, client, auth_headers):
        """Endpoints return 404 for unknown connector names."""
        resp = client.post(
            "/api/admin/connectors/nonexistent/credentials",
            json={"warehouse_id": 1, "credentials": {"key": "val"}},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_list_connectors(self, client, auth_headers):
        """GET /admin/connectors lists registered connectors."""
        resp = client.get("/api/admin/connectors", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        names = [c["name"] for c in data["connectors"]]
        assert "example" in names

    def test_get_config_schema(self, client, auth_headers):
        resp = client.get("/api/admin/connectors/example/config-schema", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert "api_key" in data["config_schema"]
        assert "capabilities" in data

    def test_upsert_overwrites(self, client, auth_headers):
        """Saving credentials for the same key overwrites the previous value."""
        client.post(
            "/api/admin/connectors/example/credentials",
            json={"warehouse_id": 1, "credentials": {"api_key": "first"}},
            headers=auth_headers,
        )
        client.post(
            "/api/admin/connectors/example/credentials",
            json={"warehouse_id": 1, "credentials": {"api_key": "second"}},
            headers=auth_headers,
        )
        # Count should still be 1 key, not 2
        resp = client.get(
            "/api/admin/connectors/example/credentials?warehouse_id=1",
            headers=auth_headers,
        )
        api_keys = [c for c in resp.get_json()["credentials"] if c["key"] == "api_key"]
        assert len(api_keys) == 1


class TestCredentialAuth:
    """Verify non-admin users cannot access credential endpoints."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _ensure_table()
        _register_example()

    def _create_user_token(self, client):
        """Create a non-admin user and return their auth headers."""
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO users (username, password_hash, full_name, role, warehouse_id, warehouse_ids)
               VALUES ('vault_user', '$2b$12$zDGRKFLmc6v/A4mVhxOzb.7uoW1ulnXn0AisK5uJ5iWk33vC2EpSK',
                       'Vault User', 'USER', 1, '{1}')
               ON CONFLICT (username) DO NOTHING"""
        )
        cur.close()
        resp = client.post("/api/auth/login", json={"username": "vault_user", "password": "admin"})
        token = resp.get_json()["token"]
        return {"Authorization": f"Bearer {token}"}

    def test_non_admin_list_connectors_403(self, client):
        headers = self._create_user_token(client)
        resp = client.get("/api/admin/connectors", headers=headers)
        assert resp.status_code == 403

    def test_non_admin_save_credentials_403(self, client):
        headers = self._create_user_token(client)
        resp = client.post(
            "/api/admin/connectors/example/credentials",
            json={"warehouse_id": 1, "credentials": {"api_key": "nope"}},
            headers=headers,
        )
        assert resp.status_code == 403

    def test_non_admin_test_connection_403(self, client):
        headers = self._create_user_token(client)
        resp = client.post(
            "/api/admin/connectors/example/test",
            json={"warehouse_id": 1},
            headers=headers,
        )
        assert resp.status_code == 403

    def test_no_auth_401(self, client):
        resp = client.get("/api/admin/connectors")
        assert resp.status_code == 401
