# Webhooks (v1.6.0)

This is the consumer integration guide for the v1.6.0 Outbound Push surface. If you are integrating an external system that wants to receive Sentry's `integration_events` as HTTPS POSTs instead of polling `/api/v1/events`, this document is the contract.

The wire envelope is byte-for-byte identical to a single-event response from the polling endpoint, so a consumer that already polls Sentry can keep its event-handling code and add a webhook entry point that calls the same handler.

## Overview

A Sentry admin registers your endpoint as a webhook subscription via the admin panel. The dispatcher daemon then POSTs each visible event to the registered URL in commit order, signs every request with HMAC-SHA256 over a shared secret, retries failures on an exponential schedule (eight attempts, ~15 hours total), and dead-letters on the eighth failure. Your endpoint's job is to verify the signature, dedupe on `event_id`, and return a 2xx within the 10-second timeout.

## What a request looks like

```
POST /your/webhook/endpoint HTTP/1.1
Host: your-host.example.com
Content-Type: application/json
X-Sentry-Signature: sha256=8c4e1b...
X-Sentry-Signature-Generation: 1
X-Sentry-Delivery-Id: 48213906:1744042927
X-Sentry-Event-Type: ship.confirmed
X-Sentry-Timestamp: 1744042927
Content-Length: 412

{"event_id":48213906,"event_type":"ship.confirmed","event_version":1,"event_timestamp":"2026-05-02T14:22:07.413Z","aggregate_type":"sales_order","aggregate_id":"2a8c34e2-...","warehouse_id":3,"source_txn_id":"6f9e7c6a-...","data":{...}}
```

## Envelope

The request body is a single-event JSON object identical to a polling response payload. Field-by-field:

| Field | Type | Description |
|-------|------|-------------|
| `event_id` | int64 | Server-side `BIGSERIAL` from `integration_events`. Monotonic in commit order via the v1.5 `visible_at` trigger. **This is the only safe dedupe key.** See [Dedupe contract](#dedupe-contract). |
| `event_type` | string | One of the catalog values returned by `GET /api/v1/events/types` (`ship.confirmed`, `pick.confirmed`, `receipt.completed`, `adjustment.applied`, `cycle_count.adjusted`, `transfer.completed`, `pack.confirmed`). |
| `event_version` | int | Schema version. The full JSON Schema is at `api/schemas_v1/events/<event_type>/<version>.json` in the Sentry repo and served at `GET /api/v1/events/schema/<type>/<version>` for runtime fetches. |
| `event_timestamp` | RFC 3339 string | When the warehouse operation that produced the event happened. Distinct from `X-Sentry-Timestamp`, which reflects dispatch (or replay) time. |
| `aggregate_type` | string | The owning entity type (`sales_order`, `purchase_order`, `inventory`, `cycle_count`, `transfer`). |
| `aggregate_id` | UUID string | The owning entity's `external_id`. Stable across the entity's lifetime; a consumer keying by aggregate gets per-aggregate FIFO across events. |
| `warehouse_id` | int | The warehouse the event happened in. Filterable at subscription-creation time. |
| `source_txn_id` | UUID string | Sentry-internal idempotency key. Exposed on the wire for distributed-tracing correlation. **Not** safe as a dedupe key because authenticated callers can steer it via `X-Request-ID`. See [Dedupe contract](#dedupe-contract). |
| `data` | object | Event-specific payload. The shape is locked by the schema for `(event_type, event_version)`; consult the schema files. |

The body is canonicalized before signing as `json.dumps(envelope, separators=(',', ':'), sort_keys=True).encode('utf-8')`. The dispatcher serializes once and signs / sends the same buffer; a runtime assertion at the HTTP-client boundary fails loudly if any code path introduces a transformation between sign and send. Your verifier should HMAC the raw request bytes, not a re-serialized form.

## Headers

All headers are case-insensitive per RFC 7230; examples use the canonical case the dispatcher emits.

| Header | Description |
|--------|-------------|
| `X-Sentry-Signature` | `sha256=<hex digest>`. The HMAC-SHA256 of `f"{X-Sentry-Timestamp}.{body}"` keyed on the shared secret matching `X-Sentry-Signature-Generation`. |
| `X-Sentry-Signature-Generation` | `1` or `2`. During the 24-hour rotation window, both generations are valid; outside the window, only generation 1 verifies. See [Dual-accept rotation](#dual-accept-rotation). |
| `X-Sentry-Delivery-Id` | `<event_id>:<X-Sentry-Timestamp>`. Per-attempt identifier; replays of the same event get a fresh delivery_id. Useful for log correlation, never for dedupe. |
| `X-Sentry-Event-Type` | The `event_type` field from the body, hoisted to the headers so a router can dispatch without parsing JSON. |
| `X-Sentry-Timestamp` | Unix epoch seconds at dispatch time. Used by the signature input AND by the [replay-protection window](#replay-protection-window). |

The signing input is the literal string `"<timestamp>.<body>"` where `<body>` is the exact bytes the dispatcher sent (no leading or trailing whitespace, no transformation). A trailing newline introduced by your framework's request parser will break verification.

## Signature verification

Reference Python verifier:

```python
import hmac
import hashlib
import time

REPLAY_WINDOW_S = 300  # 5 minutes

def verify_webhook(headers, raw_body, secret_for_generation):
    """Returns True if the request is from Sentry, False otherwise.

    headers: dict-like with the X-Sentry-* headers
    raw_body: bytes; pass the exact bytes that arrived on the wire
    secret_for_generation: callable taking int (1 or 2) and returning bytes
    """
    sig_header = headers.get("X-Sentry-Signature", "")
    if not sig_header.startswith("sha256="):
        return False
    received_hex = sig_header[len("sha256="):]

    try:
        generation = int(headers.get("X-Sentry-Signature-Generation", ""))
        timestamp = int(headers.get("X-Sentry-Timestamp", ""))
    except ValueError:
        return False

    if generation not in (1, 2):
        return False
    if abs(int(time.time()) - timestamp) > REPLAY_WINDOW_S:
        return False

    secret = secret_for_generation(generation)
    if secret is None:
        # Generation rotated past its 24h dual-accept window.
        return False

    signing_input = f"{timestamp}.".encode("utf-8") + raw_body
    expected = hmac.new(secret, signing_input, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received_hex)
```

Reference Node verifier:

```javascript
const crypto = require('crypto');
const REPLAY_WINDOW_S = 300;

function verifyWebhook(headers, rawBody, secretForGeneration) {
  const sigHeader = headers['x-sentry-signature'] || '';
  if (!sigHeader.startsWith('sha256=')) return false;
  const receivedHex = sigHeader.slice('sha256='.length);

  const generation = parseInt(headers['x-sentry-signature-generation'], 10);
  const timestamp = parseInt(headers['x-sentry-timestamp'], 10);
  if (![1, 2].includes(generation)) return false;
  if (Math.abs(Math.floor(Date.now() / 1000) - timestamp) > REPLAY_WINDOW_S) return false;

  const secret = secretForGeneration(generation);
  if (!secret) return false;

  const signingInput = Buffer.concat([
    Buffer.from(`${timestamp}.`, 'utf-8'),
    rawBody,
  ]);
  const expected = crypto.createHmac('sha256', secret).update(signingInput).digest('hex');

  const a = Buffer.from(expected, 'hex');
  const b = Buffer.from(receivedHex, 'hex');
  if (a.length !== b.length) return false;
  return crypto.timingSafeEqual(a, b);
}
```

Both verifiers use constant-time comparison; do not substitute `==` on the digests, or a network attacker observing your response timing can extract the signature byte-by-byte.

The `raw_body` argument MUST be the exact bytes the dispatcher sent. If your web framework parses JSON before your handler runs, capture the raw bytes from the framework's request object (Flask: `request.get_data(cache=True)`; Express: install the `body-parser` raw mode for the webhook route; FastAPI: `await request.body()`). A re-serialized body will not match the signed bytes.

## Dedupe contract

**Dedupe on `event_id`. Never on `source_txn_id`. Never on `delivery_id`.**

`event_id` is a server-generated `BIGSERIAL` made monotonic in commit order by the `visible_at` trigger. Two deliveries of the same event always carry the same `event_id`; two different events always carry different `event_id`s.

`source_txn_id` is set from the `X-Request-ID` header on the inbound HTTP request that produced the event. It is a Sentry-internal idempotency key for collapsing retries of the *same* request, exposed on the wire for distributed-tracing correlation. An authenticated caller inside the Sentry deployment can set it to an arbitrary UUID. A consumer that dedupes on `source_txn_id` alone trusts a value an attacker can steer; one legitimate caller with a deterministic `X-Request-ID` pattern is enough to poison downstream dedupe.

`delivery_id` (the integer in `X-Sentry-Delivery-Id`) changes on every retry and on every replay, so deduping on it would process the same event multiple times.

The correct shape: track the largest `event_id` your endpoint has successfully applied (in your own database, transactionally with whatever side effect the event triggers); on every incoming webhook, treat any `event_id` less than or equal to that watermark as already-processed and ignore.

This contract matches the v1.5 polling contract documented at `docs/events/README.md`.

## Replay-protection window

The verifier rejects any request whose `X-Sentry-Timestamp` is more than 5 minutes from your endpoint's wall clock. The window is bidirectional (past or future). The dispatcher emits the timestamp from `time.time()` at dispatch (or replay) time on the dispatcher host; your verifier compares against your endpoint's `time.time()`.

If your clock skews more than ~30 seconds, signed requests will be rejected as replays even though they are legitimate. Run NTP. The 5-minute budget gives every well-tuned host plenty of headroom.

The replay-protection window also bounds the value of a stolen webhook: an attacker who captures one of your incoming requests cannot replay it more than 5 minutes later, even with a valid signature.

## Latency characteristics

Sentry's outbound dispatcher enforces a **2-second visibility floor** between when a warehouse operation commits and when its event becomes eligible for dispatch. The floor is inherited from the v1.5 cursor semantics that the polling endpoint also depends on; it absorbs the deferred-trigger / commit-order skew between the moment `visible_at` is set on an `integration_events` row and the moment a separate session can read that row in commit order. Without the floor, a poll or dispatch could observe an event whose `event_id` is greater than a not-yet-visible neighbor and advance the cursor past a hole; the floor closes that race at the cost of a fixed delay.

What this means for the consumer:

- The earliest possible delivery time for an event is `visible_at + 2 seconds`. Plan around this when setting your own SLA. A consumer expecting "instant" delivery will be disappointed; the contract is "near real-time, with a 2-second floor."
- The dispatcher's NOTIFY-driven wake path is sub-second: the dispatcher knows about the event within ~10ms of commit. The 2-second wait is in the dispatch query, not in the wake path.
- The end-to-end p95 budget for `visible_at -> POST sent` is 2.5 seconds under healthy load. The 500ms above the floor covers signing, the cursor query round-trip, the per-subscription rate-limit token acquisition, and the HTTP request-build phase; HTTP response time is on top of that.
- Under a burst (multiple events committed within a short window for the same subscription), per-aggregate FIFO and head-of-line blocking serialize the dispatches. The N-th event in a burst sees `2 seconds + (N-1) * (your endpoint's response time)` before its POST goes out. Tune your endpoint's response time accordingly; sustained sub-200ms responses keep the queue draining at well above the 50 events/sec sustained budget.
- The `X-Sentry-Timestamp` header reflects dispatch time, not the warehouse-operation time. The envelope's `event_timestamp` field carries the original time-of-record.

## Retry semantics

The dispatcher retries any non-2xx response (and any network-level failure) on a fixed schedule:

| Attempt | Delay from previous attempt |
|---------|------------------------------|
| 1 | (initial dispatch) |
| 2 | 1 second |
| 3 | 4 seconds |
| 4 | 15 seconds |
| 5 | 60 seconds |
| 6 | 5 minutes |
| 7 | 30 minutes |
| 8 | 2 hours |
| (DLQ) | 12 hours after attempt 8's failure |

Cumulative window: ~15 hours. There is no jitter in v1.6 (one consumer, one dispatcher); revisit if your fleet grows past one subscription per dispatcher.

Per-aggregate FIFO is intentional. Head-of-line blocking applies: a stuck event blocks newer events on the same subscription until the head terminates (succeeds, hits the DLQ, or auto-pauses at the ceiling). This is by design; per-aggregate ordering matters more than throughput when the consumer is mid-failure.

The dispatch-time SSRF guard verifies your endpoint resolves to a public IP on every dispatch. If your endpoint moves to an internal address mid-flight (DNS rebinding), the next attempt will be rejected with `error_kind=ssrf_rejected`.

## Dual-accept rotation

The shared secret has two generation slots: `1` (primary, what the dispatcher signs with) and `2` (previous, valid for 24 hours after rotation). On rotation:

1. The current generation 1 is demoted to generation 2 with `expires_at = NOW() + 24h`.
2. A new plaintext is issued at generation 1; the dispatcher uses it on every subsequent dispatch.
3. The plaintext is shown to the admin exactly once; Sentry stores only the encrypted form.

During the 24-hour window, the dispatcher signs every request with generation 1 but the consumer must accept either. After 24 hours, generation 2 is no longer valid (the dispatcher's gen=2 row is reaped by the cleanup beat).

The verifier handles this by passing the `X-Sentry-Signature-Generation` header into the secret lookup. Your secret store should hold both generations during rotation:

```python
# pseudocode
SECRETS = {
    1: load_secret_gen_1_from_secret_store(),  # primary
    2: load_secret_gen_2_from_secret_store(),  # may be None outside the window
}

def secret_for_generation(g):
    return SECRETS.get(g)
```

When the admin rotates, the new generation 1 plaintext appears in the rotation modal. Update your secret store before the 24-hour window closes:

1. Move the value at slot 1 to slot 2 in your store.
2. Write the new plaintext to slot 1.

A second rotation within the 24h window overwrites generation 2 and shortens the cutover; the operator runbook documents waiting the full window before re-rotating except in compromise scenarios.

## Subscription pause + DLQ behavior

Two ceilings auto-pause the subscription:

- **Pending ceiling** (default 10,000). When pending + in_flight delivery rows for the subscription reach the ceiling, dispatch stops and the subscription flips to `paused` with `pause_reason='pending_ceiling'`. Auto-pause prevents the dispatcher from spinning forever on a stuck consumer.
- **DLQ ceiling** (default 1,000). When the count of dead-lettered rows reaches the ceiling, dispatch stops with `pause_reason='dlq_ceiling'`. Bounded operator triage volume.

A paused subscription does not retry, does not advance the cursor, and does not publish new deliveries. The admin resumes via the admin panel after triaging the DLQ; resume publishes a `resumed` event on the cross-worker pubsub channel and the dispatcher picks up where it stopped.

Your endpoint can detect a long pause by watching for a gap in `event_id`s after a sustained outage. Sentry will not silently drop events: the dispatcher's cursor stays at the last terminal delivery until you triage and resume.

## Idempotency expectations

Sentry's contract is at-least-once delivery. Your endpoint MUST be idempotent on `event_id`. The retry schedule alone produces duplicates: if your endpoint accepts the request, applies the side effect, and then crashes before returning a 2xx, the dispatcher will retry. A 12-hour gap between attempt 8 and the DLQ also means a replay-batch hours later can produce a "delayed duplicate" your endpoint must absorb.

## Error contract from your perspective

If your endpoint returns a 4xx or 5xx, or fails the network call, the dispatcher classifies the failure into one of seven `error_kind` values: `timeout`, `connection`, `tls`, `4xx`, `5xx`, `ssrf_rejected`, `unknown`. Sentry stores ONLY the categorical kind plus the HTTP status code; your response body is never persisted. This is intentional: a misconfigured consumer endpoint can echo upstream credentials (database connection strings, API tokens) into a 5xx page, and Sentry refuses to act as a persistence channel for the consumer's secrets.

If the Sentry admin needs to debug a delivery failure, they will see the categorical short message and triage hint from the server-owned error catalog. Specifics about why your endpoint failed live in your endpoint's logs.

## Response body size

The dispatcher caps the response body it will read at 64 KB and closes the connection past that point. A consumer that advertises `Content-Length` above the cap is reclassified as a 5xx-class failure without the bytes ever being drained. Ship a small JSON ACK or a bare 200; do NOT return a stack trace, an HTML error page, or any large payload. The dispatcher does not inspect the body anyway -- only the status code drives delivery state.

## Timeout budget

The dispatcher enforces three timeouts on every delivery:

- `DISPATCHER_HTTP_CONNECT_TIMEOUT_MS` (default 5 s) bounds DNS + TCP + TLS handshake.
- `DISPATCHER_HTTP_READ_TIMEOUT_MS` (default 8 s) bounds each individual socket read.
- `DISPATCHER_HTTP_TIMEOUT_MS` (default 10 s) is the WALL-CLOCK cap on the entire send. A consumer that drip-feeds bytes within the per-op read budget cannot keep the connection alive past this; the dispatcher's watchdog cancels the request and classifies the delivery as `timeout`.

Your endpoint must return a complete 2xx response inside the wall-clock cap. Streaming a slow chunked body is not supported; ship the ACK and close.

## Replay timestamps

When the admin replays a delivery, the new request carries:

- A fresh `X-Sentry-Timestamp` reflecting the replay time. The signature is recomputed against this fresh timestamp.
- The original `event_timestamp` from the envelope body, unchanged. The warehouse-operation time of record stays the same.

If your dedupe is keyed correctly on `event_id`, a replay is a no-op for already-applied events. If you also surface `event_timestamp` in your UI, replays will show the original time, not the replay time.

## Useful operator-side links

- Subscription registration: admin panel `/webhooks` page.
- Per-subscription DLQ viewer: `/webhooks` page, "DLQ" action per row.
- Cross-subscription error log: `/webhooks` page, "View errors" button.
- Per-subscription stats: `/webhooks` page, "Stats" action per row.
- Schema files: `api/schemas_v1/events/<event_type>/<version>.json`.
- Polling endpoint contract (the same envelope shape served as a list): `docs/api-reference.md`.
- Consumer contract on dedupe: `docs/events/README.md`.
