"""v1.10.0 POS shared utilities.

The POS surface has its own body-size cap (separate from the inbound
SENTRY_INBOUND_MAX_BODY_KB and the dockd SENTRY_DOCKD_MAX_BODY_KB)
because POS POST bodies sit between the two: a checkout body with
many cart lines + tender details is bigger than a dockd ship body
but smaller than an inbound canonical-shape upsert. The 256 KB
default refuses abusive clients early without constraining legitimate
multi-line counter sales.

Boot validation lives in app.create_app(); the helper here trusts
the boot guard and reads the env var on each request.
"""

import os


SENTRY_POS_MAX_BODY_KB_DEFAULT = 256
SENTRY_POS_MAX_BODY_KB_FLOOR = 16
SENTRY_POS_MAX_BODY_KB_CEILING = 4096


def get_max_body_kb() -> int:
    """SENTRY_POS_MAX_BODY_KB env var (16..4096; default 256).

    Boot validates the range in app.create_app(); this helper is the
    read-side path the handler uses on each request and trusts the
    boot guard rather than re-clamping silently. A typo'd value would
    have refused to boot the app, so on the request path we just
    int() it.
    """
    return int(
        os.getenv("SENTRY_POS_MAX_BODY_KB", str(SENTRY_POS_MAX_BODY_KB_DEFAULT))
    )


# Lock-timeout / statement-timeout for the atomic POS POSTs (checkout +
# refund). Both are in milliseconds. The defaults match the doc's 2s /
# 4s recommendation; the bounds [100, 30000] keep the operator from
# typo'ing into a posture that either deadlocks the request handler or
# silently waits forever. Boot validation lives in app.create_app().
SENTRY_POS_LOCK_TIMEOUT_MS_DEFAULT      = 2000
SENTRY_POS_STATEMENT_TIMEOUT_MS_DEFAULT = 4000
SENTRY_POS_TIMEOUT_MS_FLOOR             = 100
SENTRY_POS_TIMEOUT_MS_CEILING           = 30000


def lock_timeouts_ms() -> tuple:
    """Return (lock_timeout_ms, statement_timeout_ms) read from env.

    The route applies these via SET LOCAL inside the request transaction
    so a deadlock or runaway query surfaces as a caught LockNotAvailable
    / QueryCanceled (-> 503 lock_contention) rather than blocking the
    request handler indefinitely.
    """
    lock = int(
        os.getenv(
            "SENTRY_POS_LOCK_TIMEOUT_MS",
            str(SENTRY_POS_LOCK_TIMEOUT_MS_DEFAULT),
        )
    )
    stmt = int(
        os.getenv(
            "SENTRY_POS_STATEMENT_TIMEOUT_MS",
            str(SENTRY_POS_STATEMENT_TIMEOUT_MS_DEFAULT),
        )
    )
    return lock, stmt
