"""docs/api/inbound-openapi.yaml parity check (v1.7.0 pre-merge gate item 15).

The on-disk OpenAPI spec is generated from
services.inbound_openapi.build_inbound_openapi(). If the loader's
Pydantic body model or the per-resource config changes without
regenerating the file, this test fails in CI with a pointer to the
regen script.

Skipped under local docker (where docs/ is not mounted into the api
container) and always runs in CI on the Ubuntu runner where the
full repo is on disk.
"""

import os
import sys
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


_REPO_DOCS_CANDIDATES = [
    Path(__file__).resolve().parent.parent.parent / "docs" / "api"
        / "inbound-openapi.yaml",
    Path("/docs/api/inbound-openapi.yaml"),
]


def _resolve_committed_spec() -> Path | None:
    for c in _REPO_DOCS_CANDIDATES:
        if c.is_file():
            return c
    return None


class TestCommittedInboundOpenAPIMatchesLive:
    def test_disk_spec_matches_generator(self):
        from services.inbound_openapi import build_inbound_openapi

        committed = _resolve_committed_spec()
        if committed is None:
            pytest.skip(
                "docs/api/inbound-openapi.yaml not accessible from this "
                "runner (docs/ not mounted in local docker). Always runs "
                "in CI where the full repo is on disk."
            )
        live = build_inbound_openapi()
        on_disk = yaml.safe_load(committed.read_text())
        assert live == on_disk, (
            "docs/api/inbound-openapi.yaml is out of sync with the live "
            "build_inbound_openapi() output. Regenerate via: "
            "PYTHONPATH=api python tools/scripts/regenerate-inbound-openapi.py "
            "(or run with --check from CI / pre-commit)."
        )


class TestSpecShape:
    """Lightweight structural checks. Catch a generator regression
    that produces a syntactically valid YAML but a semantically
    broken spec (e.g., missing path, missing 201 response)."""

    def test_all_five_resource_paths_present(self):
        from services.inbound_openapi import build_inbound_openapi

        spec = build_inbound_openapi()
        for resource in (
            "sales_orders", "items", "customers", "vendors", "purchase_orders",
        ):
            path = f"/api/v1/inbound/{resource}"
            assert path in spec["paths"], f"missing path: {path}"
            assert "post" in spec["paths"][path], f"{path} missing post op"

    def test_each_post_has_201_409_413_422_responses(self):
        from services.inbound_openapi import build_inbound_openapi

        spec = build_inbound_openapi()
        for resource in (
            "sales_orders", "items", "customers", "vendors", "purchase_orders",
        ):
            responses = spec["paths"][
                f"/api/v1/inbound/{resource}"
            ]["post"]["responses"]
            for code in ("201", "200", "409", "413", "422"):
                assert code in responses, (
                    f"/api/v1/inbound/{resource} missing response code {code}"
                )

    def test_inbound_body_schema_in_components(self):
        from services.inbound_openapi import build_inbound_openapi

        spec = build_inbound_openapi()
        body = spec["components"]["schemas"]["InboundBody"]
        # extra='forbid' surfaces as additionalProperties: false
        # in Pydantic's JSON-schema output.
        assert body.get("additionalProperties") is False
        assert "external_id" in body["properties"]
        assert "external_version" in body["properties"]
        assert "source_payload" in body["properties"]
        assert "mapping_overrides" in body["properties"]

    def test_mapping_schema_endpoint_unauthenticated(self):
        from services.inbound_openapi import build_inbound_openapi

        spec = build_inbound_openapi()
        op = spec["paths"]["/api/v1/inbound/mapping-schema"]["get"]
        # security: [] means no security requirements.
        assert op["security"] == []
