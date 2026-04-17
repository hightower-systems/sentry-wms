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
