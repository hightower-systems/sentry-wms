# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Sentry WMS, please report it privately.

**Email: security@hightowersystems.io**

Do NOT open a public GitHub issue for security vulnerabilities.

We will:

- Acknowledge your report within 48 hours
- Provide an estimated fix timeline within 5 business days
- Credit you in the release notes (unless you prefer to remain anonymous)

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.x.x   | Yes       |
| < 1.0   | No        |

## Security Advisories

### SA-2026-001 -- Committed Fernet encryption key (fixed in v1.3.x)

Between commit `6cb33c8` (2026-04-16) and the fix commit, `docker-compose.yml`
shipped a hardcoded default value for `SENTRY_ENCRYPTION_KEY`:

    CrFAoVpcrJdjJoxrC4vv8RNL0r965VZ4TKkMcD2Zy4k=

This is a valid Fernet master key. Any deployment that ran with this default
(i.e., did not override `SENTRY_ENCRYPTION_KEY` in its `.env` file) stored
`connector_credentials` rows encrypted under a publicly known key. Every such
credential must be treated as compromised.

The value remains in git history and therefore in every clone, fork, and CI
cache. Rewriting history would not recover those copies, so we have not done so.

**If your deployment is affected, remediate as follows:**

1. Generate a new key:
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
2. For every row in `connector_credentials`:
   - Decrypt `encrypted_value` with the old key (`CrFAoV...`).
   - Re-encrypt the plaintext with the new key.
   - Write the new ciphertext back to the row.
3. Set the new key in `.env` as `SENTRY_ENCRYPTION_KEY=<new-value>`, restart
   the API and Celery workers, and confirm that `/api/admin/connectors/<name>/test`
   still succeeds for each configured connector.
4. Rotate the upstream API credentials themselves (NetSuite tokens, Shopify
   keys, etc.) since the plaintext values were derivable by any third party
   with access to the repo and a copy of your database.
5. Discard the old key.

Deployments created after the fix commit are not affected: the compose file
now requires `SENTRY_ENCRYPTION_KEY` to be set explicitly and fails fast at
startup if it is missing.

### SA-2026-002 -- Historical JWT_SECRET defaults in git history (fixed in commit fe49e87)

Before commit `fe49e87` (2026-04-13), `docker-compose.yml` shipped default
values for `JWT_SECRET` that are permanently preserved in git history:

- `dev-secret-change-in-production` (commit `3136f57` -> `1e614f3`)
- `dev-jwt-secret-do-not-use-in-production-b7e2f` (commit `1e614f3` -> `fe49e87`)

Any deployment that ran with either default value signed JWTs with a publicly
knowable secret. An attacker who knows a valid `user_id` for that deployment
can forge tokens with arbitrary roles until the secret is rotated. Issued
tokens expire after 8 hours, but fresh tokens can be forged at will while the
compromised secret stands.

**If your deployment was created before 2026-04-13 and did not override
`JWT_SECRET` in its `.env` file, rotate immediately:**

1. Generate a new secret: `openssl rand -hex 32`
2. Set `JWT_SECRET` in `.env` to the new value.
3. Restart all API and Celery containers. All outstanding tokens become
   invalid on restart; users must log in again.

Deployments with `JWT_SECRET` explicitly set from the start are not affected.
Current `docker-compose.yml` requires `JWT_SECRET` to be set explicitly via
the strict `:?` form and fails fast at startup if it is missing, so new
deployments cannot reproduce the exposure.

## Security Practices

### Authentication and session
- JWT authentication with live database validation on every request
- User role, warehouse access, and active status verified per-request (not cached in token)
- Deactivated users and permission changes take effect immediately
- Warehouse authorization middleware on all endpoints
- Role-based access control (ADMIN/USER)
- Login lockout after 5 failed attempts, scoped to the client IP so an
  attacker cannot DoS a known username from a different network
- bcrypt password hashing with per-password salt
- `JWT_SECRET` and `SENTRY_ENCRYPTION_KEY` required at startup; missing
  values fail the container before any request is served

### Data protection
- Encrypted credential vault for connector secrets (Fernet, AES-128
  in CBC + HMAC-SHA256). Keys are env-only; never logged, never
  written to disk outside the Postgres cipher column.
- Audit log is append-only: `BEFORE UPDATE` and `BEFORE DELETE`
  triggers reject DML on `audit_log` rows, and every row carries a
  SHA-256 chain hash (`prev_hash || payload`) so retroactive changes
  are detectable via `verify_audit_log_chain()`.
- All SQL queries use parameterized bindings (no string concatenation)
- Row-level locks (`SELECT ... FOR UPDATE`) serialize inventory moves,
  PO receipts, and pick-allocation under concurrency, preventing
  double-spend and over-receipt races.

### Tenant isolation
- Non-admin lookups are scoped in SQL (not post-filtered), so a record
  in a warehouse the user cannot see returns the same 404 as a record
  that does not exist. No existence oracle.
- `/api/lookup/item/search` for non-admins returns only items present
  as inventory or preferred-bin entries in their assigned warehouses.
- Preferred-bin writes refuse to target a bin outside the caller's
  assigned warehouses.

### Connector framework
- Outbound HTTP guarded by an SSRF allowlist. The guard rejects
  non-http(s) schemes, internal docker service hostnames, and any
  URL that resolves to a loopback / private / link-local / reserved /
  multicast / unspecified IP (IPv4 or IPv6). Single-private result
  in a multi-record lookup blocks the whole URL.
- `ConnectionResult.message` is capped at 500 characters and stripped
  of non-printable bytes, so a misbehaving upstream cannot smuggle
  response bodies or control sequences back through the admin UI.

### Inbound v1 token authentication (v1.5.0)
- `wms_tokens` is a hash-only vault. `token_hash = SHA-256(pepper ||
  plaintext).hexdigest()`; the pepper lives in `SENTRY_TOKEN_PEPPER`
  (env-only, never in the DB). Plaintext values are returned exactly
  once at issuance / rotation and never stored. Lost plaintext means
  rotate; matches the GitHub / Stripe / AWS standard.
- `SENTRY_TOKEN_PEPPER` boot guard rejects unset, empty,
  whitespace-only, the `.env.example` placeholder, and any value
  shorter than 32 characters. A misconfigured pepper fails boot
  with a generator-command pointer rather than running with weak
  hashes.
- Per-worker 60-second TTL cache on token validation, with
  cross-worker invalidation via Redis pubsub on
  `wms_token_events`. Revocation is visible across every API
  worker within sub-second wall time; the 60-second TTL remains
  only as a backstop when Redis is unavailable.
- Token scopes are typed-array columns (`warehouse_ids BIGINT[]`,
  `event_types TEXT[]`, `endpoints TEXT[]`); empty array denies
  every value on that dimension (Decision S). Issuance validates
  every entry against `warehouses` / `V150_CATALOG` / known
  endpoint slugs and rejects unknowns with the offending values
  enumerated in the response body.
- `@require_wms_token` enforces the `endpoints` scope per route via
  a server-side endpoint -> slug mapping; a token with an empty
  list cannot hit any v1 route.
- Uniform 401 body across every auth-failure path (missing header,
  unknown hash, revoked, expired) so an attacker cannot
  distinguish "this was once a valid token" from "this was never
  a valid token" from the response. Specific reason stays in a
  DEBUG log on `sentry_wms.auth.wms_token` for operator forensics;
  timing partially flattened by performing the cache lookup on
  the missing-header path.
- `/api/v1/events/ack` enforces a cursor horizon (`cursor_beyond_horizon`
  400 if the request exceeds the greatest `event_id`) and a
  per-event scope re-check (`ack_scope_violation` 403 if any event
  in `(last_cursor, cursor]` falls outside the token's scope).
  Backwards acks remain idempotent no-ops.

### Outbox + bulk snapshot (v1.5.0)
- `integration_events` is a transactional outbox: every
  inventory-changing emission lands in the same DB transaction as
  the state change that caused it. The deferred-constraint trigger
  sets `visible_at = clock_timestamp()` at COMMIT so readers
  ordering on `(visible_at, event_id)` see commit-order even when
  BIGSERIAL allocates `event_id` out of commit order.
- `event_id` is the only safe consumer-side dedupe key.
  `source_txn_id` is exposed for distributed-tracing correlation
  but is settable by any authenticated caller via `X-Request-ID`;
  the consumer contract is documented at `docs/events/README.md`
  and `docs/api/webhooks.md` (Outbound Push).
- `snapshot_scans` coordinates bulk reads via `pg_export_snapshot()`
  / `SET TRANSACTION SNAPSHOT '<id>'`. Cursor tamper protection
  runs before the snapshot import: `created_by_token_id` must
  match the caller and the cursor's `warehouse_id` must match
  the request query param; mismatch returns 403
  `cursor_scope_violation`. Per-token concurrent-scan cap of 1
  prevents pool exhaustion across distinct credentials.

### Outbound webhook dispatcher (v1.6.0)
- HMAC-SHA256 over the canonical signing input
  `f"{X-Sentry-Timestamp}.{body}"`, where `body` is the exact
  request bytes the dispatcher serialized once. Three layers of
  enforcement on the single-serialization invariant: (1) CI lint
  forbids more than one `json.dumps` call on the envelope under
  `webhook_dispatcher/`; (2) runtime assertion at the HTTP-client
  boundary fires if the request body differs from the signed
  body; (3) integration test asserts the assertion fires when a
  transformation is introduced between sign and send.
- 24-hour dual-accept rotation: each subscription has two secret
  slots (`generation=1` primary, `generation=2` previous with
  `expires_at = NOW() + 24h`). Plaintext returned exactly once at
  issuance / rotation; never echoed in `repr()`; never written to
  `audit_log.details`. `secret_rotated` events publish on the
  cross-worker `webhook_subscription_events` Redis channel so
  peer dispatcher workers refresh their cached signing key
  before the next dispatch.
- Constant-time signature comparison (`hmac.compare_digest`) at
  every comparison site under `webhook_dispatcher/`; CI lint
  forbids `==` on signature bytes.
- 5-minute replay-protection window: documented consumer-side
  contract that the verifier rejects any request whose
  `X-Sentry-Timestamp` is more than 5 minutes from the
  consumer's wall clock (bidirectional). Bounds the value of a
  stolen request to a 5-minute replay window even with a valid
  signature.
- Dispatch-time SSRF guard with DNS-rebinding mitigation
  invariant. Every POST resolves the `delivery_url` via
  `socket.getaddrinfo` and rejects any address in
  `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`,
  `127.0.0.0/8`, `169.254.0.0/16` (covers IMDS), IPv6 ULA
  `fc00::/7`, `::1/128`, `fe80::/10`, `fd00:ec2::/32` (AWS
  IMDSv2). Subscription mutations that change the resolved
  network destination force DNS resolution to re-occur on the
  next dispatch via session teardown on `delivery_url_changed`.
  `SENTRY_ALLOW_INTERNAL_WEBHOOKS=true` bypasses the check in
  dev / CI; refuses to boot in production. The combination
  `SENTRY_ALLOW_HTTP_WEBHOOKS=true + SENTRY_ALLOW_INTERNAL_WEBHOOKS=true`
  refuses to boot regardless of `FLASK_ENV` (the SSRF-into-VPC
  surface).
- `verify=True` always at the HTTP layer; `allow_redirects=False`
  so a malicious consumer cannot bounce traffic to an internal
  target via 3xx. CI lint forbids `verify=False` anywhere under
  `webhook_dispatcher/`.
- `error_detail` on `webhook_deliveries` is sourced from a
  server-owned categorical catalog
  (`api/services/webhook_dispatcher/error_catalog.py`) keyed on
  the classified `error_kind`. The consumer's response body is
  intentionally NOT stored; a misconfigured consumer endpoint
  can echo upstream credentials (database connection strings,
  API tokens, session cookies, stack traces with deploy paths)
  into a 5xx page, and persisting that body would make the DLQ
  admin viewer a credential-exfiltration channel for the
  consumer's secrets. The categorical catalog covers `timeout`,
  `connection`, `tls`, `4xx`, `5xx`, `ssrf_rejected`, `unknown`.
- Dedicated least-privilege Postgres role for the dispatcher
  via `db/role-dispatcher.sql`. Operators set
  `DISPATCHER_DATABASE_URL` to point at the role; dev / single-role
  deployments leave it unset and the dispatcher falls back to
  `DATABASE_URL`. A compromise of the dispatcher cannot read
  `users`, `wms_tokens`, or any other table outside its narrow
  grant set (`SELECT` on `integration_events`, `SELECT`/`UPDATE`
  on `webhook_subscriptions`, `INSERT`/`SELECT`/`UPDATE` on
  `webhook_deliveries`, `SELECT` on `webhook_secrets`, `LISTEN`
  on the two NOTIFY channels).
- Pending and DLQ ceilings auto-pause the subscription
  atomically with the ceiling-th write. Per-subscription override
  is constrained to the deployment-wide hard cap
  (`DISPATCHER_MAX_PENDING_HARD_CAP`,
  `DISPATCHER_MAX_DLQ_HARD_CAP`); hard caps are env-var-only so
  an admin who can pause cannot also disable the safety ceiling.
- URL-reuse tombstone gate: hard delete writes a tombstone with
  the `delivery_url_at_delete`; a subsequent CREATE under the
  same URL returns 409 `url_reuse_tombstone` until the admin
  acknowledges with `acknowledge_url_reuse: true`. Defends
  against silent webhook-URL takeover after subscription
  delete + recreate.
- Replay-batch endpoint enforces a server-computed impact
  estimate, a 10,000-row hard cap (override
  `DISPATCHER_REPLAY_BATCH_HARD_CAP`) requiring
  `acknowledge_large_replay: true` to bypass, and a 60-second
  per-subscription throttle tracked through `audit_log` so a
  missed-trigger restart cannot reset the timer.

### Inbound write surface (v1.7.0)
- POST `/api/v1/inbound/<resource>` for the five fixed resources
  (sales_orders, items, customers, vendors, purchase_orders).
  Idempotent on `(source_system, external_id, external_version)`;
  re-POST returns 200 without writing.
- Per-token `source_system` binding plus an `inbound_resources`
  scope dimension and a `mapping_override` capability flag on
  `wms_tokens`. Empty `inbound_resources` denies every inbound
  resource (Decision-S parity with `event_types` / `endpoints`).
- `inbound_source_systems_allowlist` (operator-managed) is the FK
  target for both `wms_tokens.source_system` and
  `cross_system_mappings.source_system`. Admins cannot type a
  source_system the FK would reject; a typo at issuance returns
  the labelled `unknown_source_system` error pre-INSERT.
- Cross-direction guard on `@require_wms_token`: outbound-only
  tokens hitting `/api/v1/inbound/*` and inbound-only tokens
  (`inbound_resources` non-empty, `event_types` empty) hitting
  `/api/v1/events*` / `/api/v1/snapshot/*` return 403
  `cross_direction_scope_violation`. Per-resource scope misses
  return 403 `inbound_resource_scope_violation`. Both surface
  distinctly from the v1.5.1 V-200 `endpoint_scope_violation` so
  audit and rate-limit dashboards can separate "wrong slug" from
  "wrong direction" from "wrong resource".
- Per-key `pg_try_advisory_xact_lock(hashtext(source_system || ':' || external_id))`
  prevents concurrent in-flight upserts for the same external
  entity from racing. Lock is per-transaction, released on
  commit or rollback. Concurrency contention surfaces as 409
  `lock_held` with `Retry-After: 1`.
- Mapping documents are loaded from `db/mappings/*.yaml` at boot
  (no hot reload in v1.7); strict-typed Pydantic with
  `extra="forbid"` at every level (V-204 alignment); the loader
  refuses startup on missing `version_compare`. Boot also enforces
  bidirectional consistency between
  `inbound_source_systems_allowlist` and the YAML files on disk
  (an allowlisted source without a matching doc, or a doc whose
  source isn't allowlisted, refuses boot). One
  `MAPPING_DOCUMENT_LOAD` audit_log row per loaded doc carries
  `source_system`, `path`, `sha256`, `mapping_version`,
  `version_compare`, `resource_count`, and the image-bake git SHA
  when present.
- Derived expressions in mapping docs run through a `simpleeval`
  whitelist restricted to `{int, float, str, len, abs, min, max,
  round}` with arithmetic + string concat + comparison operators
  only. No attribute access beyond `source.x.y` over a wrapped
  AttrDict, no `__import__`, no `eval`/`exec`, no subscripts
  beyond `dict[key]`. R9 regression net (`test_mapping_loader.py`
  `TestDerivedExpressions::test_eval_rejection_*`) covers
  dunder-import, `eval()`, attribute-walk, and bare `open` calls.
- Required-true `cross_system_lookup` misses return 409
  `cross_system_lookup_miss` with the missing
  `(source_system, source_type, source_id)` tuple in the body.
  No auto-stub creation; that would be silent data invention.
- Body size cap via `SENTRY_INBOUND_MAX_BODY_KB` (range
  [16, 4096]; default 256). Boot refuses any
  `SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS` below the 7-day
  hard floor (R6 / V-201 shape) so a typo'd or zero retention
  value cannot wipe forensic context silently.
- `inbound_<resource>` rows reference `wms_tokens(token_id)` via
  `ON DELETE RESTRICT`. Tokens are revoked via `revoked_at`, not
  DELETE; a hard-delete recipe lives at
  `docs/runbooks/wms-tokens-hard-delete.md` (post-release).
- Forensic triggers (V-157 pattern) on
  `inbound_source_systems_allowlist` and `cross_system_mappings`
  capture statement-level DELETE / TRUNCATE with the same
  `who / when / how-many / from-where` shape as `wms_tokens_audit`.
- `audit_log` writes one `INBOUND_<RESOURCE>` row per accepted
  POST with `source_system`, `external_id`, declared `field_set`,
  and `override_fields` populated; idempotent re-POST writes zero
  audit rows. The v1.4 hash chain extends across every new write.
- CI lints (`test_inbound_ci_lints.py`): no bare-name
  `eval`/`exec`/`compile`/`__import__` in `mapping_loader.py`
  (AST walk); every `/api/v1/inbound/<resource>` POST route's
  wrapper carries the `__wms_token_protected__` marker
  `require_wms_token` stamps; every committed
  `db/mappings/*.yaml` declares `version_compare` from the
  whitelist.
- DRAFT canonical model: every successful response carries
  `X-Sentry-Canonical-Model: DRAFT-v1`. The schema may break at
  v2.0 once NetSuite drives the canonical lock. Operator
  integration guide at `docs/api/inbound.md` (post-release) and
  the OpenAPI spec at `docs/api/inbound-openapi.yaml` mark
  every field accordingly.

### Transfer Orders + per-token mapping_overrides (v1.8.0)
- TO admin surface (`/api/admin/transfer-orders/*` + `/api/admin/picker/transfer-orders/*`) is cookie-auth + ADMIN role for write paths; the picker-submit endpoint is cookie-auth + any authenticated user (the picker is not necessarily an admin). Inventory movement happens only at admin **approve** -- pickers cannot move stock unilaterally.
- Self-approval gate via `app_settings.transfer_order_block_self_approval` (mig 049 seeded TRUE) blocks the same admin from approving their own picker submission. A different admin must approve unless an operator explicitly flips the setting (single-admin warehouse exception).
- TO state machines (header / line / approval) are explicitly enumerated in `services.transfer_order_service`; every transition is a code path with audit + event wiring. Closure derivation (`evaluate_to_closure`) requires every line to be APPROVED with `approved_qty == picked_qty` (or SHORT_CLOSED) AND no PENDING approvals -- no implicit "looks done" state.
- Over-pick guard at the SQL layer: `update_transfer_order_line_picked` issues an UPDATE with `WHERE picked_qty + :delta <= committed_qty AND status IN (PENDING, PARTIALLY_PICKED)`. A second concurrent picker attempting to push past the cap gets a zero-row UPDATE; the helper raises `OverPickAttempt` and the route surfaces 409 -- no FOR UPDATE round-trip required.
- Inventory locking pattern: TO import + cancel + delete + start-picking + approve + short-close all walk inventory rows in `inventory_id ASC` so concurrent SO + TO operations on the same item acquire row locks in identical order (deadlock prevention; matches `picking_service.create_pick_batch_for_orders` lock ordering).
- TO outbound event: `transfer.completed/1` emits via the `integration_events` outbox at admin-approve time with `aggregate_id = to_approval_id` (one event per approval batch, idempotent on the existing `(aggregate_type, aggregate_id, event_type, source_txn_id)` UNIQUE constraint). Reject does NOT emit -- consumers see a transfer only when stock actually moved.
- Per-token static `mapping_overrides` JSONB (mig 052) with the v1.7 `mapping_override` BOOLEAN as the gate: overrides apply only when both flag is TRUE and JSONB non-empty. Admin issue route validates every key against `information_schema.columns` for the token's `inbound_resources` canonical tables (422 `unknown_mapping_overrides_keys`). Audit shape uniform: every TOKEN_ISSUE / TOKEN_ROTATE / TOKEN_DELETE row carries `mapping_overrides_keys` (sorted, **never values** -- values may include defaults that look credential-shaped to a log scraper). Per-request body overrides remain rejected with 403 `mapping_overrides_not_supported_in_body`.
- Inbound payload `warehouse_id` token fallback: when source omits `warehouse_id`, the handler fills in `token.warehouse_ids[0]`. Multi-warehouse tokens take the first entry; operators who need different per-request routing must source-side `warehouse_id` or declare a mapping doc default. Token without warehouse + source omits warehouse_id -> 422 `canonical_constraint_violation` (no silent default).

### Productivity Dashboard (v1.8.0)
- `/api/v1/dashboard/productivity` (cookie + ADMIN) reads from `audit_log` (canonical "who did what") via the new `ix_audit_log_dashboard` covering index. Date range capped at 90 days at the Pydantic layer (422 `range_too_large`); `end < start` returns 422 `validation_error`. The 90-day cap prevents DoS via "give me last 10 years."
- `/api/v1/dashboard/preferences` derives `user_id` from `g.current_user` only. Body `extra='forbid'` blocks any `user_id` smuggle attempt (CSRF / IDOR protection). `chart_order` keys validated against the `DASHBOARD_EVENTS` allowlist; duplicates rejected.
- 60s in-process TTL cache per `(warehouse_id, start, end)` per worker. Cache is read-only memory; no cross-worker pubsub layer (60s staleness across workers is acceptable for a refresh-driven view).

### Dockd shipping integration (v1.9.0)
- New `dockd.dispatch` token scope is a **third dispatcher branch** in `auth_middleware` alongside `inbound` and `outbound`. Endpoint resolution gates the slug at the path layer (`_V190_DOCKD_FLASK_ENDPOINTS` frozenset). Cross-direction tokens are rejected with 403 `wrong_token_direction` -- a token issued for `events.poll` cannot reach `/api/v1/dockd/*` even if the operator forgets to revoke it; the token-direction check fails closed.
- Per-station bearer tokens are the design intent: the operator-provisioning runbook (`docs/runbooks/dockd-operator-provisioning.md`) instructs operators to issue one `dockd.dispatch` token per ship station, scope it to a single warehouse, and rotate on station decommission. Plaintext tokens are never written to `audit_log.details` on TOKEN_ISSUE / TOKEN_ROTATE / TOKEN_DELETE rows.
- **Sentinel-row idempotency** on `dockd_idempotency` keyed on `(token_id, idempotency_key)` with SHA-256 body-hash. INSERT ... ON CONFLICT DO NOTHING + body-hash check: replay with same key + same body returns the cached 200; same key + different body returns 409 `idempotency_body_mismatch` (prevents request smuggling against a leaked idempotency key). FK CASCADE to `wms_tokens` so deleting a token clears its idempotency rows; prune index on `created_at` enables ops cleanup.
- **Concurrent-ship serialization** via `SELECT ... FOR UPDATE` on `sales_orders` at the start of every ship / void-ship transaction. Two simultaneous ship attempts on the same SO are forced into sequential commit order; the second observer sees the SHIPPED status and either 409s (different idempotency key) or returns the cached response (same idempotency key). `SET LOCAL lock_timeout = '5s'` so a stuck FK share lock fails fast with 503 rather than hanging the request.
- **`ship.voided/1` outbound event** (Draft 2020-12 schema at `api/schemas_v1/events/ship.voided/1.json`) emits at void time with `pre_ship_status` (PICKED or PACKED), `voided_by_user_external_id`, `voided_at`, `reason`. Fabric polling token's `event_types` list includes `ship.voided` from issue / rotate time so the canonical consumer never lags. Body-validated through the V150_CATALOG so an event with the wrong shape never reaches the outbox.
- **Hash chain coverage**: every `ACTION_SHIP_VOID` and `ACTION_CANCEL` audit row extends the V-025 hash chain. `verify_audit_log_chain()` continues to pass post-mig 054.
- **DRAFT canonical model**: every dockd response carries `X-Sentry-Canonical-Model: DRAFT-v1` so the schema can break at v2.0 once external dockd integrations exist. OpenAPI 3.1 spec at `docs/api/dockd-openapi.yaml`; CI runs `tools/scripts/regenerate-dockd-openapi.py --check` on every PR (drift -> red).
- **No outbound event for SO_CANCELLED**: ERP-driven cancels travel ERP -> WMS only (inbound surface detects intent before `_upsert_canonical`); no `sales_order.cancelled/N` event is emitted because the canonical source is the ERP, not Sentry. Dashboard counter exposes the cancel rate to operators without leaking through the outbox.

### POS endpoint surface (v1.10.0)
- New `pos.dispatch` token scope is a **fourth dispatcher branch** in `auth_middleware` alongside `inbound`, `outbound`, and `dockd`. Endpoint resolution gates the slug at the path layer (`_V1100_POS_FLASK_ENDPOINTS` frozenset). A POS token must carry `pos.dispatch` and must NOT carry any outbound (`event_types`) or inbound (`source_system` / `inbound_resources`) markers; mixed-direction tokens are rejected with 403 `cross_direction_scope_violation`. `@require_wms_token` fails closed if the path matches `/api/v1/pos/` but the Flask endpoint name is not in the frozenset (wiring-bug guard).
- Per-register bearer tokens are the design intent: an operator issues one `pos.dispatch` token per POS terminal, scopes it to the warehouses the register is allowed to fulfill from (typically a retail floor + a back-stock warehouse for split-line carts), and rotates on register decommission. Plaintext tokens are never written to `audit_log.details` on TOKEN_ISSUE / TOKEN_ROTATE / TOKEN_DELETE rows.
- **Idempotency on `sales_orders.idempotency_key`** with the column's UNIQUE constraint as the cross-request sentinel. Each route hashes the request body via `canonical_body_sha256` (idempotency_key excluded, sort_keys, `(",", ":")` separators) and stores it on the SO row. INSERT ... ON CONFLICT (idempotency_key) DO NOTHING + body-hash check: replay with same key + same body returns the cached 200 with `X-Idempotent-Replay: true`; same key + different body returns 409 `idempotency_key_reused_with_different_body` with `existing_so_id` so the POS Service detects a tampered retry instead of silently overwriting. Replays are exempt from the rate-limit budget so a buggy retry loop cannot starve real traffic.
- **Atomic checkout / refund** via `SELECT ... FOR UPDATE` on the inventory rows being decremented or re-incremented. Per-line lock acquisition is ordered by `(item_id, bin_id)` to prevent deadlock between concurrent checkouts touching overlapping inventory. `SET LOCAL lock_timeout = SENTRY_POS_LOCK_TIMEOUT_MS` (default 2000) and `SET LOCAL statement_timeout = SENTRY_POS_STATEMENT_TIMEOUT_MS` (default 4000) inside the request transaction so a deadlock or stuck FK share lock surfaces as a caught `LockNotAvailable` / `QueryCanceled` translated to 503 `lock_contention` with `Retry-After: 1` rather than blocking the request handler.
- **PCI-scope guard at the Pydantic boundary.** `CardTender` is a strict-typed model with `extra='forbid'` accepting exactly `{type, amount_cents, card_brand, card_last4, auth_code, external_ref}`. Any other field (`card_pan`, `full_track`, `expiry`, `cvv`, etc.) fails 422 at the schema layer so Sentry never accepts PAN-shaped data on the wire. `CashTender` is its discriminated-union sibling. A regression test (`test_card_pan_field_rejected_at_pydantic`) asserts the `card_pan` rejection so a future schema change cannot silently widen the PAN attack surface.
- **Refund server-side rules** stack three orthogonal guards on the original SO before the credit-memo INSERT runs: (1) **90-day window** from `original.created_at`, else 422 `refund_window_expired` with `original_created_at`; (2) **card-vs-cash tender lock** comparing the original `POS_CHECKOUT` audit row's `payment_method` against `body.refund_summary.method`, else 422 `tender_mismatch` with `original_method` + `refund_method`; (3) **once-per-original-SO guard** via `original.refunded_at IS NULL AND original.refund_so_id IS NULL`, else 422 `already_refunded` with `existing_refund_so_id`. Missing / out-of-scope / wrong-source / wrong-state original SO conflates to 404 `original_so_not_found` to prevent enumeration; the 422 guards only fire after the token has proven it can see the SO.
- **Header-only warehouse-scope check on refund.** `sales_orders.warehouse_id` (the SO's header) must be in the token's `BIGINT[]` `warehouse_ids` array; out-of-scope warehouses surface as 404. Multi-warehouse split-sale refund security via per-line warehouse comparison is a future enhancement; v1.10 single-warehouse and dual-warehouse-token deployments cover the realistic AvidMax POS shape.
- **Audit-log coverage**: every accepted checkout writes one `ACTION_POS_CHECKOUT` row, every accepted refund one `ACTION_POS_REFUND` row. `details` carries `idempotency_key` + `external_txn_ref` (or `external_refund_ref` + `original_external_txn_ref` on refund) + `terminal_id` + `so_number` + `total_cents` + `payment_method` + `lines: [{sku, warehouse_id, bin_id, quantity, unit_price_cents, tax_cents, line_total_cents}]`. `user_id` is the wire-level `cashier_id` (POS Service's own user-table id; never FK'd to `users` per the doc). The v1.4 hash chain extends through every new write; `verify_audit_log_chain()` continues to pass.
- **Pricing stays out of Sentry's columns.** Per-line `unit_price_cents` / `tax_cents` / `line_total_cents` ride on the wire and live exclusively in `audit_log.details` for archival; mig 056 added no per-line price columns. The POS Service owns its own pricing source (local SQLite + universal tax rate from `.env`).
- **Body size cap** via `SENTRY_POS_MAX_BODY_KB` (range [16, 4096]; default 256). Boot rejects out-of-range values for the body cap and the two timeout vars (`SENTRY_POS_LOCK_TIMEOUT_MS`, `SENTRY_POS_STATEMENT_TIMEOUT_MS`, range [100, 30000]) so a typo'd value cannot silently degrade the body cap or lock posture.
- **DRAFT canonical model**: every POS response carries `X-Sentry-Canonical-Model: DRAFT-v1` so the schema can break at v2.0 once external POS integrations exist.

### Forensic triggers and audit_log coverage (v1.5.1, v1.6.0, v1.7.0)
- `wms_tokens_audit`, `webhook_subscriptions_audit`,
  `webhook_secrets_audit`, `inbound_source_systems_allowlist_audit`,
  and `cross_system_mappings_audit` capture statement-level
  DELETE / TRUNCATE on the parent tables with `event_type`,
  `rows_affected`, `sess_user`, `curr_user`, `backend_pid`,
  `application_name`, `event_at (clock_timestamp)`. A mystery
  emptying is immediately bindable to a specific role + backend.
- `audit_log` writes at every admin mutation site for tokens
  (`TOKEN_ISSUE`, `TOKEN_ROTATE`, `TOKEN_REVOKE`, `TOKEN_DELETE`),
  consumer-groups + connector-registry (`CONNECTOR_REGISTRY_CREATE`,
  `CONSUMER_GROUP_CREATE` / `_UPDATE` / `_DELETE`), the v1.6
  webhooks surface (`WEBHOOK_SUBSCRIPTION_CREATE` / `_UPDATE` /
  `_DELETE_SOFT` / `_DELETE_HARD`, `WEBHOOK_SECRET_ROTATE`,
  `WEBHOOK_DELIVERY_REPLAY_SINGLE` / `_BATCH`), and the v1.7
  inbound surface (`INBOUND_SALES_ORDER` / `_ITEM` / `_CUSTOMER` /
  `_VENDOR` / `_PURCHASE_ORDER` on accept; `MAPPING_DOCUMENT_LOAD`
  per loaded doc at boot). The v1.4 hash chain
  (`prev_hash || payload`) extends across every new write;
  `verify_audit_log_chain()` still passes with the additions.
- Plaintext secret material (token plaintexts, webhook HMAC
  plaintexts) is never written to `audit_log.details` on any
  path. Secret-rotation rows record only that a rotation
  occurred and whether a prior primary was demoted.

### Boot guards on dangerous combinations (v1.5.1, v1.6.0, v1.7.0)
- `TRUST_PROXY=true + API_BIND_HOST=0.0.0.0` refuses boot;
  `SENTRY_ALLOW_OPEN_BIND=1` is the explicit operator override
  with a CRITICAL log on every boot.
- `SENTRY_ALLOW_INTERNAL_WEBHOOKS=true` refuses boot when
  `FLASK_ENV=production`.
- `SENTRY_ALLOW_HTTP_WEBHOOKS=true + SENTRY_ALLOW_INTERNAL_WEBHOOKS=true`
  refuses boot regardless of `FLASK_ENV` (the combination is
  the SSRF-into-VPC surface).
- Every dispatcher env var is validated at boot (out-of-range
  values fail loudly with the valid range); applies to
  `DISPATCHER_HTTP_TIMEOUT_MS`, `DISPATCHER_FALLBACK_POLL_MS`,
  `DISPATCHER_SHUTDOWN_DRAIN_S`, `DISPATCHER_MAX_CONCURRENT_POSTS`,
  `DISPATCHER_MAX_PENDING_HARD_CAP`, `DISPATCHER_MAX_DLQ_HARD_CAP`.
- `SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS` below the 7-day
  hard floor refuses boot (R6 / V-201 shape). A typo'd or zero
  retention window would silently wipe forensic context on the
  first beat run; failing loud at `docker compose up` is the fix.
- The v1.7.0 mapping loader's boot cross-check refuses startup
  when an `inbound_source_systems_allowlist` row has no matching
  `db/mappings/<source_system>.yaml` (or vice versa). The
  RuntimeError message names the offending source_system and the
  expected file path.

### CSP report sink (v1.5.1)
- `report-uri /api/csp-report` directive on every CSP-protected
  response; matching unauthenticated endpoint logs every
  violation at WARNING level on stdout, rate-limited to 60/min
  per IP so a hostile page cannot flood structured logs. Legacy
  `report-uri` only; `report-to` deferred until the fan-out to
  an external collector is needed.

### Input validation
- Pydantic v2 schemas on every JSON request body, including CSV
  import rows (items, bins, purchase orders, sales orders).
- CSV cells that would start with a spreadsheet formula prefix
  (`=`, `+`, `-`, `@`, TAB, CR) are rejected on import; the existing
  DataTable sanitizer handles export.
- Request body size limited to 10MB
- Pagination capped to prevent memory exhaustion

### Infrastructure
- Postgres bound to 127.0.0.1 on the host
- Redis broker requires `requirepass`; Celery broker URL uses the
  authenticated form
- Admin panel served as a production nginx build (no Vite dev-server
  in production); a separate `docker-compose.dev.yml` restores hot
  reload for local development
- API container runs as a non-root user; Dockerfile uses a multi-stage
  pattern for the admin SPA

### Response headers
- X-Content-Type-Options: nosniff
- X-Frame-Options: DENY
- X-XSS-Protection: 0 (legacy header; CSP planned for v1.4)
- Referrer-Policy: strict-origin-when-cross-origin
- Permissions-Policy: camera=(), microphone=(), geolocation=()

### Backlog
Findings deferred to future releases are catalogued in
[`SECURITY_BACKLOG.md`](./SECURITY_BACKLOG.md).
