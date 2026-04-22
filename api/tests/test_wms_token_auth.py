"""Tests for v1.5.0 #128: token cache, @require_wms_token, rate-limit key.

Covers:
- @require_wms_token rejects missing / wrong / revoked / expired tokens
  and sets g.current_token + g.current_user on success.
- Token cache hits avoid the DB round-trip within the 60s TTL and
  refresh after.
- Revoked tokens stay active for up to TTL_SECONDS (documented contract).
- Rate-limit key prefers g.current_token.token_id over
  g.current_user.user_id over remote IP.
- App create_app raises without SENTRY_TOKEN_PEPPER.

The decorator has no route in v1.5.0 yet (the polling endpoint lands
in #122). Tests mount a throw-away Flask route under /api/v1/events/
probe at app-creation time so the decorator can be exercised end to
end through the Flask test client.
"""

import hashlib
import importlib
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest
from flask import Flask, g, jsonify

from middleware.auth_middleware import _hash_token, require_wms_token
from services import token_cache
from services.rate_limit import _rate_limit_key


PEPPER = os.environ["SENTRY_TOKEN_PEPPER"]


def _sha256_with_pepper(plaintext: str) -> str:
    return hashlib.sha256((PEPPER + plaintext).encode("utf-8")).hexdigest()


def _insert_token(
    name="test-token",
    plaintext="live-plaintext",
    status="active",
    expires_at=None,
    warehouse_ids=None,
    event_types=None,
    endpoints=None,
):
    """Insert a wms_tokens row using psycopg2 autocommit so it is visible
    to the test's own connection and the decorator's fresh session.

    expires_at=None means "use the migration-023 default" (~1 year out).
    Pass an explicit datetime to override (e.g. a past-dated value for
    the expired-token test).
    """
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    try:
        cur = conn.cursor()
        if expires_at is None:
            cur.execute(
                """
                INSERT INTO wms_tokens (
                    token_name, token_hash, status,
                    warehouse_ids, event_types, endpoints
                ) VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING token_id
                """,
                (
                    name,
                    _sha256_with_pepper(plaintext),
                    status,
                    warehouse_ids or [1],
                    event_types or [],
                    endpoints or [],
                ),
            )
        else:
            cur.execute(
                """
                INSERT INTO wms_tokens (
                    token_name, token_hash, status,
                    warehouse_ids, event_types, endpoints,
                    expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING token_id
                """,
                (
                    name,
                    _sha256_with_pepper(plaintext),
                    status,
                    warehouse_ids or [1],
                    event_types or [],
                    endpoints or [],
                    expires_at,
                ),
            )
        token_id = cur.fetchone()[0]
    finally:
        conn.close()
    return token_id


def _delete_token(token_id):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM wms_tokens WHERE token_id = %s", (token_id,))
    finally:
        conn.close()


@pytest.fixture()
def probe_app():
    """Stand up a minimal Flask app with one X-WMS-Token-protected route.

    Avoids depending on routes that do not exist yet (#122) while
    exercising the full decorator + cache + rate-limit path.
    """
    app = Flask("test-wms-auth")

    @app.route("/probe")
    @require_wms_token
    def probe():
        return jsonify(
            {
                "token_id": g.current_token["token_id"],
                "kind": g.current_user["kind"],
                "warehouse_ids": g.current_token["warehouse_ids"],
            }
        )

    return app.test_client()


@pytest.fixture(autouse=True)
def _fresh_cache():
    token_cache.clear()
    yield
    token_cache.clear()


class TestRequireWmsTokenRejections:
    def test_missing_header_returns_missing_token(self, probe_app):
        resp = probe_app.get("/probe")
        assert resp.status_code == 401
        assert resp.get_json() == {"error": "missing_token"}

    def test_wrong_hash_returns_invalid_token(self, probe_app):
        resp = probe_app.get(
            "/probe", headers={"X-WMS-Token": "not-a-real-token"}
        )
        assert resp.status_code == 401
        assert resp.get_json() == {"error": "invalid_token"}

    def test_revoked_token_returns_invalid_token(self, probe_app):
        token_id = _insert_token(plaintext="rev-target", status="revoked")
        try:
            resp = probe_app.get(
                "/probe", headers={"X-WMS-Token": "rev-target"}
            )
            assert resp.status_code == 401
            assert resp.get_json() == {"error": "invalid_token"}
        finally:
            _delete_token(token_id)

    def test_expired_token_returns_token_expired(self, probe_app):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        token_id = _insert_token(plaintext="expired-target", expires_at=past)
        try:
            resp = probe_app.get(
                "/probe", headers={"X-WMS-Token": "expired-target"}
            )
            assert resp.status_code == 401
            assert resp.get_json() == {"error": "token_expired"}
        finally:
            _delete_token(token_id)


class TestRequireWmsTokenAcceptance:
    def test_active_token_passes_and_populates_g(self, probe_app):
        token_id = _insert_token(
            plaintext="happy-path",
            warehouse_ids=[1, 2],
            event_types=["receipt.completed"],
        )
        try:
            resp = probe_app.get(
                "/probe", headers={"X-WMS-Token": "happy-path"}
            )
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["token_id"] == token_id
            assert body["kind"] == "wms_token"
            assert body["warehouse_ids"] == [1, 2]
        finally:
            _delete_token(token_id)

    def test_hash_uses_sha256_of_pepper_plus_plaintext(self):
        """Decision Q: the stored hash must be SHA256(pepper || plaintext)."""
        raw = "some-plaintext-value"
        assert _hash_token(raw) == _sha256_with_pepper(raw)


class TestTokenCacheBehaviour:
    def test_cache_hit_avoids_db_after_first_call(self, probe_app):
        token_id = _insert_token(plaintext="cache-hit-target")
        try:
            # Warm the cache.
            first = probe_app.get(
                "/probe", headers={"X-WMS-Token": "cache-hit-target"}
            )
            assert first.status_code == 200

            # Patch the DB-reaching function; a second call must NOT
            # hit it because the cache entry is still fresh.
            with patch(
                "services.token_cache._fetch_by_hash",
                side_effect=AssertionError("cache should have satisfied this call"),
            ):
                second = probe_app.get(
                    "/probe", headers={"X-WMS-Token": "cache-hit-target"}
                )
            assert second.status_code == 200
        finally:
            _delete_token(token_id)

    def test_cache_refreshes_after_ttl_expires(self, probe_app):
        token_id = _insert_token(plaintext="ttl-target")
        try:
            # TTL shrunk so "stale" is instant; next call must re-fetch.
            token_cache._testing_override_ttl(0)
            calls = {"count": 0}
            real_fetch = token_cache._fetch_by_hash

            def counting_fetch(h):
                calls["count"] += 1
                return real_fetch(h)

            with patch("services.token_cache._fetch_by_hash", side_effect=counting_fetch):
                probe_app.get("/probe", headers={"X-WMS-Token": "ttl-target"})
                probe_app.get("/probe", headers={"X-WMS-Token": "ttl-target"})
                probe_app.get("/probe", headers={"X-WMS-Token": "ttl-target"})
            assert calls["count"] == 3, (
                "with TTL=0 every request should re-fetch from the DB"
            )
        finally:
            token_cache._testing_override_ttl(60)
            _delete_token(token_id)

    def test_revocation_visible_within_ttl_window(self, probe_app):
        """Documented contract: a token revoked in the admin panel
        stops working within TTL_SECONDS (60s in prod). After the TTL
        elapses the cache refresh picks up the new status."""
        token_id = _insert_token(plaintext="revoke-after-warm")
        try:
            # Warm the cache as active.
            first = probe_app.get(
                "/probe", headers={"X-WMS-Token": "revoke-after-warm"}
            )
            assert first.status_code == 200

            # Revoke in the DB.
            conn = psycopg2.connect(os.environ["DATABASE_URL"])
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                "UPDATE wms_tokens SET status = 'revoked' WHERE token_id = %s",
                (token_id,),
            )
            conn.close()

            # Still within the TTL window: cache still says active.
            # The test simulates "TTL not yet elapsed" by leaving the
            # cache entry in place; the ability to keep serving for up
            # to 60s after revocation is the stated contract, not a bug.
            assert token_cache.get_by_hash(_sha256_with_pepper("revoke-after-warm"))["status"] == "active"

            # Expire the cache entry and repoll. Now the fresh fetch
            # sees status=revoked and the decorator rejects.
            token_cache.clear()
            resp = probe_app.get(
                "/probe", headers={"X-WMS-Token": "revoke-after-warm"}
            )
            assert resp.status_code == 401
            assert resp.get_json() == {"error": "invalid_token"}
        finally:
            _delete_token(token_id)


class TestRateLimitKey:
    def test_prefers_current_token_token_id(self, probe_app):
        with probe_app.application.test_request_context("/probe"):
            g.current_token = {"token_id": 42}
            g.current_user = {"user_id": 99, "kind": "wms_token"}
            assert _rate_limit_key() == "token:42"

    def test_falls_back_to_user_id_when_no_token(self, probe_app):
        with probe_app.application.test_request_context("/probe"):
            g.current_user = {"user_id": 7}
            assert _rate_limit_key() == "user:7"

    def test_falls_back_to_ip_when_nothing_set(self, probe_app):
        with probe_app.application.test_request_context(
            "/probe", environ_base={"REMOTE_ADDR": "10.0.0.5"}
        ):
            assert _rate_limit_key() == "ip:10.0.0.5"


class TestBootGuard:
    def test_create_app_raises_without_pepper(self):
        """app.create_app must reject deployments missing SENTRY_TOKEN_PEPPER."""
        # Import lazily so the module-level conftest env var stays set
        # for other test files.
        from app import create_app

        original = os.environ.pop("SENTRY_TOKEN_PEPPER", None)
        try:
            with pytest.raises(RuntimeError, match="SENTRY_TOKEN_PEPPER"):
                create_app()
        finally:
            if original is not None:
                os.environ["SENTRY_TOKEN_PEPPER"] = original
