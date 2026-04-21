# Changelog

Every GitHub release includes the full set of fixes and upgrade notes.
Links below point to the release page for the complete list. This page
is a shorter, docs-site-friendly summary.

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
