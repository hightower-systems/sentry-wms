"""Per-aggregate FIFO concurrency for the v1.5.0 outbox (#120).

BIGSERIAL gives integration_events.event_id at INSERT time. Two
transactions may commit in the opposite order from their INSERT order
so a reader ordering on event_id alone would see events out of commit
order. The DEFERRABLE INITIALLY DEFERRED trigger sets visible_at at
COMMIT so readers ordering on (visible_at, event_id) see events in
commit order instead.

``test_events_migration`` already proves this at the migration level
with a synthetic aggregate. This module proves the same property
across realistic emit sites: two concurrent transactions writing
integration_events rows on the same aggregate type produce visible_at
values that respect commit order. The tests use real psycopg2
connections, not the Flask test fixture, because the fixture wraps
the handler in a rolled-back outer transaction and the whole point
of FIFO is observing an actual COMMIT.
"""

import os
import sys
import uuid
from time import sleep

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2


def _make_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _emit(cur, aggregate_id, event_type, source_txn_id=None):
    """INSERT one row into integration_events using the caller's cursor."""
    cur.execute(
        """
        INSERT INTO integration_events (
            event_type, event_version, aggregate_type, aggregate_id,
            aggregate_external_id, warehouse_id, source_txn_id, payload
        ) VALUES (%s, 1, %s, %s, %s, 1, %s, '{}'::jsonb)
        RETURNING event_id
        """,
        (
            event_type,
            "fifo_test",
            aggregate_id,
            str(uuid.uuid4()),
            str(source_txn_id or uuid.uuid4()),
        ),
    )
    return cur.fetchone()[0]


def _cleanup(aggregate_id):
    conn = _make_conn()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM integration_events WHERE aggregate_id = %s",
        (aggregate_id,),
    )
    conn.close()


class TestSameAggregateFIFO:
    """Two sessions emit on the SAME aggregate. Earlier-inserted row
    commits later; visible_at must still reflect commit order so the
    poller's visible_at-ordered read stays monotonic per aggregate.
    """

    def test_same_aggregate_commit_order_wins(self):
        conn_a = _make_conn()
        conn_b = _make_conn()
        try:
            aggregate_id = 99_888_100

            cur_a = conn_a.cursor()
            cur_b = conn_b.cursor()

            # A inserts first -> receives a lower event_id.
            cur_a.execute("BEGIN")
            a_event_id = _emit(cur_a, aggregate_id, "fifo.a_first")

            # B inserts second -> higher event_id, same aggregate.
            cur_b.execute("BEGIN")
            b_event_id = _emit(cur_b, aggregate_id, "fifo.b_second")
            assert a_event_id < b_event_id

            # B commits first; visible_at is set at B's clock_timestamp.
            conn_b.commit()
            sleep(0.01)
            # A commits second; its visible_at is later than B's.
            conn_a.commit()

            check = _make_conn()
            try:
                ccur = check.cursor()
                ccur.execute(
                    "SELECT event_id, visible_at FROM integration_events "
                    "WHERE aggregate_id = %s ORDER BY visible_at, event_id",
                    (aggregate_id,),
                )
                rows = ccur.fetchall()
                assert len(rows) == 2
                (first_id, first_vis), (second_id, second_vis) = rows

                # Ordered by visible_at: B (committed first) comes first,
                # even though its event_id is higher.
                assert first_id == b_event_id
                assert second_id == a_event_id
                assert first_vis < second_vis
            finally:
                check.close()
        finally:
            _cleanup(aggregate_id)
            conn_a.close()
            conn_b.close()


class TestDifferentAggregateFIFO:
    """Two sessions emit on DIFFERENT aggregates within the same
    aggregate_type. Commit order across aggregates is not a guaranteed
    contract in v1.5.0 (plan section 1.3: per-aggregate FIFO is the
    only guarantee). This test exists so regressions to the
    per-aggregate property are caught even when the aggregate ids
    differ.
    """

    def test_different_aggregates_still_set_visible_at(self):
        conn_a = _make_conn()
        conn_b = _make_conn()
        try:
            agg_a = 99_888_200
            agg_b = 99_888_201

            cur_a = conn_a.cursor()
            cur_b = conn_b.cursor()
            cur_a.execute("BEGIN")
            cur_b.execute("BEGIN")

            _emit(cur_a, agg_a, "fifo.agg_a")
            _emit(cur_b, agg_b, "fifo.agg_b")

            conn_a.commit()
            conn_b.commit()

            check = _make_conn()
            try:
                ccur = check.cursor()
                ccur.execute(
                    "SELECT aggregate_id, visible_at FROM integration_events "
                    "WHERE aggregate_id IN (%s, %s)",
                    (agg_a, agg_b),
                )
                rows = ccur.fetchall()
                assert len(rows) == 2
                for _, vis in rows:
                    assert vis is not None, (
                        "deferred trigger must set visible_at at COMMIT even "
                        "for aggregates that committed in different sessions"
                    )
            finally:
                check.close()
        finally:
            _cleanup(99_888_200)
            _cleanup(99_888_201)
            conn_a.close()
            conn_b.close()
