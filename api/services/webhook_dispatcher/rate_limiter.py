"""Per-subscription token bucket rate limiter.

One bucket per SubscriptionWorker. Capacity equals
``rate_limit_per_second`` and the bucket refills at that rate, so a
freshly created bucket has a full burst of ``rate`` tokens that
amortizes to the configured steady-state rate.

The bucket reconciles on rate change via :meth:`set_rate`, which is
called from the dispatch loop after the subscription row is read.
The cross-worker ``rate_limit_changed`` pubsub event flows through
the existing wake fanout; the next deliver_one cycle observes the
new rate via the row read and the bucket adapts in place.
"""

import threading
import time
from typing import Optional


class TokenBucket:
    """Thread-safe token bucket. ``acquire`` blocks until a token
    is available, the deadline elapses, or the optional shutdown
    event fires.

    The clock is :func:`time.monotonic` so a wall-clock jump cannot
    grant or steal tokens.
    """

    def __init__(self, rate_per_second: int):
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be a positive integer")
        self._rate = float(rate_per_second)
        self._capacity = float(rate_per_second)
        self._tokens = float(rate_per_second)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    @property
    def rate(self) -> int:
        return int(self._rate)

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last_refill = now

    def set_rate(self, rate_per_second: int) -> None:
        """Reconcile the bucket to a new rate. No-op when the rate
        is unchanged. On change the capacity is reset to the new
        value, the current token count is clamped to the new
        capacity, and the refill timer is reset so the next acquire
        does not get a windfall from the old rate."""
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be a positive integer")
        with self._lock:
            if int(self._rate) == rate_per_second:
                return
            self._rate = float(rate_per_second)
            self._capacity = float(rate_per_second)
            if self._tokens > self._capacity:
                self._tokens = self._capacity
            self._last_refill = time.monotonic()

    def acquire(
        self,
        timeout_s: float = 5.0,
        shutdown: Optional[threading.Event] = None,
    ) -> bool:
        """Take one token. Blocks up to ``timeout_s`` waiting for
        a refill. Returns False on timeout or when ``shutdown``
        fires. Returns True when a token was successfully consumed.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            if shutdown is not None and shutdown.is_set():
                return False
            with self._lock:
                self._refill_locked()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                deficit = 1.0 - self._tokens
                wait_for_one = deficit / self._rate
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            sleep_for = min(wait_for_one, remaining, 0.5)
            if shutdown is not None:
                if shutdown.wait(timeout=sleep_for):
                    return False
            else:
                time.sleep(sleep_for)
