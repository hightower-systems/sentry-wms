"""Tests for the v1.6.0 D8 HTTP client (#180).

Plan §2.1 + §4.1 TLS policy invariants:

  * verify=True always at this layer.
  * allow_redirects=False; 3xx classifies as 4xx-bucket failure.
  * Full requests/urllib3 exception classification via
    isinstance checks.
  * Self-signed-cert e2e proves verify=True at runtime.
"""

import os
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")
# These tests exercise the HTTP client against in-process servers
# bound to 127.0.0.1. The dispatch-time SSRF guard rejects loopback
# by design; enable the dev/CI opt-out for the duration of this
# module so the network behavior under test is reachable. The
# SSRF guard itself is exercised in test_webhook_dispatcher_ssrf_guard.
os.environ["SENTRY_ALLOW_INTERNAL_WEBHOOKS"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import requests

from services.webhook_dispatcher import http_client as http_client_module


def _send(client, *, url, body=b"{}", signature="sha256=deadbeef",
          timestamp=1234567890, secret_generation=1, event_type="t.t",
          event_id=42):
    """Common send wrapper -- threads the same body through both
    body= and signed_body_for_assertion= so the runtime
    assertion does not fire."""
    return client.send(
        url=url,
        body=body,
        signature=signature,
        timestamp=timestamp,
        secret_generation=secret_generation,
        event_type=event_type,
        event_id=event_id,
        signed_body_for_assertion=body,
    )


# ---------------------------------------------------------------------
# classify_exception
# ---------------------------------------------------------------------


class TestClassifyException:
    def test_ssl_error_is_tls(self):
        from services.webhook_dispatcher import error_catalog
        kind, detail = http_client_module.classify_exception(
            requests.exceptions.SSLError("bad cert")
        )
        assert kind == "tls"
        # detail must be the server-owned catalog string, not the
        # library exception's message. The library message can echo
        # certificate subject names, hostnames, or upstream details
        # the consumer's stack dumped; the catalog string is safe.
        assert detail == error_catalog.get_short_message("tls")
        assert "bad cert" not in detail

    def test_timeout_is_timeout(self):
        for cls in (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectTimeout,
            requests.exceptions.ReadTimeout,
        ):
            kind, _ = http_client_module.classify_exception(cls("timed out"))
            assert kind == "timeout", f"{cls.__name__} must classify as timeout"

    def test_connection_error_is_connection_when_not_ssl(self):
        kind, _ = http_client_module.classify_exception(
            requests.exceptions.ConnectionError("refused")
        )
        assert kind == "connection"

    def test_ssl_error_takes_precedence_over_connection(self):
        """SSLError is a subclass of ConnectionError; the
        isinstance check must hit SSLError first or we
        misclassify TLS failures as plain connection drops."""
        # SSLError IS a ConnectionError per requests' inheritance
        # (urllib3.exceptions.SSLError is NOT, but
        # requests.exceptions.SSLError IS).
        assert issubclass(
            requests.exceptions.SSLError,
            requests.exceptions.ConnectionError,
        )
        kind, _ = http_client_module.classify_exception(
            requests.exceptions.SSLError("bad cert")
        )
        assert kind == "tls"

    def test_too_many_redirects_is_unknown(self):
        kind, _ = http_client_module.classify_exception(
            requests.exceptions.TooManyRedirects("loop")
        )
        # allow_redirects=False makes this unreachable in
        # production; defensive mapping returns 'unknown'.
        assert kind == "unknown"

    def test_unknown_exception_is_unknown(self):
        from services.webhook_dispatcher import error_catalog
        kind, detail = http_client_module.classify_exception(
            ValueError("not a network error")
        )
        assert kind == "unknown"
        assert detail == error_catalog.get_short_message("unknown")
        assert "not a network error" not in detail

    def test_detail_is_truncated_to_512(self):
        long = "x" * 1000
        _, detail = http_client_module.classify_exception(
            requests.exceptions.Timeout(long)
        )
        assert len(detail) <= 512


# ---------------------------------------------------------------------
# classify_status_code
# ---------------------------------------------------------------------


class TestClassifyStatusCode:
    @pytest.mark.parametrize(
        "code,expected",
        [
            # #213: 3xx redirects classify as 'redirected', not 4xx,
            # so operators can distinguish redirect misconfiguration
            # from genuine 4xx rejection in top_error_kinds.
            (300, "redirected"),
            (302, "redirected"),
            (399, "redirected"),
            (400, "4xx"),
            (404, "4xx"),
            (499, "4xx"),
            (500, "5xx"),
            (502, "5xx"),
            (599, "5xx"),
            (100, "4xx"),  # informational lands in 4xx bucket
        ],
    )
    def test_status_to_kind(self, code, expected):
        assert http_client_module.classify_status_code(code) == expected


# ---------------------------------------------------------------------
# Runtime body == signed_body assertion (regression of D5/D8)
# ---------------------------------------------------------------------


class TestSingleSerializationAssertion:
    def test_mismatched_bytes_raises(self):
        client = http_client_module.HttpClient()
        # #221: the check raises SingleSerializationViolation
        # (RuntimeError subclass), not AssertionError; the latter
        # would be stripped under PYTHONOPTIMIZE / python -O.
        with pytest.raises(
            http_client_module.SingleSerializationViolation,
            match="single-serialization",
        ):
            client.send(
                url="https://example.invalid/x",
                body=b"a",
                signature="sha256=deadbeef",
                timestamp=1,
                secret_generation=1,
                event_type="t",
                event_id=1,
                signed_body_for_assertion=b"b",  # different bytes
            )

    def test_check_survives_python_optimize_mode(self, tmp_path):
        """#221: re-running the check from a -O subprocess proves
        the bytecode does NOT depend on assert. A pre-#221 build
        (assert-based) silently skipped this check under
        PYTHONOPTIMIZE=1 and shipped unsigned bodies."""
        import subprocess
        import sys

        script = tmp_path / "check.py"
        script.write_text(
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "from services.webhook_dispatcher import http_client as hc\n"
            "client = hc.HttpClient()\n"
            "try:\n"
            "    client.send(\n"
            "        url='https://example.invalid/x',\n"
            "        body=b'a',\n"
            "        signature='sha256=deadbeef',\n"
            "        timestamp=1,\n"
            "        secret_generation=1,\n"
            "        event_type='t',\n"
            "        event_id=1,\n"
            "        signed_body_for_assertion=b'b',\n"
            "    )\n"
            "except hc.SingleSerializationViolation:\n"
            "    print('RAISED')\n"
            "    sys.exit(0)\n"
            "print('LEAKED')\n"
            "sys.exit(1)\n"
            % (os.path.join(os.path.dirname(__file__), ".."),)
        )
        result = subprocess.run(
            [sys.executable, "-O", str(script)],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONOPTIMIZE": "1"},
        )
        assert result.returncode == 0, (
            f"check did not raise under -O / PYTHONOPTIMIZE=1: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "RAISED" in result.stdout


# ---------------------------------------------------------------------
# Mock HTTP server fixture for happy-path + redirect tests
# ---------------------------------------------------------------------


def _start_http_server(handler_factory, use_https=False, certfile=None):
    """Run an HTTPServer on a free port in a background thread.
    Returns (server, port). Caller must call server.shutdown()."""
    server = HTTPServer(("127.0.0.1", 0), handler_factory)
    if use_https:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=certfile)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


@pytest.fixture
def http_200_server():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a, **kw):  # quiet test output
            return

    server, port = _start_http_server(Handler)
    yield port
    server.shutdown()


@pytest.fixture
def http_redirect_server():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self.send_response(302)
            self.send_header("Location", "https://example.invalid/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a, **kw):
            return

    server, port = _start_http_server(Handler)
    yield port
    server.shutdown()


@pytest.fixture
def http_500_server():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self.send_response(500)
            self.send_header("Content-Length", len(b"server error"))
            self.end_headers()
            self.wfile.write(b"server error")

        def log_message(self, *a, **kw):
            return

    server, port = _start_http_server(Handler)
    yield port
    server.shutdown()


# ---------------------------------------------------------------------
# E2E happy + failure paths
# ---------------------------------------------------------------------


class TestSendHappyPath:
    def test_200_round_trip(self, http_200_server):
        client = http_client_module.HttpClient()
        response = _send(client, url=f"http://127.0.0.1:{http_200_server}/")
        assert response.status_code == 200
        assert response.error_kind is None
        assert response.error_detail is None


class TestSendStatusClassification:
    def test_500_classifies_as_5xx(self, http_500_server):
        client = http_client_module.HttpClient()
        response = _send(client, url=f"http://127.0.0.1:{http_500_server}/")
        assert response.status_code == 500
        assert response.error_kind == "5xx"
        # error_detail must come from the server-owned catalog, not
        # the consumer's response body. Equality with the catalog
        # short_message proves no extra consumer bytes leaked through:
        # any concatenation of body content would break the equality.
        from services.webhook_dispatcher import error_catalog
        assert response.error_detail == error_catalog.get_short_message("5xx")

    def test_redirect_classifies_as_redirected_does_not_follow(self, http_redirect_server):
        """allow_redirects=False is a security invariant: a
        malicious consumer cannot bounce traffic to an internal
        target. A 302 lands in the dedicated 'redirected' bucket
        per #213 so operators triaging top_error_kinds can
        distinguish redirect misconfiguration from genuine 4xx
        rejection. Pre-#213 the kind was '4xx'."""
        client = http_client_module.HttpClient()
        response = _send(client, url=f"http://127.0.0.1:{http_redirect_server}/")
        assert response.status_code == 302
        assert response.error_kind == "redirected"
        from services.webhook_dispatcher import error_catalog
        assert response.error_detail == error_catalog.get_short_message("redirected")


class TestResponseSizeCap:
    """#226: the dispatcher caps response-body buffering so a
    consumer that ships a multi-MB 5xx body cannot blow up the
    worker's RSS. The cap reclassifies oversized advertised
    Content-Length as a 5xx-class failure without draining the
    bytes."""

    def test_oversized_advertised_content_length_reclassifies_to_5xx(self):
        """A 200 response that advertises Content-Length above the
        cap is reclassified as 5xx. Pre-#226 the dispatcher would
        return 200 + buffer the advertised body."""
        from services.webhook_dispatcher import error_catalog

        cap = http_client_module._MAX_RESPONSE_BODY_BYTES
        oversize = cap + 1024

        class OversizeHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Length", str(oversize))
                self.end_headers()
                # Do NOT actually write the body; the client's
                # cap fires off the header alone, so the test does
                # not need to ship MBs of bytes.

            def log_message(self, *a, **kw):
                return

        server, port = _start_http_server(OversizeHandler)
        try:
            client = http_client_module.HttpClient()
            response = _send(client, url=f"http://127.0.0.1:{port}/")
            assert response.status_code == 200
            assert response.error_kind == "5xx", (
                "oversized Content-Length must reclassify as 5xx-class "
                "failure so the dispatcher does not return success on a "
                "consumer that violates the response-size contract"
            )
            assert response.error_detail == error_catalog.get_short_message(
                "5xx"
            )
        finally:
            server.shutdown()

    def test_normal_response_size_unchanged(self):
        """Regression: a small ACK still classifies as 200 success.
        Catches an over-eager cap that would reject every consumer
        that returns any body at all."""

        class TinyHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                ack = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(ack)))
                self.end_headers()
                self.wfile.write(ack)

            def log_message(self, *a, **kw):
                return

        server, port = _start_http_server(TinyHandler)
        try:
            client = http_client_module.HttpClient()
            response = _send(client, url=f"http://127.0.0.1:{port}/")
            assert response.status_code == 200
            assert response.error_kind is None
        finally:
            server.shutdown()

    def test_chunked_unbounded_body_does_not_hang_dispatcher(self):
        """A consumer that streams a chunked body without
        Content-Length must not hang the dispatcher: stream=True
        + close() releases the connection without reading past
        whatever urllib3 prefetched into its buffer."""
        import threading

        class ChunkedHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                self.send_response(503)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                # Chunked encoding: write a few hundred KB of data in
                # several chunks. The dispatcher's close() should
                # break the loop after it returns from send().
                try:
                    for _ in range(20):
                        chunk = b"x" * 16384
                        self.wfile.write(
                            f"{len(chunk):x}\r\n".encode("ascii")
                            + chunk
                            + b"\r\n"
                        )
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                except Exception:  # noqa: BLE001
                    pass

            def log_message(self, *a, **kw):
                return

        server, port = _start_http_server(ChunkedHandler)
        try:
            client = http_client_module.HttpClient(timeout_s=5.0)
            done = threading.Event()
            container = {}

            def _go():
                try:
                    container["resp"] = _send(
                        client, url=f"http://127.0.0.1:{port}/"
                    )
                finally:
                    done.set()

            t = threading.Thread(target=_go)
            t.start()
            # The cap path should return well before the timeout;
            # 5s is generous for a localhost RTT plus a few KB.
            assert done.wait(timeout=8.0), (
                "dispatcher hung draining a chunked body; the cap + close "
                "path must release the connection without buffering"
            )
            t.join(timeout=2.0)
            response = container.get("resp")
            assert response is not None
            assert response.status_code == 503
        finally:
            server.shutdown()


class TestWallClockTimeout:
    """#237: the wall-clock cap is enforced via a thread watchdog
    around session.post. A consumer that drip-feeds bytes within
    the per-op read budget can keep the connection alive past
    the nominal cap without the watchdog; the watchdog ensures
    the entire exchange completes within timeout_s."""

    def test_slow_drip_response_hits_wall_clock_cap(self):
        """A consumer that sends one byte, sleeps under the
        per-op read timeout, sends another byte, etc., must hit
        the wall-clock cap and be reclassified as a timeout
        failure -- not return successfully after a multi-minute
        round trip."""

        class DripHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                # Drip: one byte every 0.4 s. Per-op read timeout
                # of 1.0 s would NOT trip; only the wall-clock cap
                # at 1.5 s does.
                try:
                    for _ in range(50):
                        self.wfile.write(b"1\r\nx\r\n")
                        self.wfile.flush()
                        time.sleep(0.4)
                except Exception:  # noqa: BLE001
                    pass

            def log_message(self, *a, **kw):
                return

        server, port = _start_http_server(DripHandler)
        try:
            client = http_client_module.HttpClient(
                timeout_s=1.5,
                connect_timeout_s=0.5,
                read_timeout_s=1.0,
            )
            started = time.monotonic()
            response = _send(client, url=f"http://127.0.0.1:{port}/")
            elapsed = time.monotonic() - started
            # Wall-clock cap fired; classified as timeout.
            assert response.status_code is None
            assert response.error_kind == "timeout"
            # Bound: must be close to the wall-clock cap, not
            # 50 * 0.4 = 20 s.
            assert elapsed < 5.0, (
                f"wall-clock cap did not fire within budget; took "
                f"{elapsed:.2f}s"
            )
        finally:
            server.shutdown()

    def test_timeout_tuple_passes_per_op_caps_to_requests(self):
        """Sanity: the (connect, read) tuple is the timeout shape
        the http_client uses. A direct connect_timeout violation
        (port 1, no listener) classifies as connection error
        within the connect cap, not the wall-clock cap."""
        client = http_client_module.HttpClient(
            timeout_s=10.0,
            connect_timeout_s=0.5,
            read_timeout_s=2.0,
        )
        started = time.monotonic()
        response = _send(client, url="http://127.0.0.1:1/")
        elapsed = time.monotonic() - started
        assert response.status_code is None
        assert response.error_kind in ("connection", "timeout"), (
            "expected connection-class failure on a closed port"
        )
        # Connect failure is fast; the wall-clock cap is 10 s but
        # the actual elapsed should be well under 5 s on any
        # reasonable host.
        assert elapsed < 5.0


class TestSendNetworkFailures:
    def test_connection_refused_classifies_as_connection(self):
        # Port 1 is reserved; nothing listens there.
        client = http_client_module.HttpClient(timeout_s=2.0)
        response = _send(client, url="http://127.0.0.1:1/")
        assert response.status_code is None
        assert response.error_kind == "connection"

    def test_timeout_classifies_as_timeout(self):
        # Slow server: blocks on read forever.
        class SlowHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                time.sleep(10)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a, **kw):
                return

        server, port = _start_http_server(SlowHandler)
        try:
            client = http_client_module.HttpClient(timeout_s=0.5)
            response = _send(client, url=f"http://127.0.0.1:{port}/")
            assert response.status_code is None
            assert response.error_kind == "timeout"
        finally:
            server.shutdown()


# ---------------------------------------------------------------------
# Self-signed cert e2e (verify=True invariant)
# ---------------------------------------------------------------------


def _make_self_signed_cert(tmp_path):
    """Generate a self-signed cert + key. Returns the path to a
    combined PEM file suitable for ssl.SSLContext.load_cert_chain."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime as _dt

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime.utcnow())
        .not_valid_after(_dt.datetime.utcnow() + _dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    pem = tmp_path / "self-signed.pem"
    pem.write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return str(pem)


class TestSelfSignedCertE2E:
    def test_verify_true_rejects_self_signed_cert(self, tmp_path):
        """Plan §4.1 verify=True invariant: dispatch to a
        self-signed-cert mock consumer fails with
        error_kind='tls'. Proves verify=True policy at
        runtime, not just by code inspection."""
        certfile = _make_self_signed_cert(tmp_path)

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a, **kw):
                return

        server, port = _start_http_server(
            Handler, use_https=True, certfile=certfile
        )
        try:
            client = http_client_module.HttpClient(timeout_s=2.0)
            response = _send(client, url=f"https://127.0.0.1:{port}/")
            assert response.status_code is None
            assert response.error_kind == "tls", (
                "self-signed-cert TLS handshake must fail with "
                "error_kind='tls'; verify=True is the policy"
            )
        finally:
            server.shutdown()


# ---------------------------------------------------------------------
# Header shape
# ---------------------------------------------------------------------


class TestHeaderShape:
    def test_all_v1_headers_sent(self):
        captured = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                captured["headers"] = dict(self.headers.items())
                length = int(self.headers.get("Content-Length", 0))
                captured["body"] = self.rfile.read(length)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a, **kw):
                return

        server, port = _start_http_server(Handler)
        try:
            client = http_client_module.HttpClient()
            client.send(
                url=f"http://127.0.0.1:{port}/",
                body=b'{"event_id":1}',
                signature="sha256=abc123",
                timestamp=1700000000,
                secret_generation=1,
                event_type="ship.confirmed",
                event_id=42,
                signed_body_for_assertion=b'{"event_id":1}',
            )
        finally:
            server.shutdown()

        h = captured["headers"]
        assert h["X-Sentry-Signature"] == "sha256=abc123"
        assert h["X-Sentry-Signature-Generation"] == "1"
        assert h["X-Sentry-Delivery-Id"] == "42:1700000000"
        assert h["X-Sentry-Event-Type"] == "ship.confirmed"
        assert h["X-Sentry-Timestamp"] == "1700000000"
        assert h["Content-Type"] == "application/json"
        assert captured["body"] == b'{"event_id":1}'


# ---------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------


class TestSessionLifecycle:
    def test_session_is_lazy(self):
        client = http_client_module.HttpClient()
        assert client._session is None  # noqa: SLF001

    def test_session_is_reused(self, http_200_server):
        client = http_client_module.HttpClient()
        url = f"http://127.0.0.1:{http_200_server}/"
        _send(client, url=url)
        first_session = client._session  # noqa: SLF001
        _send(client, url=url)
        assert client._session is first_session  # noqa: SLF001

    def test_close_clears_session(self):
        client = http_client_module.HttpClient()
        # Force lazy init.
        client._get_session()  # noqa: SLF001
        assert client._session is not None  # noqa: SLF001
        client.close()
        assert client._session is None  # noqa: SLF001

    def test_close_is_idempotent(self):
        client = http_client_module.HttpClient()
        client.close()
        client.close()  # second call: no error
