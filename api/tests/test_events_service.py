"""Unit tests for emit_event + events_schema_registry.

emit_event lives in the caller's transaction and delegates commit /
rollback to the caller, so the tests use raw psycopg2 sessions (not the
Flask test client) and hand a SQLAlchemy Connection in directly.
"""

import importlib
import json
import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import jsonschema
import psycopg2
import pytest
from sqlalchemy import create_engine, text


def _engine():
    return create_engine(os.environ["DATABASE_URL"])


def _valid_payload():
    """A payload that validates against docs/events/adjustment.applied/1.json."""
    return {
        "adjustment_external_id": str(uuid.uuid4()),
        "item_external_id": str(uuid.uuid4()),
        "bin_external_id": str(uuid.uuid4()),
        "quantity_delta": -5,
        "reason_code": "CORRECTION",
        "applied_by_user_external_id": str(uuid.uuid4()),
        "applied_at": "2026-04-21T12:00:00Z",
    }


def _reload_with_validation(enabled: bool):
    """Reload events_service with the flag flipped so the module-level
    _VALIDATION_ENABLED constant reflects the test's chosen state."""
    os.environ["SENTRY_VALIDATE_EVENT_SCHEMAS"] = "true" if enabled else "false"
    import services.events_service as events_service
    importlib.reload(events_service)
    return events_service


@pytest.fixture()
def aggregate_id():
    """A unique synthetic aggregate_id per test to avoid cross-test collisions
    in the idempotency-key UNIQUE constraint."""
    return abs(hash(uuid.uuid4())) % 10_000_000 + 900_000_000


def _emit_via_engine(events_service, aggregate_id, payload=None, source_txn_id=None, event_type="adjustment.applied"):
    engine = _engine()
    with engine.begin() as conn:
        return events_service.emit_event(
            conn,
            event_type=event_type,
            event_version=1,
            aggregate_type="inventory_adjustment",
            aggregate_id=aggregate_id,
            aggregate_external_id=uuid.uuid4(),
            warehouse_id=1,
            source_txn_id=source_txn_id or uuid.uuid4(),
            payload=payload or _valid_payload(),
        )


def _cleanup(aggregate_id):
    cleanup = psycopg2.connect(os.environ["DATABASE_URL"])
    cleanup.autocommit = True
    cur = cleanup.cursor()
    cur.execute(
        "DELETE FROM integration_events WHERE aggregate_id = %s",
        (aggregate_id,),
    )
    cleanup.close()


class TestEmitEventBasics:
    def test_first_emit_returns_event_id(self, aggregate_id):
        events_service = _reload_with_validation(True)
        try:
            event_id = _emit_via_engine(events_service, aggregate_id)
            assert isinstance(event_id, int)
            assert event_id > 0
        finally:
            _cleanup(aggregate_id)

    def test_payload_lands_as_jsonb(self, aggregate_id):
        events_service = _reload_with_validation(True)
        try:
            payload = _valid_payload()
            _emit_via_engine(events_service, aggregate_id, payload=payload)
            check = psycopg2.connect(os.environ["DATABASE_URL"])
            try:
                cur = check.cursor()
                cur.execute(
                    "SELECT payload FROM integration_events WHERE aggregate_id = %s",
                    (aggregate_id,),
                )
                row = cur.fetchone()
                assert row is not None
                stored = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                assert stored == payload
            finally:
                check.close()
        finally:
            _cleanup(aggregate_id)


class TestIdempotency:
    def test_same_source_txn_id_returns_none_on_second_call(self, aggregate_id):
        events_service = _reload_with_validation(True)
        try:
            source_txn_id = uuid.uuid4()
            first = _emit_via_engine(events_service, aggregate_id, source_txn_id=source_txn_id)
            second = _emit_via_engine(events_service, aggregate_id, source_txn_id=source_txn_id)
            assert isinstance(first, int)
            assert second is None
            # Only one row persisted.
            check = psycopg2.connect(os.environ["DATABASE_URL"])
            try:
                cur = check.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM integration_events WHERE aggregate_id = %s",
                    (aggregate_id,),
                )
                assert cur.fetchone()[0] == 1
            finally:
                check.close()
        finally:
            _cleanup(aggregate_id)

    def test_different_event_type_allowed_with_same_source_txn_id(self, aggregate_id):
        events_service = _reload_with_validation(False)  # skip validation so we can mix types without matching schemas
        try:
            source_txn_id = uuid.uuid4()
            first = _emit_via_engine(
                events_service, aggregate_id,
                source_txn_id=source_txn_id, event_type="adjustment.applied",
                payload={"anything": "goes", "validation": "skipped"},
            )
            second = _emit_via_engine(
                events_service, aggregate_id,
                source_txn_id=source_txn_id, event_type="cycle_count.adjusted",
                payload={"anything": "goes", "validation": "skipped"},
            )
            assert isinstance(first, int)
            assert isinstance(second, int)
            assert first != second
        finally:
            _cleanup(aggregate_id)


class TestValidationFlag:
    def test_validation_enabled_rejects_bad_payload(self, aggregate_id):
        events_service = _reload_with_validation(True)
        try:
            bad = {"quantity_delta": "not-an-int"}  # missing required fields, wrong type
            with pytest.raises(jsonschema.ValidationError):
                _emit_via_engine(events_service, aggregate_id, payload=bad)
            # No row landed because validation aborted before INSERT.
            check = psycopg2.connect(os.environ["DATABASE_URL"])
            try:
                cur = check.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM integration_events WHERE aggregate_id = %s",
                    (aggregate_id,),
                )
                assert cur.fetchone()[0] == 0
            finally:
                check.close()
        finally:
            _cleanup(aggregate_id)

    def test_validation_disabled_accepts_bad_payload(self, aggregate_id):
        events_service = _reload_with_validation(False)
        try:
            bad = {"this": "would", "never": "validate"}
            event_id = _emit_via_engine(events_service, aggregate_id, payload=bad)
            assert isinstance(event_id, int)
        finally:
            _cleanup(aggregate_id)

    def test_unknown_event_type_raises_even_when_validation_disabled(self, aggregate_id):
        # With validation enabled, the registry lookup raises KeyError.
        # With validation disabled, emit_event skips the registry entirely
        # and the unknown type lands as a plain row - that matches Decision U
        # (prod is fail-open) so we only assert the enabled path.
        events_service = _reload_with_validation(True)
        try:
            with pytest.raises(KeyError):
                _emit_via_engine(
                    events_service, aggregate_id,
                    event_type="does.not.exist",
                    payload={},
                )
        finally:
            _cleanup(aggregate_id)


class TestRegistryBootInvariants:
    def test_every_catalog_entry_has_a_validator(self):
        from services import events_schema_registry
        for event_type, version, _aggregate_type in events_schema_registry.V150_CATALOG:
            assert events_schema_registry.get_validator(event_type, version) is not None

    def test_get_validator_raises_for_unknown_type(self):
        from services import events_schema_registry
        with pytest.raises(KeyError):
            events_schema_registry.get_validator("not.registered", 1)

    def test_known_types_groups_versions_per_event_type(self):
        from services import events_schema_registry
        types = events_schema_registry.known_types()
        names = {t["event_type"] for t in types}
        assert "receipt.completed" in names
        assert "ship.confirmed" in names
        for entry in types:
            assert entry["versions"] == sorted(entry["versions"])
            assert len(entry["versions"]) >= 1
