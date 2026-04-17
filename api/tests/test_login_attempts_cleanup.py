"""
V-024: login_attempts table cleanup and key-length cap.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from jobs.cleanup_tasks import _cleanup_login_attempts_impl, LOGIN_ATTEMPTS_RETENTION
from routes.auth import _normalize_rate_limit_key, LOGIN_ATTEMPT_KEY_MAX_LEN


def _seed_attempt(db, key, last_attempt, locked_until=None):
    db.execute(
        text(
            "INSERT INTO login_attempts (key, attempts, last_attempt, locked_until) "
            "VALUES (:k, 1, :ts, :lu) "
            "ON CONFLICT (key) DO UPDATE "
            "SET last_attempt = :ts, locked_until = :lu"
        ),
        {"k": key, "ts": last_attempt, "lu": locked_until},
    )


class TestCleanupLoginAttempts:
    def test_deletes_rows_older_than_retention(self, _db_transaction):
        db = _db_transaction
        old = datetime.now(timezone.utc) - LOGIN_ATTEMPTS_RETENTION - timedelta(minutes=10)
        fresh = datetime.now(timezone.utc) - timedelta(minutes=1)
        _seed_attempt(db, "ip:old-1", old)
        _seed_attempt(db, "ip:old-2", old)
        _seed_attempt(db, "ip:fresh", fresh)

        deleted = _cleanup_login_attempts_impl(db)
        assert deleted == 2

        remaining = db.execute(
            text("SELECT key FROM login_attempts WHERE key LIKE 'ip:%'")
        ).fetchall()
        keys = {r.key for r in remaining}
        assert "ip:fresh" in keys
        assert "ip:old-1" not in keys
        assert "ip:old-2" not in keys

    def test_preserves_rows_still_under_lockout(self, _db_transaction):
        db = _db_transaction
        old_attempt = datetime.now(timezone.utc) - LOGIN_ATTEMPTS_RETENTION - timedelta(minutes=5)
        still_locked = datetime.now(timezone.utc) + timedelta(minutes=10)
        _seed_attempt(db, "ip:locked-out", old_attempt, locked_until=still_locked)

        _cleanup_login_attempts_impl(db)

        remaining = db.execute(
            text("SELECT key FROM login_attempts WHERE key = 'ip:locked-out'")
        ).fetchall()
        assert len(remaining) == 1

    def test_no_rows_is_noop(self, _db_transaction):
        db = _db_transaction
        db.execute(text("DELETE FROM login_attempts"))
        deleted = _cleanup_login_attempts_impl(db)
        assert deleted == 0


class TestKeyLengthCap:
    def test_short_key_unchanged(self):
        key = "ip:192.168.1.1"
        assert _normalize_rate_limit_key(key) == key

    def test_key_at_max_length_unchanged(self):
        key = "x" * LOGIN_ATTEMPT_KEY_MAX_LEN
        assert _normalize_rate_limit_key(key) == key

    def test_long_key_hashed(self):
        key = "user:" + ("a" * 500)
        result = _normalize_rate_limit_key(key)
        assert len(result) == 64  # sha256 hex digest
        assert result != key
        # Must be stable: same input -> same output.
        assert _normalize_rate_limit_key(key) == result

    def test_different_long_keys_produce_different_hashes(self):
        a = _normalize_rate_limit_key("user:" + ("a" * 500))
        b = _normalize_rate_limit_key("user:" + ("b" * 500))
        assert a != b


class TestBeatScheduleRegistered:
    def test_cleanup_task_on_beat_schedule(self):
        from jobs import celery_app
        schedule = celery_app.conf.beat_schedule
        assert any(
            entry.get("task") == "jobs.cleanup_tasks.cleanup_login_attempts"
            for entry in schedule.values()
        )
