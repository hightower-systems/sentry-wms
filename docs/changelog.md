# Changelog

Every GitHub release includes the full set of fixes and upgrade notes.
Links below point to the release page for the complete list. This page
is a shorter, docs-site-friendly summary.

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
