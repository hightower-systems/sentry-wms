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
import time
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

# #228: chunk size for the cleanup_webhook_deliveries beat task.
# Pre-#228 the task issued a single DELETE that could span tens of
# millions of rows in one transaction, holding a long lock and
# starving autovacuum. Chunked deletes commit between batches so
# the dispatcher's INSERT path competes with at most one chunk of
# locked rows at a time.
WEBHOOK_DELIVERIES_CLEANUP_CHUNK_SIZE = 1000

# #228: per-run wall-clock cap. A beat misfire backlog (worker
# restart, dropped beats) cannot compound into a multi-hour
# cleanup that monopolizes the table. The next 6-hour beat picks
# up wherever this run stopped.
WEBHOOK_DELIVERIES_CLEANUP_MAX_RUN_S = 600  # 10 minutes


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


def _cleanup_webhook_deliveries_impl(
    session,
    chunk_size: int = WEBHOOK_DELIVERIES_CLEANUP_CHUNK_SIZE,
    max_run_s: float = WEBHOOK_DELIVERIES_CLEANUP_MAX_RUN_S,
) -> int:
    """Delete terminal webhook_deliveries rows past the retention
    window. Pending and in_flight rows are NEVER touched regardless
    of age; those are live state and the dispatcher is the sole
    writer. A row stuck in_flight past the retention window is a
    sign the boot reset was skipped, not a cleanup target.

    #228: chunked deletes with COMMIT between batches. Pre-#228 the
    task issued a single DELETE that could span tens of millions
    of rows in one transaction, holding a long lock and starving
    autovacuum on the table. Chunking keeps each transaction
    short so the dispatcher's per-attempt INSERT path competes
    with at most one chunk of locked rows at a time. The
    DELETE..IN (SELECT..LIMIT) shape is the standard chunked-
    delete pattern; the inner SELECT hits the
    ``webhook_deliveries_pending_idx`` partial-index-friendly path
    via the (status, completed_at) predicate.

    Returns the total number of rows deleted across all chunks.
    Bounded by ``max_run_s`` (default 10 minutes) so a beat
    misfire backlog cannot compound into a multi-hour cleanup
    monopolizing the table; the next 6-hour beat picks up where
    this run stopped.
    """
    cutoff = datetime.now(timezone.utc) - WEBHOOK_DELIVERIES_RETENTION
    deadline = time.monotonic() + max_run_s
    total_deleted = 0
    while True:
        if time.monotonic() >= deadline:
            logger.warning(
                "cleanup_webhook_deliveries hit max_run_s=%.0fs after "
                "deleting %d row(s); the next beat will pick up the "
                "remainder",
                max_run_s,
                total_deleted,
            )
            break
        result = session.execute(
            text(
                """
                DELETE FROM webhook_deliveries
                 WHERE delivery_id IN (
                     SELECT delivery_id FROM webhook_deliveries
                      WHERE status IN ('succeeded', 'dlq')
                        AND completed_at < :cutoff
                      ORDER BY delivery_id
                      LIMIT :chunk
                 )
                """
            ),
            {"cutoff": cutoff, "chunk": chunk_size},
        )
        chunk = result.rowcount or 0
        # Commit between chunks so each batch's row locks release
        # before the next acquires its own. The Celery task wrapper
        # commits at the end too; this commits earlier-than-end.
        session.commit()
        total_deleted += chunk
        if chunk < chunk_size:
            # Short batch means the table is drained; exit clean.
            break
    return total_deleted


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
