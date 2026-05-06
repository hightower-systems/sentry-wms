"""GET /api/v1/inbound/mapping-schema tests (v1.7.0).

Covers:
- 200 OK without auth (it's a documentation aid)
- response is a valid JSON Schema (Draft 2020-12 compatible) parseable
  by jsonschema.Draft202012Validator
- top-level $schema and title set
- schema validates a known-good mapping doc and rejects an
  extra-keys mapping doc (round-trip parity with the loader)
- X-Sentry-Canonical-Model: DRAFT-v1 header set
- on-disk docs/api/mapping-document-schema.json content matches
  the live wire-served schema (so the two cannot drift)
"""

import json
import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from jsonschema import Draft202012Validator  # noqa: E402


REPO_SCHEMA_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "docs", "api",
                 "mapping-document-schema.json")
)


class TestMappingSchemaEndpoint:
    def test_returns_200_unauthenticated(self, client):
        resp = client.get("/api/v1/inbound/mapping-schema")
        assert resp.status_code == 200
        assert resp.headers.get("X-Sentry-Canonical-Model") == "DRAFT-v1"
        assert "Cache-Control" in resp.headers

    def test_returns_valid_draft_2020_12_schema(self, client):
        resp = client.get("/api/v1/inbound/mapping-schema")
        body = resp.get_json()
        assert body["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert body["title"] == "SentryWMS Inbound Mapping Document"
        # check_schema raises on a malformed schema. Validates the meta-shape.
        Draft202012Validator.check_schema(body)

    def test_schema_validates_known_good_mapping_doc(self, client):
        resp = client.get("/api/v1/inbound/mapping-schema")
        schema = resp.get_json()
        validator = Draft202012Validator(schema)
        good_doc = {
            "mapping_version": "1.0",
            "source_system": "acme",
            "version_compare": "iso_timestamp",
            "resources": {
                "customers": {
                    "canonical_type": "customer",
                    "fields": [
                        {
                            "canonical": "email",
                            "source_path": "$.contact.email",
                            "type": "string",
                        }
                    ],
                }
            },
        }
        # No errors raised; schema accepts the document.
        list(validator.iter_errors(good_doc))  # consume the iterator
        assert validator.is_valid(good_doc) is True

    def test_schema_rejects_extra_top_level_key(self, client):
        resp = client.get("/api/v1/inbound/mapping-schema")
        schema = resp.get_json()
        validator = Draft202012Validator(schema)
        bad_doc = {
            "mapping_version": "1.0",
            "source_system": "acme",
            "version_compare": "iso_timestamp",
            "resources": {},
            "EXTRA_KEY": "nope",
        }
        assert validator.is_valid(bad_doc) is False

    def test_committed_schema_matches_live_wire(self, client):
        """The on-disk docs/api/mapping-document-schema.json is checked
        in so consumers can validate offline. The live endpoint and the
        on-disk file MUST match -- if they drift, the next change to
        the loader's Pydantic models needs to regenerate the file.

        Skipped in local docker (where docs/ is not mounted into the
        api container). Always runs in CI (Ubuntu runner with the full
        repo checkout) where REPO_SCHEMA_PATH resolves cleanly."""
        import pytest as _pytest
        if not os.path.exists(REPO_SCHEMA_PATH):
            _pytest.skip(
                f"REPO_SCHEMA_PATH {REPO_SCHEMA_PATH} not accessible "
                f"from this runner (docs/ not mounted in local docker). "
                f"This check runs in CI where the full repo is on disk."
            )
        resp = client.get("/api/v1/inbound/mapping-schema")
        live = resp.get_json()
        with open(REPO_SCHEMA_PATH, "r", encoding="utf-8") as fh:
            on_disk = json.load(fh)
        assert live == on_disk, (
            "docs/api/mapping-document-schema.json out of sync with the live endpoint. "
            "Regenerate via tools/scripts/regenerate-mapping-schema.py."
        )
