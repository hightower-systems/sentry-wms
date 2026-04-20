"""v1.4.2 #73: upgrade-without-rebuild detection.

`check_build_version()` is called at the top of `create_app()`. Its job is
to catch the case where an operator ran `git pull` but skipped
`docker compose build`, leaving the container running a stale image (with
stale Python deps) against newer code. The failure mode we want is a
clear, actionable log message followed by `sys.exit(2)`, not a downstream
`ModuleNotFoundError` from a missing dependency.

These tests exercise the three branches:
  - image version == code version  -> return, no exit
  - image version != code version  -> sys.exit(2)
  - BUILD_VERSION file missing     -> warn and return (dev path)
"""

import logging

import pytest

from app import check_build_version
import version


def test_matching_versions_is_noop(tmp_path, caplog):
    build_file = tmp_path / "BUILD_VERSION"
    build_file.write_text(version.__version__ + "\n")

    with caplog.at_level(logging.CRITICAL):
        check_build_version(str(build_file))

    assert not any(r.levelno >= logging.CRITICAL for r in caplog.records)


def test_mismatched_versions_exits_with_code_2(tmp_path, caplog):
    build_file = tmp_path / "BUILD_VERSION"
    build_file.write_text("0.0.0-stale\n")

    with caplog.at_level(logging.CRITICAL):
        with pytest.raises(SystemExit) as exc_info:
            check_build_version(str(build_file))

    assert exc_info.value.code == 2
    assert any(
        "docker compose build" in r.getMessage() for r in caplog.records
    ), "Exit message must name the remediation command"
    assert any(
        "0.0.0-stale" in r.getMessage() and version.__version__ in r.getMessage()
        for r in caplog.records
    ), "Exit message must include both versions so the operator sees the drift"


def test_missing_build_file_warns_and_returns(tmp_path, caplog):
    build_file = tmp_path / "BUILD_VERSION_does_not_exist"

    with caplog.at_level(logging.WARNING):
        check_build_version(str(build_file))

    assert any(
        r.levelno == logging.WARNING and "Skipping version check" in r.getMessage()
        for r in caplog.records
    )


def test_build_file_trims_whitespace(tmp_path):
    """Dockerfile writes the version via `grep -oP ... >`, which always
    appends a newline. `.strip()` is what keeps the match honest."""
    build_file = tmp_path / "BUILD_VERSION"
    build_file.write_text(f"  {version.__version__}  \n")

    check_build_version(str(build_file))
