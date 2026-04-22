"""@require_wms_token behavior + app.create_app boot guard (v1.5.0 #130).

The decorator rejects missing / wrong-hash / revoked / expired tokens
with distinct 401 error codes and populates ``g.current_token`` +
``g.current_user`` on success. Mounts a throw-away Flask route at
``/probe`` so the tests exercise the decorator end-to-end through the
Flask test client rather than unit-testing the wrapper in isolation.

Cache behavior split out to test_token_cache.py; rate-limit bucket
isolation to test_token_rate_limit.py. Admin HTTP token CRUD lives in
test_admin_tokens.py.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from flask import Flask, g, jsonify

from _wms_token_helpers import delete_token, insert_token, sha256_with_pepper
from middleware.auth_middleware import _hash_token, require_wms_token
from services import token_cache


@pytest.fixture()
def probe_app():
    """Minimal Flask app + X-WMS-Token-gated probe route."""
    app = Flask("test-wms-decorator")

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
        token_id = insert_token(plaintext="rev-target", status="revoked")
        try:
            resp = probe_app.get(
                "/probe", headers={"X-WMS-Token": "rev-target"}
            )
            assert resp.status_code == 401
            assert resp.get_json() == {"error": "invalid_token"}
        finally:
            delete_token(token_id)

    def test_expired_token_returns_token_expired(self, probe_app):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        token_id = insert_token(plaintext="expired-target", expires_at=past)
        try:
            resp = probe_app.get(
                "/probe", headers={"X-WMS-Token": "expired-target"}
            )
            assert resp.status_code == 401
            assert resp.get_json() == {"error": "token_expired"}
        finally:
            delete_token(token_id)


class TestRequireWmsTokenAcceptance:
    def test_active_token_passes_and_populates_g(self, probe_app):
        token_id = insert_token(
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
            delete_token(token_id)

    def test_hash_uses_sha256_of_pepper_plus_plaintext(self):
        """Decision Q: the stored hash must be SHA256(pepper || plaintext)."""
        raw = "some-plaintext-value"
        assert _hash_token(raw) == sha256_with_pepper(raw)


class TestBootGuard:
    def test_create_app_raises_without_pepper(self):
        """app.create_app must reject deployments missing SENTRY_TOKEN_PEPPER."""
        from app import create_app

        original = os.environ.pop("SENTRY_TOKEN_PEPPER", None)
        try:
            with pytest.raises(RuntimeError, match="SENTRY_TOKEN_PEPPER"):
                create_app()
        finally:
            if original is not None:
                os.environ["SENTRY_TOKEN_PEPPER"] = original
