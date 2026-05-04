"""Retry schedule + terminal-attempt classification.

Hard-coded eight-slot exponential schedule. Cumulative window
between attempt 1 and the 8th-attempt DLQ flip is ~14.6h
(close to the documented ~15h). An event that fails every
attempt sits in ``webhook_deliveries`` for that window before
terminating in ``dlq`` and the cursor advancing.

#234 (V-316): each retry slot carries a +/-10% jitter applied
per attempt. Without jitter, multiple subscriptions whose first
delivery to the same consumer URL fails at the same minute
retry at the same minute every retry slot, giving the consumer
a synchronized retry storm indistinguishable from a coordinated
DoS. The cumulative window stays inside the documented ~15h
budget (worst case +10% on every slot is still under ~17h, so
the bound is bounded). Per-attempt jitter does NOT decorrelate
adjacent retries on the SAME delivery (one delivery is one
fan-in to one consumer), but it does decorrelate retries
ACROSS deliveries that share the same nominal slot.

The schedule itself is deliberately public: a consumer that
needs to budget its own incident response can plan against the
documented cumulative window.

Schema invariant pairs with this module: migration 030 (#166)
constrains ``webhook_deliveries.attempt_number BETWEEN 1 AND
8``. A code-only refactor that bumped MAX_ATTEMPTS beyond 8
would surface as a CHECK violation at INSERT time -- the
bottom rung that catches the slip before the database is
contaminated with out-of-band rows.

Indexing convention:

  * Attempt 1 fires at NOW() with no delay (D5 INSERTs the
    initial pending row at scheduled_at=NOW()).
  * For attempts 2..8, the delay BEFORE the attempt fires is
    ``RETRY_SCHEDULE_SECONDS[next_attempt_number - 1]``
    multiplied by a per-call jitter factor in [0.9, 1.1].
  * Slot 0 (1 second) is reserved; v1.6.0 does not consume it
    because the visible_at gate (2s) covers commit-vs-poll
    races.
  * Slot 7 (12 hours) is the wait BEFORE attempt 8.
"""

import secrets
from typing import Sequence


# Hard-coded schedule, 8 slots. retry_delay(N) for N in [2, 8]
# returns RETRY_SCHEDULE_SECONDS[N-1] times a per-call jitter
# factor (slots 1..7 are the delays BEFORE attempts 2..8).
RETRY_SCHEDULE_SECONDS: Sequence[int] = (
    1,           # slot 0: reserved (attempt 1 fires at NOW())
    4,           # slot 1: delay before attempt 2 (4 seconds)
    15,          # slot 2: delay before attempt 3
    60,          # slot 3: delay before attempt 4 (1 minute)
    5 * 60,      # slot 4: delay before attempt 5 (5 minutes)
    30 * 60,     # slot 5: delay before attempt 6 (30 minutes)
    2 * 3600,    # slot 6: delay before attempt 7 (2 hours)
    12 * 3600,   # slot 7: delay before attempt 8 (12 hours)
)

MAX_ATTEMPTS = 8

# #234 (V-316): jitter band for per-attempt randomization.
# +/-10% keeps the cumulative window inside the documented ~15h
# budget (worst case 1.1 ** 7 * sum(slots) is still under 17h).
# Exposed as a constant so the test can pin the bounds and a
# future expansion to broader jitter is a one-line change.
_JITTER_MIN = 0.9
_JITTER_MAX = 1.1

# secrets.SystemRandom uses os.urandom under the hood, giving
# non-predictability across processes (a deterministic random.Random
# seeded from time would let a single observation predict subsequent
# slot offsets). Per-process singleton so the seeding cost is paid
# once at import.
_JITTER_RNG = secrets.SystemRandom()


def retry_delay(next_attempt_number: int) -> int:
    """Return the seconds to wait before ``next_attempt_number``,
    with +/-10% jitter applied per call (#234).

    ``next_attempt_number`` is the attempt about to be scheduled
    (the value the dispatcher will INSERT into the new
    ``webhook_deliveries`` row's ``attempt_number`` column). For
    an event whose attempt 1 just failed, ``next_attempt_number``
    is 2 and this returns ~4 seconds (the second-slot value
    perturbed by [0.9, 1.1]).

    Raises ``ValueError`` for values outside [2, 8] -- attempt 1
    is scheduled at NOW() at emit time (no retry delay), and a
    9th attempt does not exist (the 8th is terminal).

    The minimum returned value is 1 second so a chance jitter
    on the slot-1 (4s) value cannot land below the dispatcher's
    poll cadence. A future schedule with sub-second slots would
    need to revisit the floor.
    """
    if next_attempt_number < 2 or next_attempt_number > MAX_ATTEMPTS:
        raise ValueError(
            f"retry_delay only defined for attempt 2..{MAX_ATTEMPTS}; "
            f"got {next_attempt_number}. Attempt 1 fires at NOW() with "
            f"no delay; the 8th attempt is terminal (DLQ on failure)."
        )
    base = RETRY_SCHEDULE_SECONDS[next_attempt_number - 1]
    if base == 0:
        # Preserve the zero-delay shape used by tests that
        # monkey-patch RETRY_SCHEDULE_SECONDS to ``(0,) * 8`` to
        # avoid waiting through the real schedule. Production
        # slots are never 0; this branch is test-fixture support.
        return 0
    jitter_factor = _JITTER_RNG.uniform(_JITTER_MIN, _JITTER_MAX)
    return max(1, int(base * jitter_factor))


def is_terminal_attempt(attempt_number: int) -> bool:
    """True when this attempt's failure terminates in DLQ.

    The dispatcher uses the result to decide between two paths
    after a non-2xx response: insert a fresh retry-slot row
    (False) or flip the current row to ``dlq`` and advance the
    cursor (True).
    """
    return attempt_number >= MAX_ATTEMPTS
