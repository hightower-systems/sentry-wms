"""Outbound URL allowlisting for connector HTTP traffic.

Connectors accept admin-provided base URLs and hit them over HTTP. Without
a guard, an admin (or a stolen admin token) can point the connector at
internal infrastructure (cloud metadata, the Redis broker, the database)
and turn the credential-test or sync endpoints into an SSRF primitive.

This module provides ``assert_url_allowed(url)`` which every connector
call must invoke BEFORE issuing the HTTP request. It rejects:

- URLs with no hostname or an obviously malformed scheme
- Hostnames that match the docker-compose internal service names
- Hostnames that resolve to loopback, link-local, private, reserved,
  multicast, or unspecified addresses (both IPv4 and IPv6)

The rejection raises ``BlockedDestinationError``, a subclass of
``requests.RequestException`` so that callers can catch it alongside
other HTTP failures if desired. Connector authors should let it
propagate to the admin UI, which will surface the clear error message.

V-108: DNS rebinding defence. A hostname resolves once in the guard and
a second time when ``requests`` opens the TCP connection. An attacker-
controlled DNS server can return a public IP on the first lookup and
an internal IP (metadata, Redis, Docker bridge) on the second, slipping
past the guard. To close this window, ``assert_url_allowed`` now
returns the validated IP, and callers wrap the actual request in
``pinned_host(hostname, ip)`` -- a context manager that pins the
hostname to the validated IP for the duration of the request by
intercepting ``socket.getaddrinfo`` via a thread-local map. The pin
only applies to hostnames that have been explicitly pinned; all other
lookups pass through unchanged.
"""

import ipaddress
import socket
import threading
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import urlsplit

import requests


# Hostnames of services inside the docker-compose stack. Any admin-
# supplied URL pointing at these names must be rejected, even before
# DNS resolution, because they may resolve differently inside vs.
# outside the container network.
_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "redis",
    "db",
    "api",
    "admin",
    "celery-worker",
    "sentry-redis",
    "sentry-db",
    "sentry-api",
    "sentry-admin",
    "sentry-celery",
})


class BlockedDestinationError(requests.RequestException):
    """Raised when a connector URL targets a private or internal address.

    Inherits from requests.RequestException so existing error-handling
    code in connectors (which catches RequestException) will catch this
    naturally.
    """


# V-108: thread-local map of hostname -> pinned IP. Populated by
# pinned_host(), consulted by _pinned_getaddrinfo. Lookups for hosts
# not in the map fall through to the original resolver unchanged.
_DNS_PINS = threading.local()

# Capture the unwrapped resolver before we install the pin-aware
# wrapper. assert_url_allowed and the wrapper itself call this so DNS
# pinning cannot feed itself recursively.
_ORIGINAL_GETADDRINFO = socket.getaddrinfo


def _pinned_getaddrinfo(host, port, *args, **kwargs):
    entries = getattr(_DNS_PINS, "entries", None)
    if entries and host is not None:
        pinned = entries.get(host.lower())
        if pinned is not None:
            host = pinned
    return _ORIGINAL_GETADDRINFO(host, port, *args, **kwargs)


# Install the wrapper once at module import. Callers anywhere in the
# process that do socket.getaddrinfo(...) now go through the thread-
# local map; when no pin is active for the requested host the call is
# transparently forwarded to the original resolver.
socket.getaddrinfo = _pinned_getaddrinfo


@contextmanager
def pinned_host(hostname: str, ip: str) -> Iterator[None]:
    """Pin ``hostname`` to ``ip`` for the duration of the ``with`` block.

    Any ``socket.getaddrinfo`` call that uses ``hostname`` inside the
    block is rewritten to resolve ``ip`` instead. Nested pins are
    supported: the inner pin overrides the outer and the previous
    value (or absence) is restored on exit. Thread-local; two worker
    threads with different pins do not interfere.
    """
    entries = getattr(_DNS_PINS, "entries", None)
    if entries is None:
        entries = {}
        _DNS_PINS.entries = entries
    key = hostname.lower()
    had_prior = key in entries
    prior = entries.get(key)
    entries[key] = ip
    try:
        yield
    finally:
        if had_prior:
            entries[key] = prior
        else:
            entries.pop(key, None)


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    """Return True for any address the connector must not reach."""
    return (
        ip.is_loopback          # 127.0.0.0/8, ::1
        or ip.is_link_local     # 169.254.0.0/16, fe80::/10
        or ip.is_private        # 10/8, 172.16/12, 192.168/16, fc00::/7
        or ip.is_reserved       # 240.0.0.0/4, other reserved ranges
        or ip.is_multicast      # 224.0.0.0/4, ff00::/8
        or ip.is_unspecified    # 0.0.0.0, ::
    )


def assert_url_allowed(url: str) -> str:
    """Validate ``url`` and return the IP the request must pin to.

    For hostnames, resolves once, validates every returned address, and
    returns the first non-blocked address as a string. For IP literals,
    validates and returns the literal. Callers must wrap the actual
    HTTP call in ``pinned_host(hostname, returned_ip)`` to close the
    DNS-rebinding window (V-108); ``BaseConnector.make_request`` does
    this automatically.

    Raises ``BlockedDestinationError`` on any violation.
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    hostname = (parts.hostname or "").lower()

    if scheme not in ("http", "https"):
        raise BlockedDestinationError(
            f"Blocked: only http/https URLs are permitted, got scheme {scheme!r}"
        )
    if not hostname:
        raise BlockedDestinationError("Blocked: URL has no hostname")
    if hostname in _BLOCKED_HOSTNAMES:
        raise BlockedDestinationError(
            f"Blocked: hostname {hostname!r} is reserved for internal services"
        )

    # If the hostname is already a literal IP, check it directly.
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        if _is_blocked_ip(literal_ip):
            raise BlockedDestinationError(
                f"Blocked: destination {hostname} is a private or internal address"
            )
        # IP literals need no DNS pin: the request will connect to this
        # exact address, no second resolution happens.
        return hostname

    # Resolve and check every address family returned. A single private
    # result blocks the whole URL. socket.getaddrinfo resolves through
    # _pinned_getaddrinfo; at this point no pin is active for hostname
    # (pinned_host is only entered after this function returns), so
    # behavior matches the original resolver.
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise BlockedDestinationError(
            f"Blocked: cannot resolve hostname {hostname!r}: {exc}"
        )

    safe_ip: str | None = None
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        # IPv6 sockaddr can include a scope id like 'fe80::1%lo0' -- strip it
        if "%" in ip_str:
            ip_str = ip_str.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise BlockedDestinationError(
                f"Blocked: {hostname!r} resolves to {ip_str}, which is a private or internal address"
            )
        if safe_ip is None:
            safe_ip = ip_str

    if safe_ip is None:
        raise BlockedDestinationError(
            f"Blocked: no usable address returned for {hostname!r}"
        )
    return safe_ip
