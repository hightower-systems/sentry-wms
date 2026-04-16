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

Note: this guard does NOT protect against DNS rebinding (where a
hostname resolves to a public IP on first lookup and a private IP on
the actual connection). Tracked in SECURITY_BACKLOG.md for v1.4.
"""

import ipaddress
import socket
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


def assert_url_allowed(url: str) -> None:
    """Validate that ``url`` points to a public destination.

    Raises ``BlockedDestinationError`` on any violation. Called by
    ``BaseConnector.make_request`` before every HTTP call.
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
        return

    # Otherwise resolve and check every address family returned. A single
    # private result blocks the whole URL.
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise BlockedDestinationError(
            f"Blocked: cannot resolve hostname {hostname!r}: {exc}"
        )

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
