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
        stops working within TTL_SECONDS (60s in prod). The cache is
        not eagerly flushed; subsequent workers see the revoke when
        their per-entry TTL expires."""
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
