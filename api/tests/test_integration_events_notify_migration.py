"""Schema-level tests for migration 031 (v1.6.0 #164).

Locks in the AFTER UPDATE OF visible_at trigger that fires
pg_notify('integration_events_visible', event_id::text) when a
row transitions visible_at NULL -> NOT NULL.

The end-to-end trigger chain under test is:

    INSERT integration_events (visible_at IS NULL)
      -> deferred CONSTRAINT TRIGGER tr_integration_events_visible_at
         (v1.5.0 / migration 020) fires at COMMIT and runs
         UPDATE integration_events SET visible_at = clock_timestamp()
      -> migration 031's AFTER UPDATE OF visible_at trigger fires
         and runs pg_notify('integration_events_visible', event_id::text)
      -> outer COMMIT releases the queued NOTIFY to listeners.

The happy-path test exercises the whole chain via a single emit
plus commit, exactly as production emit sites do. The negative
test asserts the NULL->NOT-NULL gate so an idempotent re-stamp
of visible_at on an already-visible row does not emit a duplicate
NOTIFY (which would force the dispatcher to re-evaluate an event
it has already considered).
"""

import os
import select
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest  # noqa: F401  -- imported for style parity with sibling migration tests


def _make_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _drain(listener, seconds=0.3):
    """Drain any pending notifies on the listener connection so a
    later assertion is not contaminated by a NOTIFY from an earlier
    statement in the same test."""
    deadline_iters = max(1, int(seconds / 0.05))
    for _ in range(deadline_iters):
        if select.select([listener], [], [], 0.05) != ([], [], []):
            listener.poll()
    listener.notifies.clear()


class TestIntegrationEventsNotifyTriggerRegistration:
    """The trigger and its function are easy to lose in a future
    schema.sql edit; this test makes that loss loud."""

    def test_trigger_is_registered(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT t.tgname, t.tgenabled, c.relname, p.proname,
                       pg_get_functiondef(p.oid)
                  FROM pg_trigger t
                  JOIN pg_class c   ON c.oid = t.tgrelid
                  JOIN pg_proc p    ON p.oid = t.tgfoid
                 WHERE t.tgname = 'tr_integration_events_notify'
                   AND NOT t.tgisinternal
                """
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None, (
            "tr_integration_events_notify must be registered on integration_events"
        )
        tgname, tgenabled, relname, proname, fn_def = row
        assert relname == "integration_events"
        assert proname == "notify_integration_event_visible"
        # 'O' = trigger fires in origin and local sessions (default ENABLED).
        assert tgenabled == "O"
        # Lock the channel name at the registration layer so a rename
        # surfaces here with an actionable message rather than as a
        # generic "no NOTIFY arrived" failure in the chain test below.
        assert "'integration_events_visible'" in fn_def, (
            "trigger function must pg_notify on the 'integration_events_visible' "
            "channel; if you renamed the channel, update both the dispatcher "
            "LISTEN target and this assertion"
        )


class TestIntegrationEventsNotifyTriggerChain:
    """Drive the full deferred-trigger -> UPDATE -> AFTER-UPDATE-trigger
    -> pg_notify chain under real semantics."""

    def test_insert_then_commit_fires_visibility_notify(self):
        listener = _make_conn()
        listener.autocommit = True
        listener_cur = listener.cursor()
        listener_cur.execute("LISTEN integration_events_visible")

        # Distinct source_txn_id so the idempotency UNIQUE constraint
        # never collides with a parallel test run on the same database.
        source_txn = uuid.uuid4()
        aggregate_external = uuid.uuid4()
        aggregate_id = abs(hash(source_txn)) % (10**9)

        writer = _make_conn()
        try:
            wcur = writer.cursor()
            wcur.execute(
                """
                INSERT INTO integration_events (
                    event_type, event_version, aggregate_type,
                    aggregate_id, aggregate_external_id, warehouse_id,
                    source_txn_id, payload
                ) VALUES (
                    'test.notify_chain', 1, 'test_aggregate',
                    %s, %s, 1,
                    %s, '{}'::jsonb
                ) RETURNING event_id
                """,
                (aggregate_id, str(aggregate_external), str(source_txn)),
            )
            event_id = wcur.fetchone()[0]
            writer.commit()  # deferred visible_at trigger fires here

            got = None
            for _ in range(40):  # up to ~4s
                if select.select([listener], [], [], 0.1) == ([], [], []):
                    continue
                listener.poll()
                if listener.notifies:
                    got = listener.notifies.pop(0)
                    break

            assert got is not None, (
                "expected NOTIFY on integration_events_visible after "
                "INSERT + COMMIT (deferred-trigger -> UPDATE -> "
                "AFTER-UPDATE-trigger -> pg_notify chain)"
            )
            assert got.channel == "integration_events_visible"
            assert got.payload == str(event_id)
        finally:
            cleanup = _make_conn()
            cleanup.autocommit = True
            cleanup.cursor().execute(
                "DELETE FROM integration_events WHERE source_txn_id = %s",
                (str(source_txn),),
            )
            cleanup.close()
            writer.close()
            listener_cur.close()
            listener.close()


class TestIntegrationEventsNotifyTriggerGate:
    """The NULL -> NOT NULL gate is the line that prevents
    duplicate NOTIFYs from re-stamping visible_at. If a future
    refactor weakens the gate the dispatcher would re-evaluate
    already-considered events. Lock the gate down here."""

    def test_update_when_visible_at_already_set_does_not_fire(self):
        # Phase 1: insert + commit so visible_at is set and one
        # NOTIFY has been emitted by the trigger chain. Drain it.
        listener = _make_conn()
        listener.autocommit = True
        listener_cur = listener.cursor()
        listener_cur.execute("LISTEN integration_events_visible")

        source_txn = uuid.uuid4()
        aggregate_external = uuid.uuid4()
        aggregate_id = abs(hash(source_txn)) % (10**9)

        writer = _make_conn()
        try:
            wcur = writer.cursor()
            wcur.execute(
                """
                INSERT INTO integration_events (
                    event_type, event_version, aggregate_type,
                    aggregate_id, aggregate_external_id, warehouse_id,
                    source_txn_id, payload
                ) VALUES (
                    'test.notify_gate', 1, 'test_aggregate',
                    %s, %s, 1,
                    %s, '{}'::jsonb
                ) RETURNING event_id
                """,
                (aggregate_id, str(aggregate_external), str(source_txn)),
            )
            event_id = wcur.fetchone()[0]
            writer.commit()
            _drain(listener, seconds=1.0)  # drop the visibility notify

            # Phase 2: manual UPDATE that re-stamps visible_at while
            # it is already NOT NULL. This is the path a misbehaved
            # admin tool or a future maintenance migration could take;
            # the gate must keep it silent.
            wcur.execute(
                "UPDATE integration_events "
                "   SET visible_at = clock_timestamp() "
                " WHERE event_id = %s",
                (event_id,),
            )
            writer.commit()

            # Allow time for a rogue NOTIFY to arrive; absence is
            # the assertion. 0.5s is enough since pg_notify is
            # released at COMMIT, not deferred.
            for _ in range(5):
                if select.select([listener], [], [], 0.1) != ([], [], []):
                    listener.poll()

            assert not listener.notifies, (
                "NOTIFY must NOT fire when visible_at is updated while "
                "already NOT NULL; the NULL -> NOT NULL gate is the "
                "deduplication invariant for the dispatcher wake path"
            )
        finally:
            cleanup = _make_conn()
            cleanup.autocommit = True
            cleanup.cursor().execute(
                "DELETE FROM integration_events WHERE source_txn_id = %s",
                (str(source_txn),),
            )
            cleanup.close()
            writer.close()
            listener_cur.close()
            listener.close()
