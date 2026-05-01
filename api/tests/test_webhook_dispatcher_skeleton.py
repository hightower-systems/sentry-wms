"""Tests for the v1.6.0 D1 dispatcher skeleton (#173).

Locks the boot sequence, kill-switch behavior, healthcheck CLI
contract, and SIGTERM cleanup. The dispatcher's real work loops
land in D2-D11; D1 just stands the daemon up and confirms it
exits cleanly under each operator scenario (kill switch, fresh
heartbeat, stale heartbeat, missing heartbeat).
"""

import os
import signal
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from services.webhook_dispatcher import healthcheck


REPO_API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class TestHealthcheckIsHealthy:
    def test_fresh_heartbeat_is_healthy(self, tmp_path):
        hb = tmp_path / "heartbeat"
        hb.write_text(str(int(time.time())))
        assert healthcheck.is_healthy(heartbeat_file=str(hb)) is True

    def test_stale_heartbeat_is_unhealthy(self, tmp_path):
        hb = tmp_path / "heartbeat"
        hb.write_text("0")
        # Force the file's mtime well past the threshold.
        old = time.time() - 600
        os.utime(hb, (old, old))
        assert healthcheck.is_healthy(heartbeat_file=str(hb)) is False

    def test_missing_heartbeat_is_unhealthy(self, tmp_path):
        path = tmp_path / "does-not-exist"
        assert healthcheck.is_healthy(heartbeat_file=str(path)) is False

    def test_threshold_boundary_inclusive(self, tmp_path):
        """A heartbeat exactly at the threshold edge is still
        healthy. Drift on the wrong side of the boundary would
        cause spurious restart loops on a slow scheduler."""
        hb = tmp_path / "heartbeat"
        hb.write_text("0")
        now = 1000.0
        os.utime(hb, (now - 30, now - 30))
        assert healthcheck.is_healthy(
            heartbeat_file=str(hb),
            threshold_s=30,
            now_fn=lambda: now,
        ) is True


class TestHealthcheckMainExitCode:
    def test_main_returns_zero_on_fresh(self, tmp_path):
        hb = tmp_path / "hb"
        hb.write_text("x")
        # main() reads the env var, not the kwarg; set it for the test.
        old = os.environ.get("DISPATCHER_HEARTBEAT_FILE")
        os.environ["DISPATCHER_HEARTBEAT_FILE"] = str(hb)
        try:
            assert healthcheck.main() == 0
        finally:
            if old is None:
                del os.environ["DISPATCHER_HEARTBEAT_FILE"]
            else:
                os.environ["DISPATCHER_HEARTBEAT_FILE"] = old

    def test_main_returns_nonzero_on_missing(self, tmp_path):
        old = os.environ.get("DISPATCHER_HEARTBEAT_FILE")
        os.environ["DISPATCHER_HEARTBEAT_FILE"] = str(tmp_path / "nope")
        try:
            assert healthcheck.main() != 0
        finally:
            if old is None:
                del os.environ["DISPATCHER_HEARTBEAT_FILE"]
            else:
                os.environ["DISPATCHER_HEARTBEAT_FILE"] = old


class TestKillSwitchBootsAndExits:
    """DISPATCHER_ENABLED=false must boot the daemon, log
    CRITICAL, and exit cleanly on SIGTERM. The healthcheck must
    keep passing throughout (heartbeat is written even in kill-
    switch mode) so docker-compose does not restart-loop."""

    def test_kill_switch_boots_writes_heartbeat_exits_on_sigterm(self, tmp_path):
        hb = tmp_path / "kill-switch-hb"
        env = os.environ.copy()
        env["DISPATCHER_ENABLED"] = "false"
        env["DISPATCHER_HEARTBEAT_FILE"] = str(hb)
        env["PYTHONPATH"] = REPO_API_DIR
        # Ensure no stray combination guard fires from the host env.
        env["FLASK_ENV"] = "development"
        env["SENTRY_ALLOW_HTTP_WEBHOOKS"] = "false"
        env["SENTRY_ALLOW_INTERNAL_WEBHOOKS"] = "false"

        proc = subprocess.Popen(
            [sys.executable, "-m", "services.webhook_dispatcher"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=REPO_API_DIR,
        )
        try:
            # Wait for the heartbeat file to appear (boot complete).
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if hb.exists():
                    break
                time.sleep(0.1)
            assert hb.exists(), "heartbeat file was never written; daemon did not reach the loop"

            # Send SIGTERM. The handler flips _shutdown and the loop
            # exits at its next iteration (1s sleep granularity).
            proc.send_signal(signal.SIGTERM)
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                pytest.fail(
                    "daemon did not exit within 10s of SIGTERM; "
                    "the SIGTERM handler is broken or the loop is wedged"
                )
            assert proc.returncode == 0, (
                f"clean SIGTERM exit must be returncode 0; got {proc.returncode}\n"
                f"stderr: {stderr.decode(errors='replace')}"
            )
            # Kill-switch CRITICAL log must surface in stderr (Python
            # logging defaults stderr); a quiet kill switch defeats
            # the diagnostic point of the gate.
            assert b"DISPATCHER_ENABLED=false" in stderr, (
                "kill-switch boot must log CRITICAL line naming the env var; "
                f"stderr was: {stderr.decode(errors='replace')}"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


class TestEnabledDefaultsToTrue:
    def test_unset_dispatcher_enabled_is_treated_as_enabled(self):
        from services.webhook_dispatcher import WebhookDispatcher

        old = os.environ.pop("DISPATCHER_ENABLED", None)
        try:
            assert WebhookDispatcher().enabled is True
        finally:
            if old is not None:
                os.environ["DISPATCHER_ENABLED"] = old

    def test_ambiguous_falsy_strings_do_not_disable(self):
        """Only a case-insensitive 'false' disables. Common
        falsy-looking values that are NOT 'false' ('0', 'no',
        'off', '') stay enabled so the kill switch never engages
        by accident on a typo or a borrowed-from-elsewhere config."""
        from services.webhook_dispatcher import WebhookDispatcher

        old = os.environ.get("DISPATCHER_ENABLED")
        try:
            for typo in ("0", "no", "off", "", "disabled", "f", "FAlse "):
                os.environ["DISPATCHER_ENABLED"] = typo
                assert WebhookDispatcher().enabled is True, (
                    f"DISPATCHER_ENABLED={typo!r} must be treated as enabled; "
                    f"only a case-insensitive 'false' disables"
                )
        finally:
            if old is None:
                os.environ.pop("DISPATCHER_ENABLED", None)
            else:
                os.environ["DISPATCHER_ENABLED"] = old

    def test_case_insensitive_false_disables(self):
        """Operators may write 'False' or 'FALSE'; both must
        engage the kill switch. The compose default is
        lowercase 'true' / 'false'; this test guards against an
        accidental case-sensitive comparison shipping."""
        from services.webhook_dispatcher import WebhookDispatcher

        old = os.environ.get("DISPATCHER_ENABLED")
        try:
            for variant in ("false", "False", "FALSE", "FaLsE"):
                os.environ["DISPATCHER_ENABLED"] = variant
                assert WebhookDispatcher().enabled is False, (
                    f"DISPATCHER_ENABLED={variant!r} must engage the kill switch"
                )
        finally:
            if old is None:
                os.environ.pop("DISPATCHER_ENABLED", None)
            else:
                os.environ["DISPATCHER_ENABLED"] = old

    def test_explicit_false_disables(self):
        from services.webhook_dispatcher import WebhookDispatcher

        old = os.environ.get("DISPATCHER_ENABLED")
        os.environ["DISPATCHER_ENABLED"] = "false"
        try:
            assert WebhookDispatcher().enabled is False
        finally:
            if old is None:
                os.environ.pop("DISPATCHER_ENABLED", None)
            else:
                os.environ["DISPATCHER_ENABLED"] = old
