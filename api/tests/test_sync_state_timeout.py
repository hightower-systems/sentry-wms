"""
V-012: stale 'running' sync state recovery.

Covers:
- A fresh running row blocks duplicate runs (DuplicateRunError)
- A stale running row (running_since > RUNNING_TIMEOUT ago) is replaced
- reset_running flips stuck rows back to 'idle'
- The admin sync-reset endpoint requires ADMIN role
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from services.sync_state_service import (
    DuplicateRunError,
    RUNNING_TIMEOUT,
    _set_running_impl,
    reset_running,
)


def _seed_sync_row(db, connector, warehouse_id, sync_type, status, running_since=None):
    db.execute(
        text("""
            INSERT INTO sync_state (connector_name, warehouse_id, sync_type,
                                    sync_status, running_since, updated_at)
            VALUES (:n, :w, :t, :s, :r, NOW())
            ON CONFLICT (connector_name, warehouse_id, sync_type)
            DO UPDATE SET sync_status = :s, running_since = :r, updated_at = NOW()
        """),
        {"n": connector, "w": warehouse_id, "t": sync_type, "s": status, "r": running_since},
    )


class TestStaleRunningRecovery:
    def test_fresh_running_blocks_new_run(self, _db_transaction):
        db = _db_transaction
        _seed_sync_row(db, "example", 1, "orders", "running",
                       datetime.now(timezone.utc))
        with pytest.raises(DuplicateRunError):
            _set_running_impl(db, "example", 1, "orders")

    def test_stale_running_allows_takeover(self, _db_transaction):
        db = _db_transaction
        stale = datetime.now(timezone.utc) - RUNNING_TIMEOUT - timedelta(minutes=5)
        _seed_sync_row(db, "example", 1, "items", "running", stale)

        _set_running_impl(db, "example", 1, "items")

        row = db.execute(
            text("SELECT sync_status, running_since FROM sync_state "
                 "WHERE connector_name='example' AND warehouse_id=1 AND sync_type='items'")
        ).fetchone()
        assert row.sync_status == "running"
        assert row.running_since > stale

    def test_running_since_null_treated_as_fresh(self, _db_transaction):
        db = _db_transaction
        _seed_sync_row(db, "example", 1, "inventory", "running", None)
        with pytest.raises(DuplicateRunError):
            _set_running_impl(db, "example", 1, "inventory")


class TestResetRunning:
    def test_reset_flips_running_to_idle(self, _db_transaction, app):
        db = _db_transaction
        _seed_sync_row(db, "example", 1, "orders", "running",
                       datetime.now(timezone.utc))

        from flask import g
        with app.test_request_context():
            g.db = db
            count = reset_running("example", 1, "orders")
        assert count == 1

        row = db.execute(
            text("SELECT sync_status, running_since FROM sync_state "
                 "WHERE connector_name='example' AND warehouse_id=1 AND sync_type='orders'")
        ).fetchone()
        assert row.sync_status == "idle"
        assert row.running_since is None

    def test_reset_all_types_when_sync_type_none(self, _db_transaction, app):
        db = _db_transaction
        now = datetime.now(timezone.utc)
        _seed_sync_row(db, "example", 1, "orders", "running", now)
        _seed_sync_row(db, "example", 1, "items", "running", now)
        _seed_sync_row(db, "example", 1, "inventory", "idle", None)

        from flask import g
        with app.test_request_context():
            g.db = db
            count = reset_running("example", 1, None)
        assert count == 2

    def test_reset_skips_non_running_rows(self, _db_transaction, app):
        db = _db_transaction
        _seed_sync_row(db, "example", 1, "orders", "idle", None)

        from flask import g
        with app.test_request_context():
            g.db = db
            count = reset_running("example", 1, "orders")
        assert count == 0


class TestAdminSyncResetEndpoint:
    def test_requires_auth(self, client):
        resp = client.post("/api/admin/connectors/example/sync-reset",
                           json={"warehouse_id": 1})
        assert resp.status_code == 401

    def test_returns_count_and_clears_running(self, client, auth_headers, _db_transaction):
        db = _db_transaction
        _seed_sync_row(db, "example", 1, "orders", "running",
                       datetime.now(timezone.utc))
        db.commit()

        resp = client.post(
            "/api/admin/connectors/example/sync-reset",
            json={"warehouse_id": 1, "sync_type": "orders"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["rows_reset"] >= 1

    def test_rejects_invalid_sync_type(self, client, auth_headers):
        resp = client.post(
            "/api/admin/connectors/example/sync-reset",
            json={"warehouse_id": 1, "sync_type": "bogus"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_rejects_unknown_connector(self, client, auth_headers):
        resp = client.post(
            "/api/admin/connectors/nonexistent_xyz/sync-reset",
            json={"warehouse_id": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_requires_warehouse_id(self, client, auth_headers):
        resp = client.post(
            "/api/admin/connectors/example/sync-reset",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 400
