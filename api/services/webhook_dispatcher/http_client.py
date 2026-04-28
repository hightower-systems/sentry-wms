"""HTTP client for the v1.6.0 webhook dispatcher (plan §2.1, §4.1).

D8 fills in the D5 placeholder. ``requests.Session`` factory,
``verify=True`` always, ``allow_redirects=False``, full error-
kind classification mapping ``requests`` / ``urllib3`` exceptions
to the documented enum.

TLS policy invariants:

  * ``verify=True`` always at this layer. The
    ``SENTRY_ALLOW_HTTP_WEBHOOKS`` opt-out only relaxes the
    scheme check at admin time; cert verification at dispatch
    time is non-negotiable.
  * ``allow_redirects=False``. A 3xx response classifies as a
    ``4xx`` failure (consumer-side wire-contract violation);
    following redirects would let a malicious consumer bounce
    traffic to an internal target.

The CI lint added in D1 (this commit retained) enforces no
disabled-TLS-verification keyword argument anywhere under
``api/services/webhook_dispatcher/``.
"""

import logging
from dataclasses import dataclass
from typing import Optional


LOGGER = logging.getLogger("webhook_dispatcher.http_client")

_DEFAULT_HTTP_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class HttpResponse:
    """Result of an HttpClient.send call. ``status_code`` is set
    when the consumer returned a response (2xx, 3xx, 4xx, 5xx);
    ``error_kind`` is set when an exception fired before the
    response landed (timeout, connection, tls, etc.). Exactly
    one of the two is populated per cycle except for the 200 OK
    happy path where status_code=200 and error_kind=None."""

    status_code: Optional[int]
    error_kind: Optional[str]
    error_detail: Optional[str]


def classify_exception(exc: Exception) -> tuple[str, str]:
    """Map a ``requests`` / ``urllib3`` exception to
    (error_kind, error_detail). The mapping uses isinstance
    checks against the documented exception hierarchy rather
    than name-substring matching (the D5 placeholder used the
    name-substring shape; D8 tightens to class hierarchy).

    Returns a tuple so the dispatcher can populate
    ``webhook_deliveries.error_kind`` + ``error_detail`` in
    one call.
    """
    import requests  # noqa: WPS433  -- localised import

    detail = (str(exc) or type(exc).__name__)[:512]

    if isinstance(exc, requests.exceptions.SSLError):
        return "tls", detail
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout", detail
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        # allow_redirects=False makes this unreachable in
        # production; defensive mapping in case a future code
        # change flips the flag.
        return "unknown", detail
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection", detail
    return "unknown", detail


def classify_status_code(status_code: int) -> str:
    """Map an HTTP status code to the dispatcher's error_kind
    enum. Plan §1.3 enumerates: 4xx, 5xx. 3xx is treated as 4xx
    because allow_redirects=False; the 3xx is the consumer's
    misconfiguration, not a transport issue."""
    if 200 <= status_code < 300:
        # 2xx is success; caller should not invoke
        # classify_status_code on a 2xx.
        return "unknown"
    if 500 <= status_code < 600:
        return "5xx"
    # Everything else (3xx redirects, 4xx client errors,
    # 1xx informational) lands in the 4xx bucket. Consumers
    # that 3xx are misconfigured; treating them as 4xx makes
    # the auto-pause + retry semantics consistent.
    return "4xx"


class HttpClient:
    """Synchronous HTTP client. One instance per
    SubscriptionWorker; the ``requests.Session`` is reused
    across deliveries to amortize TLS handshakes.

    D11 SSRF guard tears the Session down on subscription
    mutation events that could change DNS resolution
    (delivery_url_changed). D9 rate limiter wraps the send
    call. D8 ships the bare client.
    """

    def __init__(self, timeout_s: float = _DEFAULT_HTTP_TIMEOUT_S):
        self.timeout_s = timeout_s
        self._session = None  # lazy

    def _get_session(self):
        if self._session is None:
            import requests  # noqa: WPS433
            self._session = requests.Session()
            # verify=True is the default but we set it explicitly
            # so a future env-driven override has somewhere to
            # plug in (and so a code review can grep for the
            # invariant). The D1 CI lint forbids disabling it.
            self._session.verify = True
        return self._session

    def close(self) -> None:
        """Tear down the underlying Session. Called by D11 on
        subscription mutation events that change DNS, and by
        the worker's run-loop ``finally`` block on shutdown.
        Idempotent."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass
            self._session = None

    def send(
        self,
        url: str,
        body: bytes,
        signature: str,
        timestamp: int,
        secret_generation: int,
        event_type: str,
        event_id: int,
        signed_body_for_assertion: bytes,
    ) -> HttpResponse:
        """Send ``body`` to ``url`` with the v1.6.0 signature
        headers. Returns :class:`HttpResponse` with either a
        status_code (response landed) or an error_kind
        (exception fired before the response).

        Plan §3.1 single-serialization runtime assertion: the
        bytes the HTTP layer is about to send MUST equal the
        bytes that were signed. A refactor that introduces a
        transformation between sign and send surfaces here.
        """
        assert body is signed_body_for_assertion or body == signed_body_for_assertion, (
            "single-serialization invariant violated: the bytes about to be "
            "POSTed do not match the bytes that were signed."
        )

        import requests  # noqa: WPS433

        headers = {
            "Content-Type": "application/json",
            "X-Sentry-Signature": signature,
            "X-Sentry-Signature-Generation": str(secret_generation),
            "X-Sentry-Delivery-Id": f"{event_id}:{timestamp}",
            "X-Sentry-Event-Type": event_type,
            "X-Sentry-Timestamp": str(timestamp),
        }

        session = self._get_session()
        try:
            response = session.post(
                url,
                data=body,
                headers=headers,
                timeout=self.timeout_s,
                verify=True,
                allow_redirects=False,
            )
        except Exception as exc:  # noqa: BLE001
            kind, detail = classify_exception(exc)
            return HttpResponse(
                status_code=None,
                error_kind=kind,
                error_detail=detail,
            )

        if 200 <= response.status_code < 300:
            return HttpResponse(
                status_code=response.status_code,
                error_kind=None,
                error_detail=None,
            )

        kind = classify_status_code(response.status_code)
        # Truncate response body to fit error_detail (VARCHAR(512))
        # so a chatty consumer cannot silently inflate the table.
        try:
            body_text = response.text[:512]
        except Exception:  # noqa: BLE001
            body_text = ""
        return HttpResponse(
            status_code=response.status_code,
            error_kind=kind,
            error_detail=body_text or f"HTTP {response.status_code}",
        )
