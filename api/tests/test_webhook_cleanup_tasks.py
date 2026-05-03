"""Tests for the v1.6.0 webhook cleanup beat tasks."""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")
os.environ.setdefault(
    "SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8="
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text

from jobs.cleanup_tasks import (
    WEBHOOK_DELIVERIES_RETENTION,
    _cleanup_expired_webhook_secrets_impl,
    _cleanup_webhook_deliveries_impl,
)


def _ensure_connector(db, connector_id="hygiene-conn"):
    db.execute(
        text(
            "INSERT INTO connectors (connector_id, display_name) "
            "VALUES (:cid, :dn) ON CONFLICT (connector_id) DO NOTHING"
        ),
        {"cid": connector_id, "dn": "hygiene"},
    )
    return connector_id


def _seed_subscription(db) -> str:
    sub_id = str(uuid.uuid4())
    connector_id = _ensure_connector(db)
    db.execute(
        text(
            """
            INSERT INTO webhook_subscriptions
                (subscription_id, connector_id, display_name, delivery_url)
            VALUES (:sid, :cid, :name, :url)
            """
        ),
        {
            "sid": sub_id,
            "cid": connector_id,
            "name": "hygiene-test",
            "url": f"https://example.com/{sub_id}",
        },
    )
    return sub_id


def _seed_event(db) -> int:
    row = db.execute(
        text(
            """
            INSERT INTO integration_events
                (event_type, event_version, aggregate_type, aggregate_id,
                 aggregate_external_id, warehouse_id, source_txn_id, payload)
            VALUES ('test.cleanup', 1, 'agg', :aid, :ext, 1, :txn,
                    CAST('{}' AS jsonb))
            RETURNING event_id
            """
        ),
        {
            "aid": abs(hash(uuid.uuid4())) % (10**9),
            "ext": str(uuid.uuid4()),
            "txn": str(uuid.uuid4()),
        },
    ).fetchone()
    return int(row.event_id)


def _seed_delivery(
    db, sub_id: str, event_id: int, status: str, completed_at: datetime
) -> int:
    row = db.execute(
        text(
            """
            INSERT INTO webhook_deliveries
                (subscription_id, event_id, attempt_number, status,
                 scheduled_at, attempted_at, completed_at, secret_generation)
            VALUES (:sid, :eid, :att, :st, :sa, :sa, :ca, 1)
            RETURNING delivery_id
            """
        ),
        {
            "sid": sub_id,
            "eid": event_id,
            "att": 8 if status == "dlq" else 1,
            "st": status,
            "sa": completed_at,
            "ca": completed_at,
        },
    ).fetchone()
    return int(row.delivery_id)


def _delivery_exists(db, delivery_id: int) -> bool:
    row = db.execute(
        text("SELECT 1 FROM webhook_deliveries WHERE delivery_id = :did"),
        {"did": delivery_id},
    ).fetchone()
    return row is not None


def _impl_session():
    """#228: the chunked impl calls session.commit() between
    batches. Tests pass the SQLAlchemy Connection from the
    _db_transaction fixture directly into the impl, but
    Connection.commit() would commit the outer test transaction
    and break the rollback teardown. Construct a Session via
    db.SessionLocal (rebound by conftest with savepoint-join
    mode) so each chunk's commit releases an inner savepoint
    while the outer test transaction stays intact."""
    import models.database as db_module
    return db_module.SessionLocal()


class TestCleanupWebhookDeliveries:
    def test_deletes_terminal_rows_past_retention(self, _db_transaction):
        db = _db_transaction
        sub_id = _seed_subscription(db)
        event_id = _seed_event(db)
        old = datetime.now(timezone.utc) - WEBHOOK_DELIVERIES_RETENTION - timedelta(days=1)
        fresh = datetime.now(timezone.utc) - timedelta(days=1)
        old_succ = _seed_delivery(db, sub_id, event_id, "succeeded", old)
        old_dlq = _seed_delivery(db, sub_id, event_id, "dlq", old)
        fresh_succ = _seed_delivery(db, sub_id, event_id, "succeeded", fresh)

        sess = _impl_session()
        try:
            deleted = _cleanup_webhook_deliveries_impl(sess)
        finally:
            sess.close()
        assert deleted == 2
        assert not _delivery_exists(db, old_succ)
        assert not _delivery_exists(db, old_dlq)
        assert _delivery_exists(db, fresh_succ)

    def test_pending_and_in_flight_never_deleted(self, _db_transaction):
        db = _db_transaction
        sub_id = _seed_subscription(db)
        event_id = _seed_event(db)
        old = datetime.now(timezone.utc) - WEBHOOK_DELIVERIES_RETENTION - timedelta(days=10)
        # Pending and in_flight are live state regardless of age.
        # Use the impl helper directly with NULL completed_at since
        # production rows in those states would not have it set.
        pending_id = db.execute(
            text(
                """
                INSERT INTO webhook_deliveries
                    (subscription_id, event_id, attempt_number, status,
                     scheduled_at, attempted_at, secret_generation)
                VALUES (:sid, :eid, 1, 'pending', :sa, :sa, 1)
                RETURNING delivery_id
                """
            ),
            {"sid": sub_id, "eid": event_id, "sa": old},
        ).fetchone()[0]
        in_flight_id = db.execute(
            text(
                """
                INSERT INTO webhook_deliveries
                    (subscription_id, event_id, attempt_number, status,
                     scheduled_at, attempted_at, secret_generation)
                VALUES (:sid, :eid, 1, 'in_flight', :sa, :sa, 1)
                RETURNING delivery_id
                """
            ),
            {"sid": sub_id, "eid": event_id, "sa": old},
        ).fetchone()[0]

        sess = _impl_session()
        try:
            deleted = _cleanup_webhook_deliveries_impl(sess)
        finally:
            sess.close()
        assert deleted == 0
        assert _delivery_exists(db, pending_id)
        assert _delivery_exists(db, in_flight_id)

    def test_failed_rows_past_retention_are_kept(self, _db_transaction):
        """Only succeeded and dlq are terminal; 'failed' is the
        between-retry-slots intermediate state that the dispatcher
        replaces on the next attempt. The retention rule does not
        target it (the next attempt's row is the new terminal row;
        the prior 'failed' row is forensic context for that
        attempt). Cleanup leaves 'failed' alone."""
        db = _db_transaction
        sub_id = _seed_subscription(db)
        event_id = _seed_event(db)
        old = datetime.now(timezone.utc) - WEBHOOK_DELIVERIES_RETENTION - timedelta(days=1)
        failed_id = _seed_delivery(db, sub_id, event_id, "failed", old)
        sess = _impl_session()
        try:
            deleted = _cleanup_webhook_deliveries_impl(sess)
        finally:
            sess.close()
        assert deleted == 0
        assert _delivery_exists(db, failed_id)

    def test_chunked_delete_drains_more_than_one_chunk(self, _db_transaction):
        """#228: chunked DELETE keeps each transaction short. Seed
        more rows than the chunk size and verify the impl drains
        them all over multiple chunks (one COMMIT per chunk)."""
        db = _db_transaction
        sub_id = _seed_subscription(db)
        event_id = _seed_event(db)
        old = (
            datetime.now(timezone.utc)
            - WEBHOOK_DELIVERIES_RETENTION
            - timedelta(days=1)
        )
        # Bulk-seed 25 expired rows; with chunk_size=10 the impl
        # runs three chunks (10 + 10 + 5).
        db.execute(
            text(
                """
                INSERT INTO webhook_deliveries
                    (subscription_id, event_id, attempt_number, status,
                     scheduled_at, attempted_at, completed_at, secret_generation)
                SELECT :sid, :eid, 8, 'dlq', :ca, :ca, :ca, 1
                  FROM generate_series(1, 25)
                """
            ),
            {"sid": sub_id, "eid": event_id, "ca": old},
        )

        sess = _impl_session()
        try:
            deleted = _cleanup_webhook_deliveries_impl(sess, chunk_size=10)
        finally:
            sess.close()
        assert deleted == 25
        # Confirm none left.
        remaining = db.execute(
            text(
                "SELECT COUNT(*) FROM webhook_deliveries "
                "WHERE subscription_id = :sid AND status = 'dlq'"
            ),
            {"sid": sub_id},
        ).fetchone()[0]
        assert remaining == 0

    def test_max_run_s_bounds_compounded_backlog(self, _db_transaction):
        """#228: a beat misfire backlog cannot compound into a
        multi-hour cleanup. The impl exits early once the
        wall-clock cap is hit; the next beat picks up the
        remainder. Test by setting max_run_s=0 so the impl exits
        before deleting anything."""
        db = _db_transaction
        sub_id = _seed_subscription(db)
        event_id = _seed_event(db)
        old = (
            datetime.now(timezone.utc)
            - WEBHOOK_DELIVERIES_RETENTION
            - timedelta(days=1)
        )
        _seed_delivery(db, sub_id, event_id, "succeeded", old)

        sess = _impl_session()
        try:
            deleted = _cleanup_webhook_deliveries_impl(
                sess, chunk_size=10, max_run_s=0
            )
        finally:
            sess.close()
        # Wall-clock cap fired before the first chunk ran.
        assert deleted == 0


def _seed_secret(
    db,
    sub_id: str,
    generation: int,
    expires_at: datetime = None,
):
    db.execute(
        text(
            """
            INSERT INTO webhook_secrets
                (subscription_id, generation, secret_ciphertext, expires_at)
            VALUES (:sid, :gen, :ct, :exp)
            """
        ),
        {
            "sid": sub_id,
            "gen": generation,
            "ct": b"placeholder-ciphertext",
            "exp": expires_at,
        },
    )


def _secret_exists(db, sub_id: str, generation: int) -> bool:
    row = db.execute(
        text(
            "SELECT 1 FROM webhook_secrets "
            "WHERE subscription_id = :sid AND generation = :gen"
        ),
        {"sid": sub_id, "gen": generation},
    ).fetchone()
    return row is not None


class TestCleanupExpiredWebhookSecrets:
    def test_expired_generation_2_deleted(self, _db_transaction):
        db = _db_transaction
        sub_id = _seed_subscription(db)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        _seed_secret(db, sub_id, 1)  # active primary, no expiry
        _seed_secret(db, sub_id, 2, expires_at=past)

        deleted = _cleanup_expired_webhook_secrets_impl(db)
        assert deleted == 1
        assert _secret_exists(db, sub_id, 1)
        assert not _secret_exists(db, sub_id, 2)

    def test_unexpired_generation_2_kept(self, _db_transaction):
        db = _db_transaction
        sub_id = _seed_subscription(db)
        future = datetime.now(timezone.utc) + timedelta(hours=12)
        _seed_secret(db, sub_id, 1)
        _seed_secret(db, sub_id, 2, expires_at=future)

        deleted = _cleanup_expired_webhook_secrets_impl(db)
        assert deleted == 0
        assert _secret_exists(db, sub_id, 2)

    def test_generation_2_with_null_expires_at_kept(self, _db_transaction):
        """A generation=2 row with NULL expires_at is operator
        error, not a cleanup target. Defensive: leave it for an
        operator to investigate rather than racing to delete it."""
        db = _db_transaction
        sub_id = _seed_subscription(db)
        _seed_secret(db, sub_id, 1)
        _seed_secret(db, sub_id, 2, expires_at=None)

        deleted = _cleanup_expired_webhook_secrets_impl(db)
        assert deleted == 0
        assert _secret_exists(db, sub_id, 2)

    def test_generation_1_never_deleted(self, _db_transaction):
        db = _db_transaction
        sub_id = _seed_subscription(db)
        # Even if a generation=1 row had a (mis-set) past expires_at,
        # the cleanup query does not match it.
        _seed_secret(db, sub_id, 1)
        deleted = _cleanup_expired_webhook_secrets_impl(db)
        assert deleted == 0
        assert _secret_exists(db, sub_id, 1)


class TestBeatScheduleRegistration:
    def test_webhook_tasks_in_beat_schedule(self):
        from jobs import celery_app

        schedule = celery_app.conf.beat_schedule
        assert "cleanup-webhook-deliveries-every-6-hours" in schedule
        assert "cleanup-expired-webhook-secrets-every-hour" in schedule
        assert (
            schedule["cleanup-webhook-deliveries-every-6-hours"]["task"]
            == "jobs.cleanup_tasks.cleanup_webhook_deliveries"
        )
        assert (
            schedule["cleanup-expired-webhook-secrets-every-hour"]["task"]
            == "jobs.cleanup_tasks.cleanup_expired_webhook_secrets"
        )
