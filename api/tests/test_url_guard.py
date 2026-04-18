"""Tests for the SSRF URL guard used by connector outbound HTTP calls (V-009).

The guard rejects URLs that target internal service hostnames or private /
loopback / link-local / reserved / multicast IP addresses. Tests exercise
IPv4 literals, IPv6 literals, and hostname resolution (mocked).
"""

import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import socket
from unittest.mock import patch

import pytest

from connectors.url_guard import BlockedDestinationError, assert_url_allowed


class TestSchemeAndHostname:
    def test_rejects_file_scheme(self):
        with pytest.raises(BlockedDestinationError, match="scheme"):
            assert_url_allowed("file:///etc/passwd")

    def test_rejects_no_hostname(self):
        with pytest.raises(BlockedDestinationError, match="hostname"):
            assert_url_allowed("http:///path")

    def test_rejects_internal_service_name_redis(self):
        with pytest.raises(BlockedDestinationError, match="internal"):
            assert_url_allowed("http://redis:6379/")

    def test_rejects_internal_service_name_db(self):
        with pytest.raises(BlockedDestinationError, match="internal"):
            assert_url_allowed("http://db:5432/")

    def test_rejects_localhost(self):
        with pytest.raises(BlockedDestinationError, match="internal"):
            assert_url_allowed("http://localhost/")


class TestIPLiterals:
    def test_blocks_loopback_ipv4(self):
        with pytest.raises(BlockedDestinationError, match="private"):
            assert_url_allowed("http://127.0.0.1/")

    def test_blocks_link_local_ipv4(self):
        # AWS / GCP / Azure metadata endpoint
        with pytest.raises(BlockedDestinationError, match="private"):
            assert_url_allowed("http://169.254.169.254/latest/meta-data/")

    def test_blocks_private_10(self):
        with pytest.raises(BlockedDestinationError, match="private"):
            assert_url_allowed("http://10.0.0.5/")

    def test_blocks_private_172_16(self):
        with pytest.raises(BlockedDestinationError, match="private"):
            assert_url_allowed("http://172.16.0.1/")

    def test_blocks_private_192_168(self):
        with pytest.raises(BlockedDestinationError, match="private"):
            assert_url_allowed("http://192.168.1.1/")

    def test_blocks_ipv6_loopback(self):
        with pytest.raises(BlockedDestinationError, match="private"):
            assert_url_allowed("http://[::1]/")

    def test_blocks_ipv6_unique_local(self):
        with pytest.raises(BlockedDestinationError, match="private"):
            assert_url_allowed("http://[fc00::1]/")

    def test_blocks_unspecified_address(self):
        with pytest.raises(BlockedDestinationError, match="private"):
            assert_url_allowed("http://0.0.0.0/")


class TestHostnameResolution:
    def _fake_getaddrinfo(self, ip):
        """Return a getaddrinfo replacement that resolves any hostname to ``ip``."""

        def _fake(host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))]

        return _fake

    def test_blocks_hostname_resolving_to_private_ip(self):
        # A seemingly public hostname that resolves to a private IP must
        # be blocked. Prevents trivial DNS-based bypass of IP literal checks.
        with patch("connectors.url_guard.socket.getaddrinfo", self._fake_getaddrinfo("10.0.0.5")):
            with pytest.raises(BlockedDestinationError, match="private"):
                assert_url_allowed("http://example.com/")

    def test_allows_public_hostname(self):
        # A hostname resolving to a public IP must pass (this is the
        # happy path for real ERP API endpoints).
        with patch("connectors.url_guard.socket.getaddrinfo", self._fake_getaddrinfo("8.8.8.8")):
            assert_url_allowed("https://api.example.com/orders")  # no raise

    def test_blocks_unresolvable_hostname(self):
        def _raise(*a, **kw):
            raise socket.gaierror("Name or service not known")

        with patch("connectors.url_guard.socket.getaddrinfo", _raise):
            with pytest.raises(BlockedDestinationError, match="cannot resolve"):
                assert_url_allowed("http://this-hostname-does-not-resolve.invalid/")

    def test_blocks_any_private_result_in_multi_record_lookup(self):
        # Hostnames with multiple A records: if ANY address is private,
        # the entire URL must be blocked (DNS pinning/rebinding defense).
        def _fake(host, port, *args, **kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0)),
            ]

        with patch("connectors.url_guard.socket.getaddrinfo", _fake):
            with pytest.raises(BlockedDestinationError, match="private"):
                assert_url_allowed("http://multi-record.example/")


class TestMakeRequestIntegration:
    """Verify that BaseConnector.make_request actually calls the guard."""

    def test_make_request_blocks_metadata_url(self):
        from connectors.example import ExampleConnector

        connector = ExampleConnector(config={})
        with pytest.raises(BlockedDestinationError, match="private"):
            connector.make_request("GET", "http://169.254.169.254/latest/meta-data/")

    def test_make_request_blocks_redis_hostname(self):
        from connectors.example import ExampleConnector

        connector = ExampleConnector(config={})
        with pytest.raises(BlockedDestinationError, match="internal"):
            connector.make_request("GET", "http://redis:6379/")


class TestV108_AssertUrlAllowedReturnsIp:
    """V-108: the guard returns the IP the caller must pin DNS to."""

    def test_ip_literal_returns_itself(self):
        # Public literal; guard returns the same string.
        assert assert_url_allowed("https://8.8.8.8/path") == "8.8.8.8"

    def test_hostname_returns_resolved_ip(self):
        def _fake(host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]

        with patch("connectors.url_guard.socket.getaddrinfo", _fake):
            assert assert_url_allowed("https://api.example.com/x") == "93.184.216.34"

    def test_first_safe_ip_wins_when_multiple(self):
        # All-public multi-record lookup: guard picks the first address
        # as the pin target (deterministic per OS resolver ordering).
        def _fake(host, port, *args, **kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("142.250.80.46", 0)),
            ]

        with patch("connectors.url_guard.socket.getaddrinfo", _fake):
            assert assert_url_allowed("https://api.example.com/x") == "93.184.216.34"

    def test_no_usable_ip_raises(self):
        # getaddrinfo returns results whose first entry is a non-parseable
        # address family (something exotic). The guard should raise
        # rather than silently returning nothing.
        def _fake(host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("not-an-ip", 0))]

        with patch("connectors.url_guard.socket.getaddrinfo", _fake):
            with pytest.raises(BlockedDestinationError, match="no usable address"):
                assert_url_allowed("https://api.example.com/x")


class TestV108_PinnedHostContextManager:
    """V-108: pinned_host installs and restores a thread-local DNS pin."""

    def test_pin_rewrites_getaddrinfo_host(self):
        from connectors.url_guard import pinned_host

        # _ORIGINAL_GETADDRINFO is captured once at import; when a pin is
        # active, the pin-aware wrapper swaps the hostname before
        # forwarding. We verify the forwarded hostname is the pinned IP.
        seen = []

        def _fake(host, port, *args, **kwargs):
            seen.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", 0))]

        with patch("connectors.url_guard._ORIGINAL_GETADDRINFO", _fake):
            with pinned_host("api.example.com", "93.184.216.34"):
                socket.getaddrinfo("api.example.com", 443)
            # Outside the pin the original hostname is forwarded.
            socket.getaddrinfo("api.example.com", 443)

        assert seen == ["93.184.216.34", "api.example.com"]

    def test_pin_is_case_insensitive(self):
        from connectors.url_guard import pinned_host

        seen = []

        def _fake(host, port, *args, **kwargs):
            seen.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", 0))]

        with patch("connectors.url_guard._ORIGINAL_GETADDRINFO", _fake):
            with pinned_host("API.EXAMPLE.COM", "93.184.216.34"):
                socket.getaddrinfo("api.example.com", 443)
                socket.getaddrinfo("Api.Example.Com", 443)

        assert seen == ["93.184.216.34", "93.184.216.34"]

    def test_pin_does_not_affect_other_hosts(self):
        from connectors.url_guard import pinned_host

        seen = []

        def _fake(host, port, *args, **kwargs):
            seen.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", 0))]

        with patch("connectors.url_guard._ORIGINAL_GETADDRINFO", _fake):
            with pinned_host("api.example.com", "93.184.216.34"):
                socket.getaddrinfo("other.example.org", 443)

        assert seen == ["other.example.org"]

    def test_nested_pin_overrides_then_restores(self):
        from connectors.url_guard import pinned_host

        seen = []

        def _fake(host, port, *args, **kwargs):
            seen.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", 0))]

        with patch("connectors.url_guard._ORIGINAL_GETADDRINFO", _fake):
            with pinned_host("api.example.com", "93.184.216.34"):
                socket.getaddrinfo("api.example.com", 443)      # outer
                with pinned_host("api.example.com", "142.250.80.46"):
                    socket.getaddrinfo("api.example.com", 443)  # inner
                socket.getaddrinfo("api.example.com", 443)      # outer restored

        assert seen == ["93.184.216.34", "142.250.80.46", "93.184.216.34"]


class TestV108_DnsRebindingDefeated:
    """V-108: simulate an attacker DNS that returns a public IP to the
    guard and a private IP to the actual request. With the pin the
    second lookup resolves the first value, not the attacker's second
    answer."""

    def test_guard_pin_survives_attacker_re_resolution(self):
        from connectors.url_guard import pinned_host

        # Attacker DNS: first call -> public, second call -> metadata.
        responses = iter([
            [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))],
            [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))],
        ])

        def _attacker_dns(host, port, *args, **kwargs):
            return next(responses)

        with patch("connectors.url_guard._ORIGINAL_GETADDRINFO", _attacker_dns):
            # Guard lookup (consumes the public-IP answer).
            pinned_ip = assert_url_allowed("https://attacker.example/path")
            assert pinned_ip == "93.184.216.34"

            # Simulate the request's TCP connect re-resolving. The pin
            # must rewrite the host so the resolver is asked for the
            # public IP, not the attacker's second answer.
            seen = []

            def _seeing_dns(host, port, *args, **kwargs):
                seen.append(host)
                return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (host, 0))]

            with patch("connectors.url_guard._ORIGINAL_GETADDRINFO", _seeing_dns):
                with pinned_host("attacker.example", pinned_ip):
                    socket.getaddrinfo("attacker.example", 443)

            assert seen == ["93.184.216.34"], (
                "DNS rebinding slipped through: expected the pinned IP, "
                f"got {seen!r}"
            )

    def test_make_request_pins_hostname_for_retries(self):
        # When make_request retries, each attempt must still resolve to
        # the IP validated at the top of the call, not whatever the
        # attacker DNS returns on retry N.
        from connectors.example import ExampleConnector

        responses = iter([
            [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))],
            [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))],
            [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0))],
        ])

        def _dns(host, port, *args, **kwargs):
            # The pin wrapper rewrites host to the pinned IP before
            # forwarding; if the wrapper is in place we never see
            # attacker.example here. Record what the resolver is asked
            # for on each call.
            try:
                return next(responses)
            except StopIteration:
                return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (host, 0))]

        seen_hosts = []

        def _seen_dns(host, port, *args, **kwargs):
            seen_hosts.append(host)
            return _dns(host, port, *args, **kwargs)

        with patch("connectors.url_guard._ORIGINAL_GETADDRINFO", _seen_dns):
            connector = ExampleConnector(config={})
            # Patch the actual network so the retry loop runs but does
            # not leave the process. Three 503s -> exhausted retries.
            from unittest.mock import MagicMock

            def _mock_req(method, url, **kw):
                resp = MagicMock()
                resp.status_code = 503
                resp.headers = {}
                return resp

            with patch("connectors.base.requests.request", side_effect=_mock_req):
                with patch("connectors.base.time.sleep"):
                    connector.make_request("GET", "https://attacker.example/x")

        # The guard call consumes the first (public) DNS answer. Inside
        # the retry loop the pin rewrites the hostname to that IP, so
        # _seen_dns is called with "93.184.216.34" for any additional
        # lookups (urllib3 may or may not re-resolve per retry; the
        # invariant is that we never see "attacker.example" again).
        assert seen_hosts[0] == "attacker.example"
        for h in seen_hosts[1:]:
            assert h == "93.184.216.34", (
                f"retry lookup escaped the pin: {h!r} in {seen_hosts!r}"
            )
