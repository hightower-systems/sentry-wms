# Changelog

Every GitHub release includes the full set of fixes and upgrade notes.
Links below point to the release page for the complete list. This page
is a shorter, docs-site-friendly summary.

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
