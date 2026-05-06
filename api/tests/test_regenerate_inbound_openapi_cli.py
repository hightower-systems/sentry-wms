"""v1.7.0 #276: tools/scripts/regenerate-inbound-openapi.py CLI surface.

Three modes:

- default: write to docs/api/inbound-openapi.yaml
- --stdout: print to stdout (backward compat)
- --check: verify on-disk matches live, exit non-zero on drift

The parity pytest in test_inbound_openapi_parity.py already covers
in-suite drift detection. These tests pin the CLI contract -- the
mode flags, exit codes, and output destinations.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO_ROOT / "tools" / "scripts" / "regenerate-inbound-openapi.py"
_COMMITTED = _REPO_ROOT / "docs" / "api" / "inbound-openapi.yaml"


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_REPO_ROOT / "api")
    env.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
    env.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
    env.setdefault(
        "SENTRY_ENCRYPTION_KEY",
        "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=",
    )
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        cwd=cwd or _REPO_ROOT,
    )


def _script_runnable() -> bool:
    return _SCRIPT.is_file() and _COMMITTED.parent.is_dir()


pytestmark = pytest.mark.skipif(
    not _script_runnable(),
    reason="repo tools/ or docs/ not accessible from this runner",
)


class TestCheckMode:
    def test_check_passes_when_on_disk_matches_live(self):
        """Repo HEAD ships an in-sync OpenAPI spec; --check exits 0."""
        result = _run(["--check"])
        assert result.returncode == 0, (
            f"--check failed unexpectedly\nstdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

    def test_check_fails_on_drift(self, tmp_path):
        """Point --output at a file with stale content; --check exits
        non-zero with a diff in stderr naming the regen command."""
        stale = tmp_path / "stale.yaml"
        stale.write_text("openapi: 3.1.0\npaths: {}\n")
        result = _run(["--check", "--output", str(stale)])
        assert result.returncode != 0
        assert "out of sync" in result.stderr
        assert "regenerate-inbound-openapi.py" in result.stderr

    def test_check_fails_when_file_missing(self, tmp_path):
        missing = tmp_path / "nonexistent.yaml"
        result = _run(["--check", "--output", str(missing)])
        assert result.returncode != 0
        assert "does not exist" in result.stderr


class TestWriteMode:
    def test_write_default_target_produces_file_equal_to_live(self, tmp_path):
        target = tmp_path / "out.yaml"
        result = _run(["--output", str(target)])
        assert result.returncode == 0, (
            f"write mode failed\nstderr: {result.stderr}"
        )
        assert target.is_file()
        # The file must round-trip through yaml without error.
        loaded = yaml.safe_load(target.read_text())
        assert loaded["openapi"] == "3.1.0"
        assert "paths" in loaded
        # Re-running --check against the freshly written file passes.
        check = _run(["--check", "--output", str(target)])
        assert check.returncode == 0

    def test_write_creates_parent_directory(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c" / "out.yaml"
        result = _run(["--output", str(nested)])
        assert result.returncode == 0
        assert nested.is_file()


class TestStdoutMode:
    def test_stdout_emits_valid_yaml(self):
        result = _run(["--stdout"])
        assert result.returncode == 0
        loaded = yaml.safe_load(result.stdout)
        assert loaded["openapi"] == "3.1.0"
        # Spot-check one well-known path so a generator regression that
        # produces empty paths still trips the test.
        assert "/api/v1/inbound/sales_orders" in loaded["paths"]

    def test_stdout_and_check_are_mutually_exclusive(self):
        result = _run(["--stdout", "--check"])
        assert result.returncode != 0
        assert "not allowed" in result.stderr or "mutually exclusive" in result.stderr.lower()
