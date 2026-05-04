# Changelog

Every GitHub release includes the full set of fixes and upgrade notes.
Links below point to the release page for the complete list. This page
is a shorter, docs-site-friendly summary.

---

## v1.6.1 -- Webhook Security Patch

*2026-05-03.* [Full notes](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.6.1).

Security patch closing 22 findings (V-300 through V-321) from the
post-v1.6.0 audit on the new outbound webhook surface. The audit
applied a webhook-classes lens (SSRF, signature timing, retry-storm
amplification, secret-rotation race windows, DLQ poisoning,
replay-batch amplification, downstream consumer trust boundaries,
cross-worker pubsub integrity) plus the v1.5.1 21-class regression
check; 22 findings landed and every one is fixed in this release. No
deferrals. No API contract changes. Mobile is unchanged.

Three new migrations (034-036). Five new env vars
(`SENTRY_PUBSUB_HMAC_KEY`, `DISPATCHER_HTTP_CONNECT_TIMEOUT_MS`,
`DISPATCHER_HTTP_READ_TIMEOUT_MS`,
`DISPATCHER_REPLAY_BATCH_GLOBAL_BUDGET`,
`DISPATCHER_REPLAY_BATCH_GLOBAL_WINDOW_S`). The cookie-auth admin
surface is unchanged outside the response-body fields surfaced by
the replay-batch breakdown and the new `hint` field on PATCH
responses for paused-by-ceiling subscriptions.

Tombstone gate (chained pair):

- **URL canonicalization on the tombstone gate (#218).** Pre-fix the
  URL-reuse gate matched on the raw `delivery_url_at_delete` column;
  one-character casing or default-port mutations bypassed the gate
  without supplying `acknowledge_url_reuse`. New
  `canonicalize_delivery_url` helper is the single source of truth.
  Migration 034 adds `delivery_url_canonical`, backfills via a
  PL/pgSQL twin of the helper, and swaps the partial unique index
  over to the canonical column.
- **PATCH endpoint runs the tombstone gate on `delivery_url` change
  (#219).** Pre-fix the PATCH path validated a new URL against
  scheme + the dispatch-time SSRF guard but did NOT consult
  `webhook_subscriptions_tombstones`. Shared `_check_url_tombstone`
  helper now called from both POST and PATCH; `acknowledge_url_reuse`
  is accepted on `UpdateWebhookRequest`.

HMAC + secret material:

- **`SecretMaterial` refuses pickle (#220).** The default `__slots__`
  pickle path serialized `_plaintext` verbatim. Override
  `__reduce_ex__`, `__reduce__`, `__getstate__`, and `__setstate__`
  so multiprocessing IPC, joblib, APM local-capture, and shelve all
  surface loudly.
- **Single-serialization runtime check raises
  `SingleSerializationViolation` instead of `assert` (#221).**
  Python `-O` strips assertions; production deployments under that
  flag lost the `body == signed_body_for_assertion` defense
  silently. Replaced with an explicit `raise` so the check is
  emitted bytecode regardless of optimization level.
- **Secret-rotation race closed via `SELECT FOR SHARE` (#225).**
  Concurrent rotation could demote `gen=1` to `gen=2` between the
  dispatcher's read and its sign + send. `FOR SHARE` serializes
  against rotation; the row's actual `generation` is projected into
  the returned `SecretMaterial` and stamped onto
  `webhook_deliveries.secret_generation` before the HTTP send.

Cross-worker pubsub integrity:

- **HMAC-signed `webhook_subscription_events` envelope (#227).**
  SECURITY.md explicitly assumes Redis may be compromised; the
  pre-fix channel accepted unauthenticated JSON, so an attacker with
  publish rights could forge `event="deleted"`,
  `event="secret_rotated"`, or `event="delivery_url_changed"`. New
  `pubsub_signing` module owns `load_key` / `sign` / `verify` /
  `build_envelope` / `parse_envelope` keyed on
  `SENTRY_PUBSUB_HMAC_KEY`; subscriber verifies via
  `hmac.compare_digest` before enqueueing. Boot guard refuses api
  and dispatcher boot on unset / placeholder / short keys.

Replay-batch hardening:

- **Pre-INSERT `pending_ceiling` check (#222).** Auto-pause in
  `deliver_one` only fires AFTER a delivery attempt; replay-batch
  INSERTed N pending rows in one statement BEFORE any attempt,
  sidestepping the rail. Refuse 409 with structured `current_pending`
  / `impact_count` / `pending_ceiling` / `gap` fields.
- **`SELECT FOR UPDATE` on the replay-batch subscription row
  (#223).** Two HTTP requests racing each other could both pass the
  60-second per-subscription throttle SELECT before either committed
  its audit row. `FOR UPDATE` serializes concurrent replay-batches
  on the same subscription.
- **Aggregate (cross-subscription) replay-batch throttle (#224).**
  The per-subscription bucket was bypassable by a factor of N for a
  compromised admin who creates N subscriptions all pointing at the
  same consumer URL. New global throttle counts every
  `WEBHOOK_DELIVERY_REPLAY_BATCH` audit_log row across the
  deployment in a rolling window; defaults 5 batches per 5 minutes.
- **Replay-batch reports matched-but-pruned count breakdown
  (#233).** The impact COUNT used a LEFT JOIN to `integration_events`
  so rows whose underlying event was pruned silently disappeared
  from the count. Surface `matched_with_event_data` (replayable) +
  `matched_without_event_data` (pruned) on both the response body
  and audit_log details.

HTTP client:

- **Response body buffering capped at 64KB (#226).** `session.post`
  ran without `stream=True`, so a malicious consumer that streamed a
  multi-GB 5xx body spiked worker RSS by gigabytes per delivery.
  Pass `stream=True`, close in a finally block, and refuse oversized
  advertised `Content-Length` up front.
- **Tuple HTTP timeouts + wall-clock watchdog (#237).** A consumer
  dripping one byte every 9 seconds under a 10s read timeout could
  keep the connection alive forever. Pass timeout as `(connect,
  read)` and wrap the call with a thread watchdog enforcing a hard
  wall-clock cap. Two new env vars
  `DISPATCHER_HTTP_CONNECT_TIMEOUT_MS` (5000) +
  `DISPATCHER_HTTP_READ_TIMEOUT_MS` (8000); env_validator boot
  guard refuses configurations where either per-op cap exceeds the
  wall-clock cap.

Subscription state propagation + filter validation:

- **PATCH publishes `subscription_filter_changed` on filter
  mutation (#229).** New cross-worker kind appended on filter
  mutation. Filter changes stay non-retroactive: events committed
  before the PATCH that match the new filter but not the old do NOT
  re-deliver. Operators backfilling reach for the replay-batch
  endpoint.
- **PATCH publishes `ceiling_changed` and surfaces a non-resume
  hint (#230).** When the operator lifts the ceiling that paused
  the subscription but does NOT also flip `status=active`, the
  response carries a `hint` field naming the follow-up step.
  Resume stays an explicit operator decision.
- **Empty `subscription_filter` array refusal (#231).**
  `subscription_filter={"event_types": []}` looked like "deliver no
  events" but actually meant "deliver every event": filter clauses
  are truthy-gated on each list field. New
  `_reject_empty_filter_arrays` helper called from POST and PATCH
  refuses with 400 `empty_filter_array`.
- **Malformed `subscription_filter` fails closed (#232).** Pre-fix,
  a Pydantic parse failure on the JSONB column logged WARNING and
  fell back to `SubscriptionFilter()` (matches every event). For an
  authorization-shaped column this was fail-OPEN. Now fail closed:
  the dispatcher auto-pauses with `pause_reason='malformed_filter'`,
  writes a `WEBHOOK_SUBSCRIPTION_AUTO_PAUSE` audit_log row, and
  backs off.

Cleanup, forensic triggers, CHECK constraints:

- **`cleanup_webhook_deliveries` chunked deletes (#228).** The
  6-hour beat task issued a single DELETE that could span tens of
  millions of rows in one transaction at sustained 50 events/sec,
  holding a long lock and starving autovacuum. Switched to chunked
  DELETE with COMMIT between batches; default chunk 1000, default
  10-minute wall-clock cap.
- **`webhook_deliveries` DELETE/TRUNCATE forensic triggers (#235).**
  Migration 035 mirrors the V-157 / migration 032 shape on
  `webhook_deliveries`. New `webhook_deliveries_audit` table,
  statement-level AFTER DELETE + AFTER TRUNCATE triggers. Brings
  v1.6 to parity with the v1.5.1 forensic posture.
- **`webhook_subscriptions.status` + `pause_reason` CHECK
  constraints (#236).** Migration 036 adds enums for `status` and
  `pause_reason`. Pre-fix asymmetry: migration 030 had CHECK enums
  on `webhook_deliveries.status` but migration 029 left the same
  column on `webhook_subscriptions` to application validation.

Retry storm + boot validation + docs:

- **+/-10% jitter on every retry slot (#234).** Pre-fix the retry
  schedule was deterministic, so N subscriptions whose first
  delivery to the same consumer URL failed at the same minute then
  retried at the same minute on every retry slot. Apply +/-10%
  jitter using `secrets.SystemRandom`; cumulative worst-case still
  under 17h.
- **API container runs `dispatcher_env.validate_or_die` (#238).**
  validate_or_die ran ONLY in the dispatcher container pre-fix. The
  api container reads the same dispatcher env vars for
  admin-endpoint enforcement and the cross-worker pubsub publisher,
  but a typo'd or out-of-range value never tripped a boot guard
  there. Wire validate_or_die into `create_app()` after
  `validate_pepper_config` and before blueprint registration.
- **Consumer secret-handling guidance in `docs/api/webhooks.md`
  (#239).** New "Handling the secret bytes" subsection covers
  secret-manager storage, never-commit / never-log, and the pickle
  / shelve / joblib / APM / debugger leak surfaces consumers
  commonly do not think about. Symmetric with the server-side gap
  V-302 closed.

Migrations: **034** adds
`webhook_subscriptions_tombstones.delivery_url_canonical` + PL/pgSQL
backfill + partial unique index swap. **035** adds
`webhook_deliveries_audit` + statement-level DELETE / TRUNCATE
triggers. **036** adds CHECK constraints on
`webhook_subscriptions.status` and `pause_reason`; ships AFTER
V-314 so `malformed_filter` is in use before the constraint locks
it down. All three are small DDL operations, BEGIN/COMMIT-wrapped
per V-213.

Operator notes: `SENTRY_PUBSUB_HMAC_KEY` is required when the
dispatcher is enabled; both api and webhook-dispatcher containers
must receive the same value (docker-compose forwards it). Generate
with `python -c "import secrets; print(secrets.token_hex(32))"`.
`DISPATCHER_ENABLED=false` bypasses the boot guard so a
kill-switched deployment can come up without the key.
`DISPATCHER_HTTP_CONNECT_TIMEOUT_MS` (5000) +
`DISPATCHER_HTTP_READ_TIMEOUT_MS` (8000) must each be `<=
DISPATCHER_HTTP_TIMEOUT_MS` (10000, the wall-clock cap).
`DISPATCHER_REPLAY_BATCH_GLOBAL_BUDGET` (5) +
`DISPATCHER_REPLAY_BATCH_GLOBAL_WINDOW_S` (300) tune the aggregate
replay-batch throttle and are operator-only. No mobile APK ships
with v1.6.1; existing v1.5.1 APKs on Chainway C6000 devices
continue to work. Standard upgrade procedure applies: `git pull
&& docker compose down && docker compose build && docker compose
up -d`.

---

## v1.6.0 -- Outbound Push (Pipe A Write)

*2026-04-30.* [Full notes](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.6.0).

Push-delivery counterpart to v1.5.0's polling read. External systems
no longer have to long-poll `integration_events`: a new
`sentry-dispatcher` daemon reads each visible event and POSTs it to
admin-registered consumer URLs over HMAC-signed HTTPS, with
exponential-backoff retries, a 1,000-row dead-letter lane, and
admin-panel CRUD + DLQ triage + replay. Builds on the v1.5.0 outbox
and the v1.5.1 hardening pattern: every architectural choice that
drove a v1.5.1 audit finding is pre-empted here at the top of the
branch (strict-typed Pydantic filters, env-var combination guards,
Redis-pubsub cross-worker invalidation, dedicated least-privilege DB
role, `audit_log` writes at every admin mutation, DELETE / TRUNCATE
statement-level forensic triggers, BEGIN/COMMIT-wrapped migrations).

Mobile is unchanged; no new APK ships. Admin panel gains a Webhooks
page and a wired global search bar (the placeholder TopBar input that
has been a non-functional stub since v1.4 #163).

Subscription data model + forensic triggers:

- **`webhook_subscriptions` + `webhook_secrets`** (migration 029).
  UUID PK on subscriptions so admin URLs are not enumerable;
  per-subscription `rate_limit_per_second` + `pending_ceiling` +
  `dlq_ceiling` columns with CHECK bounds; secrets are
  Fernet-encrypted with `SENTRY_ENCRYPTION_KEY` and live at
  `(subscription_id, generation)` PK with `generation IN (1, 2)` for
  the dual-accept rotation pattern.
- **`webhook_deliveries`** (migration 030). Append-only per attempt
  with one exception: the terminal `dlq` transition flips the same
  row that was last `in_flight`. `ON DELETE RESTRICT` from
  `subscription_id` so a hard delete with live deliveries fails;
  soft-delete (`status='revoked'`) is the supported path. Four
  partial indexes cover the dispatcher and admin hot paths.
- **`integration_events` NOTIFY trigger** (migration 031). The v1.5
  deferred-constraint trigger UPDATEs `visible_at` at COMMIT; this
  migration adds an AFTER UPDATE trigger that fires
  `pg_notify('integration_events_visible', event_id)` so the
  dispatcher's LISTEN thread wakes within ~10ms of commit. 2-second
  fallback poll runs always so a missed NOTIFY costs at most one poll
  cycle.
- **`webhook_subscriptions_audit` + `webhook_secrets_audit`**
  (migration 032). Inherits the V-157 wms_tokens forensic-trail
  pattern from day one: every DELETE / TRUNCATE on either table
  appends a row capturing `event_type`, `rows_affected`, `sess_user`,
  `curr_user`, `backend_pid`, `application_name`, `event_at`.
- **`webhook_subscriptions_tombstones`** (migration 033). Hard-delete
  writes a tombstone capturing `delivery_url_at_delete`; a subsequent
  CREATE under the same URL returns 409 `url_reuse_tombstone` until
  the admin acknowledges with `acknowledge_url_reuse: true`. Mirrors
  v1.5.1 V-207 for consumer-groups.

Dispatcher daemon:

- **New `sentry-dispatcher` Compose service.** Synchronous psycopg2
  + ThreadPoolExecutor + `requests`; mirrors the v1.5 snapshot-keeper
  shape. One worker thread per active subscription, refreshed every
  60s, with `verify=True` always and `allow_redirects=False` so a
  malicious consumer cannot bounce traffic to an internal target via
  3xx.
- **LISTEN/NOTIFY wake + 2s fallback poll + Redis pubsub subscriber.**
  Three sources merge into one in-process queue. Cross-worker
  invalidation events (`paused`, `resumed`, `deleted`,
  `delivery_url_changed`, `rate_limit_changed`, `secret_rotated`)
  flow on the `webhook_subscription_events` channel; the §2.9 action
  table documents which combination of subscription-list eviction,
  session teardown, DB refresh, and rate-limit-bucket re-init each
  event triggers.
- **Per-subscription delivery loop.** Cursor-based; advances strictly
  on terminal state (`succeeded` or `dlq`). Head-of-line blocking is
  intentional per plan §2.5 (silent skip-ahead is worse than visible
  growing lag). Hard-coded retry schedule
  `[1s, 4s, 15s, 60s, 5m, 30m, 2h, 12h]` -- eight attempts, DLQ on
  the eighth, ~15h cumulative window.
- **Per-subscription pending and DLQ ceilings auto-pause** the
  subscription atomically with the ceiling-th write; per-subscription
  override is constrained to the deployment-wide hard cap
  (`DISPATCHER_MAX_PENDING_HARD_CAP`,
  `DISPATCHER_MAX_DLQ_HARD_CAP`), which is env-var-only so an admin
  who can pause cannot also disable the safety ceiling.
- **Dispatch-time SSRF guard with DNS-rebinding mitigation
  invariant.** Every POST resolves `delivery_url` via
  `socket.getaddrinfo` and rejects RFC1918, loopback, link-local,
  IMDS, IPv6 ULA + AWS IMDSv2. Subscription mutations that change
  the resolved network destination force fresh DNS resolution on the
  next dispatch via session teardown.
  `SENTRY_ALLOW_INTERNAL_WEBHOOKS=true` bypasses the check in dev /
  CI; production refuses to boot. The combination
  `SENTRY_ALLOW_HTTP_WEBHOOKS=true + SENTRY_ALLOW_INTERNAL_WEBHOOKS=true`
  refuses to boot regardless of `FLASK_ENV`.
- **Dedicated least-privilege Postgres role** via
  `db/role-dispatcher.sql`. Operators set `DISPATCHER_DATABASE_URL`
  to point at the role; dev / single-role deployments leave it unset
  and the dispatcher falls back to `DATABASE_URL`. A compromise of
  the dispatcher cannot read `users`, `wms_tokens`, or any table
  outside the narrow grant set.

HMAC signing + 24-hour dual-accept rotation:

- **HMAC-SHA256 over the canonical signing input
  `f"{X-Sentry-Timestamp}.{body}"`** where `body` is the exact
  request bytes the dispatcher serialized once. Three layers of
  enforcement on the single-serialization invariant: a CI lint that
  forbids more than one `json.dumps` call on the envelope under
  `webhook_dispatcher/`, a runtime assertion at the HTTP-client
  boundary that fails loudly if any code path introduces a
  transformation between sign and send, and an integration test that
  fires the assertion when a transformation is introduced.
  Constant-time signature comparison everywhere
  (`hmac.compare_digest`); CI lint forbids `==` on signature bytes.
- **24-hour dual-accept rotation.** Each subscription has two secret
  slots: `generation=1` (primary, what the dispatcher signs with),
  `generation=2` (previous, valid for 24 hours after rotation).
  Plaintext returned exactly once at issuance / rotation; never
  echoed in `repr()`; never written to `audit_log.details`.
- **5-minute replay-protection window** documented as the consumer
  contract: the verifier rejects any request whose
  `X-Sentry-Timestamp` is more than 5 minutes from the consumer's
  wall clock (bidirectional). Bounds the value of a captured request
  to a 5-minute replay window even with a valid signature.

Admin webhooks surface:

- **`/api/admin/webhooks` CRUD** with one-shot plaintext secret on
  create, server-side validation that `connector_id`, every
  `event_types` entry, and every `warehouse_ids` entry exists,
  HTTPS-only `delivery_url` policy with a documented opt-out, ceiling
  enforcement against the deployment hard caps, URL-reuse tombstone
  gate, and `audit_log` writes at every mutation site.
- **PATCH publishes the matching cross-worker pubsub event** after
  commit (`paused`, `resumed`, `delivery_url_changed`,
  `rate_limit_changed`); status transitions out of `revoked` are
  refused. DELETE soft-deletes by default; `?purge=true` hard-deletes
  with tombstone (cascades through terminal `webhook_deliveries`;
  refused while live deliveries reference the subscription).
- **DLQ viewer** paginated and joined to `integration_events` so the
  operator reads what payload failed without a second round-trip.
  **Replay-one** inserts a fresh `pending` row pointing at the
  original `event_id` (URL-tampering check rejects mismatched
  `delivery_id`). **Replay-batch** with filter, server-computed
  impact estimate, 10,000-row hard cap (override
  `DISPATCHER_REPLAY_BATCH_HARD_CAP`) requiring
  `acknowledge_large_replay: true`, and a 60-second per-subscription
  throttle tracked through `audit_log` so a missed-trigger restart
  cannot reset the timer.
- **Per-subscription stats endpoint** (`?window=1h|6h|24h|7d`) with
  attempts / succeeded / failed / dlq / in_flight / pending counters,
  p50/p95/p99 response_time_ms, top 5 error_kinds, and current cursor
  lag. 30-second in-process cache.
- **Cross-subscription error log.** `GET /api/admin/webhook-errors`
  joins delivery failures (status in `failed` / `dlq`) to the
  server-owned error catalog at response time; the consumer's
  response body is intentionally NOT stored.
  `webhook_deliveries.error_detail` carries only categorical short
  messages from
  `api/services/webhook_dispatcher/error_catalog.py`. Pre-design the
  dispatcher captured `response.text[:512]` directly into the column;
  a misconfigured consumer endpoint can echo upstream credentials
  (database connection strings, API tokens, session cookies) into a
  5xx page, and persisting that body would make the DLQ admin viewer
  a credential-exfiltration channel for the consumer's secrets. The
  catalog covers `timeout`, `connection`, `tls`, `redirected`, `4xx`,
  `5xx`, `ssrf_rejected`, `unknown`.
- **React admin Webhooks page** with subscription list (status badge,
  last-24h success rate, current pending count), create wizard
  (connector picker, HTTPS-validated URL, scope-catalog checkbox
  filter builder, rate-limit + ceiling sliders, one-shot secret
  reveal modal with saved-secret acknowledgement, URL-reuse warning
  modal), per-row actions (edit / pause-resume / rotate / DLQ /
  stats / revoke / purge), DLQ panel with replay-one + replay-batch
  (server-computed impact estimate inline; 429 throttle response
  surfaces the countdown), stats panel, and a cross-subscription
  "View errors" panel with row expansion showing the catalog
  description and triage hint.

Admin global search bar (#163, carry-forward from v1.4):

- **`GET /api/admin/search?q=&warehouse_id=`.** Single endpoint
  fanning out across items, bins, purchase_orders, sales_orders, and
  the denormalized customer columns on sales_orders. Per-type cap of
  10 rows, total cap of 50, minimum query length 2 to avoid
  worst-case wildcard scans. Items are global; bins / POs / SOs /
  customers are filtered to the supplied warehouse_id.
- **TopBar dropdown wiring + list-page `?q=` prefill.** The TopBar
  input that has been a non-functional placeholder since v1.4 now
  drives the new endpoint with a 250ms debounce and a dropdown that
  follows the existing warehouse-picker shape (click-outside
  dismisses, Arrow keys + Enter + Esc). Selection routes to the
  matching list page; the four list endpoints (items, bins, POs,
  SOs) gained `?q=` ILIKE support.

Hygiene + CI guardrails:

- **Celery beat cleanup.** `cleanup_webhook_deliveries` enforces
  90-day retention on terminal `webhook_deliveries` rows (every 6h);
  `cleanup_expired_webhook_secrets` drops gen=2 rows past their 24h
  `expires_at` (hourly).
- **CI guardrails consolidation.** Single workflow gate covers no
  `verify=False` anywhere under `webhook_dispatcher/` (extended in
  this release to include `http_client.py`); no double `json.dumps`
  on the envelope; sentinel grep that the `body == signed_body`
  runtime assertion stays present at the HTTP-client boundary;
  audit_log coverage check asserting every webhook admin mutation
  writes a `WEBHOOK_*` row.
- **Integration test matrix.** `test_v160_integration_matrix.py` maps
  each of the 26 verification-plan points to a real test function or
  to an operator-manual gate logged via `caplog`. The Chainway C6000
  smoke test is the one operator-manual gate; everything else is
  automated. **1528 backend tests passing** (up from 910 at v1.5.0,
  1002 at v1.5.1).

Migrations:

- **029** -- `webhook_subscriptions` + `webhook_secrets`. UUID PK,
  JSONB filter, ceiling columns with CHECK bounds, partial index on
  active status. BEGIN/COMMIT-wrapped per v1.5.1 V-213 discipline.
- **030** -- `webhook_deliveries`. BIGSERIAL PK, RESTRICT FK on
  subscription_id, four partial indexes covering dispatcher and admin
  hot paths.
- **031** -- AFTER UPDATE trigger on `integration_events.visible_at`
  that fires `pg_notify('integration_events_visible', event_id)`.
  Self-test asserts the deferred-trigger -> UPDATE -> AFTER-UPDATE-trigger
  -> NOTIFY chain holds under a single outer commit.
- **032** -- `webhook_subscriptions_audit` +
  `webhook_secrets_audit` tables with statement-level DELETE /
  TRUNCATE triggers on both parent tables.
- **033** -- `webhook_subscriptions_tombstones` table for the
  URL-reuse acknowledgement gate.

Notes for operators:

- **Existing v1.5.x deployments must apply migrations 029-033 in
  numeric order** before bringing the new compose stack up; the
  dispatcher container's startup queries against the new tables fail
  until they exist. Fresh installs run them automatically. CI
  verification of the upgrade path lands in v1.7 (#217); until then
  the operator runs the migration sequence manually as part of the
  upgrade.
- **`SENTRY_ENCRYPTION_KEY` now protects two ciphertext stores**:
  `connector_credentials` (v1.3 inbound vault) and `webhook_secrets`
  (v1.6 outbound HMAC). Fernet rotation must re-encrypt both in the
  same transaction; missing one leaves a half-rotated deployment
  where the affected service cannot decrypt its own secrets after
  restart. See the updated rotation section in
  [`docs/connectors.md`](connectors.md).
- **`DISPATCHER_DATABASE_URL` is optional.** Dev and single-role
  deployments leave it unset. Production should set up a dedicated
  least-privilege role via `db/role-dispatcher.sql` and point
  `DISPATCHER_DATABASE_URL` at it.
- **`DISPATCHER_ENABLED=false` is the kill switch.** Container boots,
  logs CRITICAL, sleeps with the heartbeat file still touched. Use
  it to stop dispatch globally without a code rollback.
- **No mobile APK ships with v1.6.0.** v1.6.0 has no mobile code
  changes beyond the version-string bumps for BUILD_VERSION-guard
  consistency. Operators already on the v1.5.1 APK
  (`sentry-wms-v1.5.1.apk`) should stay on it -- it carries the
  dep-tree security overrides from #158 and #61. Operators still on
  older v1.4.1 / v1.4.3 APKs continue to authenticate and dispatch
  but lack those security fixes; install v1.5.1 if you have not
  already.

---

## v1.5.1 -- Security Audit Patch

*2026-04-27.* [Full notes](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.5.1).

Security patch closing ~22 findings from the post-v1.5.0 internal audit
of the Outbound Poll attack surface: the X-WMS-Token vault, the
`/api/v1/events*` and `/api/v1/snapshot/*` endpoints, the
`integration_events` outbox, the snapshot-keeper daemon, and the admin
token / consumer-group / connector-registry CRUD pages. No new
features. No API contract changes. No mobile runtime changes (the APK
is a fresh artifact only because the dependency overrides reshape the
build tree). Existing well-formed clients with correctly-scoped tokens
see no behaviour difference; what changed is enforcement strictness.

Token auth fixes:

- **Endpoint scope is now actually enforced (#140).** Pre-fix the
  `endpoints` column on `wms_tokens` was stored and rendered in the
  admin UI but `@require_wms_token` never consulted it; a token with
  any-or-no endpoint list could hit every `/api/v1/*` route the
  warehouse / event-type scope allowed. Migration 026 backfills
  pre-existing empty arrays so old tokens keep working.
- **Cross-worker token revocation via Redis pubsub (#146).** Pre-fix
  `token_cache.clear()` only flushed the handling gunicorn worker's
  dict; every other worker honored the stale entry until per-entry TTL
  expired (up to 60s). v1.5.1 publishes revocations on a
  `wms_token_events` channel that every worker subscribes to at boot.
  Sub-second across all workers in the Redis-available path; the 60s
  TTL remains as the backstop when Redis is down.
- **Stricter pepper validation (#142).** Boot guard rejects unset,
  empty, whitespace-only, the `.env.example` placeholder, and any
  value shorter than 32 characters. Pre-fix it rejected only unset /
  empty.
- **Uniform `401 invalid_token` body (#149).** Pre-fix the decorator
  returned three distinct bodies (missing / invalid / expired); an
  attacker who captured a plaintext could distinguish "this was once
  valid" from "never valid." Specific reason now stays in DEBUG log
  on `sentry_wms.auth.wms_token`.
- **Issuance-time scope existence checks (#150).** Admin token
  issuance validates that `warehouse_ids` and `event_types` actually
  point at real entities. Unknown values fail 400 with the offending
  entries enumerated.
- **Admin CRUD writes the audit_log hash chain (#141, #154).**
  `wms_tokens`, `consumer_groups`, and `connector_registry` mutations
  now append to `audit_log` at every site (issue, rotate, revoke,
  delete). Plaintext tokens never written to `details`; delete
  captures pre-mutation scope so the trail survives row removal.
- **Checkbox scope selectors on the token-create modal (#159).** New
  admin endpoint `GET /api/admin/scope-catalog` populates the
  warehouse / event-type / endpoint lists.

Polling and snapshot fixes:

- **`/api/v1/events/ack` enforces cursor horizon and per-event scope
  (#143).** Pre-fix a token with a legacy admin-issued shape could ack
  an arbitrary cursor on any consumer_group, jumping the cursor past
  every future event and silently losing data downstream. Now returns
  `400 cursor_beyond_horizon` and `403 ack_scope_violation` on the
  failing shapes; backwards acks remain pure no-ops.
- **Per-token concurrent-scan cap on `/api/v1/snapshot/inventory`
  (#144).** A single token could pin the entire 4-slot keeper pool;
  v1.5.1 caps to one active scan per token. Cursor requests on an
  active scan are exempt so partial-page flows keep working.
- **Strict-typed `consumer_groups.subscription` (#145).** Pydantic
  with `extra="forbid"`. Belt-and-suspenders parse-error path on the
  poll handler so legacy bad rows surface `409 subscription_invalid`
  instead of 500.
- **Consumer-group recreate requires explicit replay acknowledgement
  (#148).** Migration 027 (`consumer_groups_tombstones`) records
  `last_cursor_at_delete`. CREATE under a deleted id returns
  `409 replay_would_skip_history` unless the admin sends
  `acknowledge_replay: true`.
- **`/api/v1/events/types` filters by token scope (#151).** Pre-fix
  every caller saw every event type known to the system regardless of
  scope; reconnaissance for a later pivot is no longer free.

Database and infrastructure fixes:

- **Migrations 020 + 025 wrapped in transactions (#152).** The
  ten-table ALTER blocks are now all-or-nothing.
- **Snapshot-keeper supports a least-privilege DB role (#153).** New
  `SNAPSHOT_KEEPER_DATABASE_URL` env var; falls back to
  `DATABASE_URL` when unset so dev and single-role deployments are
  unchanged. New `db/role-snapshot-keeper.sql` provisions the role
  with the narrow grant set (`SELECT` on `integration_events`,
  `SELECT`/`UPDATE`/`DELETE` on `snapshot_scans`, `EXECUTE` on
  `pg_export_snapshot`).
- **Boot guard on dangerous proxy + bind combination (#147).** Refuses
  to start with `TRUST_PROXY=true` AND `API_BIND_HOST=0.0.0.0`
  because the combo lets any caller who reaches the api port directly
  spoof `X-Forwarded-For` and poison every rate-limit bucket, audit
  attribution, and downstream IP allowlist. Escape hatch
  `SENTRY_ALLOW_OPEN_BIND=1` logs CRITICAL on every boot.
- **`wms_tokens` deletion forensic trail (#157).** Migration 028 ships
  a `wms_tokens_audit` table plus AFTER DELETE / AFTER TRUNCATE
  statement-level triggers capturing `event_type`, `rows_affected`,
  `sess_user`, `curr_user`, `backend_pid`, `application_name`,
  `event_at`. Resolves the unattributed token wipe observed during
  the v1.5.0 release gate.
- **Audit catch-all (#156).** `proxy_fix_active` hidden from anonymous
  `/api/health` (moved to admin-gated `GET /api/admin/system-info`);
  dev-only banners on `docker-compose.proxied.yml` and
  `proxy/nginx.conf`; ProxyFix `x_prefix=0` reconciled with inline
  comment; `SENTRY_VALIDATE_EVENT_SCHEMAS` no longer frozen at module
  import; external-id CI guardrail walks `db/**/*.sql` in addition to
  `api/**/*.py`.
- **`source_txn_id` consumer-dedupe contract documented (#155).**
  `docs/events/README.md` now states explicitly that consumers MUST
  dedupe on `event_id` (server-side BIGSERIAL, monotonic in commit
  order), not on `source_txn_id` (attacker-controllable via
  `X-Request-ID`).
- **CSP report sink (#54).** New unauthenticated
  `POST /api/csp-report` logs CSP violations at WARNING, rate-limited
  60/min per IP.

Dependency hygiene:

- **`@xmldom/xmldom` -> ^0.9.10 override (#158).** Closes four
  newly-disclosed GHSAs against `<=0.8.12` reachable through five
  expo-related transitive paths. Build-time only (Expo config
  plugins). Silences the nightly Dependency Audit on `main` that had
  been failing since 2026-04-24.
- **cryptography 44.0.3 -> 46.0.7 (#59).** Closes carried-over
  GHSA-r6ph-v2qm-q3c2 and GHSA-m959-cc7f-wv43. Fernet / MultiFernet
  compatibility verified across 45.x and 46.x.
- **pytest 8.3.4 -> 9.0.3, pytest-cov 6.0.0 -> 7.1.0 (#60).** Closes
  GHSA-6w46-j5rx-g56g; pip-audit allowlist now empty.
- **eas-cli dev-tree GHSAs closed (#61).** `minimatch ^5.1.9` and
  `node-forge ^1.4.0` overrides; eas-cli bumped 18.5.0 -> 18.8.1.
  `npm-audit-mobile-dev` is now a gating job matching the prod-tree
  job.

UI defects caught during the audit cycle:

- **Recent Adjustments and Recent Transfers tables on the dashboard
  render every column (#161, #162).** Both were clipping a column on
  narrower viewports.

Migrations: **026** backfills `wms_tokens.endpoints` for tokens
created before v1.5.1 (idempotent), **027** adds
`consumer_groups_tombstones`, **028** adds `wms_tokens_audit` plus the
DELETE / TRUNCATE triggers.

Operator notes: a `SENTRY_TOKEN_PEPPER` shorter than 32 characters or
set to the `.env.example` placeholder now fails boot. Existing
well-formed peppers (32+ chars of entropy) hash to the same value and
require no changes. The new APK
(`sentry-wms-v1.5.1.apk`, attached to the GitHub release) installs
over v1.5.0 on Chainway C6000 devices. Standard upgrade procedure
applies: `git pull && docker compose down && docker compose build &&
docker compose up -d`.

---

## v1.5.0 -- Outbound Poll (Pipe A Read)

*2026-04-22.* [Full notes](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.5.0).

First `/api/v1/*` surface. External systems -- ERPs, commerce
platforms, analytics pipelines -- can now consume every
inventory-changing write Sentry performs via a cursor-paginated REST
read. The release ships a transactional outbox, a commit-order
visibility gate, a bulk-snapshot endpoint for the initial load, and
X-WMS-Token auth with hash-only storage. Admin panel gains two new
pages (API tokens, Consumer groups); mobile is untouched.

Outbox + emission:

- **`integration_events` transactional outbox** (migration 020).
  `BIGSERIAL event_id`, `JSONB payload`, denormalized
  `aggregate_external_id`, four btree indexes covering the v1.5.0
  query shapes. Deferred-constraint `visible_at` trigger sets
  `visible_at = clock_timestamp()` at COMMIT so readers ordering on
  `(visible_at, event_id)` see events in commit order even when
  BIGSERIAL assigned `event_id` values in a different order.
- **Seven emissions pinned to the framework catalog**:
  `receipt.completed`, `adjustment.applied` (approval + direct),
  `cycle_count.adjusted`, `transfer.completed`, `pick.confirmed`
  (one per SO in a pick batch), `pack.confirmed`, `ship.confirmed`.
  JSON Schema files at `api/schemas_v1/events/<type>/1.json`
  validated Draft 2020-12. Per-aggregate `SELECT ... FOR UPDATE`
  retrofit gives FIFO on the outbox without behaviour change for
  users.
- **External UUID retrofit across ten aggregate / actor tables**
  (`users`, `items`, `bins`, orders, receipts, adjustments,
  transfers, counts, fulfillments). Every insert site supplies
  `uuid.uuid4()` explicitly; migration 025 drops the
  `DEFAULT gen_random_uuid()` after the retrofit so a new handler
  that forgets the column fails loudly.
- **Schema registry + CI validation.** `events_schema_registry.py`
  loads every schema at `create_app` time; boot fails on a malformed
  or missing file. A dedicated CI step imports the registry on a
  fresh checkout so a broken schema fails the job before tests run.

Polling + snapshot endpoints:

- **`GET /api/v1/events`** -- cursor + consumer-group polling. Plain
  `int64` cursor (Decision G: not base64, not opaque), no `has_more`
  field (full page implies more; partial implies caught up). Mutual
  exclusion of `after` + `consumer_group` returns 400. Strict-subset
  scope enforcement (Decision H): a filter asking for anything
  outside the token's scope returns 403, never a silent intersection.
- **`POST /api/v1/events/ack`** -- consumer-group cursor advance.
  Atomic UPDATE with a `last_cursor <= :cursor` guard; out-of-order
  ack is a no-op, retried ack is idempotent.
- **`GET /api/v1/events/types`** and
  **`GET /api/v1/events/schema/<type>/<version>`** -- in-process
  catalog + raw JSON Schema body served as `application/schema+json`.
- **`GET /api/v1/snapshot/inventory`** -- bulk-snapshot endpoint for
  the initial load, backed by a new `snapshot-keeper` daemon that
  holds REPEATABLE READ transactions and exports a `pg_snapshot_id`
  via `pg_export_snapshot()`. API tier imports the same snapshot on
  short-lived connections via `SET TRANSACTION SNAPSHOT '<id>'`.
  Keyset-paginated by `(warehouse_id, item_id, bin_id)` so page cost
  is O(limit) regardless of scan size.
- **Per-token rate limits.** 120 req/min on polling routes,
  2 req/min on the snapshot endpoint. Bucket key prefers
  `token:<id>` over `user:<id>` over remote IP so a noisy connector
  cannot starve interactive cookie users.

Auth + token vault:

- **`wms_tokens` hash-only vault** (migration 023). `CHAR(64)`
  `token_hash` UNIQUE, typed-array scope columns
  (`warehouse_ids BIGINT[]`, `event_types TEXT[]`,
  `endpoints TEXT[]`), default `expires_at = NOW() + INTERVAL '1 year'`.
  No `encrypted_token` column -- lost plaintext means rotate,
  matching the GitHub / Stripe / AWS standard.
- **`SENTRY_TOKEN_PEPPER` env var.**
  `token_hash = SHA256(pepper || plaintext).hex()`. Pepper is
  env-only (never in the DB), required at boot. Rotating it is an
  emergency-only control that invalidates every issued token at
  once; runbook at `docs/runbooks/token-pepper-rotation.md`.
- **`@require_wms_token` decorator + per-worker 60s TTL cache.**
  Applied only to `/api/v1/events*` and `/api/v1/snapshot/*`;
  cookie-auth routes keep `@require_auth`. Revocation is visible
  within 60 seconds across every API worker.

Admin panel:

- **API tokens page** (`/api-tokens`) with rotation badges +
  per-row rotate / revoke / delete actions, one-time plaintext
  reveal with copy-to-clipboard and a save-confirmation checkbox.
- **Consumer groups page** (`/consumer-groups`) with subscription
  preview + heartbeat freshness, create + edit modals.
- **Connector registry endpoints** under
  `/api/admin/connector-registry` (distinct from the v1.3
  `connector_credentials` vault; the two concepts converge in v1.9).

Migrations: 020 (`integration_events`), 021 (`connectors`,
`consumer_groups`), 022 (`credential_type`), 023 (`wms_tokens`),
024 (`snapshot_scans` + NOTIFY trigger), 025 (drops the
`external_id` DEFAULT post-retrofit).

Tests: 910 backend passing (up from 740 at v1.4.5, +170 new cases),
58 admin unchanged, 32 mobile unchanged. CI gains a dedicated
schema-validation step that imports the registry on every push so
a broken schema file fails the job before tests run.

Operator notes:

- **First `/api/v1/*` surface.** This is the outbound read side for
  Pipe A. Cookie-authed admin/mobile routes under `/api/*` keep
  their existing contract.
- **`SENTRY_TOKEN_PEPPER` is required at boot.** Generate with
  `python -c "import secrets; print(secrets.token_hex(32))"` and set
  it in `.env` before `docker compose up -d`. The api container
  refuses to boot without it. Rotating the pepper invalidates every
  issued token; see
  [`token-pepper-rotation.md`](runbooks/token-pepper-rotation.md)
  for the procedure.
- **New `snapshot-keeper` service in `docker-compose.yml`.** After
  upgrading, `docker compose up -d` starts one additional container
  alongside the existing `db`, `redis`, `api`, `celery-worker`, and
  `admin`. The keeper is required for
  `GET /api/v1/snapshot/inventory`; a downed keeper surfaces as 503
  `snapshot_keeper_unavailable` on the first page of a scan.
- **No APK update.** The v1.4.3 APK on Chainway C6000 devices stays
  current; v1.5.0 has no mobile code changes beyond the version
  string in the login / home screen footers.
- **`TRUST_PROXY` behavior unchanged from v1.4.5.** Fresh-install
  operators who run Sentry behind a TLS-terminating reverse proxy
  set `TRUST_PROXY=true` in `.env`; direct-connect deployments leave
  it unset.

Migration guidance for production deployments (multi-million-row
aggregate tables) lives at
[`docs/runbooks/v1.5.0-migration.md`](runbooks/v1.5.0-migration.md).
The apartment-lab seed applies all six migrations in seconds; larger
tables should use the documented two-step "add nullable column,
batch backfill, then add UNIQUE + NOT NULL" alternative for
migration 020's external_id backfill.

---

## v1.4.5 -- Reverse Proxy Hotfix Follow-up

*2026-04-21.* [Full notes](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.4.5).

v1.4.4 (#107) wired Werkzeug `ProxyFix` into `api/app.py` behind a
`TRUST_PROXY` env var, but `docker-compose.yml` was never updated to
pass `TRUST_PROXY` into the `api` service environment. Operators who
set `TRUST_PROXY=true` in `.env` saw no effect because Compose does
not auto-forward arbitrary host env vars: the value stopped at the
Compose shell and `os.getenv("TRUST_PROXY")` returned `None` inside
the container, so `ProxyFix` stayed off and the CSRF-403-behind-proxy
bug from v1.4.0-v1.4.3 kept firing. Fruxh hit this after installing
v1.4.4 fresh. api + Compose + docs change; admin and mobile untouched.

Fixes:

- **`TRUST_PROXY` now reaches the api container (#136, refs #107,
  Fruxh's #98).** `docker-compose.yml` `services.api.environment`
  gains `TRUST_PROXY: ${TRUST_PROXY:-false}`, same pattern as
  `FLASK_ENV`. Default `false` preserves the direct-connect posture;
  operators opt in by setting `TRUST_PROXY=true` in `.env`. Without
  this single line, v1.4.4's `ProxyFix` wiring was cosmetic for every
  Compose-deployed install.
- **ProxyFix state is logged at Flask startup.** `api/app.py` emits
  `ProxyFix active: ...` or `ProxyFix inactive: ...` at WARNING level
  so the line clears the default gunicorn stderr threshold.
  Operators verify with `docker compose logs api | grep ProxyFix`
  without execing into the container.
- **`/api/health` now returns `proxy_fix_active`.** External monitors
  and the reverse proxy itself can confirm the wiring end-to-end with
  a single HTTPS `GET`. A green health response with
  `"proxy_fix_active": false` behind an nginx deployment is the exact
  signature of this bug.
- **`.env.example` gains a `TRUST_PROXY` block with the security
  warning inline**, and `docs/deployment.md` "Reverse Proxy (HTTPS)"
  clarifies that `TRUST_PROXY` goes in `.env` at the repo root (not
  `api/.env`), that `docker compose restart api` does NOT re-read
  `.env` (use `docker compose up -d` to pick up changes), and that
  the wiring can be verified three independent ways: `env | grep
  TRUST_PROXY` in the container, `logs api | grep ProxyFix` at the
  Flask layer, and `curl /api/health` from outside.

Tests: 740 backend (up from 738 at v1.4.4), 58 admin, 32 mobile.
`api/tests/test_proxy_fix.py` gains `TestHealthEndpointReportsProxyFixState`
with two cases locking the `/api/health` `proxy_fix_active` contract
in both the unproxied and proxied-client states; the original 4 cases
(opt-in invariant, scheme/host/is_secure rewrite, Secure CSRF + auth
cookies, change-password NOT 403'ing behind proxy) are unchanged and
still green. All CI workflows green.

Operator notes: the v1.4.3 APK is stable; no APK update is needed for
v1.4.5 (mobile has zero code changes and the API contract is
unchanged). Operators who upgraded to v1.4.4 and set `TRUST_PROXY=true`
but still saw CSRF-403 errors should pull v1.4.5, run `docker compose
down && docker compose build && docker compose up -d` (NOT just
`restart`), and confirm the wiring with `docker compose exec api env
| grep TRUST_PROXY` and `curl /api/health`.

---

## v1.4.4 -- Reverse Proxy Hotfix

*2026-04-21.* [Full notes](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.4.4).

Every production deployment that fronts Sentry with a TLS-terminating
reverse proxy (nginx, Caddy, Traefik, AWS ALB, etc.) was returning
`403 CSRF token missing or invalid` on every `POST` / `PUT` / `PATCH` /
`DELETE`. Fruxh filed #98 from his production install and traced it to
the root cause: Flask's `request.host` / `request.scheme` were stuck on
the internal `127.0.0.1:<port>` hop, so cookies were scoped to the wrong
host and the browser never resubmitted them. api-only change; admin and
mobile untouched.

Fixes:

- **Trust `X-Forwarded-*` headers from a reverse proxy when
  `TRUST_PROXY=true` (#107, refs #98).** `app.wsgi_app` is now wrapped in
  Werkzeug `ProxyFix` when the env var is set, so `request.scheme`,
  `request.host`, and `request.is_secure` reflect the browser's view of
  the request instead of the internal hop. Opt-in via env var because
  honouring `X-Forwarded-*` without a proxy in front lets any client
  forge its own scheme, hostname, and client IP. The
  `services/cookie_auth.py` header-based fallback stays as belt-and-
  suspenders.
- **Reverse-proxy deployment guidance expanded in `docs/deployment.md`.**
  New `TRUST_PROXY` section with an annotated nginx config, Caddy and
  Traefik v2+ snippets, a one-line note covering AWS ALB / GCP HTTPS LB /
  Azure Application Gateway / Cloudflare Tunnels / Fly / Render, an
  explicit security warning on header-forgery risk, and a multi-hop
  section for CDN-in-front deployments.
- **`python-dotenv` bumped `1.0.1` -> `1.2.2` (#106)** to clear
  `GHSA-mf9w-mj56-hr94`. OSV published the advisory between the
  2026-04-21 scheduled `main` audit (green) and the v1.4.4 initial push
  (red). Drop-in compatible; no code changes needed.

Tests: 738 backend (up from 734 at v1.4.3), 58 admin, 32 mobile. New
file `api/tests/test_proxy_fix.py` (4 cases): the opt-in invariant,
`TRUST_PROXY=true` rewriting `scheme` / `host` / `is_secure`, login
behind proxy headers returning `Secure` + `SameSite=Strict` cookies,
and change-password behind proxy headers NOT 403'ing on the CSRF gate
(Fruxh's exact repro path). All CI workflows green.

Operator notes: the v1.4.3 APK is stable; no APK update is needed for
v1.4.4 (mobile has zero code changes and the API contract is unchanged).
API operators behind a reverse proxy MUST add `TRUST_PROXY=true` to the
API environment before rebuilding; direct-connect deployments must NOT
set it.

---

## v1.4.3 -- Mobile Patch

*2026-04-20.* [Full notes](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.4.3).

Mobile patch release. Two fixes from the v1.4.3 mobile bug bash, plus a
follow-up for a regression surfaced during Chainway C6000 verification.
Zero backend or admin code changes. Closes the keyboard-fallback half
of Fruxh's #70 report; the camera-scanner half remains tracked under
#70 for v2.x.

Fixes:

- **Put-away "done" screen no longer overlays the success checkmark on
  the title (#103).** The done phase was rendered inside a flex
  container with `justifyContent: 'center'` that also holds a growing
  session-history list. Once history overflowed the viewport, the
  centered content pushed the large check glyph visually into the
  title below it. Swapped to a ScrollView with natural top-down flow,
  matching the CountScreen done-phase pattern.
- **Scan input fields now allow keyboard fallback for manual entry and
  copy/paste (#104, refs #70).** `ScanInput` had
  `showSoftInputOnFocus={false}` and `contextMenuHidden`, so tapping a
  scan field on the Chainway C6000 did nothing and long-press did not
  expose copy/paste. Removed both. Broadcast-intent scans still route
  through `ScanSettingsContext` and bypass the TextInput; keyboard-mode
  scans still land in `onChangeText` the same way manual typing does.
- **Scan input soft keyboard now only opens on user tap, not on
  auto-refocus (#105).** The #104 removal made the 1-second refocus
  loop that keeps the field ready for hardware scans re-pop the
  keyboard on every tick. `ScanInput` now tracks a `softInput` state
  that is false by default and flipped to true only on `onPressIn`,
  with a forced blur/refocus cycle so the updated
  `showSoftInputOnFocus` prop applies. Reset on blur and after submit
  so the auto-refocus loop, mount autofocus, and post-submit refocus
  stay silent.

Tests: 734 backend, 58 admin, 32 mobile (up from 24; new file
`mobile/src/components/__tests__/ScanInput.test.js` locks the
tap-to-open contract at the source level since the mobile vitest
harness has no RN runtime). All CI workflows green.

Operator notes: a new `sentry-wms-v1.4.3.apk` is attached to the
GitHub release and installs over v1.4.1 / v1.4.2 on Chainway C6000
devices without a data wipe. API and admin images have no source
changes; rebuilding them is safe but not required for mobile-only
operators.

---

## v1.4.2 -- Admin Panel Patch

*2026-04-20.* [Full notes](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.4.2).

Admin panel patch release. Operator safeguard against upgrades-without-rebuild, the V-017 `validation_error` cluster closed on seven admin create/edit forms, admin list page CRUD affordances and UI consistency across every page, plus a bundle of Fruxh-reported fixes from external deployments. Zero mobile code changes; v1.4.3 will follow for mobile-side reports.

Highlights:

- **Upgrade-without-rebuild detection (#73)** -- v1.4.0 added Flask-Limiter;
  v1.3.x operators who ran `git pull && docker compose up` without
  rebuilding crashed on `ModuleNotFoundError: flask_limiter`. The API
  now bakes the source `__version__` into the image at build time and
  fail-fast exits 2 with a clear remediation message when the code
  and image versions disagree. `docs/deployment.md` gains an
  "Upgrading" section.
- **V-017 validation_error cluster (#74-#81, #99)** -- Bin, Zone,
  PreferredBin, Inventory Adjustment, Inter-Warehouse Transfer,
  manual PO, manual SO create, Zone edit, plus the pre-merge
  Bin-create Zone-dropdown fix. Consolidated alignment tests lock
  every form's payload shape against the backend schema.
- **Admin list page CRUD affordances (#85 #86 #87 #88 #89 #90)** -- Bin
  row click opens a detail view with delete; Zone edit gains a delete
  button with 409-guard when bins are assigned; new dedicated Sales
  Orders admin list page; Close / Reopen PO and Cancel SO as
  reversible / one-way state transitions (not deletes).
- **UI consistency pass (#102)** -- pencil (&#9998;) and trash
  (&#128465;) row actions across every admin list page. PO / SO
  show pencil only; Close / Cancel remain state transitions in the
  edit modal.

Fruxh-reported from a production v1.4.1 deployment:

- `#72` flask_limiter upgrade crash -- closed by #73.
- `#71` validation_error cluster across four admin create forms --
  closed alongside #74-#81 and #85.
- `#98` First-time-setup "Your session is out of sync" false failure
  -- closed by the redirect-to-login fix.

Test counts: 734 backend, 58 admin, 24 mobile. All CI workflows
(Tests, Dependency Audit, Lockfile Version Check, Deploy Docs) green
on the merge commit.

Operator notes: upgrades MUST rebuild Docker images.
`git pull && docker compose down && docker compose build && docker compose up -d`
is the correct procedure. Skipping the build step now exits 2 at
startup with the remediation command in the logs.

---

## v1.4.1 -- Forced Password Change + Mobile Version Fix

*2026-04-18.* [Full notes](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.4.1).

Patch release bundling two bug fixes deferred from v1.4.0.

Highlights:

- **Forced password change on first login (#69)** -- fresh installs
  seed admin as `admin/admin` with a `must_change_password` flag. Auth
  middleware blocks every route except `/api/auth/me`,
  `/api/auth/change-password`, and `/api/auth/logout` until the admin
  changes the password. Eliminates the "grep logs for the random
  password" onboarding paper-cut that shipped from v1.0 through
  v1.4.0.
- **Mobile version display fix (#68)** -- HomeScreen and LoginScreen
  had been hardcoding `v1.2.0` for two releases. Now read the current
  version. Issue #67 tracks the v1.5 refactor that eliminates this
  class of bug permanently via build-time injection.
- **Forced-mode navigator fix** -- mobile `ChangePasswordScreen` save
  spinner stuck bug resolved. React Navigation native-stack was
  preserving the route when `must_change_password` flipped false;
  removing the screen from the non-forced branch lets native-stack
  fall through to Home.

Security:

- `validate_password` rejects `admin` as the new password
  (case-insensitive, whitespace-stripped).
- Mobile force-kill-and-reopen bypass closed: the flag persists
  inside the SecureStore-backed user dict, so a relaunch rehydrates
  forced mode.
- Distinct `audit_log` action `forced_password_change_completed`
  separates onboarding completions from voluntary rotations.

Test counts: 690 backend, 42 admin, 24 mobile. All CI green.

Operator notes: fresh installs are prompted to set a new password on
first login. Existing installs are unaffected (migration 019 defaults
the column to FALSE).

---

## v1.4.0 -- Security Backlog Cleanup

*2026-04-18.* [Full notes](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.4.0).

Pure security and hardening release. No new features. Addresses remaining
High-severity items from the v1.3.0 audit, all 9 findings from a fresh
audit of the v1.4 work, and the most impactful Medium / Low items from
the deferred backlog.

Highlights:

- **HttpOnly cookie + CSRF for admin auth (V-045)** -- admin JWT no
  longer lives in `localStorage`. CSRF double-submit pattern protects
  mutating requests. Mobile continues using bearer tokens.
- **SecureStore on mobile (V-047)** -- JWT migrated from plaintext
  AsyncStorage to the Android Keystore via `expo-secure-store`. One-shot
  migration on app launch.
- **Content-Security-Policy (V-050)** -- strict CSP on both API and
  nginx. Self-hosted fonts eliminate the last third-party origin.
- **Sync state race fix (V-102)** -- `run_id` UUID prevents stale
  workers from clobbering fresh sync state after the 1-hour takeover
  threshold.
- **Flask-Limiter rate limiting (V-041)** -- Redis-backed, per-user and
  per-IP quotas on sensitive admin endpoints.
- **Dependency audit in CI (V-042)** -- `pip-audit` and `npm audit` gate
  every push.
- **DNS rebinding pin (V-108)** -- connector outbound requests pin the
  resolved IP after the SSRF guard check.

Test counts: 647 backend, 32 admin, 8 mobile. All CI workflows green.

See the release notes for the full list of V-numbers, the accepted-risk
section, and the upgrade notes for admin panel, mobile app, and Docker
deployment.

---

## v1.3.0 -- Connector Framework + Security Hardening

*2026-04-17.* [Full notes](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.3.0).

The connector foundation. All the infrastructure for ERP integration
without any actual connector -- the framework that NetSuite,
BigCommerce, and Amazon connectors will plug into starting in v2.0.

- Abstract base class with auto-discovery registration
- Celery + Redis background job runner so sync operations never block
  the API thread
- Encrypted credential vault (Fernet, per-warehouse scoping,
  credentials never in logs or API responses)
- Sync state tracking with green / yellow / red health per connector
- Per-connector rate limiter, exponential backoff with jitter, and
  5-failure circuit breaker

Security audit: 4 Critical and 12 High findings fixed before release.
Removed hardcoded encryption key default, documented historical JWT
secret exposure (SA-2026-001, SA-2026-002), admin panel rebuilt as
production nginx, Redis broker requires auth, SSRF protection on
connector outbound requests, audit log is now append-only with
SHA-256 hash chain, plus IDOR fixes and race-condition fixes on
receiving and inventory operations. 570 total backend tests.

**Breaking for operators:**

- `SENTRY_ENCRYPTION_KEY` is required (no default)
- `REDIS_PASSWORD` is required
- Admin panel port changed from 3000 to 8080
- Migration `016_audit_log_tamper_resistance.sql` must be applied

---

## v1.2.0 -- Validation Schemas & Error Boundaries

*2026-04-16.* [Full notes](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.2.0).

- Pydantic v2 validation schemas on every JSON-accepting endpoint (17
  schema files). `@validate_body` decorator for consistent request
  validation. Invalid requests now return structured `validation_error`
  responses with `type` / `loc` / `msg` detail per field.
- Admin panel: every page route wrapped in an independent error
  boundary so one section crashing no longer white-screens the whole
  panel. Retry button to recover without a full page refresh.
- Mobile: handles the new `validation_error` format with
  operator-friendly messages.
- 75 new validation tests + 4 ErrorBoundary tests. 382 backend + 10
  frontend tests passing.

---

## v1.1.1 -- Patch

*2026-04-16.* [Full notes](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.1.1).

Three fixes for issues incorrectly closed or missed in v1.1.0. API /
admin only, no APK rebuild.

- CSV formula-injection guard on exports (cell values starting with
  `=`, `+`, `-`, `@`, `\t`, `\r` are prefixed with a single quote)
- `DATABASE_URL` fallback removed (startup `RuntimeError` if unset,
  same pattern as `JWT_SECRET`)
- Login-attempt count no longer leaked in failed-login error messages

---

## v1.1.0 -- Security Hardening

*2026-04-15.* [Full notes](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.1.0).

Twelve backlog fixes from the v1.0 audit.

- **Token invalidation on password change (M1)** -- `password_changed_at`
  column added; auth middleware rejects tokens issued before the last
  password change
- **JWT `iat` / `jti` claims (L10)** -- issued-at and UUID claims for
  revocation and replay detection
- **DB-backed rate limiting (M8)** -- `login_attempts` table, persistent
  across restarts, per-username and per-IP tracking (5 attempts, 15
  min lockout)
- **Password complexity (L1)** -- minimum 8 characters, at least one
  letter and one digit
- **Self-service password change (L2)** -- `POST /api/auth/change-password`
  plus a mobile UI modal in the user dropdown
- **Warehouse listing auth (L7)** -- `GET /api/warehouses/list` now
  requires JWT; mobile warehouse selection moved to a post-login
  blocking modal
- **`suggest_bin` warehouse scope (L8)** -- preferred-bin and default-bin
  queries filtered to the user's allowed warehouses
- **CSV import limit (M10)** -- reject payloads over 5000 records
- **Cycle count self-approval check (M3)** -- configurable
  `require_count_approval_separation` setting
- **Pagination (M6)** -- `page` / `per_page` on warehouses, zones, bins,
  and users endpoints
- **Cleartext HTTP disabled for production (L5)** -- `usesCleartextTraffic`
  gated to dev / preview profiles
- **Production docker-compose (L6)** -- `docker-compose.prod.yml` with
  no source volume mounts

Migrations added: `014_password_changed_at.sql`, `015_login_attempts.sql`.
19 new tests (307 total).

---

## v1.0.0 -- Production Release

*2026-04-14.* [Full notes](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.0.0).

The first open-source warehouse management system built for e-commerce.

- Full warehouse lifecycle: Receive, Put-Away, Pick Walk, Pack, Ship,
  Cycle Count, Transfer
- React Native mobile app with Chainway C6000 broadcast-intent scanner
  support
- React admin panel with dark theme, warehouse context picker, audit log
- Inventory adjustments and inter-warehouse transfers
- CSV / JSON bulk import with templates
- Docker Compose one-command setup with demo data
- 288 automated tests passing

Security baseline: JWT with live database validation per request,
warehouse authorization middleware on every endpoint, parameterized SQL
throughout, login lockout, bcrypt hashing, CORS restriction, random
admin password on first run, and a full pre-release audit.

MIT licensed. Free forever.
