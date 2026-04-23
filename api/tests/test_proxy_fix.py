"""#107: ProxyFix middleware wiring + Fruxh's CSRF-behind-nginx repro.

Fruxh's production deployment runs Sentry behind an nginx TLS terminator
on http://127.0.0.1:8080. nginx forwards X-Forwarded-Proto: https,
X-Forwarded-Host: sentry.fruxh.example, X-Forwarded-For: <client-ip>.
Flask's request object, without ProxyFix wrapping app.wsgi_app, reported
scheme=http and host=127.0.0.1:8080; cookies were issued scoped to the
internal host, the browser never resubmitted them to the public host,
and every POST / PUT / PATCH / DELETE 403'd on CSRF.

These tests lock:
  - ProxyFix is opt-in (default off). Forging X-Forwarded-* at a
    Sentry that is NOT behind a trusted proxy must not be honoured.
  - TRUST_PROXY=true rewrites request.scheme / .host / .is_secure.
  - A full login + change-password cycle with proxy headers lands a
    Secure CSRF cookie and the change-password POST does NOT 403 on
    CSRF (Fruxh's symptom).
"""

import pytest


PROXY_HEADERS = {
    "X-Forwarded-Proto": "https",
    "X-Forwarded-Host": "sentry.fruxh.example",
    "X-Forwarded-For": "203.0.113.7",
}


def _cookie_attrs(resp, name):
    for line in resp.headers.getlist("Set-Cookie"):
        parts = [p.strip() for p in line.split(";")]
        cookie_name, _, cookie_value = parts[0].partition("=")
        if cookie_name != name:
            continue
        attrs = {"_name": cookie_name, "_value": cookie_value}
        for p in parts[1:]:
            k, _, v = p.partition("=")
            attrs[k.lower()] = v or True
        return attrs
    return None


def _build_probe_app():
    # Fresh app per fixture so env-var-driven wiring re-evaluates.
    # The session-scoped `app` fixture in conftest is not reusable here
    # because its ProxyFix state is frozen at create_app() time.
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True

    probe = {}

    @app.route("/_test_proxy_probe")
    def _probe():
        from flask import request as r
        probe.clear()
        probe["scheme"] = r.scheme
        probe["host"] = r.host
        probe["is_secure"] = r.is_secure
        return "", 204

    app._probe_state = probe
    return app


@pytest.fixture
def unproxied_app(_seed_session_database, monkeypatch):
    monkeypatch.delenv("TRUST_PROXY", raising=False)
    return _build_probe_app()


@pytest.fixture
def unproxied_client(unproxied_app):
    return unproxied_app.test_client()


@pytest.fixture
def proxied_app(_seed_session_database, monkeypatch):
    monkeypatch.setenv("TRUST_PROXY", "true")
    return _build_probe_app()


@pytest.fixture
def proxied_client(proxied_app):
    return proxied_app.test_client()


class TestProxyFixOptIn:
    def test_default_does_not_trust_x_forwarded_headers(self, unproxied_app, unproxied_client):
        # No TRUST_PROXY -> ProxyFix not wired -> X-Forwarded-* headers
        # are just headers, nothing more. Critical security invariant:
        # a Sentry running without a proxy in front must not let a
        # client claim its own origin or scheme.
        resp = unproxied_client.get("/_test_proxy_probe", headers=PROXY_HEADERS)
        assert resp.status_code == 204
        probe = unproxied_app._probe_state
        assert probe["is_secure"] is False
        assert probe["scheme"] == "http"
        assert "sentry.fruxh.example" not in probe["host"]


class TestProxyFixEnabled:
    def test_trust_proxy_rewrites_scheme_host_and_is_secure(self, proxied_app, proxied_client):
        resp = proxied_client.get("/_test_proxy_probe", headers=PROXY_HEADERS)
        assert resp.status_code == 204
        probe = proxied_app._probe_state
        assert probe["scheme"] == "https"
        assert probe["host"] == "sentry.fruxh.example"
        assert probe["is_secure"] is True


class TestCsrfCookieBehindProxy:
    def test_login_with_proxy_headers_sets_secure_csrf_and_auth_cookies(self, proxied_client):
        resp = proxied_client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin"},
            headers=PROXY_HEADERS,
        )
        assert resp.status_code == 200

        csrf = _cookie_attrs(resp, "sentry_csrf")
        assert csrf is not None
        assert "secure" in csrf, (
            "CSRF cookie must carry Secure when X-Forwarded-Proto=https "
            "and TRUST_PROXY is set; Fruxh's #107 repro turned on this."
        )
        assert csrf.get("samesite", "").lower() == "strict"

        auth = _cookie_attrs(resp, "sentry_auth")
        assert auth is not None
        assert "secure" in auth
        assert "httponly" in auth

    def test_change_password_with_proxy_headers_is_not_blocked_by_csrf(self, proxied_client):
        # Fruxh's exact symptom: login through nginx succeeds, then
        # change-password returns 403 "CSRF token missing or invalid".
        # The login response must hand back a CSRF token the subsequent
        # POST can echo, and the CSRF middleware must accept it.
        login_resp = proxied_client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin"},
            headers=PROXY_HEADERS,
        )
        assert login_resp.status_code == 200
        csrf = _cookie_attrs(login_resp, "sentry_csrf")["_value"]
        assert csrf

        resp = proxied_client.post(
            "/api/auth/change-password",
            json={"current_password": "admin", "new_password": "ProxiedPassword9!"},
            headers={**PROXY_HEADERS, "X-CSRF-Token": csrf},
        )
        # 200 is the happy path. The guard: NOT 403 -- because a 403 on
        # this exact call path is the bug this release fixes.
        assert resp.status_code != 403, (
            f"CSRF gate fired behind proxy headers; ProxyFix wiring "
            f"is wrong. response: {resp.get_json()}"
        )
        assert resp.status_code == 200


class TestHealthEndpointDoesNotLeakProxyFixState:
    """v1.5.1 V-215 (umbrella #156): /api/health is unauthenticated
    (Docker healthcheck, upstream monitors). Returning
    proxy_fix_active on that wire told any anonymous caller whether
    the deployment was a candidate for X-Forwarded-* spoofing. The
    field moved to the admin-only /api/admin/system-info endpoint;
    the anonymous health response is now {status, service} only.
    """

    def test_health_does_not_expose_proxy_state(self, unproxied_client):
        resp = unproxied_client.get("/api/health")
        assert resp.status_code == 200
        body = resp.get_json()
        assert "proxy_fix_active" not in body
        assert body.get("status") == "ok"
        assert body.get("service") == "sentry-wms"

    def test_health_shape_identical_with_and_without_trust_proxy(
        self, unproxied_client, proxied_client
    ):
        """Confirm the anonymous-health body is byte-for-byte
        identical regardless of TRUST_PROXY state so an attacker
        cannot probe the deployment topology."""
        a = unproxied_client.get("/api/health").get_json()
        b = proxied_client.get("/api/health").get_json()
        assert a == b


def _login_for(client, proxy_headers=None):
    """Issue admin credentials against the given per-app client and
    return a Bearer auth header. Each proxy fixture spins up its own
    Flask app so the session-scoped auth_headers fixture cannot be
    reused (its cookies are bound to the session app). Bearer tokens
    survive across app instances because JWT_SECRET is env-fixed."""
    headers = {**(proxy_headers or {})}
    resp = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "admin"},
        headers=headers or None,
    )
    data = resp.get_json()
    return {"Authorization": f"Bearer {data['token']}"}


class TestSystemInfoReportsProxyFixState:
    """v1.5.1 V-215: the admin-only endpoint surfaces proxy_fix_active
    so operators can still verify TRUST_PROXY reached the container.
    Gated on @require_auth + @require_role("ADMIN"); anonymous
    callers get 401."""

    def test_system_info_requires_auth(self, unproxied_client):
        resp = unproxied_client.get("/api/admin/system-info")
        assert resp.status_code == 401

    def test_system_info_reports_inactive_without_trust_proxy(
        self, unproxied_client
    ):
        auth = _login_for(unproxied_client)
        resp = unproxied_client.get(
            "/api/admin/system-info", headers=auth
        )
        assert resp.status_code == 200
        assert resp.get_json()["proxy_fix_active"] is False

    def test_system_info_reports_active_with_trust_proxy(
        self, proxied_client
    ):
        auth = _login_for(proxied_client, PROXY_HEADERS)
        resp = proxied_client.get(
            "/api/admin/system-info", headers={**auth, **PROXY_HEADERS}
        )
        assert resp.status_code == 200
        assert resp.get_json()["proxy_fix_active"] is True
