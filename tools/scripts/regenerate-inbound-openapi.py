#!/usr/bin/env python3
"""Regenerate docs/api/inbound-openapi.yaml from the v1.7.0 inbound
spec generator.

Usage:

    PYTHONPATH=api python tools/scripts/regenerate-inbound-openapi.py \
        > docs/api/inbound-openapi.yaml

The test_committed_inbound_openapi_matches_live parity test fails CI
when the on-disk file drifts from the live build_inbound_openapi()
output. The fix is to re-run this script and commit the diff.
"""

import os
import sys

import yaml


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(_REPO_ROOT, "api"))


def main() -> None:
    from services.inbound_openapi import build_inbound_openapi

    spec = build_inbound_openapi()
    print(yaml.safe_dump(spec, sort_keys=True, default_flow_style=False))


if __name__ == "__main__":
    main()
