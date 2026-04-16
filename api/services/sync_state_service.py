"""Sync state tracking for connector health monitoring.

Tracks the status of each sync operation (orders, items, inventory,
fulfillment) per connector+warehouse. Admins use this to see whether
connectors are healthy; operators see alerts when a connector stops
working so they can address it before the warehouse floor notices.

Uses a strict state machine to prevent duplicate sync runs: if a sync
is already 'running', calling set_running again raises DuplicateRunError
so two celery workers can't process the same sync simultaneously.
"""

from datetime import datetime, timezone
from typing import Optional

from flask import g
from sqlalchemy import text


# Valid sync_type values - keep in sync with the spec
VALID_SYNC_TYPES = ("orders", "items", "inventory", "fulfillment")

# Consecutive errors before flipping status to 'error' (sticky)
ERROR_THRESHOLD = 3


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


def _set_running_impl(session, connector_name: str, warehouse_id: int, sync_type: str) -> None:
    """Shared implementation of set_running that works with any session."""
    # Check current status - if already running, refuse
    row = session.execute(
        text("""
            SELECT sync_status FROM sync_state
            WHERE connector_name = :name AND warehouse_id = :wid AND sync_type = :type
        """),
        {"name": connector_name, "wid": warehouse_id, "type": sync_type},
    ).fetchone()

    if row and row.sync_status == "running":
        raise DuplicateRunError(
            f"Sync already running: {connector_name}/{warehouse_id}/{sync_type}"
        )

    # Upsert to running
    session.execute(
        text("""
            INSERT INTO sync_state (connector_name, warehouse_id, sync_type, sync_status, updated_at)
            VALUES (:name, :wid, :type, 'running', NOW())
            ON CONFLICT (connector_name, warehouse_id, sync_type)
            DO UPDATE SET sync_status = 'running', updated_at = NOW()
        """),
        {"name": connector_name, "wid": warehouse_id, "type": sync_type},
    )


def set_running(connector_name: str, warehouse_id: int, sync_type: str) -> None:
    """Mark a sync as running. Raises DuplicateRunError if already running.

    Uses g.db (Flask request context).
    """
    _set_running_impl(g.db, connector_name, warehouse_id, sync_type)


def set_success(connector_name: str, warehouse_id: int, sync_type: str) -> None:
    """Mark a sync as succeeded: status=idle, update last_success_at, reset consecutive_errors.

    Uses g.db (Flask request context).
    """
    now = datetime.now(timezone.utc)
    g.db.execute(
        text("""
            INSERT INTO sync_state (connector_name, warehouse_id, sync_type, sync_status,
                                     last_synced_at, last_success_at, consecutive_errors, updated_at)
            VALUES (:name, :wid, :type, 'idle', :now, :now, 0, :now)
            ON CONFLICT (connector_name, warehouse_id, sync_type)
            DO UPDATE SET sync_status = 'idle',
                          last_synced_at = :now,
                          last_success_at = :now,
                          consecutive_errors = 0,
                          updated_at = :now
        """),
        {"name": connector_name, "wid": warehouse_id, "type": sync_type, "now": now},
    )


def set_error(connector_name: str, warehouse_id: int, sync_type: str, error_message: str) -> None:
    """Record a sync error: increment consecutive_errors, set status='error' once threshold reached.

    Uses g.db (Flask request context).
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
                                     last_synced_at, last_error_at, last_error_message,
                                     consecutive_errors, updated_at)
            VALUES (:name, :wid, :type, :status, :now, :now, :msg, :errors, :now)
            ON CONFLICT (connector_name, warehouse_id, sync_type)
            DO UPDATE SET sync_status = :status,
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


def set_running_standalone(connector_name: str, warehouse_id: int, sync_type: str) -> None:
    """Standalone set_running for Celery tasks. Commits on success."""
    session = _standalone_session()
    try:
        _set_running_impl(session, connector_name, warehouse_id, sync_type)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def set_success_standalone(connector_name: str, warehouse_id: int, sync_type: str) -> None:
    """Standalone set_success for Celery tasks. Commits on success."""
    session = _standalone_session()
    try:
        now = datetime.now(timezone.utc)
        session.execute(
            text("""
                INSERT INTO sync_state (connector_name, warehouse_id, sync_type, sync_status,
                                         last_synced_at, last_success_at, consecutive_errors, updated_at)
                VALUES (:name, :wid, :type, 'idle', :now, :now, 0, :now)
                ON CONFLICT (connector_name, warehouse_id, sync_type)
                DO UPDATE SET sync_status = 'idle',
                              last_synced_at = :now,
                              last_success_at = :now,
                              consecutive_errors = 0,
                              updated_at = :now
            """),
            {"name": connector_name, "wid": warehouse_id, "type": sync_type, "now": now},
        )
        session.commit()
    finally:
        session.close()


def set_error_standalone(connector_name: str, warehouse_id: int, sync_type: str, error_message: str) -> None:
    """Standalone set_error for Celery tasks. Commits on success."""
    session = _standalone_session()
    try:
        now = datetime.now(timezone.utc)
        row = session.execute(
            text("""
                SELECT consecutive_errors FROM sync_state
                WHERE connector_name = :name AND warehouse_id = :wid AND sync_type = :type
            """),
            {"name": connector_name, "wid": warehouse_id, "type": sync_type},
        ).fetchone()
        current_errors = row.consecutive_errors if row else 0
        new_errors = current_errors + 1
        new_status = "error" if new_errors >= ERROR_THRESHOLD else "idle"

        session.execute(
            text("""
                INSERT INTO sync_state (connector_name, warehouse_id, sync_type, sync_status,
                                         last_synced_at, last_error_at, last_error_message,
                                         consecutive_errors, updated_at)
                VALUES (:name, :wid, :type, :status, :now, :now, :msg, :errors, :now)
                ON CONFLICT (connector_name, warehouse_id, sync_type)
                DO UPDATE SET sync_status = :status,
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
        session.commit()
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
