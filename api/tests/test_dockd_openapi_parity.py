"""docs/api/dockd-openapi.yaml parity check (v1.9.0 dockd #7).

The on-disk OpenAPI spec is generated from
services.dockd_openapi.build_dockd_openapi(). If the route's Pydantic
body models, the response shapes, or the operation metadata change
without regenerating the file, this test fails in CI with a pointer
to the regen script.

Skipped under local docker (where docs/ is not mounted into the api
container) and always runs in CI on the Ubuntu runner where the full
repo is on disk.
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
        / "dockd-openapi.yaml",
    Path("/docs/api/dockd-openapi.yaml"),
]


def _resolve_committed_spec() -> Path | None:
    for c in _REPO_DOCS_CANDIDATES:
        if c.is_file():
            return c
    return None


class TestCommittedDockdOpenAPIMatchesLive:
    def test_disk_spec_matches_generator(self):
        from services.dockd_openapi import build_dockd_openapi

        committed = _resolve_committed_spec()
        if committed is None:
            pytest.skip(
                "docs/api/dockd-openapi.yaml not accessible from this "
                "runner (docs/ not mounted in local docker). Always runs "
                "in CI where the full repo is on disk."
            )
        live = build_dockd_openapi()
        on_disk = yaml.safe_load(committed.read_text())
        assert live == on_disk, (
            "docs/api/dockd-openapi.yaml is out of sync with the live "
            "build_dockd_openapi() output. Regenerate via: "
            "PYTHONPATH=api python tools/scripts/regenerate-dockd-openapi.py "
            "(or run with --check from CI / pre-commit)."
        )


class TestSpecShape:
    """Lightweight structural checks. Catch a generator regression that
    produces syntactically valid YAML but a semantically broken spec."""

    def test_three_dockd_paths_present(self):
        from services.dockd_openapi import build_dockd_openapi

        spec = build_dockd_openapi()
        assert "/api/v1/dockd/orders/{so_number}" in spec["paths"]
        assert "/api/v1/dockd/orders/{so_number}/ship" in spec["paths"]
        assert "/api/v1/dockd/orders/{so_number}/void-ship" in spec["paths"]

    def test_get_has_200_404_422(self):
        from services.dockd_openapi import build_dockd_openapi

        spec = build_dockd_openapi()
        responses = spec["paths"]["/api/v1/dockd/orders/{so_number}"]["get"]["responses"]
        for code in ("200", "404", "422"):
            assert code in responses

    def test_ship_has_200_409_410_413_422_503(self):
        from services.dockd_openapi import build_dockd_openapi

        spec = build_dockd_openapi()
        responses = spec["paths"][
            "/api/v1/dockd/orders/{so_number}/ship"
        ]["post"]["responses"]
        for code in ("200", "409", "410", "413", "422", "503"):
            assert code in responses

    def test_void_has_200_409_413_422_503(self):
        from services.dockd_openapi import build_dockd_openapi

        spec = build_dockd_openapi()
        responses = spec["paths"][
            "/api/v1/dockd/orders/{so_number}/void-ship"
        ]["post"]["responses"]
        for code in ("200", "409", "413", "422", "503"):
            assert code in responses

    def test_ship_body_schema_in_components(self):
        from services.dockd_openapi import build_dockd_openapi

        spec = build_dockd_openapi()
        body = spec["components"]["schemas"]["ShipBody"]
        assert body.get("additionalProperties") is False
        for required in ("tracking", "carrier", "operator_username", "idempotency_key"):
            assert required in body["properties"]

    def test_void_body_schema_in_components(self):
        from services.dockd_openapi import build_dockd_openapi

        spec = build_dockd_openapi()
        body = spec["components"]["schemas"]["VoidShipBody"]
        assert body.get("additionalProperties") is False
        for required in ("reason", "operator_username", "idempotency_key"):
            assert required in body["properties"]

    def test_security_scheme_is_x_wms_token(self):
        from services.dockd_openapi import build_dockd_openapi

        spec = build_dockd_openapi()
        scheme = spec["components"]["securitySchemes"]["WmsToken"]
        assert scheme["type"] == "apiKey"
        assert scheme["in"] == "header"
        assert scheme["name"] == "X-WMS-Token"

    def test_so_number_path_param_has_regex_and_length_cap(self):
        from services.dockd_openapi import build_dockd_openapi

        spec = build_dockd_openapi()
        for path in (
            "/api/v1/dockd/orders/{so_number}",
            "/api/v1/dockd/orders/{so_number}/ship",
            "/api/v1/dockd/orders/{so_number}/void-ship",
        ):
            op_method = "get" if path.endswith("{so_number}") else "post"
            params = spec["paths"][path][op_method]["parameters"]
            assert any(
                p["name"] == "so_number"
                and p["schema"]["pattern"] == r"^[A-Za-z0-9_\-#.]+$"
                and p["schema"]["maxLength"] == 128
                for p in params
            )
