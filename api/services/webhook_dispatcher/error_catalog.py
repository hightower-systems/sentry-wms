"""Server-owned descriptions for every error_kind the dispatcher
emits. The dispatcher sets ``webhook_deliveries.error_detail`` to
the catalog ``short_message`` so the column never holds bytes the
consumer's endpoint controlled (its response body, library
exception strings echoing internals, etc.).

The admin webhook-errors viewer joins ``error_kind`` to this
catalog at response time to render ``description`` and
``triage_hint``. None of those strings come from the consumer.
"""

from typing import TypedDict


class ErrorEntry(TypedDict):
    short_message: str
    description: str
    triage_hint: str


_ERROR_CATALOG: dict[str, ErrorEntry] = {
    "timeout": {
        "short_message": "Consumer endpoint did not respond in time",
        "description": (
            "The consumer's webhook endpoint did not respond within the "
            "dispatch timeout (default 10s). The dispatcher abandoned the "
            "attempt and will retry per the exponential backoff schedule."
        ),
        "triage_hint": (
            "Check the consumer's endpoint health and response latency. "
            "Sustained timeouts often indicate the consumer is overloaded "
            "or doing synchronous work that should be enqueued. Increase "
            "DISPATCHER_HTTP_TIMEOUT_MS only as a last resort; the default "
            "is the documented contract."
        ),
    },
    "connection": {
        "short_message": "Could not connect to consumer endpoint",
        "description": (
            "The TCP connection to the consumer's endpoint failed before "
            "any HTTP exchange. The hostname may be unresolvable, the "
            "port may be closed, or a firewall may be dropping the "
            "request."
        ),
        "triage_hint": (
            "Verify the consumer endpoint is reachable from the "
            "dispatcher host. Check DNS, port reachability, and any "
            "intermediate proxy. The retry schedule will continue to "
            "reattempt; persistent connection failures auto-pause the "
            "subscription via the DLQ ceiling."
        ),
    },
    "tls": {
        "short_message": "TLS handshake failed",
        "description": (
            "The TLS handshake to the consumer's endpoint failed. The "
            "consumer may be presenting an expired or self-signed "
            "certificate, the cipher suites may not overlap, or the "
            "consumer's hostname may not match its certificate."
        ),
        "triage_hint": (
            "Inspect the consumer's certificate (`openssl s_client "
            "-connect host:port`). The dispatcher always verifies "
            "certificates; SENTRY_ALLOW_HTTP_WEBHOOKS does not relax "
            "this. If the consumer must present a self-signed "
            "certificate the contract is to use a properly issued cert "
            "instead."
        ),
    },
    "ssrf_rejected": {
        "short_message": "Delivery URL resolved to a private or internal IP",
        "description": (
            "The dispatcher's SSRF guard rejected the delivery_url "
            "because its DNS resolution returned a private (RFC1918), "
            "loopback, or cloud-IMDS address. The dispatcher refuses to "
            "POST to internal targets to defend against DNS rebinding "
            "and split-horizon DNS attacks."
        ),
        "triage_hint": (
            "Confirm the delivery_url resolves to a public address from "
            "the dispatcher host's network view. SENTRY_ALLOW_INTERNAL_"
            "WEBHOOKS=true bypasses the check in dev / CI but refuses to "
            "boot in production. Mid-session DNS rebinds are caught on "
            "the next dispatch via session teardown."
        ),
    },
    "4xx": {
        "short_message": "Consumer rejected the request (4xx response)",
        "description": (
            "The consumer's endpoint returned a 4xx HTTP response. The "
            "consumer rejected the payload, the signature, the headers, "
            "or the request shape. The dispatcher does not follow "
            "redirects; 3xx responses also classify as 4xx."
        ),
        "triage_hint": (
            "Check the consumer-side logs for the rejection reason. "
            "Common causes: signature verification failure (rotation "
            "miss), event_type unsupported by the consumer, or the "
            "consumer's deserializer rejecting the envelope shape. The "
            "exact response body is not stored to avoid leaking "
            "consumer internals."
        ),
    },
    "5xx": {
        "short_message": "Consumer endpoint returned a server error (5xx)",
        "description": (
            "The consumer's endpoint returned a 5xx HTTP response. The "
            "consumer accepted the request but failed to process it. "
            "The dispatcher will retry per the exponential backoff "
            "schedule; sustained 5xx auto-pauses the subscription via "
            "the DLQ ceiling."
        ),
        "triage_hint": (
            "Check the consumer-side logs for the underlying exception. "
            "Sustained 5xx usually indicates a downstream dependency "
            "the consumer relies on is down (their database, an "
            "upstream API). The exact response body is not stored to "
            "avoid leaking consumer internals."
        ),
    },
    "unknown": {
        "short_message": "Delivery failed (unclassified)",
        "description": (
            "The dispatcher could not classify the failure into one of "
            "the documented error_kinds. This usually indicates a "
            "library exception the classification layer did not "
            "recognize, or an internal-state mismatch (e.g. the "
            "underlying integration_events row vanished between "
            "scheduling and dispatch)."
        ),
        "triage_hint": (
            "Inspect the dispatcher logs for the originating exception. "
            "A persistent unknown classification is itself a bug; file "
            "an issue with the exception type so the catalog can be "
            "extended."
        ),
    },
}


def get_entry(error_kind: str) -> ErrorEntry:
    """Return the catalog entry for ``error_kind``. Falls back to
    the ``unknown`` entry when the kind is unrecognized so the
    admin UI never has to render ``None``."""
    return _ERROR_CATALOG.get(error_kind) or _ERROR_CATALOG["unknown"]


def get_short_message(error_kind: str) -> str:
    """Return the catalog short_message for ``error_kind``. This is
    the value the dispatcher writes to
    ``webhook_deliveries.error_detail``."""
    return get_entry(error_kind)["short_message"]


def all_kinds() -> list[str]:
    """Every error_kind the catalog covers. Tests use this to
    assert classify_exception / classify_status_code never
    produce a kind without a catalog entry."""
    return sorted(_ERROR_CATALOG.keys())
