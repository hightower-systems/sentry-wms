"""
v1.4.1: forced password change on first login (issue #69).

Covers:
- Login and /auth/me responses expose must_change_password.
- Auth middleware blocks every endpoint except the three allowlisted
  ones (/auth/me, /auth/change-password, /auth/logout) with 403
  password_change_required while the flag is set.
- Change-password endpoint clears the flag in the same transaction,
  writes forced_password_change_completed to audit_log on a forced
  change vs password_change on a voluntary one, and rejects variants
  of "admin" as the new password.
- Migration 019 leaves pre-existing users with the default FALSE and
  therefore does not change their login / access flow.
"""

import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db_test_context import get_raw_connection


# ── helpers ─────────────────────────────────────────────────────────────

def _set_must_change(username, value):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET must_change_password = %s WHERE username = %s",
        (value, username),
    )
    cur.close()


def _get_must_change(username):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT must_change_password FROM users WHERE username = %s",
        (username,),
    )
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


def _latest_audit_action(pattern="%password_change%"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT action_type, entity_type, entity_id, user_id "
        "FROM audit_log WHERE action_type LIKE %s "
        "ORDER BY log_id DESC LIMIT 1",
        (pattern,),
    )
    row = cur.fetchone()
    cur.close()
    return row


def _login(client, username="admin", password="admin"):
    resp = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    return resp, resp.get_json()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _forced_token(client):
    """Flag the admin user then log in as them. Returns a bearer token."""
    _set_must_change("admin", True)
    _, data = _login(client)
    return data["token"]


# ── tests ───────────────────────────────────────────────────────────────


class TestResponseShape:
    """Layer 5: login and /auth/me expose the flag for the clients."""

    def test_login_returns_flag_true(self, client):
        _set_must_change("admin", True)
        resp, data = _login(client)
        assert resp.status_code == 200
        assert data["user"]["must_change_password"] is True

    def test_login_returns_flag_false_for_existing_user(self, client):
        # Default after migration 019 is FALSE.
        resp, data = _login(client)
        assert resp.status_code == 200
        assert data["user"]["must_change_password"] is False

    def test_me_returns_flag_true(self, client):
        token = _forced_token(client)
        resp = client.get("/api/auth/me", headers=_bearer(token))
        assert resp.status_code == 200
        assert resp.get_json()["must_change_password"] is True

    def test_me_returns_flag_false_after_seed(self, client):
        _, data = _login(client)
        resp = client.get("/api/auth/me", headers=_bearer(data["token"]))
        assert resp.status_code == 200
        assert resp.get_json()["must_change_password"] is False


class TestMiddlewareBlocksNonAllowlistedRoutes:
    """Layer 3: anything outside the three-endpoint allowlist returns 403."""

    def test_blocks_admin_warehouses(self, client):
        token = _forced_token(client)
        resp = client.get("/api/admin/warehouses", headers=_bearer(token))
        assert resp.status_code == 403
        body = resp.get_json()
        assert body["error"] == "password_change_required"
        assert "message" in body

    def test_blocks_admin_users(self, client):
        token = _forced_token(client)
        resp = client.get("/api/admin/users", headers=_bearer(token))
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "password_change_required"

    def test_blocks_admin_items(self, client):
        token = _forced_token(client)
        resp = client.get("/api/admin/items", headers=_bearer(token))
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "password_change_required"

    def test_blocks_auth_refresh(self, client):
        # auth.refresh is intentionally NOT on the allowlist. A user cannot
        # extend their session without completing the forced change.
        token = _forced_token(client)
        resp = client.post("/api/auth/refresh", headers=_bearer(token))
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "password_change_required"


class TestMiddlewareAllowsThreeAllowlistedRoutes:
    """Layer 3: exactly three endpoints stay reachable."""

    def test_me_allowed(self, client):
        token = _forced_token(client)
        resp = client.get("/api/auth/me", headers=_bearer(token))
        assert resp.status_code == 200

    def test_change_password_allowed(self, client):
        token = _forced_token(client)
        resp = client.post(
            "/api/auth/change-password",
            headers=_bearer(token),
            json={"current_password": "admin", "new_password": "Testing12345"},
        )
        assert resp.status_code == 200

    def test_logout_allowed(self, client):
        # Logout is not decorated with @require_auth, so the forced-change
        # middleware cannot block it. Drop cookies the earlier login set
        # so the V-100 CSRF check does not shadow what we are testing
        # here; we care that a forced-change user is not refused by the
        # password_change_required gate when calling logout.
        token = _forced_token(client)
        try:
            client._cookies.clear()
        except AttributeError:
            pass
        resp = client.post("/api/auth/logout", headers=_bearer(token))
        assert resp.status_code == 200
        # Specifically: the response is NOT the forced-change 403.
        body = resp.get_json() or {}
        assert body.get("error") != "password_change_required"


class TestChangePasswordClearsFlag:
    """Layer 4: successful change flips the flag to FALSE in the same
    transaction and restores access to previously-blocked routes."""

    def test_flag_cleared_on_success(self, client):
        token = _forced_token(client)
        resp = client.post(
            "/api/auth/change-password",
            headers=_bearer(token),
            json={"current_password": "admin", "new_password": "Testing12345"},
        )
        assert resp.status_code == 200
        assert _get_must_change("admin") is False

    def test_full_access_restored_after_change(self, client):
        token = _forced_token(client)
        client.post(
            "/api/auth/change-password",
            headers=_bearer(token),
            json={"current_password": "admin", "new_password": "Testing12345"},
        )
        # Same token, same session. Middleware now reads the cleared flag.
        resp = client.get("/api/admin/warehouses", headers=_bearer(token))
        assert resp.status_code == 200

    def test_subsequent_login_reflects_new_password_and_flag(self, client):
        token = _forced_token(client)
        client.post(
            "/api/auth/change-password",
            headers=_bearer(token),
            json={"current_password": "admin", "new_password": "Testing12345"},
        )
        # New credentials work and the response shows the cleared flag.
        resp, data = _login(client, password="Testing12345")
        assert resp.status_code == 200
        assert data["user"]["must_change_password"] is False


class TestChangePasswordAuditActions:
    """Layer 4: distinct audit action names for forced vs voluntary."""

    def test_forced_change_writes_forced_action(self, client):
        token = _forced_token(client)
        client.post(
            "/api/auth/change-password",
            headers=_bearer(token),
            json={"current_password": "admin", "new_password": "Testing12345"},
        )
        row = _latest_audit_action()
        assert row is not None
        action_type, entity_type, entity_id, user_id = row
        assert action_type == "forced_password_change_completed"
        assert entity_type == "user"
        assert entity_id == 1
        assert user_id == "admin"

    def test_voluntary_change_writes_existing_action(self, client):
        # Flag is already FALSE (default). No forced flow; this is a
        # voluntary rotation and the audit action name reflects that.
        _, data = _login(client)
        token = data["token"]
        client.post(
            "/api/auth/change-password",
            headers=_bearer(token),
            json={"current_password": "admin", "new_password": "Testing12345"},
        )
        row = _latest_audit_action()
        assert row is not None
        assert row[0] == "password_change"


class TestRejectAdminAsNewPassword:
    """Layer 4: validate_password refuses exact-match "admin" variants."""

    def _attempt(self, client, new_password):
        token = _forced_token(client)
        return client.post(
            "/api/auth/change-password",
            headers=_bearer(token),
            json={
                "current_password": "admin",
                "new_password": new_password,
            },
        )

    def test_rejects_lowercase(self, client):
        resp = self._attempt(client, "admin")
        assert resp.status_code == 400
        assert "admin" in resp.get_json()["error"].lower()

    def test_rejects_uppercase(self, client):
        resp = self._attempt(client, "ADMIN")
        assert resp.status_code == 400

    def test_rejects_title_case(self, client):
        resp = self._attempt(client, "Admin")
        assert resp.status_code == 400

    def test_rejects_mixed_case(self, client):
        resp = self._attempt(client, "aDmIn")
        assert resp.status_code == 400

    def test_rejects_whitespace_padded(self, client):
        resp = self._attempt(client, " admin ")
        assert resp.status_code == 400

    def test_rejects_tab_and_newline_padded(self, client):
        resp = self._attempt(client, "\tadmin\n")
        assert resp.status_code == 400

    def test_still_accepts_valid_password(self, client):
        # Sanity: the admin-rejection rule must not break legit passwords.
        resp = self._attempt(client, "Testing12345")
        assert resp.status_code == 200


class TestMigration019Defaults:
    """Migration 019 ADD COLUMN ... DEFAULT FALSE must not force-flag
    anyone who was already installed before v1.4.1."""

    def test_seeded_admin_defaults_false(self, client):
        assert _get_must_change("admin") is False

    def test_existing_user_login_flow_unaffected(self, client):
        # Flag=false user flows through login and into previously-blocked
        # routes exactly as they did before the forced-change feature.
        resp, data = _login(client)
        assert resp.status_code == 200
        assert data["user"]["must_change_password"] is False
        token = data["token"]
        resp = client.get("/api/admin/warehouses", headers=_bearer(token))
        assert resp.status_code == 200
