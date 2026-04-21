# Connector Framework

The connector framework (new in v1.3.0) is the integration layer between
Sentry and external systems of record -- ERPs like NetSuite or
QuickBooks, commerce platforms like Shopify or BigCommerce, or any
service with an HTTP API. Sentry does not ship first-party connectors
for those systems yet (planned for v2.0.0); v1.3 delivers the
scaffolding you write one against.

## What the framework provides

- **A standard interface (`BaseConnector`)** every connector implements.
  The registry refuses to register a class that does not fully
  implement it, so broken connectors fail fast at startup.
- **Encrypted credential vault.** API keys, consumer secrets, and
  OAuth tokens are Fernet-encrypted with the `SENTRY_ENCRYPTION_KEY`
  master key and scoped per connector + warehouse. Plaintext values
  never leave the vault service; admin endpoints return `****`.
- **Background execution via Celery + Redis.** Syncs run outside the
  Flask request cycle so warehouse scanners stay responsive.
- **Sync-state tracking.** Every sync attempt records `sync_status`
  (`idle` / `running` / `error`), timestamps, and an error counter.
  The admin dashboard shows health per connector + warehouse + sync
  type.
- **Retry + rate limiting + circuit breaker.** Every connector inherits
  `BaseConnector.make_request()`, which provides 3 retries with
  exponential backoff on `429` / `503`, `Retry-After` compliance,
  proactive slowdown when `X-RateLimit-Remaining` drops below the
  configured threshold, and a circuit breaker that opens after 5
  consecutive failures (5-minute cooldown).
- **SSRF guard.** All outbound connector HTTP is validated against an
  allowlist: private / loopback / link-local / reserved addresses
  (IPv4 and IPv6) and internal docker hostnames are rejected before
  the request is issued. See `api/connectors/url_guard.py`.

## Interface

Every connector subclasses `BaseConnector` and implements:

| Method | Contract |
|--------|----------|
| `sync_orders(since: datetime) -> SyncResult` | Pull new / updated orders since `since`. |
| `sync_items(since: datetime) -> SyncResult` | Pull item master records. |
| `sync_inventory(since: datetime) -> SyncResult` | Pull inventory-level changes. |
| `push_fulfillment(order_id, tracking, carrier) -> PushResult` | Post shipment confirmation back. |
| `test_connection() -> ConnectionResult` | Lightweight reachability check (used by the admin Test button). |
| `get_config_schema() -> dict` | Field definitions the admin UI renders as a form. |
| `get_capabilities() -> list[str]` | Subset of `{"sync_orders", "sync_items", "sync_inventory", "push_fulfillment"}` your connector actually supports. |

Result types (`SyncResult`, `PushResult`, `ConnectionResult`) are
pydantic models in `api/connectors/base.py`. The `message` field on
`ConnectionResult` is length-capped at 500 characters and stripped of
non-printable bytes so a misbehaving upstream cannot smuggle payloads
back through the admin UI.

## Writing a connector

Copy `api/connectors/example.py`, rename it, and fill in the method
bodies. Use `self.make_request()` for every HTTP call so retry,
backoff, rate limiting, circuit-breaking, and the SSRF guard are applied
uniformly.

```python
from datetime import datetime
from connectors.base import BaseConnector, SyncResult, ConnectionResult, PushResult

class MyConnector(BaseConnector):
    def sync_orders(self, since: datetime) -> SyncResult:
        response = self.make_request(
            "GET",
            f"{self.config['base_url']}/orders",
            params={"modified_after": since.isoformat()},
            headers={"Authorization": f"Bearer {self.config['api_key']}"},
            timeout=30,
        )
        response.raise_for_status()
        count = 0
        for order in response.json()["orders"]:
            # upsert into sales_orders / sales_order_lines by external id
            count += 1
        return SyncResult(success=True, records_synced=count)

    # ... sync_items, sync_inventory, push_fulfillment, test_connection ...

    def get_config_schema(self) -> dict:
        return {
            "api_key":  {"type": "string", "required": True,  "label": "API Key"},
            "base_url": {"type": "string", "required": True,  "label": "API Base URL"},
        }

    def get_capabilities(self) -> list[str]:
        return ["sync_orders", "sync_items", "push_fulfillment"]


# Register at module import time so auto-discovery picks it up:
from connectors import registry as _registry
_registry.register("my_connector", MyConnector)
```

Drop the file into `api/connectors/` and restart the api + celery
containers. The admin panel will show the connector under
Settings -> ERP Connectors with a credential form generated from
`get_config_schema()`.

## Celery task flow

Each of `jobs.sync_tasks.sync_orders`, `sync_items`, `sync_inventory`,
`push_fulfillment`, and `fulfillment_health_check` follows the same
state machine:

1. `set_running` -- the sync-state row flips to `running`. If a row is
   already `running`, `DuplicateRunError` is raised and the task
   skips (no retry).
2. The connector class is loaded from the registry and instantiated
   with credentials pulled from the vault.
3. The appropriate method is called with `since = last_success_at`
   (or epoch for the first run).
4. On success: `set_success` updates `last_synced_at` and
   `last_success_at`, resets `consecutive_errors`.
5. On failure: `set_error` records the error message, increments
   `consecutive_errors`, and flips `sync_status` to `error` after
   3 consecutive failures.

Celery task-level retries (`max_retries=3`, `default_retry_delay=30`)
run on top, so transient failures automatically retry with a 30-second
gap even when the per-HTTP-call retry budget is exhausted.

## Admin endpoints

See the [API Reference -> Admin - Connectors](api-reference.md#admin-connectors-v130)
section for request / response details. Every endpoint requires
`ADMIN` role:

- `GET /api/admin/connectors`
- `GET /api/admin/connectors/{name}/config-schema`
- `POST /api/admin/connectors/{name}/credentials`
- `GET /api/admin/connectors/{name}/credentials?warehouse_id={id}`
- `DELETE /api/admin/connectors/{name}/credentials`
- `POST /api/admin/connectors/{name}/test`
- `GET /api/admin/connectors/{name}/sync-status?warehouse_id={id}`
- `POST /api/admin/connectors/{name}/sync/{sync_type}`

## Security properties

- **SSRF guard** -- admin-supplied URLs are resolved and checked
  against a private / loopback / link-local / reserved / multicast /
  unspecified blocklist (both IPv4 and IPv6) and against a list of
  internal docker service names (`redis`, `db`, `api`, `admin`,
  `celery-worker`, plus `sentry-*` aliases). A single private result
  in a multi-record DNS lookup blocks the whole URL.
- **DNS rebinding** -- v1.4.0 pins the resolved IP (V-108). The SSRF
  guard resolves the hostname once, validates the address against the
  blocklist, and then connects to the pinned IP while preserving the
  original `Host` header for TLS / vhost routing. A rebind between
  validation and connect no longer bypasses the guard.
- **Credential handling** -- credentials are never returned in
  plaintext through the API. The vault is the only code path that
  sees plaintext values; it reads / writes Fernet ciphertext to
  `connector_credentials.encrypted_value`.
- **Log hygiene** -- missing `SENTRY_ENCRYPTION_KEY` is a
  `RuntimeError` at startup, not an auto-generated key logged to
  stdout. Connector tracebacks do not include decrypted credentials
  by default (see the V-007 note in the backlog for the remaining
  footgun around credentials-in-URL).
- **Append-only audit** -- every admin credential-write action is
  covered by the global audit log, which in v1.3 is hash-chained and
  trigger-guarded against UPDATE / DELETE (V-025).

## Troubleshooting

**Sync stuck in `running`.** v1.4.0 adds stale-running recovery
(V-012, V-102). A fresh worker that finds a `running` state older
than the 1-hour takeover threshold claims it with a new `run_id`, and
the stale worker's late writes are dropped on UUID mismatch. If you
need to force an immediate reset (for example, during a controlled
restart), you can still clear the row manually:

```sql
UPDATE sync_state
   SET sync_status = 'idle'
 WHERE connector_name = 'my_connector'
   AND warehouse_id = 1
   AND sync_type = 'orders'
   AND sync_status = 'running';
```

**`BlockedDestinationError` from test_connection.** Your `base_url`
resolves to a private address. If your ERP is on a local network,
you need to proxy it through a public (or VPN-reachable public) URL.
The guard is deliberately strict; see
[SECURITY_BACKLOG.md](https://github.com/hightower-systems/sentry-wms/blob/main/SECURITY_BACKLOG.md) for the rationale.

**`CircuitOpenError`.** The connector has hit 5 consecutive failures;
calls will fail fast for 5 minutes. Check `sync_state.last_error_message`
in the admin dashboard or via the `/sync-status` endpoint.

**Rotating `SENTRY_ENCRYPTION_KEY`.** Decrypt every row of
`connector_credentials.encrypted_value` with the old key, re-encrypt
with the new key, and write it back in a transaction. Restart the api
and celery workers so they pick up the new env var. Do not try to
change the key in place; the app will not roll its own reencrypt
migration.
