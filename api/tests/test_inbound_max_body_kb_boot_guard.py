"""Boot-time validation for SENTRY_INBOUND_MAX_BODY_KB.

Pre-#273 `get_max_body_kb()` silently clamped to [16, 4096] and fell
back to 256 on parse failure. A typo'd value (e.g. 42096 vs 4096)
silently degraded with no visible signal. Boot guard refuses to start
on bad input so the misconfiguration surfaces at deploy time, not at
the first request that happens to be near the cap boundary.
"""

import os
import sys

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")
os.environ.setdefault(
    "SENTRY_PUBSUB_HMAC_KEY",
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _import_create_app():
    from app import create_app
    return create_app


class TestMaxBodyKbBootGuard:
    def test_below_floor_refuses_to_boot(self, monkeypatch):
        monkeypatch.setenv("SENTRY_INBOUND_MAX_BODY_KB", "8")
        create_app = _import_create_app()
        with pytest.raises(RuntimeError, match=r"\[16, 4096\]"):
            create_app()

    def test_zero_refuses_to_boot(self, monkeypatch):
        monkeypatch.setenv("SENTRY_INBOUND_MAX_BODY_KB", "0")
        create_app = _import_create_app()
        with pytest.raises(RuntimeError, match=r"\[16, 4096\]"):
            create_app()

    def test_above_ceiling_refuses_to_boot(self, monkeypatch):
        # The realistic typo: a missing decimal point on 4096.
        monkeypatch.setenv("SENTRY_INBOUND_MAX_BODY_KB", "42096")
        create_app = _import_create_app()
        with pytest.raises(RuntimeError, match=r"\[16, 4096\]"):
            create_app()

    def test_garbage_refuses_to_boot(self, monkeypatch):
        monkeypatch.setenv("SENTRY_INBOUND_MAX_BODY_KB", "garbage")
        create_app = _import_create_app()
        with pytest.raises(RuntimeError, match=r"is not an integer"):
            create_app()

    def test_at_floor_boots_cleanly(self, monkeypatch):
        monkeypatch.setenv("SENTRY_INBOUND_MAX_BODY_KB", "16")
        create_app = _import_create_app()
        app = create_app()
        assert app is not None

    def test_at_ceiling_boots_cleanly(self, monkeypatch):
        monkeypatch.setenv("SENTRY_INBOUND_MAX_BODY_KB", "4096")
        create_app = _import_create_app()
        app = create_app()
        assert app is not None

    def test_unset_uses_default(self, monkeypatch):
        monkeypatch.delenv("SENTRY_INBOUND_MAX_BODY_KB", raising=False)
        create_app = _import_create_app()
        app = create_app()
        assert app is not None


class TestGetMaxBodyKbRuntime:
    """The runtime helper trusts the boot guard rather than re-clamping
    silently. With a valid env value (or no env), it returns the value
    directly; out-of-range values raise rather than silently degrade."""

    def test_returns_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("SENTRY_INBOUND_MAX_BODY_KB", raising=False)
        from services.inbound_service import get_max_body_kb
        assert get_max_body_kb() == 256

    def test_returns_set_value(self, monkeypatch):
        monkeypatch.setenv("SENTRY_INBOUND_MAX_BODY_KB", "1024")
        from services.inbound_service import get_max_body_kb
        assert get_max_body_kb() == 1024

    def test_no_silent_clamp_on_invalid_post_boot(self, monkeypatch):
        """If the env var is mutated to garbage post-boot, the runtime
        helper raises rather than silently falling back to 256. Boot
        validation is the gate; the runtime path trusts it."""
        monkeypatch.setenv("SENTRY_INBOUND_MAX_BODY_KB", "garbage")
        from services.inbound_service import get_max_body_kb
        with pytest.raises(ValueError):
            get_max_body_kb()
