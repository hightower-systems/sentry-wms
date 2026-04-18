"""Sync state tracking for connector health monitoring.

Tracks the status of each sync operation (orders, items, inventory,
fulfillment) per connector+warehouse. Admins use this to see whether
connectors are healthy; operators see alerts when a connector stops
working so they can address it before the warehouse floor notices.

Uses a strict state machine to prevent duplicate sync runs: if a sync
is already 'running', calling set_running again raises DuplicateRunError
so two celery workers can't process the same sync simultaneously.

V-102: a stale 'running' row can be taken over after RUNNING_TIMEOUT
(see V-012). Each transition into 'running' mints a new run_id UUID.
Standalone completion paths (set_success_standalone / set_error_standalone)
accept the run_id they started with and only write if the row's
current run_id still matches. If it does not, the row has been taken
over by a newer run and the stale completion no-ops; without this
guard the original worker's set_success would clobber the new worker's
'running' row back to 'idle'.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from flask import g
from sqlalchemy import text


# Valid sync_type values - keep in sync with the spec
VALID_SYNC_TYPES = ("orders", "items", "inventory", "fulfillment")

# Consecutive errors before flipping status to 'error' (sticky)
ERROR_THRESHOLD = 3

# V-012: a 'running' row older than this is considered stale (worker
# crashed or was killed mid-sync) and a new run is allowed to take over.
RUNNING_TIMEOUT = timedelta(hours=1)


class DuplicateRunError(Exception):
    """Raised when set_running is called while a sync is already running.

    The celery task should catch this and skip retry (a retry would
    just hit the same condition). The running task will complete or
    fail on its own.
    """


def _row_to_dict(row):
    """Convert a SQLAlchemy row to a plain dict for JSON serialization."""
    if not row:
        return None
    return {
        "connector_name": row.connector_name,
        "warehouse_id": row.warehouse_id,
        "sync_type": row.sync_type,
        "sync_status": row.sync_status,
        "last_synced_at": row.last_synced_at.isoformat() if row.last_synced_at else None,
        "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
        "last_error_at": row.last_error_at.isoformat() if row.last_error_at else None,
        "last_error_message": row.last_error_message,
        "consecutive_errors": row.consecutive_errors,
    }


def _execute(session, stmt, params):
    """Run a statement against either g.db (if provided) or the session."""
    return session.execute(stmt, params)


def get_sync_state(connector_name: str, warehouse_id: int, sync_type: str) -> Optional[dict]:
    """Fetch the sync state for one connector+warehouse+type. Returns None if not yet tracked."""
    row = g.db.execute(
        text("""
            SELECT connector_name, warehouse_id, sync_type, sync_status,
                   last_synced_at, last_success_at, last_error_at,
                   last_error_message, consecutive_errors
            FROM sync_state
            WHERE connector_name = :name AND warehouse_id = :wid AND sync_type = :type
        """),
        {"name": connector_name, "wid": warehouse_id, "type": sync_type},
    ).fetchone()
    return _row_to_dict(row)


def get_all_sync_states(connector_name: str, warehouse_id: int) -> list[dict]:
    """Fetch all sync states for a connector+warehouse."""
    rows = g.db.execute(
        text("""
            SELECT connector_name, warehouse_id, sync_type, sync_status,
                   last_synced_at, last_success_at, last_error_at,
                   last_error_message, consecutive_errors
            FROM sync_state
            WHERE connector_name = :name AND warehouse_id = :wid
            ORDER BY sync_type
        """),
        {"name": connector_name, "wid": warehouse_id},
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _set_running_impl(session, connector_name: str, warehouse_id: int, sync_type: str) -> str:
    """Shared implementation of set_running that works with any session.

    V-012: a row stuck in 'running' past RUNNING_TIMEOUT is considered
    stale (crashed worker) and a new run takes over. Without the
    timeout a single crash blocked all future syncs indefinitely.

    V-102: every transition into 'running' mints a new UUID and stores
    it in the run_id column. Callers carry the returned UUID forward
    to set_success_standalone / set_error_standalone so their
    completion is conditional on still being the active run.

    Returns the run_id (string UUID) the caller should pass to the
    eventual success or error transition.
    """
    row = session.execute(
        text("""
            SELECT sync_status, running_since FROM sync_state
            WHERE connector_name = :name AND warehouse_id = :wid AND sync_type = :type
        """),
        {"name": connector_name, "wid": warehouse_id, "type": sync_type},
    ).fetchone()

    if row and row.sync_status == "running":
        cutoff = datetime.now(timezone.utc) - RUNNING_TIMEOUT
        # If running_since is set and older than the cutoff, allow takeover.
        # If running_since is NULL (pre-migration data) treat as fresh -- the
        # backfill sets a value on deploy; new inserts set it below.
        if row.running_since is None or row.running_since > cutoff:
            raise DuplicateRunError(
                f"Sync already running: {connector_name}/{warehouse_id}/{sync_type}"
            )

    new_run_id = str(uuid.uuid4())
    session.execute(
        text("""
            INSERT INTO sync_state (connector_name, warehouse_id, sync_type,
                                    sync_status, running_since, run_id, updated_at)
            VALUES (:name, :wid, :type, 'running', NOW(), :run_id, NOW())
            ON CONFLICT (connector_name, warehouse_id, sync_type)
            DO UPDATE SET sync_status = 'running',
                          running_since = NOW(),
                          run_id = :run_id,
                          updated_at = NOW()
        """),
        {"name": connector_name, "wid": warehouse_id, "type": sync_type, "run_id": new_run_id},
    )
    return new_run_id


def set_running(connector_name: str, warehouse_id: int, sync_type: str) -> str:
    """Mark a sync as running. Raises DuplicateRunError if already running.

    Uses g.db (Flask request context). Returns the run_id UUID (see
    V-102). Flask-context callers may ignore the return value because
    the flask path is single-worker per request and has no race with
    Celery takeovers; the UUID is still minted so the row's run_id
    column stays non-NULL.
    """
    return _set_running_impl(g.db, connector_name, warehouse_id, sync_type)


def set_success(connector_name: str, warehouse_id: int, sync_type: str) -> None:
    """Mark a sync as succeeded: status=idle, update last_success_at, reset consecutive_errors.

    Uses g.db (Flask request context). Clears run_id because the
    completed run is no longer active (V-102).
    """
    now = datetime.now(timezone.utc)
    g.db.execute(
        text("""
            INSERT INTO sync_state (connector_name, warehouse_id, sync_type, sync_status,
                                     running_since, run_id, last_synced_at, last_success_at,
                                     consecutive_errors, updated_at)
            VALUES (:name, :wid, :type, 'idle', NULL, NULL, :now, :now, 0, :now)
            ON CONFLICT (connector_name, warehouse_id, sync_type)
            DO UPDATE SET sync_status = 'idle',
                          running_since = NULL,
                          run_id = NULL,
                          last_synced_at = :now,
                          last_success_at = :now,
                          consecutive_errors = 0,
                          updated_at = :now
        """),
        {"name": connector_name, "wid": warehouse_id, "type": sync_type, "now": now},
    )


def set_error(connector_name: str, warehouse_id: int, sync_type: str, error_message: str) -> None:
    """Record a sync error: increment consecutive_errors, set status='error' once threshold reached.

    Uses g.db (Flask request context). Clears run_id because the
    failed run is no longer active (V-102).
    """
    now = datetime.now(timezone.utc)
    # Look up current error count
    row = g.db.execute(
        text("""
            SELECT consecutive_errors FROM sync_state
            WHERE connector_name = :name AND warehouse_id = :wid AND sync_type = :type
        """),
        {"name": connector_name, "wid": warehouse_id, "type": sync_type},
    ).fetchone()
    current_errors = row.consecutive_errors if row else 0
    new_errors = current_errors + 1
    new_status = "error" if new_errors >= ERROR_THRESHOLD else "idle"

    g.db.execute(
        text("""
            INSERT INTO sync_state (connector_name, warehouse_id, sync_type, sync_status,
                                     running_since, run_id, last_synced_at, last_error_at,
                                     last_error_message, consecutive_errors, updated_at)
            VALUES (:name, :wid, :type, :status, NULL, NULL, :now, :now, :msg, :errors, :now)
            ON CONFLICT (connector_name, warehouse_id, sync_type)
            DO UPDATE SET sync_status = :status,
                          running_since = NULL,
                          run_id = NULL,
                          last_synced_at = :now,
                          last_error_at = :now,
                          last_error_message = :msg,
                          consecutive_errors = :errors,
                          updated_at = :now
        """),
        {
            "name": connector_name, "wid": warehouse_id, "type": sync_type,
            "status": new_status, "now": now, "msg": error_message, "errors": new_errors,
        },
    )


def reset_running(connector_name: str, warehouse_id: int, sync_type: Optional[str] = None) -> int:
    """V-012: admin-triggered reset of stuck 'running' rows to 'idle'.

    V-102: also clears run_id so the stuck worker's eventual completion
    (if it arrives) no-ops against the cleared row instead of flipping
    it back to idle -- after a reset, only a future set_running call
    with a fresh run_id may update this row.

    If sync_type is None, all sync types for the connector+warehouse are
    reset. Returns the number of rows updated. Rows that are not currently
    running are untouched.
    """
    if sync_type is None:
        result = g.db.execute(
            text("""
                UPDATE sync_state
                SET sync_status = 'idle', running_since = NULL, run_id = NULL, updated_at = NOW()
                WHERE connector_name = :name AND warehouse_id = :wid
                  AND sync_status = 'running'
            """),
            {"name": connector_name, "wid": warehouse_id},
        )
    else:
        result = g.db.execute(
            text("""
                UPDATE sync_state
                SET sync_status = 'idle', running_since = NULL, run_id = NULL, updated_at = NOW()
                WHERE connector_name = :name AND warehouse_id = :wid
                  AND sync_type = :type AND sync_status = 'running'
            """),
            {"name": connector_name, "wid": warehouse_id, "type": sync_type},
        )
    return result.rowcount


# ---------------------------------------------------------------------------
# Standalone variants for use outside Flask request context (Celery tasks)
# ---------------------------------------------------------------------------


def _standalone_session():
    """Create a new SQLAlchemy session bound to the global engine.

    Celery tasks run outside Flask's request context and don't have
    access to g.db, so they need their own session.
    """
    import models.database as db
    return db.SessionLocal()


def set_running_standalone(connector_name: str, warehouse_id: int, sync_type: str) -> str:
    """Standalone set_running for Celery tasks. Commits on success.

    V-102: returns the run_id the caller must thread through to
    set_success_standalone / set_error_standalone.
    """
    session = _standalone_session()
    try:
        run_id = _set_running_impl(session, connector_name, warehouse_id, sync_type)
        session.commit()
        return run_id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def set_success_standalone(
    connector_name: str, warehouse_id: int, sync_type: str, run_id: str
) -> bool:
    """Standalone set_success for Celery tasks. Commits on success.

    V-102: applies the transition only when the row's current run_id
    matches the caller's run_id. Returns True if the transition was
    applied, False if the row has been taken over by a newer run (the
    stale worker's completion must not clobber the new run's state).
    """
    session = _standalone_session()
    try:
        now = datetime.now(timezone.utc)
        result = session.execute(
            text("""
                UPDATE sync_state
                SET sync_status = 'idle',
                    running_since = NULL,
                    run_id = NULL,
                    last_synced_at = :now,
                    last_success_at = :now,
                    consecutive_errors = 0,
                    updated_at = :now
                WHERE connector_name = :name
                  AND warehouse_id = :wid
                  AND sync_type = :type
                  AND run_id = :run_id
            """),
            {
                "name": connector_name, "wid": warehouse_id, "type": sync_type,
                "now": now, "run_id": run_id,
            },
        )
        session.commit()
        return result.rowcount > 0
    finally:
        session.close()


def set_error_standalone(
    connector_name: str, warehouse_id: int, sync_type: str, error_message: str, run_id: str
) -> bool:
    """Standalone set_error for Celery tasks. Commits on success.

    V-102: applies the transition only when the row's current run_id
    matches the caller's run_id. Returns True if applied, False if
    taken over.
    """
    session = _standalone_session()
    try:
        now = datetime.now(timezone.utc)
        row = session.execute(
            text("""
                SELECT consecutive_errors FROM sync_state
                WHERE connector_name = :name AND warehouse_id = :wid AND sync_type = :type
                  AND run_id = :run_id
            """),
            {
                "name": connector_name, "wid": warehouse_id, "type": sync_type,
                "run_id": run_id,
            },
        ).fetchone()
        if row is None:
            # Taken over by a newer run; stale completion must not write.
            session.commit()
            return False
        current_errors = row.consecutive_errors
        new_errors = current_errors + 1
        new_status = "error" if new_errors >= ERROR_THRESHOLD else "idle"

        result = session.execute(
            text("""
                UPDATE sync_state
                SET sync_status = :status,
                    running_since = NULL,
                    run_id = NULL,
                    last_synced_at = :now,
                    last_error_at = :now,
                    last_error_message = :msg,
                    consecutive_errors = :errors,
                    updated_at = :now
                WHERE connector_name = :name
                  AND warehouse_id = :wid
                  AND sync_type = :type
                  AND run_id = :run_id
            """),
            {
                "name": connector_name, "wid": warehouse_id, "type": sync_type,
                "status": new_status, "now": now, "msg": error_message, "errors": new_errors,
                "run_id": run_id,
            },
        )
        session.commit()
        return result.rowcount > 0
    finally:
        session.close()


def get_last_success_standalone(connector_name: str, warehouse_id: int, sync_type: str) -> Optional[datetime]:
    """Fetch the last_success_at timestamp for a sync type. Used to pass 'since' to connectors."""
    session = _standalone_session()
    try:
        row = session.execute(
            text("""
                SELECT last_success_at FROM sync_state
                WHERE connector_name = :name AND warehouse_id = :wid AND sync_type = :type
            """),
            {"name": connector_name, "wid": warehouse_id, "type": sync_type},
        ).fetchone()
        return row.last_success_at if row else None
    finally:
        session.close()
