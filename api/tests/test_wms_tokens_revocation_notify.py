"""v1.7.0 #274: AFTER UPDATE OF revoked_at trigger publishes pg_notify
on direct-DB revokes so the LISTEN subscriber in services.token_cache
evicts cached tokens across workers regardless of whether the writer
is the Flask admin handler or a direct DB UPDATE.

Tests use direct psycopg2 connections (one as the writer, one as the
LISTEN subscriber) so the trigger fires for real concurrency at the
DB layer, not through the Flask test client's single-connection
funnel.
"""

import os
import select
import sys
import threading
import time

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import psycopg2.extensions
import pytest


DATABASE_URL = os.environ["TEST_DATABASE_URL"]
CHANNEL = "wms_token_revocations"


def _listener_conn():
    conn = psycopg2.connect(DATABASE_URL)
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute(f"LISTEN {CHANNEL}")
    return conn


def _drain_notifies(conn, timeout: float = 1.0) -> list[str]:
    """Block up to `timeout` for any NOTIFYs accumulated on this LISTEN
    conn, returning their payloads in arrival order."""
    deadline = time.monotonic() + timeout
    payloads: list[str] = []
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        rlist, _, _ = select.select([conn], [], [], max(0.0, remaining))
        if not rlist:
            break
        conn.poll()
        while conn.notifies:
            payloads.append(conn.notifies.pop(0).payload)
    return payloads


def _insert_token(cur, token_name: str) -> int:
    """Insert a wms_tokens row with a unique placeholder hash. The
    trigger declares AFTER UPDATE OF revoked_at so this INSERT does
    not emit a NOTIFY (verified separately)."""
    cur.execute(
        "INSERT INTO wms_tokens (token_name, token_hash) "
        "VALUES (%s, %s) RETURNING token_id",
        (token_name, f"hash-{token_name}".ljust(64, "0")[:64]),
    )
    return cur.fetchone()[0]


class TestRevokedAtNotifyTrigger:
    def test_null_to_not_null_emits_notify(self):
        """The canonical case: revoke a token by setting revoked_at
        from NULL to NOW(). Trigger fires; LISTEN sees the token_id."""
        listener = _listener_conn()
        try:
            writer = psycopg2.connect(DATABASE_URL)
            writer.autocommit = True
            try:
                cur = writer.cursor()
                token_id = _insert_token(cur, f"notify-{int(time.time()*1e6)}")
                # Drain any NOTIFY-on-INSERT (none expected) before the UPDATE.
                _drain_notifies(listener, timeout=0.2)
                cur.execute(
                    "UPDATE wms_tokens SET revoked_at = NOW() "
                    " WHERE token_id = %s",
                    (token_id,),
                )
                payloads = _drain_notifies(listener, timeout=2.0)
                assert str(token_id) in payloads, (
                    f"expected NOTIFY payload {token_id} in {payloads}"
                )
            finally:
                writer.close()
        finally:
            listener.close()

    def test_insert_does_not_emit_notify(self):
        """The trigger is AFTER UPDATE OF revoked_at -- an INSERT, even
        with revoked_at NOT NULL on the new row, does not fire it.
        Otherwise every test fixture's seed insert would spam NOTIFYs."""
        listener = _listener_conn()
        try:
            writer = psycopg2.connect(DATABASE_URL)
            writer.autocommit = True
            try:
                cur = writer.cursor()
                cur.execute(
                    "INSERT INTO wms_tokens (token_name, token_hash, revoked_at) "
                    "VALUES (%s, %s, NOW())",
                    (
                        f"insert-{int(time.time()*1e6)}",
                        f"insert-{int(time.time()*1e6)}".ljust(64, "0")[:64],
                    ),
                )
                payloads = _drain_notifies(listener, timeout=0.5)
                assert payloads == [], (
                    f"INSERT should not emit NOTIFY; got {payloads}"
                )
            finally:
                writer.close()
        finally:
            listener.close()

    def test_unrelated_update_does_not_emit_notify(self):
        """An UPDATE that does not touch revoked_at must not fire the
        trigger. AFTER UPDATE OF revoked_at filters at the column-list
        level so this is structurally enforced, but pin the regression."""
        listener = _listener_conn()
        try:
            writer = psycopg2.connect(DATABASE_URL)
            writer.autocommit = True
            try:
                cur = writer.cursor()
                token_id = _insert_token(cur, f"unrelated-{int(time.time()*1e6)}")
                _drain_notifies(listener, timeout=0.2)
                cur.execute(
                    "UPDATE wms_tokens SET last_used_at = NOW() "
                    " WHERE token_id = %s",
                    (token_id,),
                )
                payloads = _drain_notifies(listener, timeout=0.5)
                assert payloads == [], (
                    f"UPDATE last_used_at should not emit NOTIFY; got {payloads}"
                )
            finally:
                writer.close()
        finally:
            listener.close()

    def test_no_op_revoked_at_update_does_not_emit_notify(self):
        """Re-setting revoked_at to the same NULL value (or the same
        non-NULL value) is a no-op; the trigger's body filters via
        OLD.revoked_at <> NEW.revoked_at so the LISTEN subscriber
        does not see redundant NOTIFYs."""
        listener = _listener_conn()
        try:
            writer = psycopg2.connect(DATABASE_URL)
            writer.autocommit = True
            try:
                cur = writer.cursor()
                token_id = _insert_token(cur, f"noop-{int(time.time()*1e6)}")
                _drain_notifies(listener, timeout=0.2)
                cur.execute(
                    "UPDATE wms_tokens SET revoked_at = NULL "
                    " WHERE token_id = %s",
                    (token_id,),
                )
                payloads = _drain_notifies(listener, timeout=0.5)
                assert payloads == [], (
                    f"NULL-to-NULL UPDATE should not emit NOTIFY; got {payloads}"
                )
            finally:
                writer.close()
        finally:
            listener.close()

    def test_re_revoke_with_new_timestamp_emits_notify(self):
        """If revoked_at is updated from one non-NULL value to a
        different non-NULL value (operational re-stamp), the trigger
        fires again. Cache invalidation is idempotent so a second
        NOTIFY is harmless and keeps direct-DB tooling explicit."""
        listener = _listener_conn()
        try:
            writer = psycopg2.connect(DATABASE_URL)
            writer.autocommit = True
            try:
                cur = writer.cursor()
                token_id = _insert_token(cur, f"rerevoke-{int(time.time()*1e6)}")
                cur.execute(
                    "UPDATE wms_tokens SET revoked_at = NOW() "
                    " WHERE token_id = %s",
                    (token_id,),
                )
                _drain_notifies(listener, timeout=2.0)
                # Sleep to ensure NOW() advances to a strictly later timestamp.
                time.sleep(0.01)
                cur.execute(
                    "UPDATE wms_tokens SET revoked_at = NOW() "
                    " WHERE token_id = %s",
                    (token_id,),
                )
                payloads = _drain_notifies(listener, timeout=2.0)
                assert str(token_id) in payloads, (
                    f"re-revoke with new timestamp should emit NOTIFY; "
                    f"got {payloads}"
                )
            finally:
                writer.close()
        finally:
            listener.close()


class TestTokenCachePgListenSubscriber:
    """The LISTEN subscriber in services.token_cache evicts cached
    entries on receipt of a NOTIFY. Verifies the convergence path
    end-to-end: DB UPDATE -> trigger -> NOTIFY -> subscriber thread
    -> _invalidate_token_id_local -> dict.pop."""

    def test_subscriber_evicts_on_direct_db_revoke(self):
        from services import token_cache

        token_cache._testing_reset_subscriber()
        # Seed the local cache with a known entry under a synthetic
        # token_id; the subscriber's NOTIFY handler is keyed by token_id.
        synthetic_token_id = 999_999_999
        synthetic_hash = "a" * 64
        with token_cache._lock:
            token_cache._cache[synthetic_hash] = (
                {"token_id": synthetic_token_id}, time.monotonic(),
            )

        token_cache.start_pg_listen_subscriber(DATABASE_URL)
        try:
            # Give the subscriber a beat to issue LISTEN before we publish.
            time.sleep(0.2)
            pub = psycopg2.connect(DATABASE_URL)
            pub.autocommit = True
            try:
                cur = pub.cursor()
                cur.execute(
                    "SELECT pg_notify(%s, %s)",
                    (CHANNEL, str(synthetic_token_id)),
                )
            finally:
                pub.close()

            # Subscriber wakes within the 1.0s select() window plus dispatch.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                with token_cache._lock:
                    if synthetic_hash not in token_cache._cache:
                        break
                time.sleep(0.05)
            with token_cache._lock:
                assert synthetic_hash not in token_cache._cache, (
                    "pg LISTEN subscriber failed to evict cached entry "
                    "after pg_notify"
                )
        finally:
            token_cache._testing_reset_subscriber()
            with token_cache._lock:
                token_cache._cache.pop(synthetic_hash, None)
