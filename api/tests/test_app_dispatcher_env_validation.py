"""#238: api boot path runs dispatcher_env.validate_or_die.

The api container reads dispatcher env vars
(DISPATCHER_MAX_PENDING_HARD_CAP, DISPATCHER_MAX_DLQ_HARD_CAP,
DISPATCHER_REPLAY_BATCH_HARD_CAP, SENTRY_PUBSUB_HMAC_KEY, etc.)
for admin-endpoint enforcement and for the cross-worker pubsub
publisher. Pre-#238 those values were never validated at boot
in the api container -- only in the dispatcher container. Both
containers must fail loudly with the same range messages so a
typo'd env on the api alone cannot silently fall back to
defaults.
"""

import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


def _build_app():
    """Construct a fresh Flask app via create_app under whatever
    env state the test prepared. Used in lieu of the session-scoped
    ``app`` fixture so each test sees its own boot path."""
    from app import create_app

    return create_app()


class TestApiBootValidatesDispatcherEnv:
    def test_out_of_range_pending_hard_cap_refuses_boot(self, monkeypatch):
        """A typo'd or out-of-range value tripped the dispatcher
        container's boot guard before #238; the api container
        booted happily and read the value via int_var. Both
        containers should now refuse the same misconfiguration."""
        monkeypatch.setenv("API_BIND_HOST", "127.0.0.1")
        # Above the validator range upper bound (10_000_000).
        monkeypatch.setenv("DISPATCHER_MAX_PENDING_HARD_CAP", "999999999")

        from services.webhook_dispatcher import env_validator
        with pytest.raises(env_validator.DispatcherEnvError) as excinfo:
            _build_app()
        assert "DISPATCHER_MAX_PENDING_HARD_CAP" in str(excinfo.value)

    def test_dangerous_combination_refuses_boot(self, monkeypatch):
        """The HTTP + INTERNAL combination guard in
        validate_or_die now fires from the api boot path too."""
        monkeypatch.setenv("API_BIND_HOST", "127.0.0.1")
        monkeypatch.setenv("SENTRY_ALLOW_HTTP_WEBHOOKS", "true")
        monkeypatch.setenv("SENTRY_ALLOW_INTERNAL_WEBHOOKS", "true")

        from services.webhook_dispatcher import env_validator
        with pytest.raises(env_validator.DispatcherEnvError) as excinfo:
            _build_app()
        msg = str(excinfo.value)
        assert "SENTRY_ALLOW_HTTP_WEBHOOKS" in msg
        assert "SENTRY_ALLOW_INTERNAL_WEBHOOKS" in msg

    def test_default_environment_boots_cleanly(self, monkeypatch):
        """Sanity: a clean default env (test conftest's baseline)
        still boots successfully. If the validator over-rotates
        and starts refusing the documented defaults, every test
        in the suite would fail at session setup."""
        monkeypatch.setenv("API_BIND_HOST", "127.0.0.1")
        # No timeout-var overrides; falls back to documented
        # defaults. validate_or_die should accept.
        app = _build_app()
        assert app is not None

    def test_kill_switch_skips_required_env_check(self, monkeypatch):
        """DISPATCHER_ENABLED=false bypasses the required-env
        guard so an api container can boot even on a deployment
        that has not yet wired REDIS_URL or SENTRY_PUBSUB_HMAC_KEY.
        The admin-endpoint replay-batch / PATCH publish paths
        will silently no-op publishes (a documented #212 / #227
        soft-fail), which is acceptable for a deliberately
        disabled deployment."""
        monkeypatch.setenv("API_BIND_HOST", "127.0.0.1")
        monkeypatch.setenv("DISPATCHER_ENABLED", "false")
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("SENTRY_PUBSUB_HMAC_KEY", raising=False)
        app = _build_app()
        assert app is not None
