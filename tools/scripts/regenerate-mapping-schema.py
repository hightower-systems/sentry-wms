#!/usr/bin/env python3
"""Regenerate docs/api/mapping-document-schema.json from the
loader's Pydantic models (services.mapping_loader.MappingDocument).

The on-disk schema and the live /api/v1/inbound/mapping-schema endpoint
share the same builder (routes.inbound.build_mapping_schema). The
test_inbound_mapping_schema parity check is the regression net; this
script is what authors run when they intend a schema change.

Usage:

    PYTHONPATH=api python tools/scripts/regenerate-mapping-schema.py \
        > docs/api/mapping-document-schema.json
"""

import json
import os
import sys


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(_REPO_ROOT, "api"))


def main() -> None:
    from routes.inbound import build_mapping_schema

    schema = build_mapping_schema()
    print(json.dumps(schema, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
