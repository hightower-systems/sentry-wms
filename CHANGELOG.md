# Changelog

All notable changes to Sentry WMS will be documented in this file.

## [Unreleased] - v1.5.1

Security patch release. Closes findings from the v1.5.1 post-v1.5.0 audit.

### Security fixes

- **V-200 (#140) -- `wms_tokens.endpoints` scope is now enforced.** Pre-v1.5.1 the column was stored and surfaced in the admin UI as a scope boundary, but `@require_wms_token` never consulted it; every token with any-or-no endpoint list could hit every `/api/v1/*` route the warehouse / event-type scope allowed. v1.5.1 closes the gap: the decorator maps the Flask endpoint to a user-facing slug (`events.poll`, `events.ack`, `events.types`, `events.schema`, `snapshot.inventory`) and returns `403 endpoint_scope_violation` when the token's slug list does not include the current route. Empty list denies every v1 route (plan Decision S: empty = no access; matches `warehouse_ids` / `event_types` semantics).
  - `CreateTokenRequest` now requires a non-empty `endpoints` list and rejects unknown slugs with a 400 that names the offending value.
  - Admin UI `Tokens.jsx` gains helper text listing valid slugs and a "Grant all v1 endpoints" one-click preset so the common-case "unrestricted" token still takes one click.
  - Migration 026 backfills existing tokens whose `endpoints = '{}'` with the full slug set so pre-v1.5.1 tokens keep authenticating after the upgrade.
  - Endpoint-scope enforcement is covered by four new decorator tests + three admin validation tests + one migration-backfill test.

- **V-208 (#141) -- Admin token CRUD now writes the `audit_log` hash chain.** Pre-v1.5.1 `POST /api/admin/tokens`, `POST /tokens/<id>/rotate`, `POST /tokens/<id>/revoke`, and `DELETE /tokens/<id>` all mutated `wms_tokens` without appending to `audit_log`. Post-incident forensics on a compromised admin account had no way to determine which tokens existed, who issued or rotated them, or what scope was erased on delete. v1.5.1 adds one audit row per call at every mutation site. Entity type is `WMS_TOKEN`; action types are `TOKEN_ISSUE`, `TOKEN_ROTATE`, `TOKEN_REVOKE`, `TOKEN_DELETE`. The issue row captures the full granted scope; delete captures a `previous_scope` snapshot so the trail survives the row's removal. Plaintext token values are never written to `details`. The v1.4 hash-chain trigger keeps the new rows tamper-evident; `verify_audit_log_chain()` still passes with the extra writes.

- **V-201 (#142) -- `SENTRY_TOKEN_PEPPER` boot guard rejects weak values.** The v1.5.0 guard rejected only unset / empty peppers; a 1-byte value like `SENTRY_TOKEN_PEPPER=x` passed silently and produced weakly-peppered hashes. v1.5.1 adds a shared `validate_pepper_config` helper used by the `create_app` boot check, the request-time `_load_pepper`, and the admin issuance `_hash_for_storage`. The helper rejects: unset, empty string, whitespace-only, the literal `.env.example` placeholder (`replace-me-with-secrets-token-hex-32`), and any value shorter than 32 characters. Valid bytes are returned verbatim (not normalised) so any deployment that configured a well-formed pepper keeps hashing to the same value; weak peppers now fail boot with a clear message pointing to the generator command.

- **V-202 (#143) -- `/api/v1/events/ack` now enforces cursor horizon + token-scope on every advance.** Pre-v1.5.1 the ack handler had no upper bound on the cursor value and no event-level scope check; a token with `connector_id=NULL` (legacy admin-issued shape) could ack an arbitrary cursor on any consumer_group, jumping the cursor past every future event and causing silent data loss at the downstream consumer. v1.5.1 rejects two classes of advance: (1) `cursor_beyond_horizon` (400) when the requested cursor exceeds the greatest `event_id` in `integration_events`; (2) `ack_scope_violation` (403) when any event in `(last_cursor, cursor]` falls outside the token's `warehouse_ids` or `event_types` scope. Backwards acks (`cursor <= last_cursor`) remain pure no-ops and skip both checks so retried idempotent acks do not pay the query cost. The `AckBody.cursor` schema also gains `le=9_223_372_036_854_775_807` so an int64-overflow cursor (2**63) returns 400 at the schema layer instead of surfacing a 500 from the DB (this incidentally closes V-220 from the hardening umbrella).

- **V-203 (#144) -- Per-token concurrent-scan cap on `/api/v1/snapshot/inventory`.** Pre-v1.5.1 the keeper pool (4 slots, 5-minute idle timeout) could be fully pinned by a single token holding four in-flight scans; every other token saw `503 snapshot_keeper_unavailable` until the idle timeout elapsed. v1.5.1 enforces `MAX_CONCURRENT_SCANS_PER_TOKEN = 1` at the first-page INSERT site: if the token already has a `pending` or `active` row in `snapshot_scans`, the endpoint returns `429 snapshot_in_flight` with a message explaining that the client must page the existing scan to completion before starting a new one. Cursor requests (all non-first-page requests) are exempt so partial-page flows keep working. `'done'` / `'expired'` / `'aborted'` scans do not count. The cap is per-token; two different tokens can still each hold one active scan. Pool exhaustion now requires parallel attack across distinct credentials, which is the harder-to-achieve shape.

- **V-204 (#145) -- Consumer-group subscription JSON is strict-typed.** Pre-v1.5.1 `ConsumerGroupCreateRequest.subscription` was `Dict[str, Any]`; the admin UI only client-side validated JSON-object-ness. An admin could save `{"warehouse_ids": "abc"}` and the next poll handler iterated over the string's characters, crashed on `int("a")`, and returned 500 indefinitely until an admin edited the row. v1.5.1 introduces `SubscriptionFilter` (Pydantic, `extra="forbid"`, `event_types: Optional[List[str]]`, `warehouse_ids: Optional[List[int]]`) and uses it for both `ConsumerGroupCreateRequest` and `ConsumerGroupUpdateRequest`. Unknown keys (`__proto__`, typos, etc.) and wrong-typed values (string where array expected, non-int warehouse_id) fail 400 at the admin endpoint. Belt-and-suspenders: the poll handler wraps the legacy-row parse in try/except and returns `409 subscription_invalid` instead of 500 so pre-v1.5.1 bad rows surface a recoverable contract error.

- **V-205 (#146) -- Token-cache revocation is now cross-worker via Redis pubsub.** Pre-v1.5.1 `token_cache.clear()` only flushed the handling gunicorn worker's dict; every other worker retained its stale entry until the per-entry TTL expired (up to 60s). A compromised token therefore kept working on N-1 workers for the full 60-second window after the admin clicked revoke. v1.5.1 adds a `wms_token_events` Redis channel: every worker starts a daemon subscriber thread at `create_app` time and evicts the matching entry on receipt. `admin_tokens.py` replaces the legacy `token_cache.clear()` calls with `token_cache.invalidate(token_id)` on rotate / revoke / delete; the new helper evicts locally AND publishes. Revocation latency drops from up to 60s to sub-second in the Redis-available path; the 60s TTL remains as the backstop when Redis is down. The publisher failure path is swallowed with a warning log so admin mutations never block on Redis availability. The admin UI revoke modal copy is updated to match the new guarantee.

- **V-206 (#147) -- Boot guard refuses TRUST_PROXY=true + API_BIND_HOST=0.0.0.0.** Pre-v1.5.1 the `.env.example` defaults were safe (`TRUST_PROXY=false`, `API_BIND_HOST=127.0.0.1`) but an operator who copied a dev `.env` to prod could end up with a deployment that trusts `X-Forwarded-*` from any caller AND binds port 5000 on every interface. Combined, an attacker who reached the api port directly (cloud misconfig, Security Group hole, bastion misroute) could spoof `X-Forwarded-For` and poison every rate-limit bucket, audit attribution, and any downstream IP allowlist. v1.5.1 refuses to boot on this combination: `create_app` raises `RuntimeError` naming the fix. The documented escape hatch is `SENTRY_ALLOW_OPEN_BIND=1` for deployments that apply network-level protection elsewhere (e.g. a VPC lock-down); setting it logs a CRITICAL message on every boot so the acknowledgement stays visible. `API_BIND_HOST` is now forwarded into the api container via `docker-compose.yml` so the Flask boot guard can see it. `.env.example` gains a prominent "NEVER SET BOTH" warning block.

- **V-207 (#148) -- Consumer-group recreate requires explicit replay acknowledgement.** Pre-v1.5.1 an admin could DELETE a consumer_group at `last_cursor=N`, then POST a new group with the same `consumer_group_id` and the new row defaulted to `last_cursor=0`. The connector using that group then replayed every event since the outbox dawn; downstream ERPs that treat the stream as authoritative for inventory movement saw every event twice. v1.5.1 records a tombstone on DELETE (new migration 027: `consumer_groups_tombstones` with `last_cursor_at_delete`, `connector_id`, `deleted_at`, `deleted_by`) and, on a later CREATE under the same id, returns `409 replay_would_skip_history` with the pre-delete cursor in the body. Admins who genuinely want the replay send `{"acknowledge_replay": true}` to proceed; the acknowledgement clears the tombstone so subsequent delete cycles behave the same way. A CREATE with a fresh id is unaffected.

- **V-209 (#149) -- `@require_wms_token` returns a uniform 401 body for every auth failure.** Pre-v1.5.1 the decorator returned three distinct bodies: `{"error":"missing_token"}` (no header), `{"error":"invalid_token"}` (wrong hash / revoked), `{"error":"token_expired"}` (expired row). An attacker who captured or guessed a plaintext could distinguish "this was once a valid token" from "this was never a valid token" by observing whether the response said `token_expired` vs `invalid_token`; the differential narrows the search space when paired with an adjacent leak (old backup, partial shoulder-surf, etc.). v1.5.1 collapses every 401 path into a single `{"error":"invalid_token"}` body. The specific reason (missing header, unknown hash, revoked, expired) stays in a DEBUG log on `sentry_wms.auth.wms_token` for operator forensics. Timing partially flattened: the missing-header path now performs the same cache lookup a real token would trigger, so the cache-hit / cache-miss latency gap is smaller. Endpoint-scope violations (403) stay distinct because 403 and 401 are different HTTP semantics.

- **V-210 (#150) -- Admin token issuance validates that warehouse_ids + event_types point at real entities.** Pre-v1.5.1 `CreateTokenRequest` accepted `warehouse_ids=[99999999]` or `event_types=["nonexistent.type"]` without complaint; the token stored that scope and polled empty forever. Audit intent was destroyed (an admin who typo'd `33` instead of `3` got silent-empty instead of an error, and token logs showed a scope indistinguishable from "valid warehouse that was later deleted"). v1.5.1 adds two existence checks at `POST /api/admin/tokens`: warehouse_ids is matched against the `warehouses` table and event_types against `V150_CATALOG`. Unknown values fail 400 with `unknown_warehouse_ids` or `unknown_event_types` and the offending entries enumerated in the response body so the admin can fix the input without reading server logs. Empty arrays bypass the existence check (empty = no access per Decision S, consistent with the runtime enforcement).

- **V-212 (#151) -- `GET /api/v1/events/types` now filters the catalog by token scope.** Pre-v1.5.1 the endpoint returned every entry in `V150_CATALOG` regardless of the caller's `event_types` scope; a token scoped to `receipt.completed` could still see that `ship.confirmed`, `adjustment.applied`, `cycle_count.adjusted`, etc. existed, aiding reconnaissance for a later pivot ("if I can get a broader token, these are worth mining"). v1.5.1 narrows the response to the intersection of `V150_CATALOG` and the token's `event_types` list. Empty scope returns an empty list (Decision S: empty = no access). `events_schema_registry.known_types(event_types_filter=...)` accepts None to return the full catalog so admin / internal callers can still enumerate it if a parallel admin-only endpoint is ever added.

- **V-213 (#152) -- Migrations 020 and 025 are now wrapped in a single transaction.** Both migrations issue ten `ALTER TABLE` statements back-to-back (020 adds `external_id` across ten aggregate / actor tables, 025 drops the DEFAULT across the same ten). Pre-v1.5.1 each ALTER committed on its own, so a failure on table 4 of 10 (lock timeout, disk full, unexpected schema drift) left a half-applied state. An operator who then skipped ahead to migration 025 produced an asymmetric shape where "old" tables still had the DEFAULT and "new" tables did not; insert sites that forgot `external_id` worked against some tables and failed against others, a miserable-to-debug bug. v1.5.1 wraps both migration bodies in `BEGIN` / `COMMIT` so the whole set is all-or-nothing.

- **V-214 (#153) -- Snapshot-keeper supports a dedicated least-privilege DB role.** Pre-v1.5.1 the `snapshot-keeper` container shared `DATABASE_URL` with the api and ran under the full `sentry` role; a compromise of either side gave the attacker everything the other could do. v1.5.1 adds `SNAPSHOT_KEEPER_DATABASE_URL`: the keeper reads that first and falls back to `DATABASE_URL` when unset so dev and single-role deployments are unchanged. Operators running production set up a dedicated role with the narrow grant set (`SELECT` on `integration_events`, `SELECT`/`UPDATE`/`DELETE` on `snapshot_scans`, `EXECUTE` on `pg_export_snapshot`) via the new `db/role-snapshot-keeper.sql` (operator-driven, idempotent, password supplied as a psql variable) and point `SNAPSHOT_KEEPER_DATABASE_URL` at it. `docker-compose.yml` forwards the new var; `.env.example` documents the setup.

- **V-221 (#154) -- Admin consumer-groups + connector-registry CRUD now writes the `audit_log` hash chain.** Structurally identical to V-208's fix for `wms_tokens` CRUD, scoped to the adjacent admin surface. Pre-v1.5.1 `POST /api/admin/connector-registry`, `POST /api/admin/consumer-groups`, `PATCH /api/admin/consumer-groups/<id>`, and `DELETE /api/admin/consumer-groups/<id>` mutated their tables without appending to `audit_log`; a rogue admin could delete + recreate a group with a tampered subscription (V-204) and leave no forensic trail. v1.5.1 writes one audit row per call: action types `CONNECTOR_REGISTRY_CREATE`, `CONSUMER_GROUP_CREATE`, `CONSUMER_GROUP_UPDATE`, `CONSUMER_GROUP_DELETE`. Consumer-group create records whether the call was an acknowledged replay (V-207 path), so investigators can spot replays without cross-referencing tombstones. Delete captures a full `subscription_at_delete` snapshot so the trail survives row removal. Entity-id convention: consumer_group_id + connector_id are VARCHAR so they cannot fit `audit_log.entity_id INT NOT NULL`; writes use `entity_id=0` as a sentinel and carry the real string id in `details`.

- **V-217 (umbrella #156) -- `SENTRY_VALIDATE_EVENT_SCHEMAS` is no longer frozen at module import.** Pre-v1.5.1 `_VALIDATION_ENABLED` was a module-level constant evaluated once when `services.events_service` first imported; flipping the env var at runtime (test fixtures, an operator hot-toggling during an incident) had no effect because the constant was already resolved. v1.5.1 replaces the constant with a `_validation_enabled()` helper that reads `os.getenv` on every emit, so a change to the env var takes effect on the next call without a worker restart. New test asserts that toggling the var mid-process flips validation without a module reload.

- **V-216 (umbrella #156) -- External-id CI guardrail now covers `db/**/*.sql`.** Pre-v1.5.1 `api/tests/test_external_id_inserts.py` only walked the `api/` tree (Python sources), so a seed script or migration that INSERTed into one of the ten UUID-retrofitted tables without `external_id` passed CI and failed at migration time with a `NOT NULL` violation. v1.5.1 extends the walk to every `.sql` file under `db/` (seeds, migrations, operator-driven helpers like `db/role-snapshot-keeper.sql`). Current files are clean so the check stays green on the v1.5.1 branch; the coverage closes the blind spot for future changes.

- **V-215 (umbrella #156) -- `/api/health` no longer exposes `proxy_fix_active` to anonymous callers.** The field moved to a new admin-only `/api/admin/system-info` endpoint gated on `@require_auth` + `@require_role("ADMIN")`. Pre-v1.5.1 the unauthenticated health endpoint told any caller whether the deployment was behind a trusted proxy -- useful recon for deciding whether `X-Forwarded-For` spoofing would stick. Anonymous `/api/health` now returns only `{status, service}` and is byte-for-byte identical whether TRUST_PROXY is true or false. `docs/deployment.md` is updated to route operators at the new admin endpoint; Docker healthcheck behaviour is unchanged.

- **V-211 (#155) -- Consumer-contract dedupe rule documented.** `source_txn_id` on the event envelope is a Sentry-internal idempotency key exposed on the wire for tracing; an authenticated caller can set it to an arbitrary UUID via the `X-Request-ID` header. A downstream consumer that dedupes on `source_txn_id` alone is trusting an attacker-controllable value -- one legitimate caller with a deterministic X-Request-ID pattern is enough to poison dedupe for future events on the same aggregate. `docs/events/README.md` gains a new "Consumer contract" section stating explicitly: consumers MUST dedupe on `event_id` (server-side `BIGSERIAL`, monotonic in commit order via the `visible_at` trigger), not on `source_txn_id`. The `@before_request` hook's code comment is updated to point at the new doc so future contributors do not reintroduce the attack surface in an expanded form. Passthrough retained for the distributed-tracing use case; dropping it entirely is a v1.6 consideration once the consumer ecosystem has received the contract notice.

### Dependency hygiene

- **`@xmldom/xmldom` override to `^0.9.10`** (#158). Four newly-disclosed GHSAs against `@xmldom/xmldom <= 0.8.12` (GHSA-2v35-w6hq-6mfw DoS via recursion, GHSA-f6ww-3ggp-fr8h XML injection via DocumentType, GHSA-x6wf-f3px-wcqx + GHSA-j759-j44w-7fr8 XML node injection) reachable at 5 transitive paths under Expo 54. Same override pattern as the pre-existing `tar` pin. xmldom is build-time only (Expo config plugins), not runtime on the device.

### Migrations

- **Migration 026** -- `UPDATE wms_tokens SET endpoints = ARRAY['events.poll', ..., 'snapshot.inventory'] WHERE endpoints = '{}'`. Idempotent. Fresh installs have no pre-existing empty arrays to backfill so the migration is a no-op there; upgrade deployments get grandfathered for every token created before v1.5.1.

## [v1.5.0] - 2026-04-22

"Outbound Poll" release. External systems can now consume every inventory-changing write Sentry performs via a cursor-paginated REST read. Introduces a transactional outbox (`integration_events`) populated by seven emission sites in the same DB transaction as the state change that caused it, a visibility gate that keeps the poll in commit order even when BIGSERIAL allocates `event_id` out of commit order, a bulk-snapshot endpoint for the initial load, X-WMS-Token inbound auth with hash-only storage, and admin-panel CRUD for both the connector registry and consumer groups. 170 new backend tests (910 passing, up from 740 at v1.4.5).

No mobile behaviour changes. Admin panel gains two new pages (API tokens, Consumer groups) and a sidebar entry each; existing flows unchanged. Five new migrations (020-024) plus 025 to drop the `external_id` DEFAULT after the retrofit. One new docker-compose service (`snapshot-keeper`) and one new required env var (`SENTRY_TOKEN_PEPPER`).

### Added -- Outbox + emission

- **`integration_events` transactional outbox** (migration 020). `BIGSERIAL event_id`, `JSONB payload`, denormalized `aggregate_external_id` per Decision J so the poll query never joins back to the aggregate table, four btree indexes covering the v1.5.0 query shapes (warehouse, type, visibility gate). BRIN deferred to v2.1 partitioning per Decision O.
- **Deferred-constraint `visible_at` trigger.** Sets `visible_at = clock_timestamp()` at COMMIT so readers ordering on `(visible_at, event_id)` see events in commit order even when BIGSERIAL assigned `event_id` values in a different order. Structural invariant, not discipline at each call site.
- **External UUID retrofit across ten aggregate / actor tables** (`users`, `items`, `bins`, `sales_orders`, `purchase_orders`, `item_receipts`, `inventory_adjustments`, `bin_transfers`, `cycle_counts`, `item_fulfillments`). Every insert site across `api/routes/*`, `api/services/picking_service.py`, test fixtures, and `db/seed-apartment-lab.sql` retrofitted to supply `uuid.uuid4()` explicitly. Migration 025 drops the `DEFAULT gen_random_uuid()` after the retrofit so a new handler that forgets the column fails loudly. CI grep guardrail at `api/tests/test_external_id_inserts.py` catches regressions.
- **Per-aggregate `SELECT ... FOR UPDATE` retrofit** at the seven emission sites (receiving, review_adjustments, direct_adjustment, packing, shipping, complete_batch via `FOR UPDATE OF so`). Correctness strengthening that gives per-aggregate FIFO on the outbox without behaviour change for users. `transfers.move` delegates to `move_inventory`'s V-030 source-inventory lock; `putaway.confirm` does not emit (Decision K: internal movement only).
- **Seven event emissions pinned to the framework catalog.** `receipt.completed`, `adjustment.applied` (approval + direct), `cycle_count.adjusted` (approval with non-null `cycle_count_id`), `transfer.completed`, `pick.confirmed` (one per SO in a pick batch), `pack.confirmed`, `ship.confirmed`. JSON Schema files at `api/schemas_v1/events/<type>/1.json` validated Draft 2020-12. Runtime validation gated by `SENTRY_VALIDATE_EVENT_SCHEMAS` (default true in CI, default false in prod).
- **`emit_event` helper** in `api/services/events_service.py`. Shape mirrors `write_audit_log`; lives in the caller's transaction; uses `INSERT ... ON CONFLICT (aggregate_type, aggregate_id, event_type, source_txn_id) DO NOTHING RETURNING event_id` for idempotent replay. Request-scoped `g.source_txn_id` set by a `before_request` hook that prefers `X-Request-ID` when it parses as a UUID.
- **Schema registry + CI validation.** `api/services/events_schema_registry.py` loads every schema at `create_app` time; boot fails on a malformed or missing file. New CI step in `.github/workflows/test.yml` imports the registry on a fresh checkout so a broken schema fails the job before the test step runs.

### Added -- Polling endpoints

- **`GET /api/v1/events`** -- cursor + consumer-group polling. Plain `int64` cursor (Decision G: not base64, not opaque), no `has_more` field (full page implies more; partial implies caught up). Mutual exclusion of `after` + `consumer_group` returns 400. Strict-subset scope enforcement (Decision H): a request whose `warehouse_id` or `types` filter asks for anything outside the token's scope returns 403, never a silent intersection. Visibility gate is hardcoded `visible_at <= NOW() - INTERVAL '2 seconds'`.
- **`POST /api/v1/events/ack`** -- consumer-group cursor advance. Atomic `UPDATE consumer_groups SET last_cursor = :cursor WHERE consumer_group_id = :id AND last_cursor <= :cursor`; an out-of-order ack is a no-op. 404 on unknown group, 403 on cross-connector cursor reuse (token's `connector_id` must match the group's when set).
- **`GET /api/v1/events/types`** -- catalog listing with `(event_type, versions, aggregate_type)` per entry. **`GET /api/v1/events/schema/<type>/<version>`** -- raw JSON Schema body served as `application/schema+json`. Both served from the in-process registry, no DB round-trip.
- **Per-token rate limits.** 120 req/min on polling routes, 2 req/min on the snapshot endpoint. Bucket key prefers `g.current_token.token_id` over `g.current_user.user_id` over remote IP so a noisy connector cannot starve interactive cookie users.
- **Consumer-group heartbeat throttling.** `last_heartbeat` UPDATEs throttled to once per 30 seconds via a per-process in-memory dict (Decision T). Cuts write amplification on the hot poll path without a material loss of freshness for the admin panel's heartbeat column.

### Added -- Auth + token vault

- **`wms_tokens` hash-only vault** (migration 023). `CHAR(64)` `token_hash` UNIQUE, typed-array scope columns (`warehouse_ids BIGINT[]`, `event_types TEXT[]`, `endpoints TEXT[]`) per Decision S, nullable FK to `connectors`, default `expires_at = NOW() + INTERVAL '1 year'` per Decision R. No `encrypted_token` column -- lost plaintext means rotate, matching the GitHub / Stripe / AWS standard (Decision P).
- **`SENTRY_TOKEN_PEPPER` env var.** `token_hash = SHA256(pepper || plaintext).hex()` per Decision Q. Pepper is env-only (never in the DB), required at boot. Rotating it is an emergency-only control that invalidates every issued token at once; runbook at `docs/runbooks/token-pepper-rotation.md`.
- **`@require_wms_token` decorator + per-worker 60s TTL cache.** Applied only to `/api/v1/events*` and `/api/v1/snapshot/*`; cookie-auth routes keep `@require_auth`. Cache keyed on `token_hash`, guarded by `threading.Lock`; revocation is visible within 60 seconds across every API worker.
- **Admin token CRUD** under `/api/admin/tokens`. Plaintext returned exactly once at issuance and rotation; never listable after. Rotation-age badge computed server-side at the 75/90 day thresholds so the UI is declarative. `docker-compose.yml` forwards `SENTRY_TOKEN_PEPPER` with the standard `:?` hard-fail; `.env.example` documents the env var with a `secrets.token_hex(32)` generation command.

### Added -- Bulk snapshot

- **`snapshot_scans` coordination table** (migration 024) + `AFTER INSERT` trigger that fires `pg_notify('snapshot_scans_pending', scan_id)` only for pending-status inserts. Keeper wake-up latency is sub-millisecond in the LISTEN path.
- **`snapshot-keeper` daemon** at `api/services/snapshot_keeper.py` (new docker-compose service). Holds REPEATABLE READ transactions that export a `pg_snapshot_id` via `pg_export_snapshot()` so the API tier can import the same snapshot on short-lived connections via `SET TRANSACTION SNAPSHOT '<id>'`. Pool cap 4, 5-minute idle timeout, boot-time orphan cleanup flips dead `active` rows to `aborted`, graceful SIGTERM, heartbeat file for Compose healthcheck.
- **`GET /api/v1/snapshot/inventory`**. Base64-encoded cursor of `{scan_id, warehouse_id, item_id, bin_id}` (Decision G), keyset-paginated by `(warehouse_id, item_id, bin_id)` so the page query cost is O(limit) regardless of scan size. Partial page flips the scan to `status='done'` so the keeper reaps it. Cursor tamper protection runs before `SET TRANSACTION SNAPSHOT`: `created_by_token_id` must match and the cursor's `warehouse_id` must match the request query param; mismatch returns 403 `cursor_scope_violation`. Expired / aborted scans return 410 Gone. Keeper unavailable returns 503 with the orphan pending row cleaned up.
- **Handoff invariant verified.** After a scan completes, polling `after=snapshot_event_id` returns events whose `event_id > snapshot_event_id` with no gap and no overlap. Proven at two levels: (1) `test_snapshot_keeper.test_exported_snapshot_is_importable_and_matches` asserts two separate connections importing the same `pg_snapshot_id` see identical `pg_current_snapshot()`; (2) `test_snapshot.test_poll_after_snapshot_event_id_excludes_pre_skips_to_post` asserts the HTTP-level boundary.

### Added -- Admin panel

- **API tokens page** (`/api-tokens`). List with rotation badges + per-row rotate / revoke / delete actions. Create modal with typed scope editors (comma-separated warehouse IDs, event types, endpoint slugs). One-time plaintext reveal modal with copy-to-clipboard and a "I have saved this token" checkbox gating the Close button.
- **Consumer groups page** (`/consumer-groups`). List with subscription preview + heartbeat freshness. Create modal with `connector_id` dropdown populated from the new registry endpoint. Subscription editor is a JSON textarea validated client-side before submit. Edit modal patches subscription only; last_cursor and connector_id are not operator-editable in v1.5.0.
- **Connector registry endpoints** under `/api/admin/connector-registry`. Distinct path from the existing `/api/admin/connectors` which serves the v1.3 `connector_credentials` vault; the two concepts converge in v1.9. POST returns 409 on duplicate `connector_id`; GET lists every connector with timestamps.

### Migrations

- **020**: `integration_events` table, `external_id UUID` columns on ten tables with `DEFAULT gen_random_uuid()` so existing inserts keep working, deferred `visible_at` trigger. Runs without downtime on apartment-lab seed.
- **021**: `connectors` (minimal PK + display_name; v1.9 expands) and `consumer_groups` (FK to connectors, `last_cursor`, `last_heartbeat`, `subscription JSONB`).
- **022**: `credential_type` column on `connector_credentials` (default `connector_api_key`, covers existing v1.3 rows; future values `outbound_oauth`, `outbound_api_key`, `outbound_bearer` for v2+).
- **023**: `wms_tokens` hash-only vault with typed-array scope columns and 1-year default expiry.
- **024**: `snapshot_scans` table + `snapshot_scans_pending` NOTIFY trigger.
- **025**: drops the `external_id` DEFAULT on all ten retrofitted tables after the emission + insert retrofit lands, so a future handler that forgets the column gets a NOT NULL violation instead of a random UUID.

Production-sized deployments should consult `docs/runbooks/v1.5.0-migration.md` before applying 020: the `DEFAULT gen_random_uuid()` backfill is fast on apartment-lab but does hold an `ACCESS EXCLUSIVE` lock briefly per table; the runbook covers the two-step "add nullable column, batch backfill, then add UNIQUE + NOT NULL" alternative for multi-million-row tables.

### Changed

- **`docker-compose.yml`** gains the `snapshot-keeper` service (shares the api image, runs `python -m services.snapshot_keeper`, restart `unless-stopped`, healthcheck on heartbeat file freshness). The `api` service environment now forwards `SENTRY_TOKEN_PEPPER` with the standard `:?` hard-fail; without the var, Compose refuses to start the stack.
- **`.env.example`** documents `SENTRY_TOKEN_PEPPER` with a `secrets.token_hex(32)` generator.
- **`api/app.py`** gains a `@before_request` hook that mints `g.source_txn_id` (prefers a valid inbound `X-Request-ID` UUID, else `uuid.uuid4()`) and a boot guard that fails `create_app()` without `SENTRY_TOKEN_PEPPER`.
- **`api/services/rate_limit.py`** preference chain: `token:<id>` > `user:<id>` > `ip:<addr>`.
- **Conftest** `ALL_TABLES` extended with `integration_events`, `snapshot_scans`, `wms_tokens`, `consumer_groups`, `connectors` in FK-safe TRUNCATE order.

### Developer experience

- Added `docker-compose.proxied.yml` for local TLS reverse-proxy testing (#138). Run with `docker compose -f docker-compose.yml -f docker-compose.proxied.yml up -d` to reproduce reverse-proxy deployment behavior locally without remote infrastructure. Requires mkcert.

### Tests

- **Backend: 910 passing (up from 740 at v1.4.5).** +170 new cases across: migration self-tests (020, 021, 022+023, 024), emit_event unit tests, seven per-handler emission integration tests, per-aggregate FIFO + visible_at trigger concurrency, polling contract (12 cases: empty, single event, cursor advance, limit bounds, types filter, warehouse filter, scope 403, visibility gate, mutual exclusion, plain-int cursor, no has_more, direct aggregate_external_id), consumer-group ack (monotonic, out-of-order no-op, equal-cursor idempotent, 404, cross-connector 403), types + schema endpoints, admin token CRUD, admin consumer-groups CRUD, wms_tokens decorator, token cache TTL + revocation window, rate-limit key preference, boot guard, snapshot-keeper subprocess handoff, snapshot endpoint (cursor tamper, 410 expired, 503 unavailable, keyset paging, handoff invariant).
- **Admin: 58 passing, unchanged**. New React pages ride on the existing DataTable / Modal / PageHeader primitives; no new test coverage this release.
- **Mobile: 32 passing, unchanged**. v1.5.0 has no mobile code changes.

### Notes for operators

- **`SENTRY_TOKEN_PEPPER` is required.** Before `docker compose up -d`, generate a pepper with `python -c "import secrets; print(secrets.token_hex(32))"` and set it in `.env`. The api container refuses to boot without it. Rotating the pepper invalidates every issued token; see `docs/runbooks/token-pepper-rotation.md` for the procedure.
- **New `snapshot-keeper` service.** After the v1.4.5 → v1.5.0 upgrade `docker compose up -d` starts four containers (`db`, `redis`, `api`, `celery-worker`, `admin`, plus the new `snapshot-keeper`). The keeper is required for `GET /api/v1/snapshot/inventory`; a downed keeper surfaces as 503 `snapshot_keeper_unavailable` on the first page of a scan.
- **`SENTRY_VALIDATE_EVENT_SCHEMAS` defaults to false in production.** CI and tests run with it on. Flipping it on in production fails-closed on a schema bug, which would block every mobile emit; only enable it transiently during incident investigation.
- **Upgrade procedure:** `git pull && docker compose down && docker compose build && docker compose up -d`. The BUILD_VERSION guard (#73) catches skipped rebuilds. Five migrations (020-024) + migration 025 apply automatically on the next `db/seed.sh` invocation for fresh installs; existing deployments should run them via their usual migration runner in numeric order.
- **No mobile APK ships with v1.5.0.** Existing v1.4.1 / v1.4.3 APKs on Chainway C6000 devices continue to work; v1.5.0 has no mobile code changes beyond the version-string bump.

## [v1.4.5] - 2026-04-21

Reverse-proxy hotfix follow-up. v1.4.4 (#107) wired Werkzeug `ProxyFix` into `api/app.py` behind a `TRUST_PROXY` env var, but `docker-compose.yml` was never updated to pass `TRUST_PROXY` into the `api` service environment. Compose does not auto-forward arbitrary host env vars; variables must be declared in the service's `environment:` block. Operators who set `TRUST_PROXY=true` in `.env` saw no effect because the value stopped at the Compose shell and never reached Flask: `os.getenv("TRUST_PROXY")` returned `None` inside the container, `ProxyFix` stayed off, and the CSRF-403-behind-proxy bug from v1.4.0-v1.4.3 kept firing on nginx / Caddy / Traefik / ALB deployments. Fruxh hit this after installing v1.4.4 fresh. api + Compose + docs change; admin panel and mobile unaffected at the code level.

### Fixed -- Core
- **`TRUST_PROXY` now reaches the api container.** `docker-compose.yml` `services.api.environment` gains `TRUST_PROXY: ${TRUST_PROXY:-false}`, same pattern as `FLASK_ENV`. Default `false` preserves the direct-connect posture from v1.4.0-v1.4.3; operators opt in by setting `TRUST_PROXY=true` in `.env` when Sentry sits behind a TLS-terminating reverse proxy. Without this single line, v1.4.4's `ProxyFix` wiring was cosmetic for every Compose-deployed install: the Flask check `os.getenv("TRUST_PROXY")` returned `None` because Compose never forwarded the var. Baked into the same service-environment block as `JWT_SECRET` / `SENTRY_ENCRYPTION_KEY` / `REDIS_PASSWORD` so it follows the same upgrade procedure (`docker compose up -d` after pulling). (Closes #136, refs #107 -- #107 closes once Fruxh confirms the v1.4.5 build resolves his production repro.)
- **ProxyFix state is now logged at Flask startup.** `api/app.py` emits either `ProxyFix active: trusting X-Forwarded-* headers (TRUST_PROXY=true)` or `ProxyFix inactive: not trusting proxy headers (TRUST_PROXY not set)` via the module logger at WARNING level (matching the `check_build_version` pattern already in that file so the line clears the default gunicorn stderr threshold). Operators can verify the middleware state with `docker compose logs api | grep ProxyFix` without having to exec into the container or inspect app internals. One line per gunicorn worker, so on the default 4-worker pool there are four copies of the log line -- expected, not a duplication bug.
- **`/api/health` now returns `proxy_fix_active`.** The response shape extends from `{"status": "ok", "service": "sentry-wms"}` to `{"status": "ok", "service": "sentry-wms", "proxy_fix_active": <bool>}`. Existing callers that only read `status` continue to work unchanged. A reverse proxy or external monitor can now confirm the wiring end-to-end with a single HTTPS `GET`: a green health response with `"proxy_fix_active": false` behind an nginx deployment is the exact signature of this bug (`TRUST_PROXY` set in `.env`, `ProxyFix` still off because the var never reached the container). This also serves the v1.5+ observability minimum for operator-visible app state.

### Documentation
- **`.env.example` gains a `TRUST_PROXY` entry with the security warning inline.** Placed under a new "Reverse proxy (HTTPS termination)" section separator so it is scannable. The block defaults to `false`, explains when to enable, and carries the header-forgery warning from `docs/deployment.md` so operators cannot miss it even if they copy `.env.example` to `.env` without reading the deployment guide front-to-back. Before v1.4.5, `TRUST_PROXY` did not appear in `.env.example` at all, so a fresh install had no signal that the knob existed.
- **`docs/deployment.md` "Reverse Proxy (HTTPS)" section gains `.env`-location and verification clarifications.** Three footguns called out inline: (1) `TRUST_PROXY` goes in `.env` at the repo root, NOT `api/.env` -- the Compose deployment path reads the project-root `.env`, while `api/.env` is only consulted by a direct `flask run` from inside `api/`; (2) after editing `.env`, use `docker compose up -d` (which recreates the container and re-reads `.env`), NOT `docker compose restart api` (which keeps the container and bounces the process inside it without re-reading `.env`); (3) verification commands -- `docker compose exec api env | grep TRUST_PROXY` for the Compose side, `docker compose logs api | grep ProxyFix` for the Flask side, `curl /api/health` for the external view. Each check is independent so a failure on any one narrows the diagnosis to that layer.

### Thanks
Thanks again to **Fruxh** for catching that the v1.4.4 build did not actually deliver the #107 fix on his install. v1.4.4 shipped the Flask-side wiring and the deployment docs but silently missed the Compose wiring that carries the env var into the container; Fruxh set `TRUST_PROXY=true` in `.env`, ran `docker compose up -d`, saw the same CSRF-403 wall as on v1.4.3, and reported back within a day. The one-line Compose diff plus the operator-visibility work (log line + `/api/health` field) in this release exists because of that follow-up report.

### Tests
- Backend: 740 passing (up from 738 at v1.4.4). `api/tests/test_proxy_fix.py` gains a new `TestHealthEndpointReportsProxyFixState` class with two cases: (1) without `TRUST_PROXY`, `GET /api/health` returns `proxy_fix_active: false`; (2) with `TRUST_PROXY=true`, the same endpoint returns `proxy_fix_active: true`. Both reuse the existing `unproxied_client` / `proxied_client` fixtures, so the contract rides on the same env-var-scoped `create_app()` the other four cases in the file already exercise. The original 4 cases (opt-in invariant, scheme/host/is_secure rewrite, Secure CSRF + auth cookies, change-password behind proxy not blocked by CSRF) are unchanged and still green.
- Admin: 58 passing, unchanged (v1.4.5 has no admin code changes).
- Mobile: 32 passing, unchanged (v1.4.5 has no mobile code changes).

### Notes for operators
- **Upgrade v1.4.4 -> v1.4.5 if you deploy behind a reverse proxy.** `git pull && docker compose down && docker compose build && docker compose up -d`. The BUILD_VERSION guard (#73) catches skipped rebuilds. After the container is up, verify the wiring with `docker compose exec api env | grep TRUST_PROXY` (expect `TRUST_PROXY=true`) and `docker compose logs api | grep ProxyFix` (expect `ProxyFix active: ...`). Or from outside the container, `curl https://sentry.yourcompany.com/api/health` should return `"proxy_fix_active": true`.
- **`docker compose restart api` does NOT re-read `.env`.** This is the most common way to set `TRUST_PROXY=true` correctly in `.env` and still see `ProxyFix` stay off: Compose picks up `.env` changes on container *creation*, not on restart. Always use `docker compose up -d` after editing `.env`.
- **Direct-connect (no proxy) deployments:** no action needed. `TRUST_PROXY` defaults to `false` in both `docker-compose.yml` and `.env.example`, which is the correct posture. Setting `TRUST_PROXY=true` without a trusted proxy in front remains a header-forgery vector.
- **No mobile APK ships with v1.4.5.** Existing v1.4.1 / v1.4.3 APKs on Chainway C6000 devices continue to work; the v1.4.5 tag on GitHub does not attach a new APK. Mobile version strings bumped only to keep the BUILD_VERSION guard consistent across packages.

## [v1.4.4] - 2026-04-21

Reverse-proxy hotfix. Every production deployment that fronts Sentry with a TLS-terminating reverse proxy (nginx, Caddy, Traefik, AWS ALB, etc.) was returning `403 CSRF token missing or invalid` on every `POST` / `PUT` / `PATCH` / `DELETE`. Fixed by wrapping `app.wsgi_app` in Werkzeug `ProxyFix` behind a `TRUST_PROXY` env var, so Flask's `request.scheme` / `request.host` / `request.is_secure` reflect the browser's view of the request instead of the internal `127.0.0.1` hop. Found and root-caused by Fruxh from his production install. api-only code change; admin panel and mobile unaffected.

### Fixed -- Core
- **Flask now honours `X-Forwarded-*` headers from a trusted reverse proxy when `TRUST_PROXY=true`.** Without this, Sentry behind nginx / Caddy / Traefik / ALB issued cookies scoped to the internal `127.0.0.1:<port>` host; the browser on the public hostname treated them as cross-origin garbage and never resubmitted, so the CSRF double-submit cookie was absent on every mutation and the middleware 403'd. Root cause: `app.wsgi_app` was never wrapped in `werkzeug.middleware.proxy_fix.ProxyFix`. `api/services/cookie_auth.py` already checked `X-Forwarded-Proto` defensively when setting the `Secure` flag, but Flask's `request.host` / `request.scheme` / `request.is_secure` stayed stuck on the internal view. Fixed by wiring `ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=0)` in `api/app.py`, gated behind `TRUST_PROXY=true / 1 / yes`. Opt-in only because honouring `X-Forwarded-*` when the app is NOT behind a trusted proxy lets any client forge its own scheme / hostname / client IP (the classic ProxyFix footgun). The `_cookie_secure()` header fallback stays as belt-and-suspenders with a comment noting ProxyFix is now the primary mechanism. (Closes #107, refs Fruxh's #98 -- #98 closes once Fruxh confirms the v1.4.4 build resolves his production repro.)

### Documentation
- **`docs/deployment.md` "Reverse Proxy (HTTPS)" section expanded with `TRUST_PROXY` guidance, annotated snippets, and a multi-hop note.** The prior doc had a single minimum nginx config block and no mention of `TRUST_PROXY`, which meant every v1.4.0-v1.4.3 operator who deployed behind a reverse proxy hit the #107 CSRF-403 wall without a doc trail to follow. New content covers: (1) when and why to set `TRUST_PROXY=true`, with the explicit security warning that trusting `X-Forwarded-*` without a proxy in front of the app is a header-forgery vector; (2) an annotated nginx config explaining what ProxyFix reads each `proxy_set_header` line for; (3) Caddy `reverse_proxy` and Traefik v2+ dynamic-config snippets (both set `X-Forwarded-*` automatically); (4) a one-line note that AWS ALB / GCP HTTPS LB / Azure Application Gateway / Cloudflare Tunnels / Fly.io / Render all work the same way under `TRUST_PROXY`; (5) a multi-hop section explaining that CDN -> nginx -> Sentry needs `x_for=2, x_proto=2, x_host=2` with links to the upstream Werkzeug docs for the full theory.

### Build / CI
- **`python-dotenv` bumped `1.0.1` -> `1.2.2` to clear `GHSA-mf9w-mj56-hr94`.** OSV published the advisory between the 2026-04-21 08:08 UTC scheduled `main` audit (green) and the 17:17 UTC initial v1.4.4 branch push (red). Same commit SHA, different audit outcome -- advisory data refreshed, not a code regression. `python-dotenv` is only used for `load_dotenv()` at app import; the `1.0.x` -> `1.2.x` line is drop-in compatible, no code changes needed. Riding along on v1.4.4 rather than deferring via `--ignore-vuln` because the one-line bump strictly improves the release; an ignore line would kick the can and add noise to `audit.yml`. (Closes #106)

### Thanks
Thanks to **Fruxh** for filing #98 from his production v1.4.3 install and following through to the root cause with a detailed reproduction: nginx terminating TLS at `https://sentry.fruxh.example`, forwarding to Flask on `http://127.0.0.1:8080` with the standard `X-Forwarded-*` headers, browser refusing to resubmit the cookie that Flask issued against the internal host. The diagnostic work turned what would have been a days-long triage into a one-commit fix.

### Tests
- Backend: 738 passing (up from 734 at v1.4.3). New file `api/tests/test_proxy_fix.py` (4 cases): (1) opt-in invariant -- without `TRUST_PROXY`, forged `X-Forwarded-*` headers cannot spoof `scheme` / `host` / `is_secure`; (2) `TRUST_PROXY=true` rewrites `request.scheme` to `https`, `request.host` to the `X-Forwarded-Host` value, and `request.is_secure` to `True`; (3) login behind proxy headers returns CSRF + auth cookies that both carry `Secure`, with CSRF still `SameSite=Strict` and auth still `HttpOnly`; (4) change-password behind proxy headers does NOT 403 on the CSRF gate when the client echoes the cookie value in `X-CSRF-Token` (Fruxh's exact repro path, asserting the fix).
- Admin: 58 passing, unchanged (v1.4.4 has no admin code changes).
- Mobile: 32 passing, unchanged (v1.4.4 has no mobile code changes).

### Notes for operators
- **New required env var for reverse-proxy deployments.** If you run Sentry behind nginx / Caddy / Traefik / ALB / any other TLS-terminating proxy, set `TRUST_PROXY=true` in the API container's environment after pulling v1.4.4. Without it, the `ProxyFix` middleware stays off and the CSRF-403 bug from v1.4.0-v1.4.3 continues. See `docs/deployment.md` "Reverse Proxy (HTTPS)" for the full writeup.
- **Direct-connect (no proxy) deployments:** do NOT set `TRUST_PROXY`. The default-off posture is correct for any install where Sentry is reachable directly from the browser / mobile client. Setting `TRUST_PROXY=true` without a trusted proxy in front allows clients to forge origin / scheme / client IP by sending `X-Forwarded-*` themselves.
- **Upgrade procedure unchanged** from v1.4.2: `git pull && docker compose down && docker compose build && docker compose up -d` for the API + admin host. The BUILD_VERSION guard (#73) continues to catch skipped rebuilds.
- **No mobile APK ships with v1.4.4.** Existing v1.4.1 / v1.4.3 APKs on Chainway C6000 devices continue to work; the v1.4.4 tag on GitHub does not attach a new APK. Mobile version strings bumped only to keep the BUILD_VERSION guard consistent.

## [v1.4.3] - 2026-04-20

Mobile patch release. Two fixes from the v1.4.3 mobile bug bash, plus a follow-up for the regression surfaced during Chainway C6000 verification of the second fix. Zero backend or admin code changes; tests and docs only elsewhere. Closes the keyboard-fallback half of Fruxh's #70 report; the camera-scanner half remains tracked under #70 for v2.x.

### Fixed -- Mobile
- **Put-away "done" screen no longer overlays the success checkmark on the title.** The done phase was rendered inside `doneStyles.section` (`flex: 1, justifyContent: 'center', alignItems: 'center'`) but the container also holds the session-history list, which grows with each put-away. Once history exceeded the viewport, content overflowed and `justifyContent: 'center'` pushed the large check glyph visually into the title below it. Swapped to a ScrollView with natural top-down flow, matching the CountScreen done-phase pattern. Visual regression only; no functional impact. (Closes #103)
- **Scan input fields now allow keyboard fallback for manual entry and copy/paste.** `mobile/src/components/ScanInput.js` had `showSoftInputOnFocus={false}` and `contextMenuHidden`, so tapping a scan field on the Chainway C6000 did nothing (no soft keyboard) and long-press did not expose the context menu (no copy/paste). Removed both flags. Broadcast-intent scans still route through `ScanSettingsContext`'s `activeScanHandlerRef` and bypass the TextInput entirely; keyboard-mode scans still land in `onChangeText` the same way manual typing does. Every scan screen inherits this fix via the shared component. (Closes #104, refs #70)
- **Scan input soft keyboard now only opens on user tap, not on auto-refocus.** The #104 fix removed the unconditional `showSoftInputOnFocus={false}`, but the 1-second refocus loop that keeps the field ready for hardware scans then re-popped the soft keyboard on every tick on the C6000. Track a `softInput` state that is false by default and flipped to true only on `onPressIn`; force a blur/refocus cycle on tap so the updated prop applies and the keyboard actually opens. Reset to false on blur and after submit so the auto-refocus loop, mount autofocus, and post-submit refocus all stay silent. Net behavior: tap opens the keyboard for manual entry, every other focus path leaves it hidden. (Closes #105)

### Tests
- Mobile: 32 passing (up from 24 at v1.4.2). New file `mobile/src/components/__tests__/ScanInput.test.js` locks the exact props and handlers that encode the tap-to-open, hidden-during-hardware-scan contract and the absence of `contextMenuHidden`. Source-level regression gate because the mobile vitest harness has no RN runtime (see `mobile/src/auth/__tests__/forcedChangePersistence.test.js` for the pattern note). User-visible behaviour verified on a Chainway C6000 before release.
- Backend: 734 passing, unchanged (v1.4.3 has no backend code changes).
- Admin: 58 passing, unchanged (v1.4.3 has no admin code changes).

### Thanks
Thanks to **Fruxh** for the original #70 report that asked for both an in-app camera scanner and a keyboard fallback. v1.4.3 ships the keyboard half; the camera-scanner half stays open under #70 for v2.x scope.

### Notes for operators
- **Mobile APK rebuild.** v1.4.3 has real mobile code changes (unlike v1.4.2, which did not ship a new APK). The new `sentry-wms-v1.4.3.apk` is attached to the GitHub release and installs over v1.4.1 / v1.4.2 on Chainway C6000 devices without a data wipe.
- **No backend or admin redeploy needed for mobile operators.** `api/version.py` and all package manifests still bump to 1.4.3 so the BUILD_VERSION guard (#73) stays consistent, but the API and admin images have no source changes in this release. Pulling and rebuilding is safe but not required if you only operate the mobile fleet.
- **Upgrade procedure unchanged** from v1.4.2: `git pull && docker compose down && docker compose build && docker compose up -d` for the API + admin host; `adb install sentry-wms-v1.4.3.apk` on each scanner.

## [v1.4.2] - 2026-04-20

Admin panel patch release. Headline is an operator safeguard against upgrades-without-rebuild; everything else is either a real bug reported externally by Fruxh from a production deployment or surfaced during an internal admin bug bash on 2026-04-19 plus the pre-merge gate on 2026-04-20. Zero mobile code changes; a v1.4.3 will follow for mobile-side reports.

### Fixed -- Deployment / Operations
- **CRITICAL: Upgrade path fails with `ModuleNotFoundError: flask_limiter` when the Docker image is not rebuilt.** v1.4.0 added Flask-Limiter (V-041); users upgrading from v1.3.x with cached Docker images were running v1.3-era containers against v1.4 code, and the worker crashed on `from flask_limiter import Limiter`. The API now bakes the source `__version__` into the image at build time and checks it against the code version at startup. If they drift, the container logs a clear "run docker compose build" message and exits 2 instead of crashing a worker with a dependency error. `docs/deployment.md` gains an "Upgrading" section spelling out the correct `git pull && docker compose build && docker compose up -d` procedure. (Closes #73)

### Fixed -- Admin Panel: Form Validation (V-017 cluster)
Seven admin create / edit forms returned `validation_error` on submit because V-017's `extra="forbid"` rejected fields the frontend was sending but the pydantic schemas did not declare. Each form fixed independently; a consolidated integration test file (`api/tests/test_admin_payload_alignment.py`) now locks the payload shape for every fixed endpoint so a future drift surfaces in CI before a user.
- **Bin create payload alignment** -- `barcode` renamed to `bin_barcode`, stale `rack` / `shelf` / `position` / `is_active` fields dropped from the POST body, `pick_sequence` defaults to 0 when empty. (Closes #74)
- **Zone create payload alignment** -- `is_active` stripped (belongs on `UpdateZoneRequest`, not `CreateZoneRequest`); `ZONE_TYPES` dropdown list corrected to match the backend validator (dropped `QUALITY` / `DAMAGE`, added `PICKING`). (Closes #75)
- **PreferredBin create payload alignment** -- bin dropdown bound to `b.bin_id` instead of a non-existent `b.id`, so the submitted `bin_id` is a real integer instead of `null`. (Closes #76)
- **InventoryAdjustment create payload alignment** -- reason is required client-side now; the backend schema declares `reason: str = Field(..., min_length=1)` for audit traceability. (Closes #77)
- **Inter-Warehouse Transfer create payload alignment** -- four field names renamed from `source_*` / `destination_*` to `from_*` / `to_*` to match `InterWarehouseTransferRequest`. (Closes #78)
- **Manual PO create payload alignment** -- `vendor_address` folded into `notes` (the PO table has no vendor_address column), PO lines use `quantity_ordered` instead of `quantity_expected`. (Closes #79)
- **Manual SO create payload alignment** -- `warehouse_id` falls back to the current warehouse context on first open (prior null-on-first-attempt bug), SO lines use `quantity_ordered`. Fallback also applied to the manual PO path. (Closes #80)
- **Zone edit payload alignment and EDIT-endpoint audit** -- zone edit was tripping on a `zone.id` vs `zone.zone_id` URL-construction bug; fixed, and every other admin PUT endpoint audited for the same pattern. (Closes #81)
- **Bin create Zone dropdown commits the selected zone_id** -- pre-merge gate finding; the `.map()` that feeds the zone `<select>` inside the Bin form bound key / value to `z.id` instead of `z.zone_id`, so no selected zone ever reached the POST. Same `.id` vs `.<entity>_id` pattern the #81 audit flagged; single remaining instance now closed. Regression test asserts the POST body carries a numeric `zone_id`. (Closes #99)

### Fixed -- Admin Panel: Data Management
- **Inventory search bar wired to fire on Enter.** Backend GET `/api/admin/inventory` gains the `q` query parameter the frontend was already sending (ILIKE match on SKU or item name, whitespace-only input ignored). Frontend input binds to a separate `searchInput` buffer and only commits on Enter or blur. (Closes #82)
- **Cycle Count bin selection state isolation.** Checkbox `key` / `checked` / `toggle` bound to `bin.bin_id` instead of the non-existent `bin.id`; picking one bin no longer highlighted every bin, and picking a second no longer unselected every bin. (Closes #83)
- **Bin detail view opens on row click + delete with confirmation.** Row click was building `/api/admin/bins/${bin.id}` and 404ing because the list endpoint returns `bin_id`; fixed. New `DELETE /api/admin/bins/{id}` endpoint with a 409 guard when inventory-on-hand or preferred-bin references remain. (Closes #85)
- **Zone edit modal includes delete button with confirmation.** New `DELETE /api/admin/zones/{id}`; 409 with `"Zone cannot be deleted because N bin(s) are assigned to it. Reassign or delete the bins first."` when any bin still references the zone. (Closes #86)
- **Purchase Orders list rows gain an edit affordance.** PO edit modal is bound to the header fields `UpdatePurchaseOrderRequest` accepts; lines are read-only after PO create to preserve the procurement record. (Closes #87)
- **Close / Reopen Purchase Order from edit modal.** PO records are permanent procurement history; delete is replaced with reversible state transitions. `POST /purchase-orders/{id}/close` tightened to 409 on already-CLOSED; new `POST /purchase-orders/{id}/reopen` reverses a close. Edit modal footer swaps button text based on current state. (Closes #88)
- **Sales Orders admin list page** (new `admin/src/pages/SalesOrders.jsx`). SOs previously only appeared inside Picking / Packing / Shipping workflow views, each filtered to a single status; now there is a dedicated admin list with a status filter, detail modal, and edit modal (header fields while status is OPEN, read-only afterwards). (Closes #89)
- **Cancel Sales Order from edit modal.** One-way terminal transition; Cancel Order button visible only on OPEN orders. Preserves existing inventory-release behaviour for ALLOCATED / PICKING cancellations via the existing endpoint. (Closes #90)
- **Import CSV templates include all required fields.** PO / SO templates now carry `warehouse_id`; Items template gains `category` and `weight`; Bins template gains `bin_barcode`, `warehouse_id`, `putaway_sequence`. Alignment tests lock template headers against `api/schemas/csv_import.py`. (Closes #91)
- **Create PO / SO modals reset on Cancel.** Click Cancel (or the X corner) then reopen; the form starts empty instead of carrying stale fields from the previous attempt. (Closes #92)
- **PO / SO manual entry accepts SKU instead of Item ID.** Operators memorize SKUs, not autoincrement integers. Native `<datalist>` autocomplete sources SKUs from `/api/admin/items`; unknown SKU surfaces as `"Unknown SKU: <value>"` before the POST. (Closes #93)
- **Audit Log column headers trigger sort.** Whitelisted `sort_by` (`created_at`, `action_type`, `user_id`, `entity_type`) with `sort_direction=asc|desc` accepted on the backend; injection-safe. Frontend resets to page 1 on each new sort. (Closes #95)
- **DataTable CSV export serializes status columns correctly.** Any column whose `render` returned a React element (e.g. `<StatusTag>`) was coercing to the literal string `[object Object]` in exported CSVs. DataTable now prefers an explicit `csvValue(row)`, falls back to a primitive render result, and otherwise uses the raw `row[col.key]`. (Closes #84)
- **Admin list pages use consistent pencil / trash row actions.** Every admin list page (Items, Warehouses, Zones, Users, Bins, Purchase Orders, Sales Orders) now surfaces edit via a `&#9998;` pencil icon. Items, Warehouses, Zones, Users, and Bins also expose delete via a `&#128465;` trash icon (Users preserves the self-delete guard). PO / SO are pencil-only; Close and Cancel remain state transitions in the edit modal per the #88 / #90 design. Duplicate Delete buttons inside edit modal footers were removed for single-source-of-truth. (Closes #102)

### Fixed -- Admin Panel: UX
- **Settings page stops crashing (P0 regression caught at the pre-merge gate).** `useDirtyFormGuard` initially shipped with `useBlocker` from react-router, which requires a data-router setup the admin panel does not run under; the hook threw "useBlocker must be used within a data router" on every Settings mount and the ErrorBoundary caught it. The hook now uses `beforeunload` only. (Closes #94, #100)
- **Settings page warns on browser close / refresh when it has unsaved changes.** This is the actual behaviour that ships in v1.4.2 -- the `beforeunload` listener fires on close, reload, or URL-bar navigation. Intra-SPA sidebar-click guarding was attempted via `useBlocker`, found to require a router migration out of v1.4.2 scope, and is deferred to v1.5 for proper design (#101).
- **change-password redirects to /login with a success banner.** Fruxh reported first-time setup appeared to fail with "Your session is out of sync." The password change had already succeeded server-side, but the frontend refreshed `/auth/me` with the now-invalidated token, hit 401, left `must_change_password=true` in context, and the router guard bounced the operator back to `/change-password`. The frontend now reuses the existing `logout()` helper, writes a flash message to `sessionStorage`, and navigates to `/login`, which renders the banner and lets the operator sign in with the new password cleanly. (Closes #98)

### Build / CI
- **Lockfile version drift check** (new `.github/workflows/lockfile-check.yml`). Fails CI whenever `admin/` or `mobile/` `package.json` and `package-lock.json` disagree on the top-level project version. Prevents recurrence of the v1.4.1 "lockfiles stuck at 1.4.0" bug this release fixes. (Closes #96)
- **admin/package-lock.json and mobile/package-lock.json regenerated** to match `package.json` v1.4.1. They had been stuck at `1.4.0` since the v1.4.0 release because 818617a bumped the manifests but not the lockfiles.
- **api/BUILD_VERSION gitignored** so the file the #73 startup guard reads from a prod image cannot accidentally leak into a dev host via the bind mount (observed once during v1.4.2 work; would have tripped the guard on every dev restart after a version bump).

### Security
Three bugs reported directly by external user Fruxh from a production deployment:
- **#72** -- the flask_limiter upgrade crash; closed by the #73 headline fix and the new "Upgrading" docs section.
- **#71** -- the full V-017 `validation_error` cluster across Bin / Zone / PreferredBin / Transfer create forms (Fruxh hit four of the seven; the internal bug bash surfaced the other three); closed alongside the #74-#81 cluster plus #85 (bin detail). Also the DataTable CSV export [object Object] bug (#84) which Fruxh reported separately on PO exports.
- **#98** -- the first-time-setup "Your session is out of sync" false failure; closed by the `/login` redirect + flash banner.

All three close on v1.4.2 merge via their respective commits' `Closes` keyword.

### Thanks
Thanks to **Fruxh** for filing three external bug reports (#71, #72, #98) from a production v1.4.1 deployment, with clear reproductions and a screenshot. Those reports drove v1.4.2's priorities and shaped the Phase 1 / Phase 2 split.

### Tests
- Backend: 734 passing (up from 690 at v1.4.1). New coverage: V-017 payload alignment for every fixed form, bin delete, zone delete, PO close / reopen state machine, SO double-cancel guard, audit log sort whitelist, inventory `q` parameter, #73 build-version check.
- Admin: 58 passing (up from 42). New test files: `bins-zone-dropdown.test.jsx`, `DataTable.csv-export.test.jsx`, `imports.test.jsx`, `settings-router-mount.test.jsx`, `useDirtyFormGuard.test.jsx` (rewritten against the non-useBlocker hook), `forced-change-flow.test.jsx` gains a post-change redirect suite.
- Mobile: 24 passing, unchanged (v1.4.2 has no mobile code changes).
- New CI workflow: Lockfile Version Check runs alongside Tests and Dependency Audit on every push and PR.

### Notes for operators
- **Upgrading from v1.3 or v1.4.0 / v1.4.1:** run `git pull && docker compose down && docker compose build && docker compose up -d`. If you skip the build step, the API exits with code 2 on startup and logs the correct remediation command (the #73 guard).
- **Fresh installs:** unchanged from v1.4.1. The forced-password-change flow still applies. Post-change now redirects to `/login` with a success banner instead of trying to refresh the session with an already-invalidated token.
- **No mobile APK ships with v1.4.2.** Existing v1.4.1 APKs on Chainway C6000 devices continue to work; the v1.4.2 tag on GitHub does not attach a new APK. A v1.4.3 is planned for mobile-side reports.
- **Settings unsaved-changes guard** currently only covers browser-level exits (close tab, reload). Clicking a sidebar link with unsaved draft settings discards them silently; proper design for an intra-SPA guard is captured in #101 and will ship in v1.5.

## [v1.4.1] - 2026-04-18

Patch release bundling two bug fixes deferred from v1.4.0.

### Added
- **Forced password change on first login.** Fresh installs seed admin as `admin/admin` with a `must_change_password` flag set. Auth middleware blocks every endpoint except `/api/auth/me`, `/api/auth/change-password`, and `/api/auth/logout` until the admin changes the password. Eliminates the "docker compose up, then grep logs for the random password" onboarding paper-cut carried from v1.0 through v1.4.0. (#69)
- **Migration 019** adds `must_change_password BOOLEAN NOT NULL DEFAULT FALSE` on the `users` table. Existing users get the default FALSE on ALTER, so pre-existing deployments are never force-flagged by this migration.
- **Distinct audit_log action** `forced_password_change_completed` for the first-time flow, separate from voluntary `password_change` on subsequent rotations. Greppable apart from normal rotations.
- **Admin panel change-password page** (new in `admin/src/pages/ChangePassword.jsx`). Router guard redirects to `/change-password` whenever `must_change_password` is true; brand-red banner, hidden Cancel button, and a sidebar-less layout enforce the forced-mode UI. The page also serves voluntary changes (Cancel visible, no banner, full shell).
- **Mobile `ChangePasswordScreen`** with the matching banner, Android hardware back-handler and iOS swipe-back both disabled, and a "Log out" escape. `AppNavigator` registers the screen only in the forced-mode branch so the rest of the app is literally unreachable while the flag is set.

### Fixed
- **Mobile HomeScreen and LoginScreen version display.** Both hardcoded `v1.2.0` and were never bumped during v1.3.0 or v1.4.0. Now read `v1.4.1`. Issue #67 tracks the v1.5 refactor that eliminates this class of bug permanently via build-time injection. (#68)
- **Forced-mode navigator stuck spinner.** React Navigation native-stack was preserving the `ChangePassword` route across the `must_change_password` flip because the screen was registered in both the forced and non-forced branches. The non-forced branch no longer registers it, so the route ceases to exist when the flag clears and native-stack falls through to Home. (#69)

### Security
- **`admin` rejected as a new password** (case-insensitive, whitespace-stripped). `validate_password` refuses `admin`, `ADMIN`, `Admin`, `aDmIn`, ` admin `, `\tadmin\n`, etc. Prevents "changing" the seeded default back to itself.
- **Middleware exception list is exactly three endpoints.** Anything else returns 403 `password_change_required` while the flag is set. Matched by Flask endpoint (blueprint.view_fn), not URL path, so query strings and method variants cannot slip past.
- **Force-kill-and-reopen bypass closed on mobile.** `must_change_password` lives inside the persisted `user_data` dict in SecureStore, so a relaunch rehydrates the forced state and the navigator re-renders `ChangePasswordScreen`. `completePasswordChange` write-throughs the cleared flag to SecureStore so a resume after a successful change lands on Home.

### Tests
- Backend: 25 new tests (`api/tests/test_forced_password_change.py`), 690 total passing.
- Admin: 10 new tests (`admin/src/test/forced-change-flow.test.jsx`), 42 total passing.
- Mobile: 11 new tests (`mobile/src/auth/__tests__/forcedChangePersistence.test.js`), 24 total passing. React Navigation and hardware-back behaviour verified manually on a Chainway C6000 since mobile vitest has no RN runtime.

### Notes for operators
- **Fresh installs:** log in with `admin/admin`; you will be prompted to set a new password before any other route becomes accessible.
- **Existing installs:** no change in behaviour. Migration 019 sets the column to FALSE for all existing rows; your admin user flows through login exactly as before.
- **Dev / demo workflow:** `docker compose down -v && docker compose up -d` resets to `admin/admin` plus the forced-change flow.
- **`ADMIN_PASSWORD` env override** still bypasses the forced flow for CI, automation, and deterministic dev environments. When set, the provided password ships with `must_change_password = FALSE` and the legacy banner.

## [v1.4.0] - 2026-04-18

Pure security and hardening release. No new features. Closes the remaining High-severity items from the v1.3.0 audit backlog, every finding from a fresh audit of the v1.4 work (V-100 through V-111), and the most impactful Medium / Low items.

### Security -- v1.3 backlog (Priority 1)
- **V-045** -- Admin JWT moved out of `localStorage` into an HttpOnly cookie. CSRF double-submit pattern (`X-CSRF-Token` header cross-checked against a non-HttpOnly companion cookie) protects all mutating methods on admin endpoints. Mobile continues using bearer tokens; the cookie + CSRF path and the bearer path resolve to the same server-side auth middleware.
- **V-047** -- Mobile JWT migrated from plaintext AsyncStorage to the Android Keystore via `expo-secure-store`. One-shot migration on app launch copies any existing AsyncStorage token into SecureStore and then wipes the AsyncStorage copy; `clearAllAuth` wipes both backends.
- **V-048** -- Cleartext HTTP stays allowed in all build profiles (warehouse LANs require it). Risk is accepted and documented in `SECURITY_BACKLOG.md` with revisit condition: the hosted / cloud deployment option.
- **V-050** -- Strict Content-Security-Policy set by the API and mirrored by nginx on the admin container. `default-src 'self'`, `script-src 'self'` with per-build SRI hashes, `style-src 'self' 'unsafe-inline'`, `img-src 'self' data:`, `font-src 'self'`, `connect-src 'self'`.

### Security -- v1.3 backlog (Priority 2)
- **V-006** -- Fernet cache held in module state. Python cannot reliably zero memory; threat model assumes local-read adversary. Accepted, documented in `SECURITY_BACKLOG.md`.
- **V-007** -- `scrub_secrets()` applied to Celery task error strings so decrypted credentials cannot leak through traceback payloads.
- **V-008** -- Closed via the v1.3 V-001 hard `RuntimeError` on missing `SENTRY_ENCRYPTION_KEY`. No new code change; verification only.
- **V-010** -- Connector registry rejects duplicate registration of the same connector name with a `ValueError` at import time rather than silently overwriting.
- **V-012** -- Stale `running` sync_state recovery. New `running_since` and `run_id` columns (migrations 017 and 018). A fresh worker whose state is older than the 1-hour takeover threshold claims the row with a new `run_id`; any late write from the stale worker is dropped on UUID mismatch.
- **V-016** -- `Content-Type: application/json` now required on POST / PUT / PATCH; non-JSON requests return `415 Unsupported Media Type`.
- **V-017** -- Pydantic schemas use `model_config = ConfigDict(extra="forbid")` so unknown request body fields return `validation_error` instead of silently dropping.
- **V-020** -- ErrorBoundary console output goes through a scrub helper that redacts bearer tokens and cookie values before log emission.
- **V-021** -- Admin UI surfaces operator-friendly messages on common failures (403, 404, 409, 429, validation) instead of raw `response.error`.
- **V-024** -- `login_attempts` table now has a periodic cleanup (`delete_stale_login_attempts()`) and a 254-char cap on the username column to bound storage.
- **V-031** -- Last-admin delete race closed by SELECT FOR UPDATE on the count query inside the delete transaction. Two concurrent deletes can no longer both pass the "one admin remains" check.
- **V-033** -- URL-path `warehouse_id` is now the authoritative value; middleware rejects any request whose body or query-string `warehouse_id` disagrees with the path.
- **V-040** -- API and admin containers default to `127.0.0.1` bind (parametrized via `API_BIND_HOST` and `ADMIN_BIND_HOST`). Production deployments behind a reverse proxy are unaffected. LAN-dev workflows set both to `0.0.0.0` in a local `.env`.
- **V-041** -- Flask-Limiter enabled globally (300 / minute per client) and tightened per-route on auth, sync-reset, and connector test-connection. Backed by the existing `REDIS_PASSWORD`-authenticated Redis broker.
- **V-042** -- `pip-audit` and `npm audit` gate every push. `.github/workflows/dependency-audit.yml` runs both on the hardened prod deps and fails the run on any non-ignored advisory.
- **V-046** -- Subresource Integrity hashes generated at admin build time and written into the nginx-served `index.html` for the Vite bundle `<script>` and `<style>` tags.
- **V-051** -- HSTS header set on the API when the request is served over HTTPS; `Strict-Transport-Security: max-age=63072000; includeSubDomains`. Nginx mirror added in V-111 below.

### Security -- v1.4 audit findings (V-100 through V-111)
- **V-100** -- Logout endpoint gated by CSRF. Previously the cookie-only logout path could be triggered cross-origin.
- **V-101** -- `POST /api/admin/connectors/<name>/sync-reset` writes an `audit_log` row and is rate-limited; it was the only admin-mutation without either.
- **V-102** -- Sync state write race. The `run_id` UUID from V-012 plus a `WHERE run_id = :expected` clause prevents a stale worker from clobbering a fresh run's progress even after the takeover threshold.
- **V-103** -- Coverage extension of V-033 to the remaining admin endpoints that still read `warehouse_id` from the body.
- **V-104** -- Mobile SecureStore migration hardening. The migration step always clears AsyncStorage (even when the migration source is empty) so an abandoned pre-1.4 token cannot resurface. `clearAllAuth` wipes both backends on logout.
- **V-107** -- Rate limiter docstring was inaccurate about storage URI precedence; corrected. Added `rediss://` (TLS) URI support for deployments with a TLS-fronted Redis.
- **V-108** -- DNS rebinding pin on connector outbound requests. The SSRF guard resolves the hostname once, validates the address against the blocklist, then connects to the pinned IP while preserving the original `Host` header. A rebind between validate and connect no longer bypasses the guard.
- **V-110** -- Self-hosted Instrument Sans and JetBrains Mono under `admin/public/fonts/`, SIL Open Font License. Removes the last third-party origin; lets `font-src 'self'` stay strict.
- **V-111** -- Nginx CSP and HSTS headers mirror the API's values so admin responses that are served straight out of nginx carry the same policy.

### Fixes -- collateral
- **#56** -- `test_compose_admin_listens_on_8080` updated for the V-040 loopback binding; the assertion now matches either `127.0.0.1:8080` or `0.0.0.0:8080` depending on `ADMIN_BIND_HOST`.
- **#57** -- Mobile npm audit scoped to production dependencies. `tar` pinned to clear two GHSA path-traversal advisories in the dev toolchain.
- **#58** -- Bumped `flask`, `flask-cors`, `pyjwt`, and `requests` to clear pip-audit advisories.
- **#62** -- Deferred-advisory ignore pattern for pip-audit and npm audit so the job does not page on findings tracked as known-accepted in `SECURITY_BACKLOG.md`.
- **#63** -- Admin Users edit form no longer sends `username` in the PUT body. The `UserUpdate` schema rejects it with `extra="forbid"` per V-017, which the form was tripping.
- **#64** -- `API_BIND_HOST` and `ADMIN_BIND_HOST` parametrized so LAN-dev can override the V-040 loopback binding without forking the compose file.
- **#65** -- `/api/admin/inter-warehouse-transfers` removed a `bt.notes` SELECT + response field that referenced a column the `bin_transfers` schema never had. Endpoint returned 500 on every call; now returns 200.

### Accepted risks
- **V-048** -- Cleartext HTTP in all build profiles. Profile-gating was tried in v1.1 and reverted in v1.1.1 because warehouse LAN deployments require HTTP. Revisit when the hosted / cloud deployment option ships.
- **V-006** -- Fernet cache in module state. Python cannot reliably zero memory; threat model assumes a local-read adversary already has process memory access.

### Deferred to v1.5
Open issues with `v1.5` labels: **#52** (V-105 broaden `scrub_secrets` to non-URL credential fragments), **#53** (V-106 scrub `ConnectionResult.message` content), **#54** (V-109 CSP `report-to` endpoint), **#55** (V-113 drop carriage return from `ConnectionResult` allowlist), **#59** (bump `cryptography` 44.0.3 -> 46.0.x), **#60** (bump `pytest` 8.3.4 -> 9.0.3), **#61** (bump `eas-cli` to 0.52.0).

### Tests
- 647 backend tests passing (up from 570), 54 skipped inside the api container for infrastructure-config assertions.
- 32 admin frontend tests passing.
- 8 mobile tests passing.
- All CI workflows green on tag (`Tests`, `Dependency Audit`, `Deploy Docs`).

### Notes for operators
- Admin panel now authenticates via HttpOnly cookie + CSRF. On upgrade, clear `localStorage` and cookies for the admin origin and log in fresh.
- Mobile JWT migrated to SecureStore on first launch. Users may need to log in once after upgrade.
- API and admin containers default to `127.0.0.1` bind. Production behind a reverse proxy is unaffected. LAN-dev with a scanner on the same network must set `API_BIND_HOST=0.0.0.0` and `ADMIN_BIND_HOST=0.0.0.0` in a local `.env`.
- Migrations `017_sync_state_running_since.sql` and `018_sync_state_run_id.sql` must be applied before running v1.4.0 against an existing v1.3 database.

## [v1.3.0] - 2026-04-16

### Added -- connector framework (Phases 1-5)
- Connector interface contract and registry with auto-discovery (`api/connectors/`)
- Celery + Redis background job runner with JSON serialization
- Encrypted credential vault (Fernet) with per-connector + per-warehouse scoping
- Sync state tracking with consecutive-error threshold and admin health dashboard
- Rate limiter, circuit breaker, and `make_request` helper inherited by every connector

### Security (Phase 6 -- audit findings triaged and fixed in Phase 7)
- **V-001** -- Removed hardcoded `SENTRY_ENCRYPTION_KEY` default from `docker-compose.yml`. Key is now required via strict `${... :?}` form. The auto-generation + logging path in `credential_vault.py` is gone; missing key is a `RuntimeError`. See `SECURITY.md` `SA-2026-001` for remediation if your deployment used the previous default.
- **V-002** -- Documented historical `JWT_SECRET` defaults that remain in git history (`dev-secret-change-in-production`, `dev-jwt-secret-do-not-use-in-production-b7e2f`). Current compose uses the strict `${... :?}` form. See `SA-2026-002` for rotation steps.
- **V-003** -- Admin panel rebuilt as a production nginx multi-stage image. Vite dev-server no longer runs in production; dev-mode hot reload is available via `docker-compose.dev.yml`. Admin container runs as `USER nginx`; source tree bind-mount removed from default compose.
- **V-004** -- Redis broker now requires `--requirepass ${REDIS_PASSWORD}`. Celery broker and result backend URLs use the authenticated form. Healthcheck authenticates with `redis-cli -a`.
- **V-005** -- No credential-vault code path logs key material. Any missing `SENTRY_ENCRYPTION_KEY` raises `RuntimeError` rather than silently generating and printing a replacement.
- **V-009** -- SSRF allowlist on every connector outbound request. The guard rejects non-http(s) schemes, internal docker service hostnames, and any URL that resolves to loopback / private / link-local / reserved / multicast / unspecified addresses (IPv4 or IPv6). Applies uniformly via `BaseConnector.make_request`.
- **V-014** -- `ConnectionResult.message` is capped at 500 characters and stripped of non-printable bytes so connectors cannot smuggle response bodies or control sequences back through the admin UI.
- **V-015** -- CSV import (`/api/admin/import/<type>`) now runs every row through a per-entity pydantic schema. Text-field validators reject leading characters that a spreadsheet treats as a formula prefix (`=`, `+`, `-`, `@`, tab, CR). Numeric coercion is handled by pydantic; a non-numeric value skips its row instead of crashing the whole import.
- **V-023** -- Login lockout is now IP-scoped. An attacker at one IP can no longer DoS a known username by exhausting its per-username counter: the real user at a different IP keeps working.
- **V-025** -- `audit_log` is append-only. `BEFORE UPDATE` / `BEFORE DELETE` triggers reject DML, and every row carries a SHA-256 chain hash. The operational helper `verify_audit_log_chain()` returns the first broken `log_id` or NULL when intact. Cancel-receiving audit rows now derive warehouse_id from the receipt rows themselves rather than from the request body.
- **V-026** -- Lookup endpoints (receiving, packing, shipping, lookup) scope `warehouse_id` in the SELECT for non-admin users. A record in another warehouse produces the same 404 as a missing barcode; no existence oracle.
- **V-027** -- `/api/lookup/item/search` for non-admin users returns only items present as inventory or preferred-bin entries in their assigned warehouses. Admins keep the full catalogue.
- **V-028** -- `/api/putaway/update-preferred` refuses to target a bin outside the caller's assigned warehouses. Admins bypass as usual.
- **V-029** -- Receive over-receipt TOCTOU closed via `SELECT ... FOR UPDATE` on the PO line. Two concurrent receives against the same line can no longer both pass the remaining-quantity check.
- **V-030** -- Inventory move, pick allocation, and wave allocation acquire row locks (`FOR UPDATE` / `FOR UPDATE OF inv`) before reading and writing. `add_inventory` serializes NULL-lot upserts with a transaction-scoped advisory lock so concurrent callers never create duplicate rows.
- **V-069** -- Removed the hardcoded bcrypt hash of `admin` from `db/seed-apartment-lab.sql`. Seed SQL now inserts a placeholder; `seed.sh` must run to install a random password. Running the SQL directly leaves the admin account unable to authenticate (safe failure).

### Security -- infrastructure defaults
- `api/` and `admin/` services are rebuilt with production settings (nginx runtime for admin, non-root user in both). Dev reload moved to `docker-compose.dev.yml`.
- `SECURITY.md` reorganized into Authentication, Data protection, Tenant isolation, Connector framework, Input validation, Infrastructure, Response headers, and Backlog sections.
- `SECURITY_BACKLOG.md` (new) catalogues every Phase 6 finding not fixed in v1.3 with suggested fixes and target versions.

### Tests
- Security-oriented tests across 6 new files: `test_security_config.py`, `test_url_guard.py`, `test_idor_scope.py`, `test_concurrency.py`, `test_audit_tamper.py`, `test_csv_import_security.py`, plus additions to existing `test_auth.py`, `test_connectors.py`, `test_credential_vault.py`.
- Replaced the test JWT secret with a 32-byte value to silence PyJWT's `InsecureKeyLengthWarning` across the suite.
- Total: 570 backend tests passing.

### Notes for operators
- If you ever deployed this repo with the default compose file, read `SA-2026-001` and `SA-2026-002` in `SECURITY.md` and rotate `SENTRY_ENCRYPTION_KEY` and `JWT_SECRET` accordingly.
- The new `REDIS_PASSWORD` variable is required. See `.env.example`.
- The admin panel now listens on port 8080. Update reverse-proxy configs if applicable.
- The `db/migrations/016_audit_log_tamper_resistance.sql` migration must be applied on existing deployments before running v1.3.

## [v1.2.0] - 2026-04-16

### Added
- Pydantic v2 input validation schemas on every API endpoint that accepts a JSON body (17 schema files in `api/schemas/`)
- `@validate_body` decorator for consistent request validation across all routes (`api/utils/validation.py`)
- Standardized `validation_error` response format: `{error: "validation_error", details: [{type, loc, msg}]}`
- React error boundaries on all 21 admin panel page routes - each section fails independently with retry button and brand-colored fallback UI
- Mobile app handles `validation_error` responses - extracts first detail message for operator-friendly display

### Changed
- All API request payloads strictly validated before reaching the service layer
- Admin panel sections fail independently rather than white-screening the entire app
- 5 existing tests updated for new `validation_error` response format (putaway, transfers, shipping)

### Tests
- 75 new backend validation tests (unit tests for all schema files + integration tests for the decorator)
- 4 new ErrorBoundary frontend tests (catch, fallback message, reset/retry)
- Total: 382 backend + 10 frontend (was 307 backend + 6 frontend)

## [v1.1.1] - 2026-04-15

### Security
- **CSV formula injection on export (M9)** - cell values starting with `=`, `+`, `-`, `@`, `\t`, `\r` are now prefixed with a single quote in CSV exports (DataTable and PreferredBins)
- **DATABASE_URL hardcoded fallback removed (M7)** - app raises RuntimeError on startup if DATABASE_URL is not set, same pattern as JWT_SECRET
- **Login attempt count hidden** - failed login response no longer reveals remaining attempts before lockout

## [v1.1.0] - 2026-04-14

### Security - Backlog Audit (12 fixes)
- **Token invalidation on password change (M1)** - added `password_changed_at` column to users table; auth middleware rejects tokens issued before the last password change
- **JWT iat/jti claims (L10)** - tokens now include `iat` (issued-at, unix seconds) and `jti` (UUID) for revocation and replay detection
- **DB-backed rate limiting (M8)** - replaced in-memory `_login_attempts` dict with `login_attempts` table; persistent across restarts, per-username and per-IP tracking (5 attempts, 15 min lockout)
- **Password complexity (L1)** - `validate_password()` enforces minimum 8 characters, at least one letter, at least one digit; applied on user creation, admin password update, and self-service password change
- **Self-service password change (L2)** - `POST /api/auth/change-password` endpoint; mobile UI added as modal in user dropdown (current password, new password, confirm)
- **Warehouse listing auth (L7)** - `GET /api/warehouses/list` now requires JWT; mobile warehouse selection moved from pre-login to a blocking post-login modal on HomeScreen
- **suggest_bin warehouse scope (L8)** - preferred bin and default bin queries filtered to user's allowed warehouses; admins bypass the filter
- **CSV import limit (M10)** - import endpoint rejects payloads with more than 5000 records
- **Cycle count self-approval check (M3)** - configurable `require_count_approval_separation` app setting; when enabled, the counter cannot approve their own cycle count adjustments (403); when disabled, self-approvals are logged as `SELF_APPROVED_COUNT` in the audit log
- **Pagination (M6)** - added `page`/`per_page` query params with `LIMIT`/`OFFSET` to warehouses, zones, bins, and users list endpoints (default 50, max 1000)
- **Cleartext HTTP disabled for production (L5)** - `usesCleartextTraffic` set to false in app.json; `with-cleartext-traffic` plugin now checks `EAS_BUILD_PROFILE` and only enables cleartext for non-production builds
- **Production docker-compose (L6)** - `docker-compose.prod.yml` omits source volume mounts and requires all credentials via env vars

### Admin Panel
- New "Inventory" settings section with "Require separate approver for cycle count adjustments" checkbox
- Version updated to 1.1.0

### Mobile
- Warehouse selection moved from login screen to post-login blocking modal
- "Change Password" option added to user dropdown on home screen
- Auto-selects warehouse if only one is available
- Version updated to 1.1.0

### Infrastructure
- Migration 014: `password_changed_at TIMESTAMPTZ` column on users table
- Migration 015: `login_attempts` table with key, attempts, locked_until, last_attempt columns

### Bug Fixes
- Change-password endpoint returns 403 (not 401) for wrong current password, preventing the mobile client's auto-logout interceptor from firing

### Tests
- 19 new tests (307 total, 0 regressions)
- Warehouse list auth test (401 without JWT, 200 with JWT)
- CSV import limit test (5001 records returns 400)
- Cycle count self-approval tests (both modes)
- Password complexity tests (short, no digit, no letter)
- Self-service password change tests (success, wrong current, weak new, requires auth)
- JWT iat/jti claim tests (presence, uniqueness)
- Token invalidation tests (old token rejected, new token works after password change)
- Per-IP lockout test
- Pagination tests (zones, bins)

## [v1.0.0] - 2026-04-14

### Security - Full Code Audit
- **Default admin password eliminated** - seed script generates random 16-char password at runtime via `/dev/urandom`, prints to docker logs. Set `ADMIN_PASSWORD` env var to override.
- **Password minimum 8 characters** - enforced on user creation and password updates via admin panel
- **Over-pick prevention** - `quantity_picked` capped at `quantity_to_pick` in pick confirmation; prevents inventory drain via API manipulation
- **Inventory floor protection** - picking decrements use `GREATEST(0, ...)` to prevent negative inventory from race conditions
- **Short pick quantity cap** - `quantity_available` validated against task requirement
- **Packing quantity validation** - verify endpoint rejects zero and negative quantities
- **PO/SO line quantity validation** - `quantity_ordered` must be greater than zero on order creation
- **Receiving bin-warehouse validation** - bin must belong to PO's warehouse; prevents cross-warehouse inventory corruption
- **Lookup endpoint warehouse isolation** - item locations, bin contents, SO details, and bin search filtered by user's assigned warehouses (IDOR fix)
- **Security response headers** - X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy on all responses
- **Stack trace suppression** - global 500 error handler returns generic error instead of leaking internals
- **Debug mode disabled** - `debug=False` hardcoded; Werkzeug interactive debugger no longer activatable via env var
- **Login lockout** - 5 failed attempts locks the account for 15 minutes (per-username tracking, resets on successful login)
- **PostgreSQL port bound to localhost** - `127.0.0.1:5432:5432` prevents network exposure

### Infrastructure
- **Gunicorn in production** - Dockerfile CMD switched from `python app.py` (single-threaded Flask dev server) to `gunicorn -w 4` (4 workers)
- **Non-root container** - Dockerfile creates and runs as `appuser` instead of root

### Tests
- 5 new login lockout tests
- Test passwords updated to meet 8-character minimum
- 288 tests passing, 0 regressions

### Version
- All version numbers bumped to 1.0.0 across API, admin, mobile, README

## [v0.9.9] - 2026-04-13

### Security
- **SQL parameterization** - 30+ SQL queries converted from f-string constant interpolation to parameterized bindings across all route and service files (admin_orders, admin_users, packing, picking, receiving, shipping, picking_service)
- **Warehouse authorization middleware** - non-admin users blocked at the request level from accessing unassigned warehouses (checks both query params and JSON body, returns 403)
- **JWT_SECRET required** - app raises `RuntimeError` on startup if missing; `docker-compose.yml` uses `${JWT_SECRET:?}` syntax to error if not set
- **JWT payload includes warehouse_ids** - enables middleware enforcement without DB lookup
- **DB credentials configurable** - PostgreSQL user/password/database use env vars with defaults instead of hardcoded values
- **Debug mode conditional** - Flask debug mode tied to `FLASK_ENV` instead of always-on
- CORS origins now include port 5000 and are logged on startup

### Performance
- **17 FK indexes** added to `schema.sql` - PostgreSQL does not auto-index foreign key columns; these improve JOIN performance and cascading delete efficiency across zones, orders, PO/SO lines, pick tasks, fulfillments, transfers, cycle counts, and audit log

### Mobile
- **First-run setup screen** - detects if no server URL has been saved and shows a dedicated connect screen with health check validation before accepting the URL
- **Chainway scanner plugin fix** - config plugin now detects Kotlin vs Java `MainApplication` and uses correct patterns for package registration (fixes "Native module not available" on standalone APK)
- **Cleartext traffic plugin** - new `with-cleartext-traffic.js` Expo config plugin for Android 9+ HTTP support
- **API client improvements** - `hasStoredApiUrl()` helper, full URL in debug logs, server URL modal validates connectivity before saving

### Admin
- **Auth reload loop fixed** - 401 handler now clears both token and user from localStorage
- **Warehouse fetch gated behind auth** - `WarehouseProvider` waits for authenticated user before fetching warehouses
- **Vitest config** added for frontend testing

### Config
- `.env.example` expanded with all variables, organized with comments, proper `JWT_SECRET` generation instructions

### Tests
- 6 new warehouse authorization tests
- 283 tests passing (was 277)

## [v0.9.8] - 2026-04-11

### Security
- **JWT_SECRET required**  -  app raises `RuntimeError` on startup if `JWT_SECRET` env var is missing (was silently falling back to hardcoded default)
- **CORS restricted**  -  `CORS(app)` wildcard replaced with explicit origin whitelist (`CORS_ORIGINS` env var, defaults to `localhost:3000,localhost:8081`)
- **Explicit allowed-field sets**  -  all admin update endpoints (items, POs, SOs, users, warehouses, zones, bins) now use `ALLOWED_FIELDS` sets instead of iterating arbitrary request keys
- **Dead code removed**  -  unused `_paginate()` helper deleted from admin `__init__.py`

### Admin Panel
- Dark theme overhaul  -  header (#2a2520), sidebar, copper accents, cream text, 48px header with 192px sidebar
- Warehouse picker dropdown in header  -  admin users switch warehouse context from the topbar
- `WarehouseContext` provider persists selection in sessionStorage, auto-selects first warehouse on login
- All pages use dynamic `warehouseId` from context instead of hardcoded `warehouse_id=1`
- All pages re-fetch data automatically when warehouse selection changes
- **Adjustments page**  -  direct inventory add/remove with searchable bin/item pickers, recent adjustments table
- **Inter-Warehouse Transfers page**  -  cross-warehouse inventory moves with cascading warehouse/bin/item selects, transfer history
- **Imports page**  -  merged import type selector and file upload into single card with download template buttons
- Settings: removed import tools (moved to Imports page), added address fields to SO modal, vendor address to PO modal
- Audit log: batch-resolves entity IDs to human-readable names (bins, items, SOs, POs)
- Sidebar: added Adjustments and Transfers nav items under Warehouse group
- 4 new API endpoints: `POST /admin/adjustments/direct`, `GET /admin/adjustments/list`, `POST /admin/inter-warehouse-transfer`, `GET /admin/inter-warehouse-transfers`

### Mobile
- PutAwayScreen: compressed spacing for suggest/item/confirm cards
- TransferScreen: tightened step dots, labels, info cards, quantity row
- PickWalkScreen: reduced bin/item/next card padding and margins
- CountScreen: reduced bin header, turbo card, count input spacing
- ReceiveScreen: reduced PO header and receive card spacing
- LoginScreen: server URL moved to modal popup (was inline toggle), render guard for duplicate mount prevention
- ActiveBatchBanner: layout fixes for C6000 small screen
- Mobile version updated to v0.9.8

### Code Quality
- New `constants.py` with named constants for all status strings (PO, SO, batch, task, count, adjustment, audit action, bin type, role)
- All 12 route files + `picking_service.py` refactored from hardcoded string literals (`'OPEN'`, `'PICKED'`, `'PENDING'`, etc.) to imported constants  -  eliminates typo risk across 100+ status comparisons

### Data
- Renamed 11 branded items to generic descriptions (e.g. "Orvis Clearwater Rod 9ft" → "9ft 5wt Fly Rod"); fly pattern names kept as-is (not trademarked)
- Added `SKIP_SEED` environment variable: `SKIP_SEED=true` creates only admin user + default warehouse + default bins (no demo data); seed script converted to shell wrapper (`db/seed.sh`)

## [v0.9.7] - 2026-04-10

### Repeat Offender Fixes (8 bugs, 14 new tests)
- Admin login: 401 handler no longer redirects during login attempt, preserving username field (#12)
- Item weight: `save()` now sends `weight_lbs` correctly to API (#19)
- Audit log: batch-resolves bin_id/item_id/so_id/po_id to human-readable names (bin codes, SKUs, SO/PO numbers) (#20)
- Receiving bin filter: added `bin_type` query param to `/admin/bins` endpoint (#21)
- Settings unsaved warning: `useBlocker` from react-router-dom v7 replaces manual navigation guard (#22)
- Warehouse delete: hard DELETE with safety checks (bins, zones, inventory) replaces soft-deactivate (#23)
- Login version pin: absolute positioning pinned to bottom of screen (#26)
- Splash double title: removed splash image from app.json (#27)

### Handheld Functional (5 bugs, 2 new tests)
- Cancel receiving: new `/api/receiving/cancel` endpoint reverses receipts, PO line quantities, and inventory; ReceiveScreen tracks session receipt IDs (#2)
- Put-away quantity tracking: remaining qty updates per item instead of removing from queue, green checkmark when fully put away (#3)
- PagedList scroll: changed container from View to ScrollView (#4)
- Over-receive popup: shows warning only once per item per session (#5)
- PICKED SO routing: scanned PICKED orders now navigate to Ship screen (#10)

### Handheld UI (7 bugs)
- Settings menu: centered overlay with scrollable scan config (#1)
- Renamed "Wave picking" to "Pick orders" on home screen (#6)
- Double pick confirmation: auto-submits batch when all tasks complete, eliminated intermediate "Round Complete" view (#7)
- Replaced all 9 `Alert.alert` calls with styled React Native modals across HomeScreen and ReceiveScreen (#8)
- Warehouse selector: `TouchableOpacity` → `Pressable` for single-tap selection on Android, added overlay dismiss (#9)
- Removed badge numbers from home screen operation cards (#11)
- Scroll position: added `useScrollToTop` from React Navigation to all 7 scrollable screens (#14)

### Admin Panel (8 bugs)
- Cycle count approval: per-bin Submit/Approve All/Reject All buttons replace single global submit (#13)
- User management: Delete (hard) replaces Deactivate, with styled confirmation modal (#15)
- Create SO: full form on Picking page with so_number, warehouse, customer name/phone/address, ship method/address, order lines with item picker (#16)
- Item management: view modal is read-only, edit modal now has Delete/Archive buttons (#17)
- Delete item: styled confirmation popup replaces `confirm()` (#18)
- SO clickable: row click on Picking/Packing/Shipping pages opens customer detail modal (#24)
- Customer fields: added customer_phone and customer_address to sales order list API response (#25)

### EAS Build
- AsyncStorage URL: new `initApiUrl()` preloads saved server URL before any screens render; AuthProvider awaits it during loading phase

### Stats
- 277 tests passing (16 new)
- 29 files changed, +1,105 / -212 lines

## [v0.9.6] - 2026-04-09

### Fixed
- Scan hardening, cycle count approval, put-away reorder, manual picking, admin UX overhaul, CSV templates, role simplification

## [v0.9.5] - 2026-04-08

### Admin Panel
- Cycle count approval page: review pending adjustments per item, approve/reject individually, apply approved changes to inventory
- Inventory page: sortable columns by clicking headers (SKU, item name, bin, zone, quantities)
- Item edit: Delete button (hard delete with confirmation, blocked if order history) and Archive button (soft delete, restorable)
- Items page: filter dropdown for Active, Archived, or All items
- Purchase orders: dedicated page showing all POs with status filter, clickable rows with Ordered/Received line detail
- User creation: warehouse checkbox list (multi-warehouse assignment), simplified roles (Admin/User), mobile module access checkboxes (Pick, Pack, Ship, Receive, Put-Away, Count, Transfer)
- User role enforcement: USER role shows "Not authorized, contact admin" on admin panel login
- Warehouse management page: create, edit, delete warehouses
- Settings: batch Save button replaces auto-save, "Unsaved changes" indicator, browser beforeunload warning
- Admin panel version updated to 0.9.5

### Mobile (Batch 1  -  Scan Debug)
- Added `[SCAN_DEBUG]` logging to every scan handler across all screens
- Added `[API_DEBUG]` request/response logging to API client
- ScanInput: removed 300ms auto-submit timer (caused partial barcodes on C6000), added processing lock, improved whitespace/CR sanitization
- All scan handlers: process only on Enter/Submit, trim `\r\n\s`, ignore empty, disable during processing

### Mobile (Batch 2  -  Features)
- Put-away: replaced forced sequential flow with scrollable item list (scan or tap any item)
- Pick walk: item detail modal now has PICK + CLOSE buttons side by side for manual picking
- Pick walk: replaced Alert.alert cancel with styled app modal (white card, 12px radius, tan border)
- Pick walk: fixed NEXT ITEM PREVIEW  -  wrong API URL, stale task list, forward-scan logic for next PENDING task, "LAST ITEM IN BATCH" on final item
- Cycle count architecture: removed auto-adjustment of inventory on variance  -  creates PENDING audit records instead
- Cycle count: support for unexpected items (items found during count not in snapshot), flagged with "NEW" badge
- Cycle count: blind count mode respects `count_show_expected` setting from admin
- Transfer: X clear buttons on FROM BIN and TO BIN fields to correct mis-scans

### CSV Templates
- Added `docs/templates/` with 4 import templates: items, purchase orders, sales orders, bins (3 example rows each)
- CSV import now supports purchase orders and sales orders (SKU-based line matching, auto-creates PO/SO headers)
- "Download Template" link next to each import type selector

### Database
- Migration 013: `warehouse_ids INT[]` on users for multi-warehouse, role simplification (ADMIN/USER), default mobile module access
- New endpoints: `GET/POST /api/admin/adjustments/pending|review`, `POST /api/admin/items/:id/archive`, `DELETE /api/admin/warehouses/:id`

### Stats
- 261 tests passing

## [v0.9.4] - 2026-04-08

### Refactored
- Extracted `inventory_service.py` with `add_inventory()` and `move_inventory()`  -  inventory math now lives in one place instead of 3 route files
- Created `@with_db` decorator  -  eliminates manual db session boilerplate from all 10 route files + 43 admin routes
- Split 1,925-line `admin.py` monolith into 4 focused modules: `admin_warehouse.py`, `admin_items.py`, `admin_orders.py`, `admin_users.py`
- Extracted shared mobile StyleSheets: `screenStyles`, `buttonStyles`, `modalStyles`, `listStyles`, `doneStyles`  -  removed ~360 lines of duplicate styles across 12 screens
- Created `useScreenError` hook  -  consolidated error + scanDisabled state in 10 screens
- Created `ScreenHeader` component  -  replaced ~20 lines of duplicated header JSX per screen
- Created `ModeSelector` component  -  reusable Standard/Turbo toggle for Receive and Count screens
- Added `ActivityIndicator` loading states to HomeScreen and PickWalkScreen

### Fixed
- ReceiveScreen hardcoded `warehouse_id=1` now uses auth context (multi-warehouse support)
- Removed `console.log` statements from ScanInput and HomeScreen

### Stats
- 261 tests passing
- Net: +2,081 / -12,918 lines (mostly deduplication)

## [v0.9.3] - 2026-04-08

### Fixed
- UI revamp: tan cards, 12px radius, accent stripes, NEXT pick preview, blind cycle counts, carrier picker, password clear on bad login

## [v0.9.2] - 2026-04-08

### Fixed
- Test suite refactored from per-test TRUNCATE+reseed to transaction rollback (261 tests in ~4.3s, fixes 365-min CI deadlock)
- ScanInput auto-refocus every 500ms and auto-submit after 100ms pause for C6000 hardware scanner
- Expanded ignored keys in ScanInput (F1-F12, Tab, Escape, GoBack)

### Added
- Short pick admin reporting endpoint (GET /api/admin/short-picks) with SKU, bin, expected/picked/shortage, picker, timestamp
- Short pick count on dashboard pipeline (7d rolling, red when > 0)
- Pick walk item detail modal  -  tap any item card for SKU, UPC, bin, zone, qty, contributing orders
- `count_show_expected` setting enforced (hides expected qty for blind counts)

### Changed
- Bin types simplified from 6 (RECEIVING, PICKING, BULK, STAGING, SHIPPING, QC) to 3 (Staging, PickableStaging, Pickable)
- Migration: db/migrations/011_bin_type_qc_used.sql
- Updated across: schema.sql, seed data, admin.py, picking_service.py, putaway.py, PutAwayScreen.js, Settings.jsx, Bins.jsx
- Seed data fully rewritten to match 61 printed Zebra barcode labels (20 items, 16 bins, 5 POs, 20 SOs)
- All 12 test files rewritten for new seed data
- 49 files changed, +2,089 / -665 lines

## [v0.9.1] - 2026-04-06

### Fixed
- Put-Away missing from home screen (allowed_functions didn't include 'putaway')
- Receiving confirm fails with PO_id error
- Cycle count "Failed to create count" (FK constraint on inventory_adjustments)
- ScanInput doesn't clear after scan
- Double-tap required on home screen buttons
- One scan confirms entire pick quantity (now one scan = one unit)
- Pick quantities showing zeros (field mapping for line_count/total_units)
- End-of-batch flow redesign with Submit/Cancel
- Admin login shows no error on wrong password
- SO status lifecycle (removed ALLOCATED, added proper PICKING/PICKED statuses)

### Added
- Two receiving modes: Standard (manual qty entry) and Turbo (each scan = 1 unit)
- User icon dropdown menu with Logout
- Second warehouse for testing
- Preferred bins system with put-away suggestions (`preferred_bins` table)
- SKU display on pick walk screen
- Admin preferred bins page with full CRUD, inline priority editing, CSV export
- Admin cycle counts page with detail modal (expected/counted/variance breakdown)
- `count_show_expected` app setting for hiding expected quantities during counts
- `useScanQueue` hook for sequential barcode processing in turbo mode
- `POST /api/putaway/update-preferred` - set/change preferred bin from mobile
- `GET/POST/PUT/DELETE /api/admin/preferred-bins` - admin CRUD for preferred bins
- `GET /api/admin/cycle-counts` - cycle count list with line details
- `GET/PUT /api/admin/settings` - app settings management

### Changed
- Put-away flow redesigned: scan item → see preferred bin suggestion → scan destination → optional preferred bin prompt
- Receiving screen restructured to match pick scan pattern (PO queue → work through items)
- Count screen supports Standard/Turbo modes with AsyncStorage persistence
- Suggest bin endpoint queries `preferred_bins` table first, falls back to `default_bin_id`
- Items admin page shows default bin column from preferred bins

### Database
- New `preferred_bins` table with priority ranking and UNIQUE(item_id, bin_id)
- Seed data reset to match printed Zebra labels (fly fishing catalog)
- PO quantities reduced to 5–10, SO quantities reduced to 1–2 for lab testing

## [v0.9.0] - 2026-04-04

### Added
- React Native / Expo mobile scanner app (`mobile/` directory) for warehouse floor operations
- 10 screens: Login, Home, Receive, Put-Away, Pick Scan (wave), Pick Walk, Pick Complete, Pack/Ship, Cycle Count, Transfer
- 5 shared components: ScanInput (keyboard wedge), ErrorPopup (blocking modal), ActiveBatchBanner, WarehouseSelector, PagedList
- Hardware barcode scanner support via keyboard wedge (TextInput capture on Enter key)
- JWT auth context with session timeout (8-hour default), auto-logout on app foreground
- API client (native fetch) with JWT interceptor and 401 auto-logout
- Stack navigation (React Navigation) with auth-gated routing
- Universal scan bar on home screen (item/bin lookup from any barcode)
- Role-based function visibility on home screen (ADMIN sees all, others see allowed_functions)
- Active batch resume banner on home screen
- Warehouse switching from header tap
- Wave picking: scan SOs, build batch, walk pick path with zone/aisle display
- Short pick modal with quantity input
- Contributing orders collapsible section on pick walk
- Pack verification: scan-to-verify each item, then ship with carrier/tracking
- Cycle count: scan bin, enter counts, auto-variance detection
- Transfer: 3-step scan flow (item, from bin, to bin) with quantity input
- Brand theme: Accent Red (#8e2715), Copper (#c4722a), Cream (#FCF4E3), monospace typography, 48dp tap targets
- `GET /api/picking/active-batch` - returns user's incomplete pick batch for resume
- `GET /api/warehouses/list` - public endpoint (no auth) for login screen warehouse selector
- `GET /api/auth/me` - returns user info with role-based allowed_functions
- `app_settings` table for configurable session timeout
- `allowed_functions` column on users table for per-user function visibility
- Migration: `db/migrations/009_mobile_app.sql`
- 9 new API tests in `test_mobile_endpoints.py` (warehouse list, auth/me, active batch, session settings)

## [v0.8.2] - 2026-04-04

### Changed
- `GET /api/picking/batch/<id>/next` now includes explicit `zone` and `aisle` fields
- Zone and aisle return as null (not empty string) when bin has no zone or aisle assignment
- Pick task queries use LEFT JOIN on zones for bins without zone assignment
- Added zone_name to batch task list and next-task responses

## [v0.8.1] - 2026-04-04

### Added
- Wave picking workflow for combining identical items across multiple sales orders
- `POST /api/picking/wave-validate` - lightweight SO barcode validation before adding to wave
- `POST /api/picking/wave-create` - creates wave batch with combined picks and optimized walk path
- `wave_pick_orders` table linking SOs to wave batches
- `wave_pick_breakdown` table tracking per-SO contributions to combined pick tasks
- Contributing orders shown on `GET /api/picking/batch/<id>/next` with pick_number/total_picks
- Short pick FIFO distribution across contributing orders (fills earlier SOs first)
- Confirm pick updates all contributing SO lines via wave breakdown records
- ERP connector stub (`connector_stub.py`) with `enrich_order()` placeholder for future integration
- 19 wave picking tests covering validation, creation, breakdown, short distribution, and full flow

## [v0.8.0] - 2026-04-04

### Added
- React admin panel frontend (`admin/` directory) built with Vite + React Router
- Login page with JWT authentication and token persistence
- Dashboard with pipeline bar (To Receive, Put-away, To Pick, To Pack, To Ship, Low Stock)
- Dashboard order table, low stock alerts, recent activity feed, and inbound PO table
- Inventory overview page with search and pagination
- Cycle count page with bin selection and count creation
- Receiving page with PO list and line detail modal
- Put-away page showing items in staging bins
- Picking page with orders ready to pick
- Packing page with orders waiting to pack
- Shipping page with orders waiting to ship
- Bin management page with create, detail view, edit, and inventory contents
- Zone management page with create and edit
- Item management page with search, create, detail view, edit, and soft delete
- User management page with create, edit, role assignment, and deactivation
- Audit log viewer with action type, user, and date range filters
- Settings page with warehouse config, CSV/JSON import, manual PO/SO creation, and version info
- Reusable components: DataTable (with CSV export), StatusTag, Pipeline, Modal, PageHeader
- Sidebar navigation organized by warehouse workflow (Floor, Inbound, Outbound, Warehouse, System)
- Sidebar count badges from dashboard stats
- API client with JWT auto-injection and 401 redirect
- CSS custom properties for theming with Instrument Sans and JetBrains Mono fonts
- Docker support for admin panel in docker-compose.yml
- Vite dev server with API proxy to Flask backend

## [v0.7.0] - 2026-04-04

### Added
- Full admin CRUD API for the web admin panel (`/api/admin` blueprint)
- Warehouse management: list, get (with zones), create, update
- Zone management: list (filter by warehouse), create with type validation, update
- Bin management: list (filter by warehouse/zone), get (with inventory), create with type validation, update
- Item management: list with pagination and category/active filters, get (with inventory locations), create with SKU/UPC uniqueness, update, soft delete (blocks if inventory exists)
- Purchase order management: list with pagination and status/warehouse filters, get (with lines), create with lines, update (OPEN only), close
- Sales order management: list with pagination and status/warehouse filters, get (with lines), create with lines, update (OPEN only), cancel (releases allocated inventory if ALLOCATED)
- User management: list (excludes password_hash), create with bcrypt hashing and role validation, update (including password change), soft delete (blocks self-deactivation)
- Audit log viewer: paginated list with action_type, user_id, date range filters
- Inventory overview: paginated list with warehouse/item filters, joins item and bin details
- CSV/JSON bulk import for items and bins with per-row validation and error reporting
- Dashboard stats endpoint: open POs, pending receipts, putaway queue, order pipeline counts, total SKUs/bins, low stock alerts, recent activity feed
- Role enforcement: write operations require ADMIN or MANAGER role, read operations open to all authenticated users

## [v0.6.0] - 2026-04-02

### Added
- Cycle counting workflow: create counts, view expected vs actual, submit with variance detection
- `POST /api/inventory/cycle-count/create` - create cycle counts for one or more bins with inventory snapshot
- `GET /api/inventory/cycle-count/<count_id>` - view count with expected quantities and count status
- `POST /api/inventory/cycle-count/submit` - submit physical counts, auto-create adjustments for variances
- Inventory adjustment records with reason codes and cycle count linkage
- General-purpose bin transfers for stock reorganization
- `POST /api/transfers/move` - move items between any two bins with audit trail
- Automatic inventory correction on cycle count variance (updates quantity_on_hand)
- Last-counted-at tracking on inventory rows

## [v0.5.0] - 2026-04-02

### Added
- Packing workflow: scan-to-verify pack station with barcode validation
- `GET /api/packing/order/<barcode>` - load order for packing with calculated weight
- `POST /api/packing/verify` - scan item barcode to verify against picked list
- `POST /api/packing/complete` - mark order fully packed after all items verified
- Shipping / fulfillment workflow: record tracking info and create fulfillment records
- `POST /api/shipping/fulfill` - submit shipment with tracking number, carrier, and ship method
- Fulfillment line traceability (links shipped items back to source pick bins)
- Calculated package weight from item weights × picked quantities
- Over-pack prevention (blocks verifying more than picked quantity)
- Status enforcement: packing requires PICKING status, shipping requires PACKED status

## [v0.4.0] - 2026-04-02

### Added
- Batch picking with pick path optimization (`pick_sequence`-based serpentine walk)
- `POST /api/picking/create-batch` - create pick batch from multiple SOs with inventory allocation
- `GET /api/picking/batch/<id>` - full batch with tasks in walk-path order
- `GET /api/picking/batch/<id>/next` - next pending pick task
- `POST /api/picking/confirm` - confirm pick with barcode validation (rejects wrong scans)
- `POST /api/picking/short` - report short picks with shortage tracking
- `POST /api/picking/complete-batch` - complete batch, update SO statuses
- Picking service (`picking_service.py`) with core allocation and path optimization logic

## [v0.3.0] - 2026-04-02

### Added
- Receiving workflow: scan PO barcode, verify items, submit receipt to staging bin
- Put-away workflow: pending items list, bin suggestion (default bin or stock consolidation), scan-to-confirm transfer
- `GET /api/receiving/po/<barcode>` - PO lookup with lines and expected items
- `POST /api/receiving/receive` - submit item receipts with inventory updates
- `GET /api/putaway/pending/<warehouse_id>` - items in staging awaiting put-away
- `GET /api/putaway/suggest/<item_id>` - suggested bin for put-away
- `POST /api/putaway/confirm` - confirm put-away with bin transfer record
- Reusable audit logging service (`audit_service.py`)
- Over-receipt warnings (allowed but flagged)

## [v0.2.0] - 2026-04-02

### Added
- JWT authentication system (`POST /api/auth/login`, `POST /api/auth/refresh`)
- `@require_auth` and `@require_role` middleware decorators
- Item lookup by barcode (`GET /api/lookup/item/<barcode>`) with inventory locations
- Bin lookup by barcode (`GET /api/lookup/bin/<barcode>`) with contents
- Item search (`GET /api/lookup/item/search?q=`) - case-insensitive by SKU, name, UPC
- Bin search (`GET /api/lookup/bin/search?q=`) - case-insensitive by code
- User model with bcrypt password verification
- Auth service with JWT token generation and validation
- Password hashing utility (`scripts/hash_password.py`)

## [v0.1.0] - 2026-04-02

### Added
- Initial project structure matching build plan
- PostgreSQL schema - 20 tables covering warehouses, zones, bins, items, inventory, POs, SOs, pick batches, fulfillments, audit log, users
- Flask API skeleton with `/api/health` endpoint
- Docker Compose for local development (PostgreSQL 16 + Flask API)
- Apartment test lab seed data (1 warehouse, 5 zones, 9 bins, 10 items, sample PO + SOs)
- README, CONTRIBUTING.md, LICENSE (MIT), .gitignore, .env.example
