"""Tests for the v1.6.0 D1 env-validator (#173 / plan §2.10).

Each range and combination guard is exercised independently so a
regression in one validator does not cascade into other test
failures. Mirrors the table-driven shape of the V-201 #142
weak-pepper validation tests in v1.5.1.
"""

import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from services.webhook_dispatcher import env_validator


# Range tunables (var, lower, upper). Defaults are inside the
# validator; tests poke values OUTSIDE the bounds and confirm
# refusal, then poke values INSIDE and confirm acceptance.
_RANGE_CASES = [
    ("DISPATCHER_HTTP_TIMEOUT_MS", 1000, 60000),
    ("DISPATCHER_FALLBACK_POLL_MS", 500, 10000),
    ("DISPATCHER_SHUTDOWN_DRAIN_S", 1, 300),
    ("DISPATCHER_MAX_CONCURRENT_POSTS", 1, 100),
    ("DISPATCHER_MAX_PENDING_HARD_CAP", 1000, 10_000_000),
    ("DISPATCHER_MAX_DLQ_HARD_CAP", 100, 1_000_000),
]


def _clean_env(monkeypatch):
    """Wipe every env var the validator looks at so a host-shell
    leak does not interfere with the test. REDIS_URL is set to a
    valid placeholder because #212 added it as a required env;
    SENTRY_PUBSUB_HMAC_KEY is set to a valid 32-byte placeholder
    because #227 added it as a required env. Tests that target
    those guards specifically delete the var after this helper runs."""
    for name, _lo, _hi in _RANGE_CASES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("SENTRY_ALLOW_HTTP_WEBHOOKS", raising=False)
    monkeypatch.delenv("SENTRY_ALLOW_INTERNAL_WEBHOOKS", raising=False)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.delenv("DISPATCHER_ENABLED", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv(
        "SENTRY_PUBSUB_HMAC_KEY",
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    )


class TestRangeValidators:
    @pytest.mark.parametrize("name,lo,hi", _RANGE_CASES, ids=[c[0] for c in _RANGE_CASES])
    def test_below_lower_bound_refuses_boot(self, monkeypatch, name, lo, hi):
        _clean_env(monkeypatch)
        monkeypatch.setenv(name, str(lo - 1))
        with pytest.raises(env_validator.DispatcherEnvError) as excinfo:
            env_validator.validate_or_die()
        msg = str(excinfo.value)
        assert name in msg, f"refusal message must name the offending var; got {msg!r}"

    @pytest.mark.parametrize("name,lo,hi", _RANGE_CASES, ids=[c[0] for c in _RANGE_CASES])
    def test_above_upper_bound_refuses_boot(self, monkeypatch, name, lo, hi):
        _clean_env(monkeypatch)
        monkeypatch.setenv(name, str(hi + 1))
        with pytest.raises(env_validator.DispatcherEnvError) as excinfo:
            env_validator.validate_or_die()
        assert name in str(excinfo.value)

    @pytest.mark.parametrize("name,lo,hi", _RANGE_CASES, ids=[c[0] for c in _RANGE_CASES])
    def test_at_boundary_accepts(self, monkeypatch, name, lo, hi):
        _clean_env(monkeypatch)
        monkeypatch.setenv(name, str(lo))
        env_validator.validate_or_die()  # no raise
        monkeypatch.setenv(name, str(hi))
        env_validator.validate_or_die()  # no raise

    def test_unset_var_uses_default_no_raise(self, monkeypatch):
        _clean_env(monkeypatch)
        env_validator.validate_or_die()  # no raise

    def test_non_integer_value_refuses(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("DISPATCHER_HTTP_TIMEOUT_MS", "not-a-number")
        with pytest.raises(env_validator.DispatcherEnvError) as excinfo:
            env_validator.validate_or_die()
        assert "not-a-number" in str(excinfo.value) or "valid integer" in str(excinfo.value)


class TestCombinationGuards:
    def test_http_plus_internal_refuses_in_any_environment(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("SENTRY_ALLOW_HTTP_WEBHOOKS", "true")
        monkeypatch.setenv("SENTRY_ALLOW_INTERNAL_WEBHOOKS", "true")
        # No FLASK_ENV set -> not production. The combination is still
        # rejected because the SSRF-into-VPC surface is operator error
        # in any environment.
        with pytest.raises(env_validator.DispatcherEnvError) as excinfo:
            env_validator.validate_or_die()
        msg = str(excinfo.value)
        assert "SENTRY_ALLOW_HTTP_WEBHOOKS" in msg
        assert "SENTRY_ALLOW_INTERNAL_WEBHOOKS" in msg

    def test_internal_in_production_refuses(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("SENTRY_ALLOW_INTERNAL_WEBHOOKS", "true")
        monkeypatch.setenv("FLASK_ENV", "production")
        with pytest.raises(env_validator.DispatcherEnvError) as excinfo:
            env_validator.validate_or_die()
        assert "SENTRY_ALLOW_INTERNAL_WEBHOOKS" in str(excinfo.value)
        assert "production" in str(excinfo.value)

    def test_internal_in_development_does_not_refuse(self, monkeypatch):
        """Dev/CI legitimately needs to dispatch to localhost or
        a private-range mock consumer. The opt-out is allowed
        outside production; the validator must not refuse."""
        _clean_env(monkeypatch)
        monkeypatch.setenv("SENTRY_ALLOW_INTERNAL_WEBHOOKS", "true")
        monkeypatch.setenv("FLASK_ENV", "development")
        env_validator.validate_or_die()  # no raise

    def test_http_in_production_logs_critical_does_not_refuse(self, monkeypatch, caplog):
        """The http opt-out is a soft warning in production; the
        deployment continues to boot but the CRITICAL log line
        keeps the acknowledgement visible in compose logs."""
        import logging

        _clean_env(monkeypatch)
        monkeypatch.setenv("SENTRY_ALLOW_HTTP_WEBHOOKS", "true")
        monkeypatch.setenv("FLASK_ENV", "production")
        with caplog.at_level(logging.CRITICAL, logger="webhook_dispatcher.env_validator"):
            env_validator.validate_or_die()  # no raise
        assert any(
            "SENTRY_ALLOW_HTTP_WEBHOOKS" in record.getMessage()
            for record in caplog.records
        ), "production http opt-out must produce a CRITICAL log line"

    def test_http_in_development_does_not_refuse_or_warn(self, monkeypatch, caplog):
        import logging

        _clean_env(monkeypatch)
        monkeypatch.setenv("SENTRY_ALLOW_HTTP_WEBHOOKS", "true")
        monkeypatch.setenv("FLASK_ENV", "development")
        with caplog.at_level(logging.CRITICAL, logger="webhook_dispatcher.env_validator"):
            env_validator.validate_or_die()
        assert not [
            record for record in caplog.records
            if "SENTRY_ALLOW_HTTP_WEBHOOKS" in record.getMessage()
        ], "dev http opt-out should not produce a CRITICAL log; only production opt-out should"

    def test_default_combination_is_safe_no_raise_no_warn(self, monkeypatch, caplog):
        import logging

        _clean_env(monkeypatch)
        monkeypatch.setenv("FLASK_ENV", "production")
        with caplog.at_level(logging.CRITICAL, logger="webhook_dispatcher.env_validator"):
            env_validator.validate_or_die()  # no raise
        assert not caplog.records, "default (both opt-outs unset) must produce no CRITICAL warnings"


class TestEnvVarHelpers:
    def test_int_var_returns_default_when_unset(self, monkeypatch):
        _clean_env(monkeypatch)
        assert env_validator.int_var("DISPATCHER_HTTP_TIMEOUT_MS") == 10000
        assert env_validator.int_var("DISPATCHER_MAX_CONCURRENT_POSTS") == 16

    def test_int_var_re_reads_on_every_call(self, monkeypatch):
        """V-217 #156 lesson: tunables must NOT be frozen at import.
        Setting the var after the first read must take effect on the
        next call."""
        _clean_env(monkeypatch)
        first = env_validator.int_var("DISPATCHER_HTTP_TIMEOUT_MS")
        monkeypatch.setenv("DISPATCHER_HTTP_TIMEOUT_MS", "5000")
        second = env_validator.int_var("DISPATCHER_HTTP_TIMEOUT_MS")
        assert first == 10000
        assert second == 5000

    def test_bool_var_only_true_for_literal_lowercase_true(self, monkeypatch):
        _clean_env(monkeypatch)
        for value, expected in (
            ("true", True),
            ("True", True),
            ("TRUE", True),  # case-insensitive after lowercase
            ("false", False),
            ("False", False),
            ("0", False),
            ("1", False),
            ("yes", False),
            ("", False),
        ):
            monkeypatch.setenv("SENTRY_ALLOW_HTTP_WEBHOOKS", value)
            assert (
                env_validator.bool_var("SENTRY_ALLOW_HTTP_WEBHOOKS") is expected
            ), f"bool_var({value!r}) should be {expected}"

    def test_int_var_unknown_var_raises(self, monkeypatch):
        _clean_env(monkeypatch)
        with pytest.raises(env_validator.DispatcherEnvError):
            env_validator.int_var("DISPATCHER_NONSENSE")


class TestRequiredEnvVars:
    """#212: REDIS_URL is required when the dispatcher is enabled.
    Pre-fix the dispatcher booted cleanly without REDIS_URL and
    cross-worker invalidation publishes silently no-op'd."""

    def test_unset_redis_url_refuses_boot(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.delenv("REDIS_URL", raising=False)
        with pytest.raises(env_validator.DispatcherEnvError) as excinfo:
            env_validator.validate_or_die()
        assert "REDIS_URL" in str(excinfo.value)

    def test_empty_redis_url_refuses_boot(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("REDIS_URL", "")
        with pytest.raises(env_validator.DispatcherEnvError) as excinfo:
            env_validator.validate_or_die()
        assert "REDIS_URL" in str(excinfo.value)

    def test_kill_switch_skips_required_check(self, monkeypatch):
        """DISPATCHER_ENABLED=false bypasses the required-env guard
        so an operator can run the kill switch even without Redis."""
        _clean_env(monkeypatch)
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.setenv("DISPATCHER_ENABLED", "false")
        env_validator.validate_or_die()  # no raise

    def test_set_redis_url_accepted(self, monkeypatch):
        _clean_env(monkeypatch)
        # _clean_env sets REDIS_URL; validate succeeds.
        env_validator.validate_or_die()  # no raise


class TestPubsubHmacKeyBootGuard:
    """#227: SENTRY_PUBSUB_HMAC_KEY is required when the dispatcher
    is enabled. The wake module's pubsub envelope is HMAC-signed
    with this key; an unset / placeholder / short key would let a
    Redis-side attacker forge subscription_event messages."""

    def test_unset_key_refuses_boot(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.delenv("SENTRY_PUBSUB_HMAC_KEY", raising=False)
        with pytest.raises(env_validator.DispatcherEnvError) as excinfo:
            env_validator.validate_or_die()
        assert "SENTRY_PUBSUB_HMAC_KEY" in str(excinfo.value)

    def test_empty_key_refuses_boot(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("SENTRY_PUBSUB_HMAC_KEY", "")
        with pytest.raises(env_validator.DispatcherEnvError) as excinfo:
            env_validator.validate_or_die()
        assert "SENTRY_PUBSUB_HMAC_KEY" in str(excinfo.value)

    def test_placeholder_key_refuses_boot(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv(
            "SENTRY_PUBSUB_HMAC_KEY",
            "replace-me-with-secrets-token-hex-32",
        )
        with pytest.raises(env_validator.DispatcherEnvError) as excinfo:
            env_validator.validate_or_die()
        assert "placeholder" in str(excinfo.value)

    def test_short_key_refuses_boot(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("SENTRY_PUBSUB_HMAC_KEY", "short")
        with pytest.raises(env_validator.DispatcherEnvError) as excinfo:
            env_validator.validate_or_die()
        assert "32 bytes" in str(excinfo.value)

    def test_kill_switch_skips_pubsub_key_check(self, monkeypatch):
        """DISPATCHER_ENABLED=false bypasses the pubsub-key guard
        too, so an operator can run the kill switch without having
        configured the key."""
        _clean_env(monkeypatch)
        monkeypatch.delenv("SENTRY_PUBSUB_HMAC_KEY", raising=False)
        monkeypatch.setenv("DISPATCHER_ENABLED", "false")
        env_validator.validate_or_die()  # no raise


class TestPubsubPublishCounter:
    """#212: pubsub publish failures increment a module-level
    counter and emit a WARNING log on every failure path. The
    counter surfaces via WakeOrchestrator.health_snapshot() so
    operators can grep whether the publish path is alive."""

    def test_unset_url_increments_counter_and_logs(self, monkeypatch, caplog):
        import logging
        from services.webhook_dispatcher import wake as wake_module

        wake_module.reset_publish_failure_count()
        before = wake_module.get_publish_failure_count()
        with caplog.at_level(logging.WARNING, logger="webhook_dispatcher.wake"):
            wake_module.publish_subscription_event(None, "sub-id", "paused")
        assert wake_module.get_publish_failure_count() == before + 1
        assert any(
            "REDIS_URL is unset" in r.getMessage() for r in caplog.records
        )

    def test_empty_url_increments_counter(self, monkeypatch):
        from services.webhook_dispatcher import wake as wake_module
        wake_module.reset_publish_failure_count()
        wake_module.publish_subscription_event("", "sub-id", "paused")
        assert wake_module.get_publish_failure_count() == 1

    def test_unreachable_redis_increments_counter_and_logs_host(
        self, monkeypatch, caplog
    ):
        """An unreachable Redis URL hits the exception path; the
        counter increments and the WARNING log includes the host
        but NOT the password."""
        import logging
        from services.webhook_dispatcher import wake as wake_module

        wake_module.reset_publish_failure_count()
        before = wake_module.get_publish_failure_count()
        with caplog.at_level(logging.WARNING, logger="webhook_dispatcher.wake"):
            wake_module.publish_subscription_event(
                "redis://:secretpw@127.0.0.1:1/0",
                "sub-id",
                "paused",
            )
        assert wake_module.get_publish_failure_count() >= before + 1
        msg = " ".join(r.getMessage() for r in caplog.records)
        # Host shows up; password must not.
        assert "127.0.0.1" in msg
        assert "secretpw" not in msg

    def test_health_snapshot_includes_publish_counter(self, monkeypatch):
        from queue import Queue
        from services.webhook_dispatcher import wake as wake_module

        wake_module.reset_publish_failure_count()
        wake_module.publish_subscription_event(None, "sub-id", "paused")

        # Build an orchestrator without starting threads. health_snapshot
        # only reads counters; no DB / Redis required.
        orch = wake_module.WakeOrchestrator(
            database_url="postgresql://unused/db",
            redis_url=None,
            fallback_poll_ms=2000,
        )
        snap = orch.health_snapshot()
        assert "pubsub_publish_failure_count" in snap
        assert snap["pubsub_publish_failure_count"] >= 1
