"""token_cache TTL + revocation-window contract (v1.5.0 #130).

Split from the original test_wms_token_auth.py so the cache semantics
(60s TTL, per-entry refresh, revocation visible within TTL) have a
dedicated file. Decorator tests live in test_wms_token_decorator.py.
"""

import os
import sys
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest
from flask import Flask, g, jsonify

from _wms_token_helpers import (
    DATABASE_URL,
    delete_token,
    insert_token,
    sha256_with_pepper,
)
from middleware.auth_middleware import require_wms_token
from services import token_cache


@pytest.fixture()
def probe_app():
    app = Flask("test-wms-cache")

    # v1.5.1 V-200 (#140): the decorator now enforces endpoint scope
    # against the Flask endpoint name. Register the probe under a
    # real V150_ENDPOINT_SLUGS Flask-endpoint name so tokens seeded
    # with the helper's DEFAULT_TEST_ENDPOINTS pass the scope check
    # alongside the TTL / revocation assertions this file exercises.
    @app.route("/probe", endpoint="polling.poll_events")
    @require_wms_token
    def probe():
        return jsonify(
            {
                "token_id": g.current_token["token_id"],
                "kind": g.current_user["kind"],
            }
        )

    return app.test_client()


@pytest.fixture(autouse=True)
def _fresh_cache():
    token_cache.clear()
    yield
    token_cache.clear()


class TestTokenCacheBehaviour:
    def test_cache_hit_avoids_db_after_first_call(self, probe_app):
        token_id = insert_token(plaintext="cache-hit-target")
        try:
            first = probe_app.get(
                "/probe", headers={"X-WMS-Token": "cache-hit-target"}
            )
            assert first.status_code == 200

            # Patch the DB-reaching function; a second call must NOT
            # hit it because the cache entry is still fresh.
            with patch(
                "services.token_cache._fetch_by_hash",
                side_effect=AssertionError(
                    "cache should have satisfied this call"
                ),
            ):
                second = probe_app.get(
                    "/probe", headers={"X-WMS-Token": "cache-hit-target"}
                )
            assert second.status_code == 200
        finally:
            delete_token(token_id)

    def test_cache_refreshes_after_ttl_expires(self, probe_app):
        token_id = insert_token(plaintext="ttl-target")
        try:
            token_cache._testing_override_ttl(0)
            calls = {"count": 0}
            real_fetch = token_cache._fetch_by_hash

            def counting_fetch(h):
                calls["count"] += 1
                return real_fetch(h)

            with patch(
                "services.token_cache._fetch_by_hash",
                side_effect=counting_fetch,
            ):
                probe_app.get("/probe", headers={"X-WMS-Token": "ttl-target"})
                probe_app.get("/probe", headers={"X-WMS-Token": "ttl-target"})
                probe_app.get("/probe", headers={"X-WMS-Token": "ttl-target"})
            assert calls["count"] == 3, (
                "with TTL=0 every request should re-fetch from the DB"
            )
        finally:
            token_cache._testing_override_ttl(60)
            delete_token(token_id)

    def test_revocation_visible_within_ttl_window(self, probe_app):
        """Documented contract: a token revoked in the admin panel
        stops working within TTL_SECONDS (60s in prod) even without
        pubsub. v1.5.1 V-205 (#146) adds Redis pubsub for targeted
        sub-second invalidation; this test still exercises the TTL
        backstop path by mutating the DB directly (no publish), to
        prove the per-entry TTL continues to protect against a down
        pubsub channel.
        """
        token_id = insert_token(plaintext="revoke-after-warm")
        try:
            first = probe_app.get(
                "/probe", headers={"X-WMS-Token": "revoke-after-warm"}
            )
            assert first.status_code == 200

            # Revoke in the DB while cache still holds 'active'.
            conn = psycopg2.connect(DATABASE_URL)
            conn.autocommit = True
            conn.cursor().execute(
                "UPDATE wms_tokens SET status = 'revoked' WHERE token_id = %s",
                (token_id,),
            )
            conn.close()

            # Within TTL window: cache still says active (the stated
            # revocation-window contract).
            cached = token_cache.get_by_hash(
                sha256_with_pepper("revoke-after-warm")
            )
            assert cached["status"] == "active"

            # Expire the cache entry (TTL elapsed) and repoll; fresh
            # fetch sees status=revoked and the decorator rejects.
            token_cache.clear()
            resp = probe_app.get(
                "/probe", headers={"X-WMS-Token": "revoke-after-warm"}
            )
            assert resp.status_code == 401
            assert resp.get_json() == {"error": "invalid_token"}
        finally:
            delete_token(token_id)


class TestInvalidation:
    """v1.5.1 V-205 (#146): targeted cross-worker invalidation. The
    local-evict path is unit-tested here; the cross-worker pubsub
    path relies on Redis and is covered by a separate integration
    test that mocks the publisher to prove admin rotate / revoke /
    delete call invalidate() with the correct token_id.
    """

    def test_invalidate_evicts_local_entry(self):
        """Seed a cache entry, call invalidate(token_id), confirm
        the entry is gone from the local dict."""
        import hashlib
        from services import token_cache as tc

        tc.clear()
        # Seed a synthetic entry directly so the test doesn't need
        # to go through _fetch_by_hash.
        fake_hash = hashlib.sha256(b"fake").hexdigest()
        with tc._lock:
            tc._cache[fake_hash] = (
                {"token_id": 4242, "status": "active"},
                9_999_999.0,
            )
        assert fake_hash in tc._cache

        tc.invalidate(4242)

        assert fake_hash not in tc._cache, (
            "invalidate must evict the entry whose row.token_id matches"
        )

    def test_invalidate_is_no_op_for_unknown_token_id(self):
        """Pubsub messages for unknown token_ids (e.g. a token that
        was never cached on this worker) must not raise."""
        import hashlib
        from services import token_cache as tc

        tc.clear()
        fake_hash = hashlib.sha256(b"other").hexdigest()
        with tc._lock:
            tc._cache[fake_hash] = (
                {"token_id": 111, "status": "active"},
                9_999_999.0,
            )

        tc.invalidate(999_999)  # nothing with this token_id cached
        # Existing entry untouched.
        assert fake_hash in tc._cache

    def test_invalidate_publishes_when_publisher_configured(self, monkeypatch):
        """When a Redis publisher is wired, invalidate() publishes a
        JSON message with the token_id on the invalidation channel."""
        from services import token_cache as tc

        published = []

        class _FakePublisher:
            def publish(self, channel, data):
                published.append((channel, data))

        monkeypatch.setattr(tc, "_redis_publisher", _FakePublisher())
        try:
            tc.invalidate(777)
        finally:
            monkeypatch.setattr(tc, "_redis_publisher", None)

        assert len(published) == 1
        channel, data = published[0]
        assert channel == tc.INVALIDATION_CHANNEL
        import json as _json
        assert _json.loads(data) == {"token_id": 777}

    def test_invalidate_swallows_publisher_errors(self, monkeypatch):
        """A down Redis must not break the admin rotate / revoke /
        delete path. invalidate() logs + returns normally."""
        from services import token_cache as tc

        class _ExplodingPublisher:
            def publish(self, channel, data):
                raise RuntimeError("redis down")

        monkeypatch.setattr(tc, "_redis_publisher", _ExplodingPublisher())
        try:
            # Must not raise.
            tc.invalidate(123)
        finally:
            monkeypatch.setattr(tc, "_redis_publisher", None)

    def test_start_subscriber_with_none_url_is_noop(self, monkeypatch):
        """A deployment without Redis keeps working on TTL only; the
        subscriber startup is idempotent and does not open any
        connections."""
        from services import token_cache as tc

        tc._testing_reset_subscriber()
        tc.start_invalidation_subscriber(None)
        # Publisher stays None -> invalidate() falls back to local-only.
        assert tc._redis_publisher is None
        tc.invalidate(1)  # must not raise
