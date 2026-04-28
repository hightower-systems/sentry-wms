"""Retry schedule + terminal-attempt classification (plan §2.4).

Hard-coded eight-slot exponential schedule. Cumulative window
between attempt 1 and the 8th-attempt DLQ flip is ~14.6h (close
to the plan's documented ~15h). An event that fails every
attempt sits in ``webhook_deliveries`` for that window before
terminating in ``dlq`` and the cursor advancing.

Plan §2.4 prescribes no jitter for v1.6.0 (one consumer, one
dispatcher). When v1.9 introduces per-connector dispatcher
pools, jitter becomes the next lever before pool isolation.
The schedule itself is deliberately public: a consumer that
needs to budget its own incident response can plan against
the documented cumulative window.

Schema invariant pairs with this module: migration 030 (#166)
constrains ``webhook_deliveries.attempt_number BETWEEN 1 AND
8``. A code-only refactor that bumped MAX_ATTEMPTS beyond 8
would surface as a CHECK violation at INSERT time -- the
bottom rung that catches the slip before the database is
contaminated with out-of-band rows.

Indexing convention (the part the v1.6.0 D6 review caught):

  * Attempt 1 fires at NOW() with no delay (D5 INSERTs the
    initial pending row at scheduled_at=NOW()).
  * For attempts 2..8, the delay BEFORE the attempt fires is
    ``RETRY_SCHEDULE_SECONDS[next_attempt_number - 1]``.
  * Slot 0 (1 second) is plan-prescribed but currently unused;
    it represents a reserved "dispatcher startup latency
    budget" that v1.6.0 does not consume because the visible_at
    gate (2s) already covers any commit-vs-poll race. Keeping
    the slot in the public tuple preserves byte-for-byte
    fidelity with the plan's §2.4 vector.
  * Slot 7 (12 hours) is the wait BEFORE attempt 8. Without
    this slot the cumulative retry window collapses from ~15h
    to ~2.6h, which would silently shrink consumers' incident-
    response budget.
"""

from typing import Sequence


# Plan §2.4 hard-coded schedule, 8 slots. retry_delay(N) for
# N in [2, 8] returns RETRY_SCHEDULE_SECONDS[N-1] (slots 1..7
# are the delays BEFORE attempts 2..8). Slot 0 is reserved per
# the docstring above.
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


def retry_delay(next_attempt_number: int) -> int:
    """Return the seconds to wait before ``next_attempt_number``.

    ``next_attempt_number`` is the attempt about to be scheduled
    (the value the dispatcher will INSERT into the new
    ``webhook_deliveries`` row's ``attempt_number`` column). For
    an event whose attempt 1 just failed, ``next_attempt_number``
    is 2 and this returns 4 (seconds, plan §2.4 second slot).

    Raises ``ValueError`` for values outside [2, 8] -- attempt 1
    is scheduled at NOW() at emit time (no retry delay), and a
    9th attempt does not exist (the 8th is terminal).
    """
    if next_attempt_number < 2 or next_attempt_number > MAX_ATTEMPTS:
        raise ValueError(
            f"retry_delay only defined for attempt 2..{MAX_ATTEMPTS}; "
            f"got {next_attempt_number}. Attempt 1 fires at NOW() with "
            f"no delay; the 8th attempt is terminal (DLQ on failure)."
        )
    return RETRY_SCHEDULE_SECONDS[next_attempt_number - 1]


def is_terminal_attempt(attempt_number: int) -> bool:
    """True when this attempt's failure terminates in DLQ.

    The dispatcher uses the result to decide between two paths
    after a non-2xx response: insert a fresh retry-slot row
    (False) or flip the current row to ``dlq`` and advance the
    cursor (True).
    """
    return attempt_number >= MAX_ATTEMPTS
