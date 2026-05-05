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
