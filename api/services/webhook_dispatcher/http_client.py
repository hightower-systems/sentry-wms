"""HTTP client + connection pool + error-kind classification.

Filled in by D8. ``requests.Session`` factory, 10s timeout,
TLS verification always enabled, ``allow_redirects=False``,
error-kind classification mapping ``requests`` exceptions to the
``error_kind`` enum on ``webhook_deliveries`` (timeout,
connection, tls, 4xx, 5xx, signing, ssrf, unknown).

The CI lint added in D1 (this commit) enforces no disabled-TLS-
verification keyword argument anywhere under
``api/services/webhook_dispatcher/``; the lint is the bottom
rung that catches a regression before D8 fills in this module.
"""
