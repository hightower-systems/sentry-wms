"""
V-012: stale 'running' sync state recovery.
V-102: run_id generation so a stale worker's completion does not clobber
the new run that took over after RUNNING_TIMEOUT.

Covers:
- A fresh running row blocks duplicate runs (DuplicateRunError)
- A stale running row (running_since > RUNNING_TIMEOUT ago) is replaced
- reset_running flips stuck rows back to 'idle' and clears run_id
- The admin sync-reset endpoint requires ADMIN role
- set_success_standalone / set_error_standalone only apply when run_id matches
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from services.sync_state_service import (
    DuplicateRunError,
    RUNNING_TIMEOUT,
    _set_running_impl,
    reset_running,
    set_error_standalone,
    set_running_standalone,
    set_success_standalone,
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


class TestV101SyncResetAudit:
    """V-101: sync-reset must write an audit_log row so an admin cannot
    silently mask a persistent connector failure."""

    def test_reset_writes_audit_log_with_expected_fields(
        self, client, auth_headers, _db_transaction
    ):
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

        row = db.execute(
            text("""
                SELECT action_type, entity_type, entity_id, user_id,
                       warehouse_id, details
                FROM audit_log
                WHERE action_type = 'SYNC_RESET'
                ORDER BY log_id DESC LIMIT 1
            """)
        ).fetchone()
        assert row is not None
        assert row.action_type == "SYNC_RESET"
        assert row.entity_type == "CONNECTOR"
        assert row.entity_id == 1
        assert row.user_id == "admin"
        assert row.warehouse_id == 1
        assert row.details["connector"] == "example"
        assert row.details["sync_type"] == "orders"
        assert row.details["rows_reset"] >= 1

    def test_reset_all_types_audit_has_null_sync_type(
        self, client, auth_headers, _db_transaction
    ):
        db = _db_transaction
        _seed_sync_row(db, "example", 1, "items", "running",
                       datetime.now(timezone.utc))
        db.commit()

        resp = client.post(
            "/api/admin/connectors/example/sync-reset",
            json={"warehouse_id": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        row = db.execute(
            text("SELECT details FROM audit_log WHERE action_type='SYNC_RESET' "
                 "ORDER BY log_id DESC LIMIT 1")
        ).fetchone()
        assert row is not None
        assert row.details["sync_type"] is None


class TestV102RunIdGenerationRace:
    """V-102: a stale 'running' row can be taken over (V-012). Without
    run_id, the original worker's eventual set_success / set_error
    would clobber the new run's 'running' state back to 'idle'. With
    run_id, the original worker's completion targets its own run_id
    and no-ops when the row has been taken over."""

    @pytest.fixture(autouse=True)
    def _scrub_sync_state(self, _db_transaction):
        # set_running_standalone opens its own session on the shared test
        # connection and commits a savepoint. The outer test rollback
        # reverts it, but the order of savepoint releases can leave rows
        # visible to sibling tests within the same session. Start each
        # test with an empty sync_state table for the two sync_types
        # these tests touch.
        _db_transaction.execute(
            text("DELETE FROM sync_state WHERE connector_name='example' "
                 "AND warehouse_id=1 AND sync_type IN ('orders', 'items')")
        )
        _db_transaction.commit()
        yield

    def test_set_running_impl_returns_fresh_uuid(self, _db_transaction):
        db = _db_transaction
        run_id = _set_running_impl(db, "example", 1, "orders")
        assert isinstance(run_id, str) and len(run_id) == 36  # uuid4 string

        row = db.execute(
            text("SELECT run_id FROM sync_state WHERE connector_name='example' "
                 "AND warehouse_id=1 AND sync_type='orders'")
        ).fetchone()
        assert str(row.run_id) == run_id

    def test_takeover_mints_new_run_id(self, _db_transaction):
        db = _db_transaction
        stale = datetime.now(timezone.utc) - RUNNING_TIMEOUT - timedelta(minutes=5)
        _seed_sync_row(db, "example", 1, "items", "running", stale)
        db.execute(
            text("UPDATE sync_state SET run_id = gen_random_uuid() "
                 "WHERE connector_name='example' AND warehouse_id=1 AND sync_type='items'")
        )
        original_run_id = db.execute(
            text("SELECT run_id FROM sync_state WHERE connector_name='example' "
                 "AND warehouse_id=1 AND sync_type='items'")
        ).fetchone().run_id

        new_run_id = _set_running_impl(db, "example", 1, "items")

        assert new_run_id != str(original_run_id), (
            "takeover must mint a fresh run_id so the original worker "
            "cannot clobber the new run on completion"
        )

    def test_set_success_standalone_applies_when_run_id_matches(self, _db_transaction):
        db = _db_transaction
        run_id = set_running_standalone("example", 1, "orders")
        applied = set_success_standalone("example", 1, "orders", run_id)
        assert applied is True

        row = db.execute(
            text("SELECT sync_status, run_id, last_success_at FROM sync_state "
                 "WHERE connector_name='example' AND warehouse_id=1 AND sync_type='orders'")
        ).fetchone()
        assert row.sync_status == "idle"
        assert row.run_id is None
        assert row.last_success_at is not None

    def test_set_success_standalone_noop_after_takeover(self, _db_transaction):
        # Worker A starts, gets run_id A. After stale-takeover a new run
        # (B) is installed with run_id B. Worker A's set_success must
        # no-op and leave B's 'running' state untouched.
        db = _db_transaction
        stale = datetime.now(timezone.utc) - RUNNING_TIMEOUT - timedelta(minutes=5)
        _seed_sync_row(db, "example", 1, "orders", "running", stale)
        import uuid as _uuid
        run_id_a = str(_uuid.uuid4())
        db.execute(
            text("UPDATE sync_state SET run_id=:rid "
                 "WHERE connector_name='example' AND warehouse_id=1 AND sync_type='orders'"),
            {"rid": run_id_a},
        )
        db.commit()

        # Simulate takeover (installs a fresh run_id B).
        run_id_b = set_running_standalone("example", 1, "orders")
        assert run_id_b != run_id_a

        # Worker A (original) finally completes and tries to mark success
        # with its stale run_id. Must no-op.
        applied = set_success_standalone("example", 1, "orders", run_id_a)
        assert applied is False

        row = db.execute(
            text("SELECT sync_status, run_id FROM sync_state "
                 "WHERE connector_name='example' AND warehouse_id=1 AND sync_type='orders'")
        ).fetchone()
        assert row.sync_status == "running", (
            "stale worker completion clobbered the new run's 'running' state"
        )
        assert str(row.run_id) == run_id_b

    def test_set_error_standalone_noop_after_takeover(self, _db_transaction):
        db = _db_transaction
        stale = datetime.now(timezone.utc) - RUNNING_TIMEOUT - timedelta(minutes=5)
        _seed_sync_row(db, "example", 1, "items", "running", stale)
        import uuid as _uuid
        run_id_a = str(_uuid.uuid4())
        db.execute(
            text("UPDATE sync_state SET run_id=:rid "
                 "WHERE connector_name='example' AND warehouse_id=1 AND sync_type='items'"),
            {"rid": run_id_a},
        )
        db.commit()

        run_id_b = set_running_standalone("example", 1, "items")
        applied = set_error_standalone("example", 1, "items", "boom", run_id_a)
        assert applied is False

        row = db.execute(
            text("SELECT sync_status, run_id, last_error_message FROM sync_state "
                 "WHERE connector_name='example' AND warehouse_id=1 AND sync_type='items'")
        ).fetchone()
        assert row.sync_status == "running"
        assert str(row.run_id) == run_id_b
        assert row.last_error_message is None, (
            "stale worker's error message leaked into the new run's row"
        )

    def test_admin_reset_clears_run_id(self, client, auth_headers, _db_transaction):
        # reset_sync_state must clear run_id so a stuck worker's eventual
        # completion (with the old run_id) cannot flip the row back to
        # idle and confuse operators.
        db = _db_transaction
        run_id_a = set_running_standalone("example", 1, "orders")
        db.commit()

        resp = client.post(
            "/api/admin/connectors/example/sync-reset",
            json={"warehouse_id": 1, "sync_type": "orders"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        row = db.execute(
            text("SELECT sync_status, run_id FROM sync_state "
                 "WHERE connector_name='example' AND warehouse_id=1 AND sync_type='orders'")
        ).fetchone()
        assert row.sync_status == "idle"
        assert row.run_id is None

        # The stuck worker's completion with the original run_id must
        # no-op now because no row has that run_id.
        applied = set_success_standalone("example", 1, "orders", run_id_a)
        assert applied is False
        row_after = db.execute(
            text("SELECT sync_status, last_success_at FROM sync_state "
                 "WHERE connector_name='example' AND warehouse_id=1 AND sync_type='orders'")
        ).fetchone()
        assert row_after.sync_status == "idle"
        assert row_after.last_success_at is None, (
            "stuck worker's late success overwrote the admin reset"
        )
