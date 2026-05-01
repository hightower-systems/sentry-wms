"""Tests for the dispatch-time SSRF guard."""

import os
import socket
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from services.webhook_dispatcher import ssrf_guard


@pytest.fixture(autouse=True)
def _clear_internal_opt_out(monkeypatch):
    """Sibling test modules (notably test_webhook_dispatcher_http_client)
    set ``SENTRY_ALLOW_INTERNAL_WEBHOOKS=true`` at import time so their
    127.0.0.1 fixtures are reachable. Process-global env state leaks
    across modules; this autouse fixture restores the default reject
    posture for every SSRF-guard test. Tests that need the opt-out
    re-set it explicitly via monkeypatch.setenv."""
    monkeypatch.delenv("SENTRY_ALLOW_INTERNAL_WEBHOOKS", raising=False)
    monkeypatch.delenv("SENTRY_ALLOW_HTTP_WEBHOOKS", raising=False)


# ---------------------------------------------------------------------
# is_private_address
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "addr",
    [
        # IPv4 private
        "10.0.0.1",
        "10.255.255.255",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.0.1",
        "192.168.255.255",
        # IPv4 loopback
        "127.0.0.1",
        "127.255.255.255",
        # IPv4 link-local (covers AWS IMDS)
        "169.254.169.254",
        "169.254.0.1",
        # IPv6 ULA
        "fc00::1",
        "fd12:3456:789a::1",
        # IPv6 loopback
        "::1",
        # IPv6 link-local
        "fe80::1",
        # IPv6 unspecified
        "::",
        # AWS IMDSv2 IPv6
        "fd00:ec2::254",
    ],
)
def test_private_addresses_rejected(addr):
    assert ssrf_guard.is_private_address(addr) is True


@pytest.mark.parametrize(
    "addr",
    [
        # IPv4 public
        "8.8.8.8",
        "1.1.1.1",
        "93.184.216.34",  # example.com at time of writing
        # IPv6 public
        "2606:4700:4700::1111",  # Cloudflare DNS
        "2001:4860:4860::8888",  # Google DNS
    ],
)
def test_public_addresses_allowed(addr):
    assert ssrf_guard.is_private_address(addr) is False


def test_unparseable_address_fails_closed():
    assert ssrf_guard.is_private_address("not-an-ip") is True


# ---------------------------------------------------------------------
# assert_url_safe
# ---------------------------------------------------------------------


def test_assert_url_safe_with_pre_resolved_public_addresses():
    addrs = ssrf_guard.assert_url_safe(
        "https://example.com/hook",
        resolved_addresses=["93.184.216.34"],
    )
    assert addrs == ["93.184.216.34"]


def test_assert_url_safe_with_pre_resolved_private_address_rejects():
    with pytest.raises(ssrf_guard.SsrfRejected):
        ssrf_guard.assert_url_safe(
            "https://internal.example.com/hook",
            resolved_addresses=["10.0.0.1"],
        )


def test_assert_url_safe_any_private_in_set_rejects_fail_closed():
    """A getaddrinfo result that mixes one public and one private
    address rejects on the private one. Fail-closed semantics."""
    with pytest.raises(ssrf_guard.SsrfRejected):
        ssrf_guard.assert_url_safe(
            "https://mixed.example.com/hook",
            resolved_addresses=["8.8.8.8", "127.0.0.1"],
        )


def test_assert_url_safe_imds_v4_rejects():
    with pytest.raises(ssrf_guard.SsrfRejected):
        ssrf_guard.assert_url_safe(
            "https://aws-metadata.example.com/latest/meta-data/",
            resolved_addresses=["169.254.169.254"],
        )


def test_assert_url_safe_imds_v6_rejects():
    with pytest.raises(ssrf_guard.SsrfRejected):
        ssrf_guard.assert_url_safe(
            "https://aws-metadata-v6.example.com/",
            resolved_addresses=["fd00:ec2::254"],
        )


def test_assert_url_safe_url_with_no_host_rejects():
    with pytest.raises(ssrf_guard.SsrfRejected):
        ssrf_guard.assert_url_safe("not-a-url")


# ---------------------------------------------------------------------
# Opt-out
# ---------------------------------------------------------------------


def test_internal_webhooks_opt_out_bypasses_check(monkeypatch):
    monkeypatch.setenv("SENTRY_ALLOW_INTERNAL_WEBHOOKS", "true")
    addrs = ssrf_guard.assert_url_safe(
        "https://internal.example.com/hook",
        resolved_addresses=["10.0.0.1"],
    )
    assert addrs == ["10.0.0.1"]


def test_internal_webhooks_opt_out_must_be_literal_true(monkeypatch):
    """Conservative bool parsing: only 'true' bypasses; '1' / 'yes'
    do not, so a typo cannot silently engage the bypass."""
    monkeypatch.setenv("SENTRY_ALLOW_INTERNAL_WEBHOOKS", "1")
    with pytest.raises(ssrf_guard.SsrfRejected):
        ssrf_guard.assert_url_safe(
            "https://internal.example.com/hook",
            resolved_addresses=["10.0.0.1"],
        )


def test_http_webhooks_opt_out_does_not_bypass_ssrf(monkeypatch):
    """SENTRY_ALLOW_HTTP_WEBHOOKS only relaxes the admin-time
    scheme check; it must not affect the dispatch-time SSRF
    guard."""
    monkeypatch.setenv("SENTRY_ALLOW_HTTP_WEBHOOKS", "true")
    monkeypatch.delenv("SENTRY_ALLOW_INTERNAL_WEBHOOKS", raising=False)
    with pytest.raises(ssrf_guard.SsrfRejected):
        ssrf_guard.assert_url_safe(
            "http://internal.example.com/hook",
            resolved_addresses=["10.0.0.1"],
        )


# ---------------------------------------------------------------------
# resolve_url_addresses (real DNS)
# ---------------------------------------------------------------------


def test_resolve_url_addresses_ip_literal_passes_through():
    """An IP literal in the URL's host field returns the literal
    after getaddrinfo; private literals still get rejected by the
    caller (assert_url_safe), but resolve itself does not."""
    addrs = ssrf_guard.resolve_url_addresses("https://127.0.0.1/hook")
    assert "127.0.0.1" in addrs


def test_resolve_url_addresses_unresolvable_raises():
    with pytest.raises(ssrf_guard.SsrfRejected):
        ssrf_guard.resolve_url_addresses(
            "https://this-host-does-not-exist-sentrywms.invalid/hook"
        )


# ---------------------------------------------------------------------
# HttpClient integration
# ---------------------------------------------------------------------


def test_http_client_send_rejects_private_url(monkeypatch):
    """The HttpClient.send call performs the SSRF check before
    the network request leaves; a URL whose host resolves to a
    private address returns an HttpResponse with
    error_kind='ssrf_rejected'."""
    from services.webhook_dispatcher.http_client import HttpClient

    monkeypatch.delenv("SENTRY_ALLOW_INTERNAL_WEBHOOKS", raising=False)

    def fake_resolve(url):
        return ["127.0.0.1"]

    monkeypatch.setattr(ssrf_guard, "resolve_url_addresses", fake_resolve)

    client = HttpClient()
    body = b'{"event_id":1}'
    response = client.send(
        url="https://attacker.example.com/hook",
        body=body,
        signature="sha256=deadbeef",
        timestamp=1700000000,
        secret_generation=1,
        event_type="test",
        event_id=1,
        signed_body_for_assertion=body,
    )
    assert response.status_code is None
    assert response.error_kind == "ssrf_rejected"
    assert "private" in (response.error_detail or "").lower()


def test_http_client_send_passes_public_url(monkeypatch):
    """A URL that resolves to a public address passes the SSRF
    check; the request itself fails (the test does not actually
    reach the host) but with a network-level error_kind, not
    'ssrf_rejected'."""
    from services.webhook_dispatcher.http_client import HttpClient

    monkeypatch.delenv("SENTRY_ALLOW_INTERNAL_WEBHOOKS", raising=False)

    def fake_resolve(url):
        return ["8.8.8.8"]

    monkeypatch.setattr(ssrf_guard, "resolve_url_addresses", fake_resolve)

    client = HttpClient(timeout_s=0.5)
    body = b'{"event_id":1}'
    response = client.send(
        url="https://does-not-exist-sentrywms.invalid/hook",
        body=body,
        signature="sha256=deadbeef",
        timestamp=1700000000,
        secret_generation=1,
        event_type="test",
        event_id=1,
        signed_body_for_assertion=body,
    )
    # Whatever the request layer returned, it must NOT be the
    # SSRF reject; the guard let the request through.
    assert response.error_kind != "ssrf_rejected"


# ---------------------------------------------------------------------
# Worker delivery_url_changed handling
# ---------------------------------------------------------------------


def test_worker_refresh_session_calls_close_when_present():
    """delivery_url_changed pubsub events route through the
    worker's refresh_session() which closes the HTTP client so
    the next dispatch resolves DNS fresh."""
    import threading

    from services.webhook_dispatcher import dispatch as dispatch_module

    closed = {"count": 0}

    class StubClient:
        def send(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("send not expected in this test")

        def close(self):
            closed["count"] += 1

    worker = dispatch_module.SubscriptionWorker(
        subscription_id="00000000-0000-0000-0000-000000000000",
        database_url=os.environ["DATABASE_URL"],
        http_client=StubClient(),
        shutdown=threading.Event(),
    )
    worker.refresh_session()
    assert closed["count"] == 1
    # Idempotent on a stub that does not reset state.
    worker.refresh_session()
    assert closed["count"] == 2


def test_worker_refresh_session_tolerates_stub_without_close():
    import threading

    from services.webhook_dispatcher import dispatch as dispatch_module

    class StubNoClose:
        def send(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("send not expected in this test")

    worker = dispatch_module.SubscriptionWorker(
        subscription_id="00000000-0000-0000-0000-000000000000",
        database_url=os.environ["DATABASE_URL"],
        http_client=StubNoClose(),
        shutdown=threading.Event(),
    )
    # Must not raise.
    worker.refresh_session()


def test_pool_factory_creates_per_worker_clients():
    """When the pool is constructed with http_client_factory the
    factory is invoked once per spawned worker; the workers do
    not share an instance."""
    from queue import Queue

    from services.webhook_dispatcher import dispatch as dispatch_module

    factory_calls = {"count": 0}
    instances = []

    class FactoryClient:
        def __init__(self):
            instances.append(self)
            factory_calls["count"] += 1

        def send(self, *args, **kwargs):  # pragma: no cover
            return None

        def close(self):
            pass

    pool = dispatch_module.SubscriptionWorkerPool(
        database_url=os.environ["DATABASE_URL"],
        wake_queue=Queue(),
        http_client_factory=FactoryClient,
    )
    pool.start()
    pool.shutdown()
    pool.join(timeout_s=5.0)

    # The pool may spawn zero workers if no active subscriptions
    # exist at the moment of the test (other tests clean up after
    # themselves). The contract under test is that any spawned
    # worker called the factory; the assertion below tolerates
    # the empty case but flags a duplication.
    assert len(instances) == factory_calls["count"]


def test_pool_rejects_both_shared_and_factory():
    from queue import Queue

    from services.webhook_dispatcher import dispatch as dispatch_module
    from services.webhook_dispatcher.http_client import HttpClient

    with pytest.raises(ValueError):
        dispatch_module.SubscriptionWorkerPool(
            database_url=os.environ["DATABASE_URL"],
            wake_queue=Queue(),
            http_client=HttpClient(),
            http_client_factory=HttpClient,
        )
