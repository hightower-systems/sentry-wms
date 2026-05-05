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

v1.7.0 adds the Pipe B inbound source_payload retention task (R6).
inbound_<resource>.source_payload is the original consumer-shaped JSON
the canonical row was derived from. After
SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS (default 90, hard floor
7 days; V-201 shape) the retention task NULLs out source_payload while
preserving canonical_payload, dropping the bulk of per-row size while
keeping the canonical history queryable. One inbound_cleanup_runs row
per (resource, run) tuple records the start/finish/rows_nullified so
operators can detect a partial-failure shape (one resource succeeds,
another aborts on a lock-timeout).
"""

import logging
import os
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


# ============================================================
# v1.7.0 inbound source_payload retention (R6)
# ============================================================


# Hard floor: a typo'd or zero retention would silently wipe forensic
# context. V-201 shape -- the validator below rejects any value < 7
# days (the operator must edit a runbook variable to drop it lower,
# at which point the typo defense isn't the point any more).
INBOUND_SOURCE_PAYLOAD_RETENTION_FLOOR_DAYS = 7
INBOUND_SOURCE_PAYLOAD_RETENTION_DEFAULT_DAYS = 90

# Per-resource chunk size + wall-clock cap, mirroring #228 webhook
# pattern. A beat misfire backlog cannot compound into a multi-hour
# UPDATE that monopolizes the table; the next 24-hour beat picks up.
INBOUND_RETENTION_CHUNK_SIZE = 1000
INBOUND_RETENTION_MAX_RUN_S = 600  # per resource

_INBOUND_TABLES = (
    "inbound_sales_orders",
    "inbound_items",
    "inbound_customers",
    "inbound_vendors",
    "inbound_purchase_orders",
)


def get_inbound_retention_days() -> int:
    """Read SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS with the
    7-day floor enforced at read time. Boot validation runs separately
    in app.create_app() so misconfigured deployments fail loud at boot;
    this helper is the worker-side last-line that ensures even a
    runtime env tweak cannot push retention below the floor.

    Returns the configured days clamped to [floor, +inf). Invalid /
    unset values fall back to the default (90)."""
    raw = os.environ.get("SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS")
    if raw is None or raw.strip() == "":
        return INBOUND_SOURCE_PAYLOAD_RETENTION_DEFAULT_DAYS
    try:
        days = int(raw)
    except ValueError:
        logger.warning(
            "SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS=%r is not an integer; "
            "falling back to default %d",
            raw, INBOUND_SOURCE_PAYLOAD_RETENTION_DEFAULT_DAYS,
        )
        return INBOUND_SOURCE_PAYLOAD_RETENTION_DEFAULT_DAYS
    if days < INBOUND_SOURCE_PAYLOAD_RETENTION_FLOOR_DAYS:
        logger.error(
            "SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS=%d below "
            "floor %d; clamping. Boot guard should have caught this.",
            days, INBOUND_SOURCE_PAYLOAD_RETENTION_FLOOR_DAYS,
        )
        return INBOUND_SOURCE_PAYLOAD_RETENTION_FLOOR_DAYS
    return days


def _nullify_one_resource(session, table: str, retention_days: int) -> int:
    """NULL source_payload on rows older than the cutoff. Chunked
    UPDATEs commit between batches so concurrent inbound POSTs hit at
    most one chunk's worth of contended rows at a time. Returns the
    total nullified row count."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    started = time.monotonic()
    total = 0
    while True:
        if time.monotonic() - started > INBOUND_RETENTION_MAX_RUN_S:
            logger.warning(
                "inbound retention task hit per-resource wall-clock cap on %s "
                "after nullifying %d rows; next beat run resumes.", table, total,
            )
            break
        # ctid-based chunking is the standard PostgreSQL pattern for
        # bounded UPDATEs that can resume between beats. The partial
        # index on (status, received_at) covers the WHERE selection.
        result = session.execute(
            text(
                f"WITH targets AS ( "
                f"  SELECT inbound_id FROM {table} "
                f"   WHERE received_at < :cutoff "
                f"     AND source_payload IS NOT NULL "
                f"   ORDER BY inbound_id "
                f"   LIMIT :chunk "
                f") "
                f"UPDATE {table} SET source_payload = NULL "
                f" WHERE inbound_id IN (SELECT inbound_id FROM targets)"
            ),
            {"cutoff": cutoff, "chunk": INBOUND_RETENTION_CHUNK_SIZE},
        )
        rowcount = result.rowcount or 0
        session.commit()
        total += rowcount
        if rowcount < INBOUND_RETENTION_CHUNK_SIZE:
            break
    return total


def _cleanup_inbound_source_payload_impl(session, retention_days: int) -> dict:
    """Iterate the five staging tables, nullifying old source_payload
    on each. One inbound_cleanup_runs row per (resource, run) so a
    partial failure (one resource aborts; others succeed) is visible
    without inferring it from missing rows."""
    summary: dict[str, dict] = {}
    for table in _INBOUND_TABLES:
        resource = table.removeprefix("inbound_")
        # log the run start
        run_row = session.execute(
            text(
                "INSERT INTO inbound_cleanup_runs "
                "  (resource, retention_days, status) "
                "VALUES (:r, :rd, 'running') RETURNING run_id"
            ),
            {"r": resource, "rd": retention_days},
        ).fetchone()
        run_id = run_row.run_id
        session.commit()
        try:
            nullified = _nullify_one_resource(session, table, retention_days)
            session.execute(
                text(
                    "UPDATE inbound_cleanup_runs "
                    "   SET status = 'succeeded', finished_at = NOW(), "
                    "       rows_nullified = :n "
                    " WHERE run_id = :rid"
                ),
                {"n": nullified, "rid": run_id},
            )
            session.commit()
            summary[resource] = {"nullified": nullified, "status": "succeeded"}
        except Exception as exc:
            # Mark this resource as failed but keep iterating: a
            # lock-timeout on one table should not stop retention on
            # the other four. Operators investigate via the
            # inbound_cleanup_runs log.
            session.rollback()
            session.execute(
                text(
                    "UPDATE inbound_cleanup_runs "
                    "   SET status = 'failed', finished_at = NOW(), "
                    "       error_message = :em "
                    " WHERE run_id = :rid"
                ),
                {"em": str(exc)[:500], "rid": run_id},
            )
            session.commit()
            logger.exception(
                "inbound retention failed for %s; continuing", resource,
            )
            summary[resource] = {"status": "failed", "error": str(exc)[:200]}
    return summary


@celery_app.task
def cleanup_inbound_source_payload() -> dict:
    """v1.7.0 R6: NULL source_payload on inbound_<resource> rows older
    than SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS while preserving
    canonical_payload. Returns the per-resource summary so operators
    can confirm the task is running.

    Idempotent: a row already nullified is filtered out by
    `source_payload IS NOT NULL`. Running the task twice in succession
    has no second-pass effect."""
    import models.database as db
    retention_days = get_inbound_retention_days()
    session = db.SessionLocal()
    try:
        summary = _cleanup_inbound_source_payload_impl(session, retention_days)
        logger.info(
            "cleanup_inbound_source_payload retention=%dd summary=%s",
            retention_days, summary,
        )
        return {"retention_days": retention_days, "per_resource": summary}
    finally:
        session.close()
