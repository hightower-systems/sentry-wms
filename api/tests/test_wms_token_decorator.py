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
    """Minimal Flask app + X-WMS-Token-gated probe route.

    v1.5.1 V-200 (#140): the decorator now enforces endpoint scope
    against the Flask endpoint name. Register the probe under a real
    v1 endpoint name (``polling.poll_events``) so tokens seeded with
    DEFAULT_TEST_ENDPOINTS (which contains ``events.poll``) pass the
    endpoint-scope check. Tests that exercise scope-denial explicitly
    override endpoints at insert time and probe a different route.
    """
    app = Flask("test-wms-decorator")

    @app.route("/probe", endpoint="polling.poll_events")
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


class TestPepperValidation:
    """v1.5.1 V-201 (#142): the guard rejects weak pepper values, not
    just empty / unset. Short, whitespace-only, and the .env.example
    placeholder all fail boot and request-time hashing.
    """

    def _run(self, value):
        """Temporarily set the pepper to ``value``, call
        validate_pepper_config, restore the original env.
        """
        from middleware.auth_middleware import validate_pepper_config

        return validate_pepper_config(value)

    def test_none_raises(self):
        with pytest.raises(RuntimeError, match="required"):
            self._run(None)

    def test_empty_string_raises(self):
        with pytest.raises(RuntimeError, match="required"):
            self._run("")

    def test_whitespace_only_raises(self):
        with pytest.raises(RuntimeError, match="whitespace-only"):
            self._run("   \t\n  ")

    def test_placeholder_from_env_example_raises(self):
        with pytest.raises(RuntimeError, match="placeholder"):
            self._run("replace-me-with-secrets-token-hex-32")

    def test_short_pepper_raises(self):
        with pytest.raises(RuntimeError, match="at least 32"):
            self._run("tooshort")

    def test_edge_one_under_minimum_raises(self):
        # 31 chars.
        with pytest.raises(RuntimeError, match="at least 32"):
            self._run("a" * 31)

    def test_exactly_minimum_passes(self):
        # 32 chars: allowed. Returns bytes form of input unchanged.
        assert self._run("a" * 32) == b"a" * 32

    def test_long_pepper_passes(self):
        # secrets.token_hex(32) produces 64 hex chars, the recommended form.
        import secrets
        val = secrets.token_hex(32)
        assert self._run(val) == val.encode("utf-8")

    def test_bytes_are_returned_verbatim(self):
        """Whitespace inside a valid pepper is preserved; the helper
        is a gate not a normaliser. A deployment that set a pepper
        with embedded whitespace must keep hashing against the exact
        byte sequence it started with."""
        val = "valid_pepper_with space and chars 0123456789"
        assert len(val) >= 32
        assert self._run(val) == val.encode("utf-8")

    def test_create_app_rejects_placeholder(self):
        """End-to-end: the boot guard in app.py calls the validator."""
        from app import create_app

        original = os.environ.get("SENTRY_TOKEN_PEPPER")
        os.environ["SENTRY_TOKEN_PEPPER"] = "replace-me-with-secrets-token-hex-32"
        try:
            with pytest.raises(RuntimeError, match="placeholder"):
                create_app()
        finally:
            if original is not None:
                os.environ["SENTRY_TOKEN_PEPPER"] = original
            else:
                os.environ.pop("SENTRY_TOKEN_PEPPER", None)

    def test_create_app_rejects_short_pepper(self):
        from app import create_app

        original = os.environ.get("SENTRY_TOKEN_PEPPER")
        os.environ["SENTRY_TOKEN_PEPPER"] = "x"
        try:
            with pytest.raises(RuntimeError, match="at least 32"):
                create_app()
        finally:
            if original is not None:
                os.environ["SENTRY_TOKEN_PEPPER"] = original
            else:
                os.environ.pop("SENTRY_TOKEN_PEPPER", None)


class TestUnsafeBindBootGuard:
    """v1.5.1 V-206 (#147): create_app refuses to boot when
    TRUST_PROXY=true is combined with API_BIND_HOST=0.0.0.0.
    SENTRY_ALLOW_OPEN_BIND=1 is the documented escape hatch.
    """

    def _with_env(self, **overrides):
        """Apply env overrides, snapshot/restore the originals, yield
        the create_app reference."""
        saved = {k: os.environ.get(k) for k in overrides}

        def _restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return _restore

    def test_rejects_trust_proxy_true_with_open_bind(self):
        from app import create_app

        restore = self._with_env(
            TRUST_PROXY="true",
            API_BIND_HOST="0.0.0.0",
            SENTRY_ALLOW_OPEN_BIND=None,
        )
        try:
            with pytest.raises(RuntimeError, match="Unsafe deployment"):
                create_app()
        finally:
            restore()

    def test_accepts_trust_proxy_true_with_loopback_bind(self):
        from app import create_app

        restore = self._with_env(
            TRUST_PROXY="true",
            API_BIND_HOST="127.0.0.1",
            SENTRY_ALLOW_OPEN_BIND=None,
        )
        try:
            # Must not raise.
            create_app()
        finally:
            restore()

    def test_accepts_trust_proxy_false_with_open_bind(self):
        """A deployment that does not trust X-Forwarded-* is safe
        regardless of the bind shape -- the api sees only the direct
        client IP, no header spoofing to poison."""
        from app import create_app

        restore = self._with_env(
            TRUST_PROXY="false",
            API_BIND_HOST="0.0.0.0",
            SENTRY_ALLOW_OPEN_BIND=None,
        )
        try:
            create_app()
        finally:
            restore()

    def test_opt_in_override_allows_unsafe_combo(self):
        """SENTRY_ALLOW_OPEN_BIND=1 lets the operator acknowledge the
        risk for deployments that apply network-level protection
        elsewhere (e.g. a VPC lock-down)."""
        from app import create_app

        restore = self._with_env(
            TRUST_PROXY="true",
            API_BIND_HOST="0.0.0.0",
            SENTRY_ALLOW_OPEN_BIND="1",
        )
        try:
            # Must not raise even though the combo is the unsafe one.
            create_app()
        finally:
            restore()


@pytest.fixture()
def two_route_app():
    """Flask app with two v1 endpoints so endpoint-scope tests can
    exercise "allowed here, denied there" without crossing fixture
    boundaries. Each route is registered under a real V150_ENDPOINT_SLUGS
    Flask-endpoint name so the decorator's map lookup succeeds.
    """
    app = Flask("test-endpoint-scope")

    @app.route("/poll", endpoint="polling.poll_events")
    @require_wms_token
    def poll():
        return jsonify({"route": "poll"})

    @app.route("/snap", endpoint="snapshot.snapshot_inventory")
    @require_wms_token
    def snap():
        return jsonify({"route": "snap"})

    return app.test_client()


class TestEndpointScopeEnforcement:
    """v1.5.1 V-200 (#140): the decorator enforces wms_tokens.endpoints.
    Empty list = deny everything; populated list = allow only listed
    slugs. Pre-v1.5.1 the field was stored but never consulted.
    """

    def test_empty_endpoints_denies_every_v1_route(self, two_route_app):
        token_id = insert_token(plaintext="empty-endpoints", endpoints=[])
        try:
            for path in ("/poll", "/snap"):
                resp = two_route_app.get(
                    path, headers={"X-WMS-Token": "empty-endpoints"}
                )
                assert resp.status_code == 403, path
                assert resp.get_json() == {"error": "endpoint_scope_violation"}
        finally:
            delete_token(token_id)

    def test_single_slug_allows_that_route_only(self, two_route_app):
        token_id = insert_token(
            plaintext="poll-only", endpoints=["events.poll"]
        )
        try:
            ok = two_route_app.get(
                "/poll", headers={"X-WMS-Token": "poll-only"}
            )
            assert ok.status_code == 200

            denied = two_route_app.get(
                "/snap", headers={"X-WMS-Token": "poll-only"}
            )
            assert denied.status_code == 403
            assert denied.get_json() == {"error": "endpoint_scope_violation"}
        finally:
            delete_token(token_id)

    def test_unknown_slug_in_db_is_treated_as_not_allowed(self, two_route_app):
        """Garbage slugs smuggled in via pre-v1.5.1 tokens (or direct DB
        inserts) never map to any real Flask endpoint, so they silently
        fail the scope check. The CreateTokenRequest validator keeps
        this from happening via the admin UI path; this test covers the
        direct-DB-insert path (which still exists in seed scripts /
        migration test fixtures)."""
        token_id = insert_token(
            plaintext="garbage-slug", endpoints=["not.a.real.slug"]
        )
        try:
            resp = two_route_app.get(
                "/poll", headers={"X-WMS-Token": "garbage-slug"}
            )
            assert resp.status_code == 403
            assert resp.get_json() == {"error": "endpoint_scope_violation"}
        finally:
            delete_token(token_id)

    def test_full_slug_set_passes_every_route(self, two_route_app):
        token_id = insert_token(
            plaintext="full-scope",
            endpoints=[
                "events.poll",
                "events.ack",
                "events.types",
                "events.schema",
                "snapshot.inventory",
            ],
        )
        try:
            for path in ("/poll", "/snap"):
                resp = two_route_app.get(
                    path, headers={"X-WMS-Token": "full-scope"}
                )
                assert resp.status_code == 200, path
        finally:
            delete_token(token_id)
