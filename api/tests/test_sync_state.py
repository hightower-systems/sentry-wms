"""Tests for sync state tracking service and admin endpoints.

Covers:
- State transitions: idle -> running -> idle (success and error paths)
- Consecutive error counting and threshold-based status flip
- Recovery: error status resets on success
- Duplicate run prevention
- Admin endpoints return correct states
- Manual sync trigger returns 202 and queues task
- Manual sync trigger returns 409 when already running
"""

import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from db_test_context import get_raw_connection


@pytest.fixture(autouse=True)
def _eager_celery():
    """Put celery in eager mode so task.delay() runs synchronously in tests."""
    from jobs import celery_app
    prev_eager = celery_app.conf.task_always_eager
    prev_prop = celery_app.conf.task_eager_propagates
    celery_app.conf.update(task_always_eager=True, task_eager_propagates=False)
    yield
    celery_app.conf.update(task_always_eager=prev_eager, task_eager_propagates=prev_prop)


def _ensure_table():
    """Create sync_state table if missing (idempotent)."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sync_state (
            id SERIAL PRIMARY KEY,
            connector_name VARCHAR(64) NOT NULL,
            warehouse_id INT NOT NULL,
            sync_type VARCHAR(32) NOT NULL,
            sync_status VARCHAR(16) DEFAULT 'idle',
            last_synced_at TIMESTAMPTZ,
            last_success_at TIMESTAMPTZ,
            last_error_at TIMESTAMPTZ,
            last_error_message TEXT,
            consecutive_errors INT DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(connector_name, warehouse_id, sync_type)
        )
    """)
    cur.close()


def _register_example():
    from connectors import registry
    from connectors.example import ExampleConnector
    try:
        registry.get("example")
    except KeyError:
        registry.register("example", ExampleConnector)


# ---------------------------------------------------------------------------
# State transition tests (via g.db inside Flask request context)
# ---------------------------------------------------------------------------


class TestStateTransitions:
    """Exercise the in-request (g.db) versions of the state service functions."""

    @pytest.fixture(autouse=True)
    def _setup(self, client, auth_headers):
        _ensure_table()
        _register_example()
        # Trigger a request to establish g.db context via the Flask test client
        self.client = client
        self.headers = auth_headers

    def _run_in_context(self, func):
        """Run `func(g.db)` inside a Flask test request context."""
        from app import create_app
        import models.database as db
        app = create_app()

        with app.test_request_context():
            from flask import g
            g.db = db.SessionLocal()
            try:
                result = func()
                g.db.commit()
                return result
            except Exception:
                g.db.rollback()
                raise
            finally:
                g.db.close()

    def test_idle_to_running_to_idle_success(self):
        """Happy path: idle -> running -> idle on success."""
        from services.sync_state_service import set_running, set_success, get_sync_state

        def flow():
            set_running("example", 1, "orders")
            state1 = get_sync_state("example", 1, "orders")
            assert state1["sync_status"] == "running"

            set_success("example", 1, "orders")
            state2 = get_sync_state("example", 1, "orders")
            assert state2["sync_status"] == "idle"
            assert state2["last_success_at"] is not None
            assert state2["consecutive_errors"] == 0

        self._run_in_context(flow)

    def test_idle_to_running_to_error(self):
        """Failure path: idle -> running -> idle with consecutive_errors = 1."""
        from services.sync_state_service import set_running, set_error, get_sync_state

        def flow():
            set_running("example", 1, "items")
            set_error("example", 1, "items", "API timeout")
            state = get_sync_state("example", 1, "items")
            assert state["sync_status"] == "idle"  # Not yet 'error' (need 3)
            assert state["last_error_message"] == "API timeout"
            assert state["consecutive_errors"] == 1
            assert state["last_error_at"] is not None

        self._run_in_context(flow)

    def test_consecutive_errors_flip_to_error_status(self):
        """After 3 consecutive errors, status flips to 'error'."""
        from services.sync_state_service import set_error, get_sync_state

        def flow():
            set_error("example", 1, "inventory", "fail 1")
            set_error("example", 1, "inventory", "fail 2")
            state = get_sync_state("example", 1, "inventory")
            assert state["consecutive_errors"] == 2
            assert state["sync_status"] == "idle"  # Not yet 3

            set_error("example", 1, "inventory", "fail 3")
            state = get_sync_state("example", 1, "inventory")
            assert state["consecutive_errors"] == 3
            assert state["sync_status"] == "error"  # Now sticky

            set_error("example", 1, "inventory", "fail 4")
            state = get_sync_state("example", 1, "inventory")
            assert state["consecutive_errors"] == 4
            assert state["sync_status"] == "error"

        self._run_in_context(flow)

    def test_recovery_resets_consecutive_errors(self):
        """Successful sync after errors resets the counter and clears error status."""
        from services.sync_state_service import set_error, set_success, get_sync_state

        def flow():
            # Build up errors
            for msg in ["e1", "e2", "e3", "e4"]:
                set_error("example", 1, "orders", msg)
            state = get_sync_state("example", 1, "orders")
            assert state["sync_status"] == "error"
            assert state["consecutive_errors"] == 4

            # Successful sync resets
            set_success("example", 1, "orders")
            state = get_sync_state("example", 1, "orders")
            assert state["sync_status"] == "idle"
            assert state["consecutive_errors"] == 0

        self._run_in_context(flow)

    def test_duplicate_run_raises(self):
        """set_running while already running raises DuplicateRunError."""
        from services.sync_state_service import set_running, DuplicateRunError

        def flow():
            set_running("example", 1, "fulfillment")
            with pytest.raises(DuplicateRunError):
                set_running("example", 1, "fulfillment")

        self._run_in_context(flow)

    def test_get_all_states(self):
        """get_all_sync_states returns all types for a connector+warehouse."""
        from services.sync_state_service import set_success, get_all_sync_states

        def flow():
            set_success("example", 2, "orders")
            set_success("example", 2, "items")
            states = get_all_sync_states("example", 2)
            types = [s["sync_type"] for s in states]
            assert "orders" in types
            assert "items" in types

        self._run_in_context(flow)

    def test_scoping_by_warehouse(self):
        """States for one warehouse don't leak into another."""
        from services.sync_state_service import set_success, get_sync_state

        def flow():
            set_success("example", 1, "fulfillment")
            # Query for warehouse 2 (exists, but no data) - should be None
            state_wh2 = get_sync_state("example", 2, "fulfillment")
            assert state_wh2 is None

        self._run_in_context(flow)


# ---------------------------------------------------------------------------
# Admin endpoint tests
# ---------------------------------------------------------------------------


class TestSyncStatusEndpoints:
    @pytest.fixture(autouse=True)
    def _setup(self):
        _ensure_table()
        _register_example()

    def test_get_sync_status_empty(self, client, auth_headers):
        """Returns empty sync_states array when nothing tracked yet."""
        resp = client.get(
            "/api/admin/connectors/example/sync-status?warehouse_id=99",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["connector"] == "example"
        assert data["warehouse_id"] == 99
        assert data["sync_states"] == []

    def test_get_sync_status_requires_warehouse_id(self, client, auth_headers):
        resp = client.get("/api/admin/connectors/example/sync-status", headers=auth_headers)
        assert resp.status_code == 400

    def test_get_sync_status_unknown_connector(self, client, auth_headers):
        resp = client.get(
            "/api/admin/connectors/nonexistent/sync-status?warehouse_id=1",
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_get_sync_status_non_admin_403(self, client):
        # Create non-admin user
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO users (username, password_hash, full_name, role, warehouse_id, warehouse_ids)
               VALUES ('sync_test_user', '$2b$12$zDGRKFLmc6v/A4mVhxOzb.7uoW1ulnXn0AisK5uJ5iWk33vC2EpSK',
                       'Test', 'USER', 1, '{1}')
               ON CONFLICT (username) DO NOTHING"""
        )
        cur.close()
        resp = client.post("/api/auth/login", json={"username": "sync_test_user", "password": "admin"})
        token = resp.get_json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.get(
            "/api/admin/connectors/example/sync-status?warehouse_id=1",
            headers=headers,
        )
        assert resp.status_code == 403


class TestManualSyncTrigger:
    @pytest.fixture(autouse=True)
    def _setup(self):
        _ensure_table()
        _register_example()

    def test_trigger_sync_queues_task(self, client, auth_headers):
        """POST /sync/orders returns 202 with task ID."""
        resp = client.post(
            "/api/admin/connectors/example/sync/orders",
            json={"warehouse_id": 2},
            headers=auth_headers,
        )
        assert resp.status_code == 202
        data = resp.get_json()
        assert data["message"] == "Sync queued"
        assert "task_id" in data
        assert data["sync_type"] == "orders"

    def test_trigger_sync_invalid_type(self, client, auth_headers):
        resp = client.post(
            "/api/admin/connectors/example/sync/bogus",
            json={"warehouse_id": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_trigger_fulfillment_health_check(self, client, auth_headers):
        """Fulfillment sync triggers a health check (test_connection)."""
        resp = client.post(
            "/api/admin/connectors/example/sync/fulfillment",
            json={"warehouse_id": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 202
        data = resp.get_json()
        assert data["sync_type"] == "fulfillment"
        assert "task_id" in data

    def test_trigger_sync_unknown_connector(self, client, auth_headers):
        resp = client.post(
            "/api/admin/connectors/nonexistent/sync/orders",
            json={"warehouse_id": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_trigger_sync_409_when_running(self, client, auth_headers):
        """Returns 409 Conflict if a sync is already running."""
        # Manually mark as running via SQL
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO sync_state (connector_name, warehouse_id, sync_type, sync_status)
            VALUES ('example', 2, 'items', 'running')
            ON CONFLICT (connector_name, warehouse_id, sync_type)
            DO UPDATE SET sync_status = 'running'
            """
        )
        cur.close()

        resp = client.post(
            "/api/admin/connectors/example/sync/items",
            json={"warehouse_id": 2},
            headers=auth_headers,
        )
        assert resp.status_code == 409

    def test_trigger_sync_non_admin_403(self, client):
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO users (username, password_hash, full_name, role, warehouse_id, warehouse_ids)
               VALUES ('sync_trigger_user', '$2b$12$zDGRKFLmc6v/A4mVhxOzb.7uoW1ulnXn0AisK5uJ5iWk33vC2EpSK',
                       'Test', 'USER', 1, '{1}')
               ON CONFLICT (username) DO NOTHING"""
        )
        cur.close()
        resp = client.post("/api/auth/login", json={"username": "sync_trigger_user", "password": "admin"})
        token = resp.get_json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.post(
            "/api/admin/connectors/example/sync/orders",
            json={"warehouse_id": 1},
            headers=headers,
        )
        assert resp.status_code == 403

    def test_trigger_sync_missing_warehouse_id(self, client, auth_headers):
        resp = client.post(
            "/api/admin/connectors/example/sync/orders",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 400
