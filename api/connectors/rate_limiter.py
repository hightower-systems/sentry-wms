"""Rate limiting, exponential backoff, and circuit breaker utilities.

Every connector inherits these behaviors via BaseConnector.make_request,
so individual connectors don't need to implement retry or throttling.

Three layers of defense against flaky external APIs:

1. Exponential backoff with jitter: on 429/503, wait (2^attempt)*base + jitter
   and retry up to 3 times per HTTP call. Jitter prevents thundering herd
   when multiple celery workers retry simultaneously.

2. Rate limit headers: after each response, read X-RateLimit-Remaining.
   When remaining drops below a threshold (10% of limit), add a small
   proactive delay before the next call. Respects Retry-After when present.

3. Circuit breaker: after 5 consecutive failures, open the circuit for
   5 minutes. All calls during cooldown fail fast with CircuitOpenError.
   After cooldown, the next call is half-open: success closes the circuit,
   failure re-opens it for another cooldown.
"""

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


class CircuitOpenError(Exception):
    """Raised when a request is attempted while the circuit breaker is open.

    Callers should catch this and treat it as a terminal failure for the
    current operation. The circuit will reset after the cooldown period.
    """


# ---------------------------------------------------------------------------
# Exponential backoff with jitter
# ---------------------------------------------------------------------------


def exponential_backoff(attempt: int, base_delay: float = 1.0, max_jitter: float = 1.0) -> float:
    """Return seconds to wait before retrying.

    attempt is 0-indexed:
        attempt=0 -> ~1s (1*base + [0, max_jitter])
        attempt=1 -> ~2s
        attempt=2 -> ~4s

    Random jitter prevents correlated retries across multiple workers.
    """
    return (2 ** attempt) * base_delay + random.uniform(0, max_jitter)


# Max per-call retries on 429/503 before giving up. This is separate from
# the celery task-level retry configured on @celery_app.task.
MAX_RETRIES_PER_CALL = 3


# ---------------------------------------------------------------------------
# Rate limit state (per-connector-instance)
# ---------------------------------------------------------------------------


@dataclass
class RateLimitState:
    """Tracks the latest rate limit headers from an external API.

    Updated after every response. Used to decide whether to slow down
    proactively before the next call.
    """

    remaining: Optional[int] = None
    limit: Optional[int] = None
    retry_after: Optional[float] = None  # seconds to wait, from Retry-After

    def update_from_response(self, response, remaining_header: str, limit_header: str, retry_after_header: str) -> None:
        """Parse rate limit headers from a requests.Response."""
        headers = response.headers

        raw_remaining = headers.get(remaining_header)
        if raw_remaining is not None:
            try:
                self.remaining = int(raw_remaining)
            except (ValueError, TypeError):
                pass

        raw_limit = headers.get(limit_header)
        if raw_limit is not None:
            try:
                self.limit = int(raw_limit)
            except (ValueError, TypeError):
                pass

        raw_retry = headers.get(retry_after_header)
        if raw_retry is not None:
            try:
                self.retry_after = float(raw_retry)
            except (ValueError, TypeError):
                pass

    def compute_slowdown(self, slowdown_threshold: float) -> float:
        """Return seconds to wait proactively before the next call.

        Returns 0 when we have plenty of headroom. Returns a small delay
        when X-RateLimit-Remaining is below slowdown_threshold * limit.
        """
        if self.remaining is None or self.limit is None or self.limit <= 0:
            return 0.0
        if self.remaining / self.limit > slowdown_threshold:
            return 0.0
        # Scale delay: closer to zero remaining = longer delay, max 2s
        ratio = self.remaining / self.limit
        return max(0.1, 2.0 * (1.0 - ratio / slowdown_threshold))


# ---------------------------------------------------------------------------
# Circuit breaker (per-connector-instance)
# ---------------------------------------------------------------------------


@dataclass
class CircuitBreakerState:
    """Tracks consecutive failures and cooldown for a single connector.

    States:
        closed    - normal operation, failures counted
        open      - cooldown in progress, all calls fail fast
        half_open - cooldown expired, next call decides (success closes, failure re-opens)
    """

    threshold: int = 5                  # failures before opening
    cooldown_seconds: int = 300         # 5 minutes
    failures: int = 0
    opened_at: Optional[float] = None

    def check(self) -> None:
        """Raise CircuitOpenError if the circuit is open and cooldown hasn't expired.

        Transitions to half-open (allowing one call through) when cooldown expires.
        """
        if self.opened_at is None:
            return  # closed

        elapsed = time.monotonic() - self.opened_at
        if elapsed < self.cooldown_seconds:
            remaining = int(self.cooldown_seconds - elapsed)
            raise CircuitOpenError(
                f"Circuit breaker open, {remaining}s remaining in cooldown"
            )

        # Cooldown expired - let one call through (half-open).
        # opened_at stays set until the call resolves, so concurrent calls
        # still see the breaker as armed. record_success will reset it.
        logger.info("Circuit breaker entering half-open state")

    def record_success(self) -> None:
        """Reset the breaker on any successful call."""
        if self.opened_at is not None:
            logger.info("Circuit breaker closed after successful call")
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        """Increment failure count and open the circuit if threshold reached."""
        self.failures += 1
        if self.failures >= self.threshold and self.opened_at is None:
            self.opened_at = time.monotonic()
            logger.warning(
                "Circuit breaker opened after %d consecutive failures", self.failures,
            )
        elif self.opened_at is not None:
            # Half-open call failed - reset the cooldown timer
            self.opened_at = time.monotonic()
            logger.warning("Circuit breaker re-opened after half-open call failed")

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None
