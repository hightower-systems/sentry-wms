#!/usr/bin/env python3
"""Regenerate or verify docs/api/inbound-openapi.yaml.

Usage:

    # Default: write the file in place.
    PYTHONPATH=api python tools/scripts/regenerate-inbound-openapi.py

    # Verify on-disk file is in sync (CI / pre-commit).
    PYTHONPATH=api python tools/scripts/regenerate-inbound-openapi.py --check

    # Backward-compat: print to stdout.
    PYTHONPATH=api python tools/scripts/regenerate-inbound-openapi.py --stdout

The pytest `test_committed_inbound_openapi_matches_live` parity test
covers the same drift in-suite. The CLI form lets CI fail fast before
pytest spins up the full test database, and lets local operators run
it pre-commit without a Python test runner.
"""

import argparse
import difflib
import os
import sys
from pathlib import Path

import yaml


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_OUTPUT = _REPO_ROOT / "docs" / "api" / "inbound-openapi.yaml"


def _live_yaml() -> str:
    sys.path.insert(0, str(_REPO_ROOT / "api"))
    from services.inbound_openapi import build_inbound_openapi

    spec = build_inbound_openapi()
    body = yaml.safe_dump(spec, sort_keys=True, default_flow_style=False)
    # The on-disk file ends with a trailing blank line (POSIX text-file
    # convention + the original `print` invocation appended a newline
    # past yaml.safe_dump's terminal newline). Match it so --check is
    # byte-equal rather than tolerating a one-line cosmetic drift.
    if not body.endswith("\n\n"):
        body = body.rstrip("\n") + "\n\n"
    return body


def _cmd_write(output_path: Path) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_live_yaml())
    print(f"wrote {output_path.relative_to(_REPO_ROOT)}", file=sys.stderr)
    return 0


def _cmd_stdout() -> int:
    sys.stdout.write(_live_yaml())
    return 0


def _cmd_check(output_path: Path) -> int:
    live = _live_yaml()
    if not output_path.is_file():
        print(
            f"::error::{output_path.relative_to(_REPO_ROOT)} does not exist. "
            f"Run `python tools/scripts/regenerate-inbound-openapi.py` to "
            f"regenerate it.",
            file=sys.stderr,
        )
        return 1
    on_disk = output_path.read_text()
    if live == on_disk:
        return 0
    diff = "".join(
        difflib.unified_diff(
            on_disk.splitlines(keepends=True),
            live.splitlines(keepends=True),
            fromfile=f"{output_path.relative_to(_REPO_ROOT)} (on disk)",
            tofile="build_inbound_openapi() (live)",
            n=3,
        )
    )
    print(
        f"::error::{output_path.relative_to(_REPO_ROOT)} is out of sync with "
        f"the live build_inbound_openapi() output. Regenerate via:\n"
        f"    python tools/scripts/regenerate-inbound-openapi.py\n"
        f"and commit the diff.\n\n{diff}",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="verify on-disk file matches live; exit non-zero on drift",
    )
    mode.add_argument(
        "--stdout",
        action="store_true",
        help="print regenerated YAML to stdout",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help=f"path to OpenAPI YAML (default: {_DEFAULT_OUTPUT.relative_to(_REPO_ROOT)})",
    )
    args = parser.parse_args(argv)

    if args.check:
        return _cmd_check(args.output)
    if args.stdout:
        return _cmd_stdout()
    return _cmd_write(args.output)


if __name__ == "__main__":
    sys.exit(main())
