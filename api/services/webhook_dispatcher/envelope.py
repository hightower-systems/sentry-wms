"""Canonical envelope construction + bytes serialization (plan §3.1).

The single-serialization invariant requires the dispatcher to
serialize the envelope to bytes EXACTLY ONCE per delivery and
sign + send the same ``bytes`` object. This module is the
SINGLE permitted home of that ``json.dumps`` call across the
entire ``api/services/webhook_dispatcher/`` package; the CI lint
added in D2 (this commit) counts ``json.dumps`` occurrences
under that path and fails on more than one.

The wire shape matches the polling-API envelope byte-for-byte so
a consumer can write a single parser that handles both push and
poll deliveries (plan §3.1: "Wire envelope matches the poll
envelope byte-for-byte").
"""

import json
from typing import Any, Dict, Mapping


def build_envelope(event_row: Mapping[str, Any]) -> Dict[str, Any]:
    """Shape the wire envelope from an ``integration_events`` row.

    Keys here MUST match the polling-API envelope (plan §3.1).
    The dict is intentionally not frozen / typed -- it gets
    handed straight to ``serialize_envelope`` and the wire shape
    is what matters; mutating the dict between build and
    serialize is on the caller, but the dispatcher's call sites
    pass it through immediately.
    """
    return {
        "event_id": event_row["event_id"],
        "event_type": event_row["event_type"],
        "event_version": event_row["event_version"],
        "event_timestamp": event_row["event_timestamp"],
        "aggregate_type": event_row["aggregate_type"],
        "aggregate_id": str(event_row["aggregate_external_id"]),
        "warehouse_id": event_row["warehouse_id"],
        "source_txn_id": str(event_row["source_txn_id"]),
        "data": event_row["payload"],
    }


def serialize_envelope(envelope: Mapping[str, Any]) -> bytes:
    """Serialize the envelope to canonical bytes, exactly once
    per delivery. The output is byte-for-byte deterministic for
    any given input dict so the consumer's reconstructor (which
    HMACs over the wire bytes, not the dict) can verify the
    signature without needing the dispatcher's exact dict
    representation.

    Tight separators + sort_keys gives a stable canonical form;
    UTF-8 encoding gives a consistent byte stream regardless of
    the platform's default text encoding.
    """
    return json.dumps(
        envelope,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
