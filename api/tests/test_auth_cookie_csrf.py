"""
V-045: HttpOnly cookie auth + CSRF protection for admin SPA.

The bearer-token path remains for mobile clients and is covered in
test_auth.py. This file focuses on the cookie-auth path and its CSRF
double-submit requirement.
"""


def _login(client):
    return client.post("/api/auth/login", json={"username": "admin", "password": "admin"})


def _cookies(resp):
    # Flask test response exposes cookies via headers; pull them into a
    # list of tuples for easier assertion.
    return resp.headers.getlist("Set-Cookie")


def _cookie_attrs(set_cookie_line: str) -> dict:
    # "name=value; Attr1=v1; Attr2" -> {"_name": name, "_value": value, attr: value}
    parts = [p.strip() for p in set_cookie_line.split(";")]
    name, _, value = parts[0].partition("=")
    attrs = {"_name": name, "_value": value}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        attrs[k.lower()] = v or True
    return attrs


def _find_cookie(resp, name):
    for line in _cookies(resp):
        attrs = _cookie_attrs(line)
        if attrs["_name"] == name:
            return attrs
    return None


class TestLoginCookies:
    def test_login_sets_httponly_auth_cookie(self, client):
        resp = _login(client)
        assert resp.status_code == 200
        auth_cookie = _find_cookie(resp, "sentry_auth")
        assert auth_cookie is not None
        assert "httponly" in auth_cookie
        assert auth_cookie.get("samesite", "").lower() == "strict"
        assert auth_cookie.get("path") == "/"
        assert auth_cookie["_value"]  # non-empty

    def test_login_sets_readable_csrf_cookie(self, client):
        resp = _login(client)
        csrf_cookie = _find_cookie(resp, "sentry_csrf")
        assert csrf_cookie is not None
        # CSRF cookie must NOT be HttpOnly -- JS must read it to attach the header
        assert "httponly" not in csrf_cookie
        assert csrf_cookie.get("samesite", "").lower() == "strict"
        assert csrf_cookie["_value"]

    def test_login_body_still_contains_token_for_mobile(self, client):
        resp = _login(client)
        data = resp.get_json()
        assert "token" in data and data["token"]
        assert "user" in data

    def test_refresh_rotates_cookies(self, client):
        login_resp = _login(client)
        first_auth = _find_cookie(login_resp, "sentry_auth")["_value"]
        first_csrf = _find_cookie(login_resp, "sentry_csrf")["_value"]

        # Refresh via cookie auth. GET-equivalent path is /auth/refresh POST;
        # no CSRF header needed because we'll use the Authorization header
        # fallback here to keep this test focused on cookie rotation.
        refresh_resp = client.post(
            "/api/auth/refresh",
            headers={"Authorization": f"Bearer {login_resp.get_json()['token']}"},
        )
        assert refresh_resp.status_code == 200
        new_auth = _find_cookie(refresh_resp, "sentry_auth")
        new_csrf = _find_cookie(refresh_resp, "sentry_csrf")
        assert new_auth is not None and new_auth["_value"]
        assert new_csrf is not None and new_csrf["_value"]
        assert new_auth["_value"] != first_auth
        assert new_csrf["_value"] != first_csrf


class TestCookieAuth:
    def test_get_with_cookie_only_authenticates(self, client):
        _login(client)
        # No Authorization header; test client auto-sends cookies from prior login.
        resp = client.get("/api/auth/me")
        assert resp.status_code == 200
        assert resp.get_json()["username"] == "admin"

    def test_get_without_cookie_or_header_rejects(self, client):
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401

    def test_cookie_auth_takes_effect_without_localstorage_token(self, client):
        # Simulates admin SPA after V-045: no Authorization header ever sent.
        _login(client)
        resp = client.get("/api/lookup/item/100000000001")
        # Either 404 (item missing in test seed) or 200 -- the important part
        # is that it's not 401. Cookie auth must be accepted.
        assert resp.status_code != 401


class TestCsrfEnforcement:
    def _login_with_csrf(self, client):
        resp = _login(client)
        csrf = _find_cookie(resp, "sentry_csrf")["_value"]
        return csrf

    def test_post_with_cookie_and_no_csrf_header_is_forbidden(self, client):
        self._login_with_csrf(client)
        # refresh is a POST under @require_auth. No CSRF header -> 403.
        resp = client.post("/api/auth/refresh")
        assert resp.status_code == 403
        assert "csrf" in (resp.get_json() or {}).get("error", "").lower()

    def test_post_with_cookie_and_wrong_csrf_header_is_forbidden(self, client):
        self._login_with_csrf(client)
        resp = client.post(
            "/api/auth/refresh",
            headers={"X-CSRF-Token": "not-the-real-csrf-value"},
        )
        assert resp.status_code == 403

    def test_post_with_cookie_and_matching_csrf_succeeds(self, client):
        csrf = self._login_with_csrf(client)
        resp = client.post("/api/auth/refresh", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200

    def test_bearer_header_bypasses_csrf_check(self, client):
        # Mobile clients don't have cookies; their bearer-header path must
        # not be gated by CSRF (they're not vulnerable to it).
        login_resp = _login(client)
        token = login_resp.get_json()["token"]
        # Clear cookies so we're purely on the header path.
        client._cookies.clear()
        resp = client.post(
            "/api/auth/refresh",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

    def test_get_with_cookie_does_not_require_csrf(self, client):
        # CSRF is only enforced on mutating methods. GETs with cookie auth work.
        self._login_with_csrf(client)
        resp = client.get("/api/auth/me")
        assert resp.status_code == 200


class TestLogout:
    def test_logout_clears_both_cookies(self, client):
        login_resp = _login(client)
        csrf = _find_cookie(login_resp, "sentry_csrf")["_value"]
        resp = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200

        auth_set = _find_cookie(resp, "sentry_auth")
        csrf_set = _find_cookie(resp, "sentry_csrf")
        assert auth_set is not None and auth_set["_value"] == ""
        assert csrf_set is not None and csrf_set["_value"] == ""

    def test_after_logout_cookie_auth_no_longer_works(self, client):
        login_resp = _login(client)
        csrf = _find_cookie(login_resp, "sentry_csrf")["_value"]
        client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401

    def test_logout_without_session_is_noop(self, client):
        # V-100: no prior login, no cookies. Should succeed idempotently
        # WITHOUT emitting Set-Cookie (nothing to clear). A cross-origin
        # attacker (SameSite=Strict strips the cookie) lands here too,
        # and must not be able to force the victim's session to end.
        resp = client.post("/api/auth/logout")
        assert resp.status_code == 200
        assert _find_cookie(resp, "sentry_auth") is None
        assert _find_cookie(resp, "sentry_csrf") is None

    def test_logout_with_valid_cookie_but_no_csrf_is_forbidden(self, client):
        # V-100: a same-origin request with the auth cookie attached
        # must still present a valid CSRF token. Blocks forced-logout
        # CSRF even in the rare cases where the auth cookie leaks into
        # a cross-origin request.
        _login(client)
        resp = client.post("/api/auth/logout")
        assert resp.status_code == 403
        assert "csrf" in (resp.get_json() or {}).get("error", "").lower()

    def test_logout_with_valid_cookie_and_bad_csrf_is_forbidden(self, client):
        _login(client)
        resp = client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": "not-the-real-csrf-value"},
        )
        assert resp.status_code == 403

    def test_logout_with_expired_cookie_clears_silently(self, client):
        # V-100: a cookie that cannot decode (expired or garbage JWT)
        # represents a session that is already dead. Clear the cookies
        # without demanding a CSRF token the client may no longer have.
        client.set_cookie("sentry_auth", "not-a-real-jwt", domain="localhost")
        resp = client.post("/api/auth/logout")
        assert resp.status_code == 200
        auth_set = _find_cookie(resp, "sentry_auth")
        csrf_set = _find_cookie(resp, "sentry_csrf")
        assert auth_set is not None and auth_set["_value"] == ""
        assert csrf_set is not None and csrf_set["_value"] == ""
