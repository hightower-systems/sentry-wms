# Event schemas moved

The v1.5.0 integration event JSON Schemas now live at
[`api/schemas_v1/events/`](../../api/schemas_v1/events/).

They were relocated from this directory in #137 so that the api
container's Docker image (which builds from the `./api/` context)
actually carries them. The schemas are a runtime API contract loaded
at Flask boot by `api/services/events_schema_registry.py`, not
human-facing documentation, so they belong with the code that
consumes them.

Each subdirectory is named after an event type (`receipt.completed`,
`adjustment.applied`, etc.) and contains one JSON Schema file per
version (`1.json`, future `2.json`, ...). See the
[API reference](../api-reference.md) for the event catalog and the
wire format.

## Consumer contract: dedupe on `event_id`, not `source_txn_id` (v1.5.1 V-211 #155)

Every event envelope carries both `event_id` (server-side
`BIGSERIAL`, monotonic, not client-settable) and `source_txn_id`
(UUID, client-settable via the `X-Request-ID` request header that
triggered the emit). Only `event_id` is safe as a dedupe key.

`source_txn_id` is a Sentry-internal idempotency key: its job is to
collapse retries of the *same* HTTP request into one
`integration_events` row via the `ON CONFLICT (aggregate_type,
aggregate_id, event_type, source_txn_id) DO NOTHING` constraint.
It is exposed on the wire for distributed-tracing convenience, but
any authenticated caller can set it to an arbitrary UUID by
sending `X-Request-ID: <uuid>`. A consumer that dedupes on
`source_txn_id` alone is trusting a value an attacker inside the
Sentry deployment can steer; one legitimate caller with a
deterministic X-Request-ID pattern is enough to poison downstream
dedupe for future events on the same aggregate.

**Correct shape:** consumers process every event whose `event_id`
is strictly greater than the last `event_id` successfully applied.
Use `source_txn_id` only for correlation / tracing, never for
"have I seen this before" checks.

Per Sentry's polling contract, `event_id` is server-generated
`BIGSERIAL` and the outbox's deferred `visible_at` trigger keeps
readers ordering on `(visible_at, event_id)` in commit order even
when BIGSERIAL allocates event_ids out of commit order. Dedupe on
`event_id` is therefore sound against both replay and out-of-order
emission.
