"""
V-016: validate_body rejects non-JSON Content-Type with 415.

The decorator gate runs before body parsing so a text/plain or
form-encoded request cannot slip through to pydantic as {}.
"""


class TestContentTypeRequired:
    def test_text_plain_returns_415(self, client):
        # Login endpoint requires validate_body. No auth header needed;
        # the Content-Type gate runs before validation / auth.
        resp = client.post(
            "/api/auth/login",
            data="username=admin&password=admin",
            content_type="text/plain",
        )
        assert resp.status_code == 415
        assert resp.get_json()["error"] == "unsupported_media_type"

    def test_form_urlencoded_returns_415(self, client):
        resp = client.post(
            "/api/auth/login",
            data={"username": "admin", "password": "admin"},
            content_type="application/x-www-form-urlencoded",
        )
        assert resp.status_code == 415

    def test_missing_content_type_returns_415(self, client):
        resp = client.post(
            "/api/auth/login",
            data="{}",
            content_type="",
        )
        assert resp.status_code == 415

    def test_application_json_accepted(self, client):
        resp = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin"},
        )
        assert resp.status_code == 200

    def test_application_json_with_charset_accepted(self, client):
        resp = client.post(
            "/api/auth/login",
            data='{"username":"admin","password":"admin"}',
            content_type="application/json; charset=utf-8",
        )
        assert resp.status_code == 200
