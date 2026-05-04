"""Tests for the strict-typed subscription_filter Pydantic model."""

import json
import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest
from pydantic import ValidationError

from services.webhook_dispatcher import dispatch as dispatch_module
from services.webhook_dispatcher import subscription_filter as sf_module
from services.webhook_dispatcher.subscription_filter import SubscriptionFilter

from tests.test_webhook_dispatcher_dispatch import (  # noqa: E402
    StubHttpClient,
    _conn,
    _emit_event,
    _make_subscription,
    _wait_for_visible,
)


# ---------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------


def test_default_is_empty():
    f = SubscriptionFilter()
    assert f.event_types is None
    assert f.warehouse_ids is None
    assert f.aggregate_external_id_allowlist is None


def test_extra_keys_rejected():
    with pytest.raises(ValidationError):
        SubscriptionFilter.model_validate({"event_types": ["a"], "bogus_key": 1})


def test_event_types_must_be_list_of_strings():
    with pytest.raises(ValidationError):
        SubscriptionFilter.model_validate({"event_types": [1, 2]})


def test_warehouse_ids_must_be_list_of_ints():
    with pytest.raises(ValidationError):
        SubscriptionFilter.model_validate({"warehouse_ids": ["one"]})


def test_aggregate_external_id_allowlist_accepts_uuid_strings():
    u = str(uuid.uuid4())
    f = SubscriptionFilter.model_validate({"aggregate_external_id_allowlist": [u]})
    assert str(f.aggregate_external_id_allowlist[0]) == u


def test_aggregate_external_id_allowlist_rejects_non_uuid():
    with pytest.raises(ValidationError):
        SubscriptionFilter.model_validate(
            {"aggregate_external_id_allowlist": ["not-a-uuid"]}
        )


# ---------------------------------------------------------------------
# parse()
# ---------------------------------------------------------------------


def test_parse_none_returns_empty():
    assert sf_module.parse(None) == SubscriptionFilter()


def test_parse_empty_string_returns_empty():
    assert sf_module.parse("") == SubscriptionFilter()


def test_parse_dict_round_trip():
    f = sf_module.parse({"event_types": ["wms.test"], "warehouse_ids": [1, 2]})
    assert f.event_types == ["wms.test"]
    assert f.warehouse_ids == [1, 2]


def test_parse_json_string_round_trip():
    raw = json.dumps({"event_types": ["wms.test"]})
    f = sf_module.parse(raw)
    assert f.event_types == ["wms.test"]


def test_parse_existing_model_returns_same_instance():
    base = SubscriptionFilter(event_types=["a"])
    assert sf_module.parse(base) is base


def test_parse_malformed_json_raises():
    with pytest.raises(json.JSONDecodeError):
        sf_module.parse("{not json")


def test_parse_unknown_key_raises_validation():
    with pytest.raises(ValidationError):
        sf_module.parse({"event_types": ["a"], "extra": 1})


# ---------------------------------------------------------------------
# _build_filter_clauses
# ---------------------------------------------------------------------


def test_build_filter_clauses_empty():
    clause, params = dispatch_module._build_filter_clauses(SubscriptionFilter())
    assert clause == ""
    assert params == []


def test_build_filter_clauses_event_types_only():
    clause, params = dispatch_module._build_filter_clauses(
        SubscriptionFilter(event_types=["wms.test"])
    )
    assert "event_type = ANY" in clause
    assert "warehouse_id" not in clause
    assert params == [["wms.test"]]


def test_build_filter_clauses_all_three():
    u = uuid.uuid4()
    clause, params = dispatch_module._build_filter_clauses(
        SubscriptionFilter(
            event_types=["wms.test"],
            warehouse_ids=[1],
            aggregate_external_id_allowlist=[u],
        )
    )
    assert "event_type = ANY" in clause
    assert "warehouse_id = ANY" in clause
    assert "aggregate_external_id = ANY" in clause
    # UUIDs serialized to canonical strings for the SQL parameter.
    assert params == [["wms.test"], [1], [str(u)]]


def test_build_filter_clauses_empty_lists_are_ignored():
    """An explicit empty list contributes no clause, identical to
    None. This avoids emitting an always-false predicate that
    would silently match zero events."""
    clause, params = dispatch_module._build_filter_clauses(
        SubscriptionFilter(event_types=[], warehouse_ids=[])
    )
    assert clause == ""
    assert params == []


# ---------------------------------------------------------------------
# Dispatcher integration
# ---------------------------------------------------------------------


def _set_subscription_filter_raw(subscription_id: str, raw_json: str) -> None:
    """Write the column directly so the test can stage malformed
    JSONB that the admin endpoint would normally reject."""
    conn = _conn()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE webhook_subscriptions SET subscription_filter = %s::jsonb "
            "WHERE subscription_id = %s",
            (raw_json, subscription_id),
        )
    finally:
        conn.close()


def _delete_events(event_ids):
    if not event_ids:
        return
    conn = _conn()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM integration_events WHERE event_id = ANY(%s)",
            (list(event_ids),),
        )
    finally:
        conn.close()


def test_filter_narrows_by_event_type():
    sub_id, _, cleanup = _make_subscription()
    emitted = []
    try:
        _set_subscription_filter_raw(
            sub_id, json.dumps({"event_types": ["wms.match"]})
        )
        e_match = _emit_event(event_type="wms.match")
        e_skip = _emit_event(event_type="wms.skip")
        emitted = [e_match, e_skip]
        for eid in emitted:
            _wait_for_visible(eid)

        client = StubHttpClient(responses=[200, 200])
        conn = _conn()
        try:
            outcome = dispatch_module.deliver_one(conn, sub_id, client)
        finally:
            conn.close()
        assert outcome is not None
        assert outcome.event_id == e_match
        assert len(client.calls) == 1
        assert client.calls[0]["event_type"] == "wms.match"
    finally:
        cleanup()
        _delete_events(emitted)


def test_filter_narrows_by_aggregate_external_id_allowlist():
    sub_id, _, cleanup = _make_subscription()
    emitted = []
    allowed_uuid = uuid.uuid4()
    try:
        # Emit one event with the allowed UUID and one without.
        e_allow = _emit_event(event_type="wms.test")
        # Replace its aggregate_external_id with the allowed UUID
        # so the filter matches.
        c = _conn()
        c.autocommit = True
        try:
            cur = c.cursor()
            cur.execute(
                "UPDATE integration_events SET aggregate_external_id = %s "
                "WHERE event_id = %s",
                (str(allowed_uuid), e_allow),
            )
        finally:
            c.close()
        e_deny = _emit_event(event_type="wms.test")
        emitted = [e_allow, e_deny]
        for eid in emitted:
            _wait_for_visible(eid)

        _set_subscription_filter_raw(
            sub_id,
            json.dumps(
                {"aggregate_external_id_allowlist": [str(allowed_uuid)]}
            ),
        )

        client = StubHttpClient(responses=[200])
        conn = _conn()
        try:
            outcome = dispatch_module.deliver_one(conn, sub_id, client)
        finally:
            conn.close()
        assert outcome is not None
        assert outcome.event_id == e_allow
    finally:
        cleanup()
        _delete_events(emitted)


def test_malformed_filter_fails_closed_auto_pauses(caplog):
    """#232: a Pydantic-incompatible filter shape USED TO fall
    open (empty filter, deliver every event). The new fail-closed
    contract auto-pauses the subscription with
    pause_reason='malformed_filter' and refuses to deliver until
    the operator fixes the column. This test pins the new
    behavior in the subscription_filter test module so a future
    regression to fail-open surfaces here too."""
    sub_id, _, cleanup = _make_subscription()
    emitted = []
    try:
        _set_subscription_filter_raw(
            sub_id, json.dumps({"event_types": ["a"], "unknown_key": 1})
        )
        e1 = _emit_event(event_type="wms.any")
        emitted = [e1]
        _wait_for_visible(e1)

        client = StubHttpClient(responses=[200])
        conn = _conn()
        with caplog.at_level("ERROR", logger="webhook_dispatcher.dispatch"):
            try:
                outcome = dispatch_module.deliver_one(conn, sub_id, client)
            finally:
                conn.close()
        # No event delivered; subscription is now paused.
        assert outcome is None
        assert client.calls == []
        assert any(
            "auto-pausing" in rec.message
            and "malformed_filter" in rec.message
            for rec in caplog.records
        )
        # Subscription row reflects the auto-pause.
        c = _conn()
        c.autocommit = True
        cur = c.cursor()
        cur.execute(
            "SELECT status, pause_reason FROM webhook_subscriptions "
            "WHERE subscription_id = %s",
            (sub_id,),
        )
        status, pause_reason = cur.fetchone()
        c.close()
        assert status == "paused"
        assert pause_reason == "malformed_filter"
    finally:
        # Audit_log row from the auto-pause; cleanup so the row
        # does not leak into the next test's view.
        cleanup_conn = _conn()
        cleanup_conn.autocommit = True
        cleanup_conn.cursor().execute(
            "DELETE FROM audit_log "
            "WHERE action_type = 'WEBHOOK_SUBSCRIPTION_AUTO_PAUSE' "
            "  AND details->>'subscription_id' = %s",
            (sub_id,),
        )
        cleanup_conn.close()
        cleanup()
        _delete_events(emitted)
