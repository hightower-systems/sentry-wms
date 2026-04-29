"""SSRF guard with dispatch-time DNS resolution and private-range
rejection. The security boundary against DNS rebinding and
split-horizon DNS for outbound webhooks.

Dispatch-time enforcement: every POST resolves the URL host via
:func:`socket.getaddrinfo` and rejects the request if any returned
address is in a private range. Fail-closed semantics: one private
address among multiple resolved results rejects the send. Admin-time
validation (the webhook CRUD endpoints) imports
:func:`assert_url_safe` and uses it as advisory validation at
create / PATCH time.

Reject ranges:

  * IPv4: ``10.0.0.0/8``, ``172.16.0.0/12``, ``192.168.0.0/16``,
    ``127.0.0.0/8``, ``169.254.0.0/16`` (link-local; covers AWS
    IMDS at ``169.254.169.254``).
  * IPv6: ``fc00::/7`` (ULA), ``::1/128`` (loopback),
    ``fe80::/10`` (link-local), ``fd00:ec2::/32`` (AWS IMDSv2).

Implementation uses :py:meth:`ipaddress.IPv4Address.is_private` /
``is_loopback`` / ``is_link_local`` / ``is_unspecified`` plus an
explicit IMDS IPv6 check; :py:meth:`is_private` alone does not
treat ``fd00:ec2::/32`` as private (it is part of the wider ULA
range so this is technically redundant on most stdlib versions, but
the explicit check is documented as a belt-and-suspenders against
future-version drift).

Opt-out: ``SENTRY_ALLOW_INTERNAL_WEBHOOKS=true`` disables the
dispatch-time check. The env validator refuses to boot if this is
set in production (``FLASK_ENV=production``); dev / CI only. The
companion ``SENTRY_ALLOW_HTTP_WEBHOOKS`` only relaxes the scheme
check at admin time and is unrelated to this module's behavior.
"""

import ipaddress
import logging
import socket
from typing import Iterable, List, Optional
from urllib.parse import urlparse

from . import env_validator


LOGGER = logging.getLogger("webhook_dispatcher.ssrf_guard")


# AWS IMDSv2 IPv6 prefix. is_private already returns True for this
# range on stdlib >= 3.9 (it is inside fc00::/7), but explicit
# membership keeps the policy independent of stdlib drift.
_IMDS_V6_NET = ipaddress.ip_network("fd00:ec2::/32")


class SsrfRejected(Exception):
    """Raised by :func:`assert_url_safe` when a URL fails the
    dispatch-time SSRF check. The HTTP layer maps this to
    ``error_kind='ssrf_rejected'`` on the delivery row."""


def _is_disallowed_address(addr_str: str) -> bool:
    """True when the literal IP address ``addr_str`` is in a
    rejected range. Accepts both IPv4 and IPv6 string forms."""
    try:
        addr = ipaddress.ip_address(addr_str)
    except ValueError:
        # An unparseable address is rejected fail-closed; this
        # branch is unreachable from getaddrinfo output but
        # protects callers that pass admin-supplied strings
        # without pre-validation.
        return True

    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_unspecified
        or addr.is_multicast
        or addr.is_reserved
    ):
        return True

    if isinstance(addr, ipaddress.IPv6Address) and addr in _IMDS_V6_NET:
        return True

    return False


def is_private_address(addr_str: str) -> bool:
    """Public alias of :func:`_is_disallowed_address` for callers
    that want to inspect a single address without going through
    the URL-parsing path. Used by the admin-time validation hook
    and by tests."""
    return _is_disallowed_address(addr_str)


def resolve_url_addresses(url: str) -> List[str]:
    """Resolve ``url``'s host via :func:`socket.getaddrinfo` and
    return the deduplicated list of literal IP strings. An IP
    literal in the URL is returned as-is. Raises
    :class:`SsrfRejected` when the URL has no resolvable host."""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise SsrfRejected(f"url has no host: {url!r}")

    try:
        results = socket.getaddrinfo(
            host,
            None,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise SsrfRejected(f"DNS resolution failed for {host!r}: {exc}")

    addrs: List[str] = []
    seen = set()
    for family, _socktype, _proto, _canonname, sockaddr in results:
        if family == socket.AF_INET:
            ip_str = sockaddr[0]
        elif family == socket.AF_INET6:
            ip_str = sockaddr[0]
            # Strip the IPv6 scope-id (everything after %) so the
            # address parses as a plain IPv6Address.
            if "%" in ip_str:
                ip_str = ip_str.split("%", 1)[0]
        else:
            continue
        if ip_str in seen:
            continue
        seen.add(ip_str)
        addrs.append(ip_str)

    if not addrs:
        raise SsrfRejected(
            f"no usable address family in getaddrinfo result for {host!r}"
        )
    return addrs


def assert_url_safe(
    url: str,
    *,
    resolved_addresses: Optional[Iterable[str]] = None,
) -> List[str]:
    """Resolve ``url`` (or accept a pre-resolved address list) and
    raise :class:`SsrfRejected` if any returned address is in a
    private / loopback / link-local / IMDS range. Returns the
    resolved address list on pass so the caller can log it.

    Honors the ``SENTRY_ALLOW_INTERNAL_WEBHOOKS`` opt-out: when
    set to ``true`` the function returns immediately without
    checking. The env validator refuses to boot if this is set in
    production, so the bypass exists only in dev / CI.
    """
    if env_validator.bool_var("SENTRY_ALLOW_INTERNAL_WEBHOOKS", default=False):
        # Dev/CI bypass. Caller still resolves on send.
        return list(resolved_addresses) if resolved_addresses is not None else []

    addrs = (
        list(resolved_addresses)
        if resolved_addresses is not None
        else resolve_url_addresses(url)
    )

    rejected = [a for a in addrs if _is_disallowed_address(a)]
    if rejected:
        raise SsrfRejected(
            f"refusing to dispatch to {url!r}: resolved address(es) "
            f"{rejected} are in a private / loopback / link-local / "
            f"IMDS range"
        )
    return addrs
