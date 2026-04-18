"""
V-024: periodic cleanup of ephemeral tables.

login_attempts accumulates one row per unique rate-limit key (user or
IP). Without a cleanup job the table grows unbounded under a spraying
attack. This task runs on the Celery beat schedule and deletes rows
older than 1 hour (beyond the lockout window).
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from jobs import celery_app

logger = logging.getLogger(__name__)

# Keep slightly longer than the lockout window (15 min) so operators
# still see recent attempt counts during an investigation.
LOGIN_ATTEMPTS_RETENTION = timedelta(hours=1)


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
