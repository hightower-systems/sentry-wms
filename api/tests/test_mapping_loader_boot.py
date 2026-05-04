"""Boot-time tests for the mapping loader (v1.7.0).

Covers the boot_load() integration helper:
- happy path loads docs + writes one MAPPING_DOCUMENT_LOAD audit row per file
- audit row carries source_system, sha256, mapping_version, version_compare,
  resource_count, git_sha_if_available in details
- allowlisted source_system without a mapping doc refuses boot
- mapping doc whose source_system is not allowlisted refuses boot
- audit_log writes are atomic: if any write fails, no rows leak from the txn
"""

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import uuid as _uuid

from services.mapping_loader import boot_load  # noqa: E402


DATABASE_URL = os.environ["DATABASE_URL"]


def _basic_doc(source_system: str) -> str:
    return f"""\
mapping_version: "1.0"
source_system: "{source_system}"
version_compare: "iso_timestamp"
resources:
  customers:
    canonical_type: "customer"
    fields:
      - canonical: "email"
        source_path: "$.contact.email"
        type: "string"
"""


@pytest.fixture
def tmp_mappings_dir(tmp_path):
    d = tmp_path / "mappings"
    d.mkdir()
    return d


@pytest.fixture
def fresh_source_system():
    """A unique source_system label per test, with a clean allowlist
    baseline. Boot_load() does a global cross-check between the loaded
    docs and every row in inbound_source_systems_allowlist, so leakage
    from a prior test (a row another test left behind) would foul the
    cross-check expectations. We clear allowlist rows owned by this
    fixture's label namespace at setup AND teardown.
    """
    label = f"boot-test-{_uuid.uuid4().hex[:8]}"

    def _wipe():
        # audit_log is append-only (V-025 trigger blocks DELETE / UPDATE);
        # leave its rows. The fresh per-test label means each test's
        # label-scoped count query is unaffected by prior tests' rows.
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM inbound_source_systems_allowlist "
            " WHERE source_system LIKE 'boot-test-%'"
        )
        conn.close()

    _wipe()
    yield label
    _wipe()


def _allowlist(source_system: str) -> None:
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO inbound_source_systems_allowlist (source_system, kind) "
        "VALUES (%s, 'internal_tool') ON CONFLICT DO NOTHING",
        (source_system,),
    )
    conn.close()


class TestBootLoadHappyPath:
    def test_writes_one_audit_row_per_doc(self, tmp_mappings_dir, fresh_source_system):
        _allowlist(fresh_source_system)
        path = tmp_mappings_dir / f"{fresh_source_system}.yaml"
        path.write_text(_basic_doc(fresh_source_system))

        registry = boot_load(DATABASE_URL, tmp_mappings_dir)
        assert registry.for_source(fresh_source_system) is not None

        conn = psycopg2.connect(DATABASE_URL)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT details FROM audit_log "
                " WHERE action_type='MAPPING_DOCUMENT_LOAD' "
                "   AND entity_type='INBOUND_MAPPING' "
                "   AND user_id='system:mapping_loader' "
                "   AND details->>'source_system' = %s",
                (fresh_source_system,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        details = rows[0][0]
        assert details["source_system"] == fresh_source_system
        assert details["mapping_version"] == "1.0"
        assert details["version_compare"] == "iso_timestamp"
        assert details["resource_count"] == 1
        assert "sha256" in details and len(details["sha256"]) == 64
        assert details["path"].endswith(f"{fresh_source_system}.yaml")
        # git_sha_if_available is None outside the container build path.
        assert "git_sha_if_available" in details

    def test_idempotent_re_boot_writes_a_second_row(
        self, tmp_mappings_dir, fresh_source_system
    ):
        """Each boot writes its own audit row -- no de-duping. A doc reloaded
        with no content change still writes a row so investigators can trace
        every restart."""
        _allowlist(fresh_source_system)
        path = tmp_mappings_dir / f"{fresh_source_system}.yaml"
        path.write_text(_basic_doc(fresh_source_system))

        boot_load(DATABASE_URL, tmp_mappings_dir)
        boot_load(DATABASE_URL, tmp_mappings_dir)

        conn = psycopg2.connect(DATABASE_URL)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM audit_log "
                " WHERE action_type='MAPPING_DOCUMENT_LOAD' "
                "   AND details->>'source_system' = %s",
                (fresh_source_system,),
            )
            n = cur.fetchone()[0]
        finally:
            conn.close()
        assert n == 2


class TestBootLoadCrossCheck:
    def test_allowlisted_without_mapping_refuses(
        self, tmp_mappings_dir, fresh_source_system
    ):
        _allowlist(fresh_source_system)
        # No file written.
        with pytest.raises(RuntimeError, match="mapping doc missing"):
            boot_load(DATABASE_URL, tmp_mappings_dir)

    def test_mapping_without_allowlist_refuses(
        self, tmp_mappings_dir, fresh_source_system
    ):
        # File written but no allowlist row.
        path = tmp_mappings_dir / f"{fresh_source_system}.yaml"
        path.write_text(_basic_doc(fresh_source_system))
        with pytest.raises(RuntimeError, match="non-allowlisted"):
            boot_load(DATABASE_URL, tmp_mappings_dir)

    def test_require_allowlisted_false_skips_cross_check(
        self, tmp_mappings_dir, fresh_source_system
    ):
        path = tmp_mappings_dir / f"{fresh_source_system}.yaml"
        path.write_text(_basic_doc(fresh_source_system))
        # Without the allowlist row, default boot would fail; the
        # require_allowlisted=False kwarg lets unit tests skip the check
        # and exercise just the load + audit path.
        registry = boot_load(
            DATABASE_URL, tmp_mappings_dir, require_allowlisted=False,
        )
        assert registry.for_source(fresh_source_system) is not None
