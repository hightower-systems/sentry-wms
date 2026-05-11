"""Boot-time validation for SENTRY_DOCKD_MAX_BODY_KB (v1.9.0).

Mirrors the SENTRY_INBOUND_MAX_BODY_KB shape but for the dockd surface.
Default 64 KB; range [16, 1024]. A typo'd value would silently degrade
the per-request body cap on the dockd POST routes (ship / void-ship);
the boot guard refuses to start so misconfiguration surfaces at deploy
time instead of at the first request near the boundary.
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


class TestDockdMaxBodyKbBootGuard:
    def test_below_floor_refuses_to_boot(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DOCKD_MAX_BODY_KB", "8")
        create_app = _import_create_app()
        with pytest.raises(RuntimeError, match=r"\[16, 1024\]"):
            create_app()

    def test_zero_refuses_to_boot(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DOCKD_MAX_BODY_KB", "0")
        create_app = _import_create_app()
        with pytest.raises(RuntimeError, match=r"\[16, 1024\]"):
            create_app()

    def test_above_ceiling_refuses_to_boot(self, monkeypatch):
        # Realistic typo: missing decimal on 1024 -> 10240.
        monkeypatch.setenv("SENTRY_DOCKD_MAX_BODY_KB", "10240")
        create_app = _import_create_app()
        with pytest.raises(RuntimeError, match=r"\[16, 1024\]"):
            create_app()

    def test_garbage_refuses_to_boot(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DOCKD_MAX_BODY_KB", "garbage")
        create_app = _import_create_app()
        with pytest.raises(RuntimeError, match=r"is not an integer"):
            create_app()

    def test_at_floor_boots_cleanly(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DOCKD_MAX_BODY_KB", "16")
        create_app = _import_create_app()
        app = create_app()
        assert app is not None

    def test_at_ceiling_boots_cleanly(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DOCKD_MAX_BODY_KB", "1024")
        create_app = _import_create_app()
        app = create_app()
        assert app is not None

    def test_unset_uses_default(self, monkeypatch):
        monkeypatch.delenv("SENTRY_DOCKD_MAX_BODY_KB", raising=False)
        create_app = _import_create_app()
        app = create_app()
        assert app is not None


class TestGetMaxBodyKbRuntime:
    def test_returns_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("SENTRY_DOCKD_MAX_BODY_KB", raising=False)
        from services.dockd_service import get_max_body_kb
        assert get_max_body_kb() == 64

    def test_returns_set_value(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DOCKD_MAX_BODY_KB", "256")
        from services.dockd_service import get_max_body_kb
        assert get_max_body_kb() == 256
