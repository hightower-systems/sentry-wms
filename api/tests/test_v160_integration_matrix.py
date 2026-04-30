"""v1.6.0 integration coverage matrix (plan §3.5).

The 26-point matrix is the file: each numbered entry maps a plan
verification point to either an automated test function or to a
manual operator gate. The test asserts every automated entry
resolves to a real, importable test function, so a future rename
that orphans the matrix fails CI.

Manual gates surface as ``MATRIX MANUAL`` log lines so the
operator's pre-merge sweep can grep for them.

The matrix is intentionally kept as a single, hand-curated source
of truth rather than auto-derived from test names: it's the
artifact a release-reviewer reads to confirm v1.6.0 has been
verified end-to-end.
"""

import importlib
import os
import sys
from typing import NamedTuple, Optional

os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")
os.environ.setdefault(
    "SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8="
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class Manual(NamedTuple):
    """Operator-manual gate. The release runbook lists the action;
    the test only logs the entry so it appears in the suite output."""

    action: str


class Auto(NamedTuple):
    """Automated coverage. ``module`` is the dotted import path of
    the test module (relative to api/tests, no .py suffix);
    ``qualname`` is ClassName.method or just function_name."""

    module: str
    qualname: str


MATRIX: dict = {
    1: Manual("Full backend test suite green at branch cut"),
    2: Auto(
        "test_webhook_dispatcher_dispatch",
        "TestHappyPathFiveEvents.test_five_events_deliver_in_order_with_cursor_advance",
    ),
    3: Auto(
        "test_webhook_dispatcher_retry",
        "TestEightFailuresEndInDLQ.test_500_forever_produces_eight_rows_terminating_in_dlq",
    ),
    4: Auto(
        "test_webhook_dispatcher_dispatch",
        "TestHeadOfLineBlocking.test_failed_first_event_blocks_second",
    ),
    5: Auto(
        "test_webhook_dispatcher_wake",
        "TestListenNotifyWake.test_notify_lands_on_queue_within_100ms",
    ),
    6: Auto(
        "test_webhook_dispatcher_wake",
        "TestFallbackPoll.test_poll_continues_after_listen_connection_drop",
    ),
    7: Auto(
        "test_webhook_dispatcher_signing",
        "TestVerifySignature.test_dual_accept_returns_matching_generation",
    ),
    8: Auto(
        "test_admin_webhooks",
        "TestReplaySingle.test_replay_does_not_advance_cursor",
    ),
    9: Auto(
        "test_webhook_dispatcher_shutdown",
        "test_reset_orphaned_in_flight_unconditional_for_stale_rows",
    ),
    10: Auto(
        "test_webhook_dispatcher_ceiling",
        "TestDlqCeilingAutoPause.test_dlq_ceiling_flip_at_threshold",
    ),
    11: Auto(
        "test_webhook_dispatcher_ceiling",
        "TestPendingCeilingAutoPause.test_pending_ceiling_flip_at_threshold",
    ),
    12: Auto(
        "test_webhook_dispatcher_ceiling",
        "TestWorkerEviction.test_request_eviction_exits_run_loop",
    ),
    13: Auto(
        "test_webhook_dispatcher_ssrf_guard",
        "test_worker_refresh_session_calls_close_when_present",
    ),
    14: Manual(
        "Admin browser sweep: subscription CRUD, secret rotation modal, "
        "DLQ viewer pagination, replay flows with impact estimate, stats panel"
    ),
    15: Auto(
        "test_webhook_dispatcher_rate_limiter",
        "test_subscription_worker_rate_throttles_burst",
    ),
    16: Auto(
        "test_webhook_dispatcher_ssrf_guard",
        "test_http_client_send_rejects_private_url",
    ),
    17: Auto(
        "test_webhook_dispatcher_http_client",
        "TestSelfSignedCertE2E.test_verify_true_rejects_self_signed_cert",
    ),
    18: Auto(
        "test_webhook_dispatcher_dispatch",
        "TestSingleSerializationRuntimeAssertion.test_mutating_client_triggers_assertion_error",
    ),
    19: Auto(
        "test_webhook_audit_triggers_migration",
        "TestWebhookSubscriptionsAuditFiring.test_multi_row_delete_writes_one_audit_row",
    ),
    20: Auto(
        "test_admin_webhooks",
        "TestUrlReuseTombstone.test_reuse_without_acknowledge_returns_409",
    ),
    21: Auto(
        "test_admin_webhooks",
        "TestValidationFailures.test_pending_ceiling_above_hard_cap_rejected",
    ),
    22: Auto(
        "test_role_creation_scripts",
        "TestRoleCreationScripts.test_expected_grants_land_per_information_schema",
    ),
    23: Auto(
        "test_webhook_dispatcher_skeleton",
        "TestKillSwitchBootsAndExits.test_kill_switch_boots_writes_heartbeat_exits_on_sigterm",
    ),
    24: Auto(
        "test_webhook_subscriptions_migration",
        "TestWebhookSubscriptionsShape.test_columns",
    ),
    25: Auto(
        "test_integration_events_notify_migration",
        "TestIntegrationEventsNotifyTriggerChain.test_insert_then_commit_fires_visibility_notify",
    ),
    26: Manual(
        "Chainway C6000 smoke: receive, pick, pack, ship; events produced; "
        "dispatcher POSTs to scratch consumer within latency budget"
    ),
}


def _resolve_qualname(module, qualname: str) -> Optional[object]:
    """Walk ``ClassName.method`` (or bare function name) inside the
    imported module. Returns the resolved attribute or None when
    any step misses."""
    obj = module
    for part in qualname.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def test_matrix_has_twenty_six_entries():
    assert sorted(MATRIX.keys()) == list(range(1, 27)), (
        f"matrix must cover points 1..26; got {sorted(MATRIX.keys())}"
    )


def test_every_automated_entry_resolves(caplog):
    """Each Auto entry must point at an importable module and a
    resolvable test function. A rename that orphaned the matrix
    fails here with the offending point number."""
    missing: list[str] = []
    for point, entry in MATRIX.items():
        if isinstance(entry, Manual):
            continue
        try:
            module = importlib.import_module(f"tests.{entry.module}")
        except ImportError as exc:
            missing.append(f"point {point}: import {entry.module} failed: {exc}")
            continue
        resolved = _resolve_qualname(module, entry.qualname)
        if resolved is None:
            missing.append(
                f"point {point}: {entry.module}::{entry.qualname} not found"
            )
    assert not missing, (
        "the following matrix entries do not resolve to a test function "
        "(rename, move, or update MATRIX):\n  - "
        + "\n  - ".join(missing)
    )


def test_manual_entries_logged_for_pre_merge_sweep(caplog):
    """Manual gates surface as MATRIX MANUAL log lines so the
    operator can grep the suite output for the pre-merge
    checklist."""
    import logging

    logger = logging.getLogger("v160_matrix")
    with caplog.at_level(logging.INFO, logger="v160_matrix"):
        for point, entry in MATRIX.items():
            if isinstance(entry, Manual):
                logger.info("MATRIX MANUAL point %d: %s", point, entry.action)

    manual_records = [
        r for r in caplog.records if "MATRIX MANUAL" in r.message
    ]
    # Three operator gates at v1.6.0: full-suite-green baseline (point 1),
    # admin browser sweep (14), and Chainway C6000 smoke (26).
    assert len(manual_records) == 3
