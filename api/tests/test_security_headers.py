"""
Tests for response security headers, including Content-Security-Policy (V-050).
"""


EXPECTED_CSP_DIRECTIVES = {
    "default-src": "'self'",
    "script-src": "'self'",
    # V-110: fonts are self-hosted under admin/public/fonts; neither
    # style-src nor font-src carry a Google origin.
    "style-src": "'self' 'unsafe-inline'",
    "font-src": "'self'",
    "img-src": "'self' data:",
    "connect-src": "'self'",
    "frame-ancestors": "'none'",
    "base-uri": "'self'",
    "form-action": "'self'",
    "object-src": "'none'",
    # v1.5.1 V-109 (#54): report-uri points browsers at the
    # /api/csp-report sink so CSP violations are logged rather
    # than silently dropped.
    "report-uri": "/api/csp-report",
}


def _parse_csp(header_value):
    directives = {}
    for chunk in header_value.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, value = chunk.partition(" ")
        directives[name.strip()] = value.strip()
    return directives


def test_csp_header_present_on_health_endpoint(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert "Content-Security-Policy" in resp.headers


def test_csp_directives_match_expected_policy(client):
    resp = client.get("/api/health")
    directives = _parse_csp(resp.headers["Content-Security-Policy"])
    for name, value in EXPECTED_CSP_DIRECTIVES.items():
        assert name in directives, f"CSP missing directive: {name}"
        assert directives[name] == value, (
            f"CSP directive {name} mismatch: got {directives[name]!r}, "
            f"expected {value!r}"
        )


def test_csp_header_present_on_authenticated_endpoint(client, auth_headers):
    resp = client.get("/api/warehouses", headers=auth_headers)
    assert "Content-Security-Policy" in resp.headers


def test_csp_header_present_on_error_response(client):
    # Security headers must be present on error responses as well.
    resp = client.get("/api/this-route-does-not-exist")
    assert resp.status_code == 404
    assert "Content-Security-Policy" in resp.headers


def test_csp_frame_ancestors_blocks_framing(client):
    resp = client.get("/api/health")
    directives = _parse_csp(resp.headers["Content-Security-Policy"])
    assert directives["frame-ancestors"] == "'none'"


def test_csp_object_src_none(client):
    # object-src 'none' neutralizes legacy plugin vectors (<object>, <embed>).
    resp = client.get("/api/health")
    directives = _parse_csp(resp.headers["Content-Security-Policy"])
    assert directives["object-src"] == "'none'"


class TestCspReportEndpoint:
    """v1.5.1 V-109 (#54): /api/csp-report accepts POSTed CSP
    violation reports from browsers and logs them at WARNING level.
    Unauthenticated by design (the victim browser has no session
    context); rate-limited to 60/min per IP to cap log-flood
    attacks. Legacy report-uri wire format; modern report-to /
    Reporting-Endpoints plumbing is deferred.
    """

    def test_post_returns_204(self, client):
        resp = client.post(
            "/api/csp-report",
            data='{"csp-report":{"violated-directive":"script-src"}}',
            content_type="application/csp-report",
        )
        assert resp.status_code == 204

    def test_post_accepts_malformed_body(self, client):
        """A hostile or buggy browser might send non-JSON; the sink
        must not 500 and must not drop -- just log the raw prefix
        so operators can see what the client sent."""
        resp = client.post(
            "/api/csp-report",
            data="not-json-at-all",
            content_type="application/csp-report",
        )
        assert resp.status_code == 204

    def test_post_accepts_empty_body(self, client):
        resp = client.post(
            "/api/csp-report",
            data="",
            content_type="application/csp-report",
        )
        assert resp.status_code == 204

    def test_endpoint_does_not_require_auth(self, client):
        """CSP reports come from the user's browser after a page
        loads but before the session may be established; no
        Authorization header should be expected or parsed."""
        resp = client.post(
            "/api/csp-report",
            data='{}',
            content_type="application/csp-report",
        )
        # Any 2xx / 3xx is fine; critically NOT 401 / 403.
        assert resp.status_code < 400


def test_existing_security_headers_still_set(client):
    resp = client.get("/api/health")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert "Permissions-Policy" in resp.headers


# ---------------------------------------------------------------------------
# V-051 -- HSTS header, gated on HTTPS
# ---------------------------------------------------------------------------


def test_hsts_absent_on_plain_http(client):
    # Flask test client defaults to http://; HSTS must not be emitted so
    # warehouse-LAN HTTP deployments are not forced into HTTPS-only mode.
    resp = client.get("/api/health")
    assert "Strict-Transport-Security" not in resp.headers


def test_hsts_set_when_x_forwarded_proto_is_https(client):
    resp = client.get("/api/health", headers={"X-Forwarded-Proto": "https"})
    assert resp.headers.get("Strict-Transport-Security") == (
        "max-age=31536000; includeSubDomains"
    )


def test_hsts_value_format(client):
    resp = client.get("/api/health", headers={"X-Forwarded-Proto": "https"})
    hsts = resp.headers.get("Strict-Transport-Security", "")
    assert "max-age=" in hsts
    assert "includeSubDomains" in hsts
