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

from . import error_catalog
from . import ssrf_guard


LOGGER = logging.getLogger("webhook_dispatcher.http_client")

_DEFAULT_HTTP_TIMEOUT_S = 10.0

# #226: maximum response-body bytes the dispatcher will read.
# The dispatcher never inspects the body (error_detail comes from
# the server-owned catalog; consumer bodies are intentionally
# discarded to avoid credential-exfiltration via the DLQ viewer).
# A malicious or misconfigured consumer can stream an unbounded
# 5xx body that the default `requests` path buffers entirely
# before the call returns; under sustained abuse the dispatcher
# worker's RSS spikes. Cap at 64 KB (orders of magnitude above
# any reasonable ACK) and close the connection past that point.
_MAX_RESPONSE_BODY_BYTES = 64 * 1024


class SingleSerializationViolation(RuntimeError):
    """Raised when the bytes about to be POSTed do not match the
    bytes that were signed. The dispatcher catches this by name
    and re-raises so the test suite (and the operator's logs)
    surface a programmer error rather than reclassifying the
    breach as a generic delivery failure.

    #221: replaced ``assert`` so the check is part of the emitted
    bytecode regardless of ``python -O`` / PYTHONOPTIMIZE level.
    """


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
    than name-substring matching.

    The detail is sourced from :mod:`error_catalog`, never from
    ``str(exc)``. Library exception strings can echo URL
    fragments, hostnames, or upstream credential material the
    consumer's stack trace dumped; the catalog string is
    server-controlled and never carries external bytes.
    """
    import requests  # noqa: WPS433  -- localised import

    if isinstance(exc, requests.exceptions.SSLError):
        kind = "tls"
    elif isinstance(exc, requests.exceptions.Timeout):
        kind = "timeout"
    elif isinstance(exc, requests.exceptions.TooManyRedirects):
        # allow_redirects=False makes this unreachable in
        # production; defensive mapping in case a future code
        # change flips the flag.
        kind = "unknown"
    elif isinstance(exc, requests.exceptions.ConnectionError):
        kind = "connection"
    else:
        kind = "unknown"
    return kind, error_catalog.get_short_message(kind)


def classify_status_code(status_code: int) -> str:
    """Map an HTTP status code to the dispatcher's error_kind
    enum.

    #213: 3xx responses get their own ``redirected`` bucket so
    operators triaging the stats endpoint's top_error_kinds
    breakdown can tell consumer-side redirect misconfigurations
    apart from genuine 4xx rejections. The dispatcher still does
    not follow redirects (``allow_redirects=False`` is wired and
    CI-linted to stay that way); the label split is signal-only.
    Pre-#213 every 3xx silently rolled up under ``4xx``.

    1xx informational responses also go into ``redirected`` since
    they are unreachable in practice for the dispatcher's POST
    flow and are operator-debug shape rather than transport
    failures.
    """
    if 200 <= status_code < 300:
        # 2xx is success; caller should not invoke
        # classify_status_code on a 2xx.
        return "unknown"
    if 500 <= status_code < 600:
        return "5xx"
    if 300 <= status_code < 400:
        return "redirected"
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

        Single-serialization runtime check: the bytes the HTTP
        layer is about to send MUST equal the bytes that were
        signed. A refactor that introduces a transformation
        between sign and send surfaces here.

        #221: this used to be ``assert``, which Python ``-O``
        strips at compile time (the bytecode emitted under
        PYTHONOPTIMIZE=1 omits the SETUP_ASSERTION block entirely).
        A production deployment that runs the dispatcher with
        ``python -O`` or with PYTHONOPTIMIZE in the environment
        loses this defense silently, and a body mismatch
        introduced by a logging middleware or instrumentation
        layer ships unsigned. Replaced with an explicit
        ``raise RuntimeError`` so the check is part of the
        emitted bytecode regardless of optimization level.
        """
        if not (
            body is signed_body_for_assertion
            or body == signed_body_for_assertion
        ):
            raise SingleSerializationViolation(
                "single-serialization invariant violated: the bytes about "
                "to be POSTed do not match the bytes that were signed."
            )

        try:
            ssrf_guard.assert_url_safe(url)
        except ssrf_guard.SsrfRejected:
            return HttpResponse(
                status_code=None,
                error_kind="ssrf_rejected",
                error_detail=error_catalog.get_short_message("ssrf_rejected"),
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
            # #226: stream=True so the body is NOT buffered into
            # memory by `requests` before this method returns.
            # We never inspect the body anyway -- error_detail
            # comes from the server-owned catalog -- so reading
            # only status_code + headers and immediately closing
            # the connection bounds the worker's RSS regardless
            # of how large a body the consumer tries to ship.
            response = session.post(
                url,
                data=body,
                headers=headers,
                timeout=self.timeout_s,
                verify=True,
                allow_redirects=False,
                stream=True,
            )
        except Exception as exc:  # noqa: BLE001
            kind, detail = classify_exception(exc)
            return HttpResponse(
                status_code=None,
                error_kind=kind,
                error_detail=detail,
            )

        # #226: regardless of the status_code branches below, the
        # response is closed via try/finally so a 2xx with a huge
        # body (e.g. a chatty consumer ACK) cannot leak buffered
        # bytes either. The error_detail is sourced from the
        # server-owned catalog, never from response.text or
        # response.content -- a misconfigured consumer endpoint
        # can echo upstream credentials (DB connection strings,
        # API tokens, session cookies) into a 5xx page; persisting
        # that body would make the DLQ admin viewer a credential-
        # exfiltration channel for the consumer's secrets.
        try:
            # #226: refuse oversized advertised bodies up front.
            # A consumer that advertises Content-Length above the
            # cap is reclassified as a 5xx-class failure so the
            # dispatcher never even drains the bytes.
            try:
                advertised = int(response.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                advertised = 0
            if advertised > _MAX_RESPONSE_BODY_BYTES:
                kind = classify_status_code(response.status_code)
                if kind not in ("4xx", "5xx", "redirected"):
                    kind = "5xx"
                return HttpResponse(
                    status_code=response.status_code,
                    error_kind=kind,
                    error_detail=error_catalog.get_short_message(kind),
                )

            if 200 <= response.status_code < 300:
                return HttpResponse(
                    status_code=response.status_code,
                    error_kind=None,
                    error_detail=None,
                )
            kind = classify_status_code(response.status_code)
            return HttpResponse(
                status_code=response.status_code,
                error_kind=kind,
                error_detail=error_catalog.get_short_message(kind),
            )
        finally:
            # close() returns the underlying connection to the
            # urllib3 pool without reading the body. Any bytes
            # urllib3 has already prefetched are bounded by its
            # internal buffer (a few KB at most); never the full
            # body the consumer is trying to ship.
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass
