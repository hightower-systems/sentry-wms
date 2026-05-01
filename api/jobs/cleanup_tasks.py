"""
V-024: periodic cleanup of ephemeral tables.

login_attempts accumulates one row per unique rate-limit key (user or
IP). Without a cleanup job the table grows unbounded under a spraying
attack. This task runs on the Celery beat schedule and deletes rows
older than 1 hour (beyond the lockout window).

v1.6.0 adds two webhook tasks: a 90-day retention sweep on terminal
``webhook_deliveries`` rows and an hourly prune of expired
``webhook_secrets`` (generation=2 rows whose dual-accept window has
ended).
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from jobs import celery_app

logger = logging.getLogger(__name__)

# Keep slightly longer than the lockout window (15 min) so operators
# still see recent attempt counts during an investigation.
LOGIN_ATTEMPTS_RETENTION = timedelta(hours=1)

# Operational floor for post-incident forensics on webhook delivery
# attempts. The audit_log row stays put regardless; this only prunes
# the per-attempt webhook_deliveries row past the window.
WEBHOOK_DELIVERIES_RETENTION = timedelta(days=90)


def _cleanup_login_attempts_impl(session) -> int:
    cutoff = datetime.now(timezone.utc) - LOGIN_ATTEMPTS_RETENTION
    result = session.execute(
        text(
            "DELETE FROM login_attempts "
            "WHERE last_attempt < :cutoff "
            "AND (locked_until IS NULL OR locked_until < :cutoff)"
        ),
        {"cutoff": cutoff},
    )
    return result.rowcount or 0


def _cleanup_webhook_deliveries_impl(session) -> int:
    """Delete terminal webhook_deliveries rows past the retention
    window. Pending and in_flight rows are NEVER touched regardless
    of age; those are live state and the dispatcher is the sole
    writer. A row stuck in_flight past the retention window is a
    sign the boot reset was skipped, not a cleanup target."""
    cutoff = datetime.now(timezone.utc) - WEBHOOK_DELIVERIES_RETENTION
    result = session.execute(
        text(
            """
            DELETE FROM webhook_deliveries
             WHERE status IN ('succeeded', 'dlq')
               AND completed_at < :cutoff
            """
        ),
        {"cutoff": cutoff},
    )
    return result.rowcount or 0


def _cleanup_expired_webhook_secrets_impl(session) -> int:
    """Delete generation=2 webhook_secrets rows whose expires_at has
    passed. The 24h dual-accept window is over by then; consumers
    who have not switched have already seen sustained reject
    behavior. Generation=1 rows are never pruned (they are the
    active signing key); a generation=2 row with NULL expires_at is
    operator error, not a target."""
    now = datetime.now(timezone.utc)
    result = session.execute(
        text(
            """
            DELETE FROM webhook_secrets
             WHERE generation = 2
               AND expires_at IS NOT NULL
               AND expires_at < :now
            """
        ),
        {"now": now},
    )
    return result.rowcount or 0


@celery_app.task
def cleanup_webhook_deliveries() -> dict:
    """Delete terminal webhook_deliveries past the 90-day window."""
    import models.database as db
    session = db.SessionLocal()
    try:
        deleted = _cleanup_webhook_deliveries_impl(session)
        session.commit()
        logger.info("cleanup_webhook_deliveries deleted %d row(s)", deleted)
        return {"deleted": deleted}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task
def cleanup_expired_webhook_secrets() -> dict:
    """Prune generation=2 webhook_secrets whose dual-accept window
    has ended."""
    import models.database as db
    session = db.SessionLocal()
    try:
        deleted = _cleanup_expired_webhook_secrets_impl(session)
        session.commit()
        logger.info(
            "cleanup_expired_webhook_secrets deleted %d row(s)", deleted
        )
        return {"deleted": deleted}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task
def cleanup_login_attempts() -> dict:
    """Delete login_attempts rows older than LOGIN_ATTEMPTS_RETENTION.

    Called by Celery beat on a recurring schedule. Returns a dict with
    the deletion count so operators can confirm the task is running.
    """
    import models.database as db
    session = db.SessionLocal()
    try:
        deleted = _cleanup_login_attempts_impl(session)
        session.commit()
        logger.info("cleanup_login_attempts deleted %d stale rows", deleted)
        return {"deleted": deleted}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
