"""Boot-time validation for SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS.

The worker-side get_inbound_retention_days() helper clamps below
the 7-day floor. The boot guard refuses to start at all so a
misconfigured deployment fails LOUD on `docker compose up` rather
than silently clamping in the background after every restart.
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


class TestRetentionBootGuard:
    def test_below_floor_refuses_to_boot(self, monkeypatch):
        monkeypatch.setenv(
            "SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS", "1",
        )
        create_app = _import_create_app()
        with pytest.raises(RuntimeError, match=r"7-day hard floor"):
            create_app()

    def test_zero_refuses_to_boot(self, monkeypatch):
        monkeypatch.setenv(
            "SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS", "0",
        )
        create_app = _import_create_app()
        with pytest.raises(RuntimeError, match=r"7-day hard floor"):
            create_app()

    def test_garbage_refuses_to_boot(self, monkeypatch):
        monkeypatch.setenv(
            "SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS", "garbage",
        )
        create_app = _import_create_app()
        with pytest.raises(RuntimeError, match=r"is not an integer"):
            create_app()

    def test_at_floor_boots_cleanly(self, monkeypatch):
        monkeypatch.setenv(
            "SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS", "7",
        )
        create_app = _import_create_app()
        # Booting create_app() succeeds; we don't need the result.
        # The mapping_loader boot path also runs here, which requires
        # a clean allowlist (conftest's session TRUNCATE handles it).
        app = create_app()
        assert app is not None

    def test_unset_uses_default(self, monkeypatch):
        monkeypatch.delenv(
            "SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS", raising=False,
        )
        create_app = _import_create_app()
        app = create_app()
        assert app is not None
