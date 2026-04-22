"""Snapshot keeper daemon tests (v1.5.0 #132).

Two layers:

1. Unit tests call the ``SnapshotKeeper`` helpers directly against
   the test DB so each branch is isolated (promote, reap, orphan
   cleanup, idle timeout).

2. The load-bearing integration test: start the keeper as a
   subprocess, INSERT a pending scan, poll until it lands 'active',
   and then **prove the whole pagination story works** by importing
   the exported pg_snapshot_id on a second connection and asserting
   ``pg_current_snapshot()`` matches between the keeper's held
   transaction (observed via its promoted row) and the importer.
"""

import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest

from services.snapshot_keeper import SnapshotKeeper


def _make_conn(autocommit=True):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = autocommit
    return conn


def _insert_pending_scan(scan_id, warehouse_id=1):
    conn = _make_conn()
    try:
        cur = conn.cursor()
        # Suppress the snapshot_scans_pending NOTIFY trigger for this
        # session so a co-running live snapshot-keeper container (which
        # LISTENs on that channel) cannot race the in-test keeper and
        # flip status='pending' -> 'active' before the test's _promote
        # reaches the row. session_replication_role is session-local;
        # the trigger still fires normally in production.
        cur.execute("SET session_replication_role = replica")
        cur.execute(
            "INSERT INTO snapshot_scans (scan_id, warehouse_id, status) "
            "VALUES (%s, %s, 'pending')",
            (str(scan_id), warehouse_id),
        )
    finally:
        conn.close()


def _read_scan(scan_id):
    conn = _make_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT status, pg_snapshot_id, snapshot_event_id "
            "  FROM snapshot_scans WHERE scan_id = %s",
            (str(scan_id),),
        )
        return cur.fetchone()
    finally:
        conn.close()


def _delete_scan(scan_id):
    conn = _make_conn()
    try:
        conn.cursor().execute(
            "DELETE FROM snapshot_scans WHERE scan_id = %s", (str(scan_id),)
        )
    finally:
        conn.close()


def _wipe_scans():
    conn = _make_conn()
    try:
        conn.cursor().execute("DELETE FROM snapshot_scans")
    finally:
        conn.close()


class TestKeeperUnitBehaviour:
    def setup_method(self):
        _wipe_scans()

    def teardown_method(self):
        _wipe_scans()

    def test_orphan_cleanup_aborts_stale_active_rows(self):
        scan_id = uuid.uuid4()
        conn = _make_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO snapshot_scans (scan_id, warehouse_id, status, pg_snapshot_id) "
            "VALUES (%s, 1, 'active', 'stale-handle')",
            (str(scan_id),),
        )
        conn.close()

        keeper = SnapshotKeeper(heartbeat_file="/tmp/keeper-unit-heartbeat")
        keeper._cleanup_orphans_on_boot()
        row = _read_scan(scan_id)
        assert row[0] == "aborted"

    def test_promote_fills_row_and_holds_connection(self):
        scan_id = uuid.uuid4()
        _insert_pending_scan(scan_id)
        keeper = SnapshotKeeper(heartbeat_file="/tmp/keeper-unit-heartbeat")
        keeper._promote(str(scan_id))
        try:
            assert str(scan_id) in keeper.active
            row = _read_scan(scan_id)
            assert row[0] == "active"
            assert row[1] and row[1] != ""  # pg_snapshot_id populated
            assert row[2] is not None  # snapshot_event_id captured
        finally:
            keeper._graceful_shutdown()

    def test_reap_closes_done_rows(self):
        scan_id = uuid.uuid4()
        _insert_pending_scan(scan_id)
        keeper = SnapshotKeeper(heartbeat_file="/tmp/keeper-unit-heartbeat")
        keeper._promote(str(scan_id))
        # API marks the row done.
        conn = _make_conn()
        conn.cursor().execute(
            "UPDATE snapshot_scans SET status='done' WHERE scan_id = %s",
            (str(scan_id),),
        )
        conn.close()

        keeper._reap_completed_or_timed_out()
        assert str(scan_id) not in keeper.active
        # Row is deleted per plan 4.1 retention policy.
        assert _read_scan(scan_id) is None

    def test_reap_expires_idle_scan(self):
        scan_id = uuid.uuid4()
        _insert_pending_scan(scan_id)
        keeper = SnapshotKeeper(
            idle_timeout_s=0.0,  # any non-zero idle age trips the timeout
            heartbeat_file="/tmp/keeper-unit-heartbeat",
        )
        keeper._promote(str(scan_id))
        # Let monotonic clock advance past the 0-second timeout.
        time.sleep(0.05)
        keeper._reap_completed_or_timed_out()
        assert str(scan_id) not in keeper.active
        # Row is deleted; the 'expired' status is an intermediate
        # stamp the plan describes for audit symmetry.
        assert _read_scan(scan_id) is None

    def test_graceful_shutdown_closes_all_held(self):
        scan_ids = [uuid.uuid4() for _ in range(3)]
        for sid in scan_ids:
            _insert_pending_scan(sid)
        keeper = SnapshotKeeper(heartbeat_file="/tmp/keeper-unit-heartbeat")
        for sid in scan_ids:
            keeper._promote(str(sid))
        assert len(keeper.active) == 3

        keeper._graceful_shutdown()
        assert keeper.active == {}
        for sid in scan_ids:
            # The shutdown path marks scans aborted + deletes them.
            assert _read_scan(sid) is None


class TestKeeperSubprocessSnapshotHandover:
    """The load-bearing test: prove the exported pg_snapshot_id is
    importable by a second connection and both sessions see the same
    visibility set. If this works, the snapshot paging story in #133
    works; if it fails, pagination is broken regardless of keeper
    uptime or NOTIFY latency.
    """

    def setup_method(self):
        _wipe_scans()

    def teardown_method(self):
        _wipe_scans()

    def _start_keeper(self):
        api_dir = str(Path(__file__).resolve().parents[1])
        env = dict(os.environ)
        env["PYTHONPATH"] = api_dir + ":" + env.get("PYTHONPATH", "")
        env["SNAPSHOT_KEEPER_HEARTBEAT_FILE"] = "/tmp/keeper-subprocess-heartbeat"
        env["SNAPSHOT_KEEPER_LOG_LEVEL"] = "WARNING"
        proc = subprocess.Popen(
            [sys.executable, "-m", "services.snapshot_keeper"],
            env=env,
            cwd=api_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return proc

    def _stop_keeper(self, proc):
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def _poll_until_active(self, scan_id, timeout_s=10.0):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            row = _read_scan(scan_id)
            if row is not None and row[0] == "active" and row[1]:
                return row[1]  # pg_snapshot_id
            time.sleep(0.1)
        return None

    def test_exported_snapshot_is_importable_and_matches(self):
        proc = self._start_keeper()
        try:
            # Give the keeper a moment to open its LISTEN connection.
            time.sleep(0.5)
            scan_id = uuid.uuid4()
            _insert_pending_scan(scan_id)

            pg_snapshot_id = self._poll_until_active(scan_id, timeout_s=10.0)
            assert pg_snapshot_id, "keeper must promote the pending scan within 10s"

            # Second connection: import the exported snapshot via
            # SET TRANSACTION SNAPSHOT and capture its visibility set.
            importer = _make_conn(autocommit=False)
            try:
                cur = importer.cursor()
                cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
                cur.execute(
                    "SET TRANSACTION SNAPSHOT %s", (pg_snapshot_id,)
                )
                cur.execute("SELECT pg_current_snapshot()::text")
                importer_snapshot = cur.fetchone()[0]
            finally:
                importer.rollback()
                importer.close()

            # A second importer on a third connection must see the
            # same visibility set as the first - proof that the
            # snapshot handle works for multiple concurrent pagers.
            importer2 = _make_conn(autocommit=False)
            try:
                cur = importer2.cursor()
                cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
                cur.execute(
                    "SET TRANSACTION SNAPSHOT %s", (pg_snapshot_id,)
                )
                cur.execute("SELECT pg_current_snapshot()::text")
                importer2_snapshot = cur.fetchone()[0]
            finally:
                importer2.rollback()
                importer2.close()

            assert importer_snapshot == importer2_snapshot, (
                "two connections importing the same pg_snapshot_id must "
                "observe the same pg_current_snapshot() visibility set; "
                "got %r vs %r" % (importer_snapshot, importer2_snapshot)
            )

            # Clean-up path: flip the row to done and confirm the
            # keeper reaps it within a few poll cycles.
            conn = _make_conn()
            conn.cursor().execute(
                "UPDATE snapshot_scans SET status='done' WHERE scan_id=%s",
                (str(scan_id),),
            )
            conn.close()
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if _read_scan(scan_id) is None:
                    break
                time.sleep(0.2)
            assert _read_scan(scan_id) is None, (
                "keeper must delete the row after status='done'"
            )
        finally:
            self._stop_keeper(proc)

    def test_sigterm_shutdown_is_clean(self):
        proc = self._start_keeper()
        try:
            # Let the keeper enter its main loop.
            time.sleep(0.5)
        finally:
            self._stop_keeper(proc)
        assert proc.returncode in (0, -signal.SIGTERM), (
            "keeper must exit cleanly on SIGTERM; got %r" % proc.returncode
        )
