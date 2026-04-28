"""SSRF guard with admin-time + dispatch-time DNS resolution.

Filled in by D11. Two enforcement points per plan §2.7:

  1. Admin-time validation on ``POST /api/admin/webhooks`` and
     ``PATCH /api/admin/webhooks/<id>`` ``delivery_url``.
     Refuses URLs that resolve to private ranges at registration.
  2. Dispatch-time check: every POST resolves ``delivery_url`` via
     ``socket.getaddrinfo`` at send time and rejects if any
     returned address is private. Defeats DNS rebinding and
     split-horizon DNS.

Reject ranges include IPv4 RFC1918 + loopback + IMDS, IPv6 ULA +
link-local + loopback + AWS IMDSv2.
"""
