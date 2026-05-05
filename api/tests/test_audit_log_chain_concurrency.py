"""v1.7.0 #271: audit_log strict-by-log_id chain holds under concurrent insert.

Pre-merge gate confirmed the original BEFORE INSERT trigger read
prev_hash without serialization: two concurrent inserts both saw the
same prev_hash and forked the chain. Mig 047 wraps the read + insert
in a transaction-scoped advisory lock.

Tests use direct psycopg2 connections (bypassing SQLAlchemy + the
conftest's _db_transaction fixture) so every writer sees its own
transaction and the trigger fires for real concurrency. The
operator's pre-merge gate forensic test already confirmed the
runtime shape (4 concurrent inbound POSTs forked the chain
pre-mig-047); these tests pin the post-fix contract.
"""

import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2

DATABASE_URL = os.environ["TEST_DATABASE_URL"]


def _snapshot_audit_baseline() -> tuple[int, bytes]:
    """Return (max_log_id, sentinel_row_hash) so a test can assert
    the chain holds from that anchor forward.

    Anchor pulls from audit_log_chain_head (the sentinel #271 added)
    rather than audit_log itself: the conftest's session-start
    TRUNCATE wipes audit_log but doesn't reset the sentinel, so the
    sentinel is the source of truth for "what prev_hash will the
    next inserter see". The next trigger fire reads the sentinel
    inside its lock and uses that as prev_hash.
    """
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(MAX(log_id), 0) FROM audit_log")
        max_log_id = cur.fetchone()[0]
        cur.execute(
            "SELECT row_hash FROM audit_log_chain_head WHERE singleton = TRUE"
        )
        sentinel = cur.fetchone()
        sentinel_hash = bytes(sentinel[0]) if sentinel else b"\x00"
        return max_log_id, sentinel_hash
    finally:
        conn.close()


def _fetch_chain_after(baseline_log_id: int) -> list[tuple]:
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT log_id, prev_hash, row_hash FROM audit_log "
            " WHERE log_id > %s ORDER BY log_id",
            (baseline_log_id,),
        )
        return cur.fetchall()
    finally:
        conn.close()


def _assert_strict_chain(rows: list[tuple], anchor_row_hash: bytes) -> None:
    expected_prev = anchor_row_hash
    for log_id, prev_hash, row_hash in rows:
        assert bytes(prev_hash) == expected_prev, (
            f"strict chain broken at log_id={log_id}; "
            f"expected prev_hash={expected_prev.hex()[:16]}..., "
            f"got {bytes(prev_hash).hex()[:16]}..."
        )
        expected_prev = bytes(row_hash)


# ----------------------------------------------------------------------
# Boot-time burst shape: parallel _write_load_audit calls
# ----------------------------------------------------------------------


class TestBootBurstChain:
    def test_parallel_audit_writes_form_strict_chain(self):
        """Simulates the boot-time burst: N gunicorn workers each
        opening their own connection to write a MAPPING_DOCUMENT_LOAD
        row. Pre-mig-047 this forked the chain; the advisory lock
        serializes them now."""
        marker = f"chain-boot-{uuid.uuid4().hex[:8]}"
        n_writers = 8
        rows_per_writer = 5
        baseline_log_id, anchor_hash = _snapshot_audit_baseline()

        def writer(worker_id: int):
            conn = psycopg2.connect(DATABASE_URL)
            try:
                conn.autocommit = False
                cur = conn.cursor()
                for i in range(rows_per_writer):
                    cur.execute(
                        "INSERT INTO audit_log "
                        " (action_type, entity_type, entity_id, user_id, "
                        "  details) "
                        "VALUES ('CHAIN_TEST', 'TEST', 0, "
                        "        'system:chain-test', "
                        "        jsonb_build_object('marker', %s, "
                        "                           'worker', %s, "
                        "                           'i', %s))",
                        (marker, worker_id, i),
                    )
                conn.commit()
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=n_writers) as ex:
            list(ex.map(writer, range(n_writers)))

        rows = _fetch_chain_after(baseline_log_id)
        # At least our N*M rows landed; concurrent test-suite
        # interleavers (none expected when this test runs in
        # isolation, but the assertion is robust to them).
        assert len(rows) >= n_writers * rows_per_writer
        _assert_strict_chain(rows, anchor_hash)


# ----------------------------------------------------------------------
# Runtime burst shape: 4 concurrent INSERTs from distinct connections
# ----------------------------------------------------------------------
#
# This mirrors the pre-merge gate forensic shape (4 concurrent runtime
# inbound POSTs forked the chain). Going through Flask test client +
# the conftest's _db_transaction fixture would funnel writes through a
# single conn and never actually exercise the trigger's race; direct
# psycopg2 conns reproduce the runtime concurrency at the DB layer
# where the trigger lives.


class TestRuntimeBurstChain:
    def test_four_concurrent_writes_keep_chain_strict(self):
        """Four parallel transactions each opening their own conn,
        each INSERTing one INBOUND_CUSTOMER-shaped audit row. Pre-mig-047
        this produced 1 of 4 rows breaking the strict-by-log_id chain;
        with the advisory lock the chain holds."""
        marker = f"chain-runtime-{uuid.uuid4().hex[:8]}"
        n = 4
        baseline_log_id, anchor_hash = _snapshot_audit_baseline()

        def writer(idx: int):
            conn = psycopg2.connect(DATABASE_URL)
            try:
                conn.autocommit = False
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO audit_log "
                    " (action_type, entity_type, entity_id, user_id, "
                    "  details) "
                    "VALUES ('INBOUND_CUSTOMER_CHAIN_TEST', 'TEST', %s, "
                    "        'system:chain-test', "
                    "        jsonb_build_object('marker', %s, 'i', %s))",
                    (idx, marker, idx),
                )
                conn.commit()
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(writer, range(n)))

        rows = _fetch_chain_after(baseline_log_id)
        assert len(rows) >= n
        _assert_strict_chain(rows, anchor_hash)
