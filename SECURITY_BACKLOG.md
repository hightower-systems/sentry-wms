# Security Backlog

Findings from the Phase 6 audit that are not fixed in v1.3. Entries are
grouped by target version. Short descriptions only; the original Phase 6
audit report (V-IDs) is the authoritative source for exploit steps and
impact analysis.

Chain exploits from the audit are documented at the end with a note on
which individual fixes break them.

---

## Target v1.4 (Deferred High)

### V-045 -- JWT stored in admin panel localStorage
- **Severity:** High
- **Category:** AI-Pattern
- **Description:** The admin SPA stores the bearer JWT in
  `localStorage.sentry_token`. Any XSS primitive on the admin origin
  exfiltrates it instantly; HttpOnly cookies are not an option without
  a CSRF refactor.
- **Suggested fix:** Move session to `HttpOnly; Secure; SameSite=Strict`
  cookie. Add double-submit CSRF token for state-changing routes.
  Requires coordinated backend + frontend change.
- **Target version:** v1.4.

### V-047 -- Mobile JWT in AsyncStorage (plaintext)
- **Severity:** High
- **Category:** Infrastructure
- **Description:** React Native app stores the 8-hour JWT in
  `AsyncStorage`, which is unencrypted SQLite on Android and an
  unprotected plist on iOS. `expo-secure-store` is already declared as
  a dependency but never imported.
- **Suggested fix:** Replace every `AsyncStorage.getItem/setItem` that
  touches `jwt_token` or `user_data` with `SecureStore.getItemAsync /
  setItemAsync`. Migrate existing installed clients by falling back to
  AsyncStorage on first launch then rewriting into SecureStore.
- **Target version:** v1.4.

### V-048 -- Mobile cleartext HTTP forcibly enabled
- **Severity:** High
- **Category:** Infrastructure
- **Description:** `mobile/app.json` and `mobile/plugins/
  with-cleartext-traffic.js` unconditionally enable cleartext HTTP in
  every build profile. CHANGELOG v1.1.0 claimed this was fixed; the
  fix was reverted in commit `ff36caf` for warehouse-deployment
  convenience and the changelog was not updated.
- **Suggested fix:** Gate the plugin on `process.env.EAS_BUILD_PROFILE !==
  "production"`. Coordinate with the warehouse deployments that
  actually rely on cleartext (they should migrate to a local TLS
  reverse proxy instead).
- **Target version:** v1.4.

### V-050 -- No Content-Security-Policy header
- **Severity:** High
- **Category:** AI-Pattern
- **Description:** The API sets X-Frame-Options, X-Content-Type-Options,
  X-XSS-Protection, Referrer-Policy, and Permissions-Policy, but NOT
  Content-Security-Policy. Without CSP a successful XSS can exfiltrate
  to any origin.
- **Suggested fix:** Add `Content-Security-Policy: default-src 'self';
  script-src 'self'; style-src 'self' 'unsafe-inline'
  https://fonts.googleapis.com; font-src https://fonts.gstatic.com;
  connect-src 'self'; frame-ancestors 'none'; base-uri 'self';
  form-action 'self'`. Test carefully against the admin SPA, especially
  Google Fonts preconnects and inline styles from the React component
  library.
- **Target version:** v1.4.

---

## Mediums

### V-006 -- Fernet cached globally in module state
- **Severity:** Medium
- **Category:** Sensitive-Info
- **Description:** `_fernet` is a module-level cache, so the Fernet key
  lives in process memory for the process lifetime.
- **Suggested fix:** Per-request cache or a `SecretBytes` wrapper that
  zeroes memory after use (best-effort in Python).
- **Target version:** backlog.

### V-007 -- Celery task tracebacks may leak credentials
- **Severity:** Medium
- **Category:** Sensitive-Info
- **Description:** If a connector ever builds a URL with userinfo
  (`https://key:secret@host`), a network error's `str(exc)` pulled into
  `logger.error` or `sync_state.last_error_message` can leak the
  credential.
- **Suggested fix:** Scrub userinfo from URLs before logging; audit
  connector implementations for any credential-in-URL pattern.
- **Target version:** v1.4.

### V-008 -- Missing SENTRY_ENCRYPTION_KEY auto-regeneration risk
- **Severity:** Medium (closed by V-001)
- **Category:** AI-Pattern
- **Description:** Noted in Phase 6 as a distinct item; in practice
  addressed by V-001 which hard-fails on missing key.
- **Target version:** Done (bundled into V-001).

### V-010 -- Connector name collision silently overwrites
- **Severity:** Medium
- **Category:** Chain
- **Description:** `ConnectorRegistry.register()` assigns to the dict
  with no duplicate guard. A second `registry.register("netsuite",
  EvilConnector)` wins silently.
- **Suggested fix:** Raise `ValueError` on duplicate name. Pin an
  allowlist of connector module names that auto-register.
- **Target version:** v1.4.

### V-016 -- Content-Type not explicitly required on POST endpoints
- **Severity:** Medium
- **Category:** Injection
- **Description:** `validate_body` uses `request.get_json(silent=True)`
  which returns None for non-JSON bodies. Pydantic then validates `{}`
  and fails clean today, but a future `force=True` slip would allow
  non-JSON bodies through.
- **Suggested fix:** Explicitly reject requests without
  `Content-Type: application/json` with 415.
- **Target version:** backlog.

### V-020 -- ErrorBoundary writes full error to console.error
- **Severity:** Medium
- **Category:** Sensitive-Info
- **Description:** React error boundary logs the full error object.
  On a shared workstation or with a malicious browser extension this
  leaks sensitive context (API URLs, potentially Authorization header
  reconstruction).
- **Suggested fix:** Scrub tokens before logging; POST a sanitized
  telemetry event to the backend instead of `console.error`.
- **Target version:** v1.4.

### V-021 -- Admin UI renders raw backend error strings
- **Severity:** Medium
- **Category:** Sensitive-Info
- **Description:** Several admin pages display `data.error` verbatim.
  React's default escaping prevents XSS, but backend internals (SQL
  constraint names, Python exception reprs) end up on screen.
- **Suggested fix:** Define a finite error-code enum on the backend;
  map codes to localized strings in the frontend.
- **Target version:** v1.4.

### V-024 -- login_attempts table grows unbounded
- **Severity:** Medium
- **Category:** DoS
- **Description:** The login_attempts rate-limit table accumulates one
  row per unique username or IP key. No cleanup job. An attacker
  spraying random usernames can bloat the table indefinitely.
- **Suggested fix:** Celery beat task that deletes rows older than one
  hour. Cap key length at the schema level.
- **Target version:** v1.4.

### V-031 -- Last-admin delete race
- **Severity:** Medium
- **Category:** Logic
- **Description:** With two admins, two simultaneous DELETE /users/X
  requests each see `admin_count = 2` and both proceed, leaving zero
  admins.
- **Suggested fix:** Wrap the check in `SELECT ... FOR UPDATE` on a
  serialization lock row, or enforce a deferred `CHECK` that at least
  one active admin exists.
- **Target version:** v1.4.

### V-033 -- URL-path warehouse_id bypasses middleware check
- **Severity:** Medium
- **Category:** IDOR
- **Description:** `require_auth`'s warehouse check only reads
  `warehouse_id` from request body or query string, not from path
  parameters. Routes must call `check_warehouse_access()` or
  `warehouse_scope_clause()` explicitly. Most already do; this is a
  defense-in-depth gap.
- **Suggested fix:** Teach the middleware to inspect view args too, or
  add a CI lint that flags new routes that take `warehouse_id` in the
  path without also calling the helper.
- **Target version:** v1.4.

### V-034 -- Picking service error messages leak IDs
- **Severity:** Medium
- **Category:** Sensitive-Info
- **Description:** `ValueError("SO X is in a different warehouse")` and
  similar messages in picking_service are relayed to the client,
  enabling cross-tenant SO enumeration.
- **Suggested fix:** Collapse to a single generic message
  ("not found or access denied") for non-admins.
- **Target version:** v1.4.

### V-035 -- Celery worker bind-mounts host source tree
- **Severity:** Medium
- **Category:** Infrastructure
- **Description:** Default `docker-compose.yml` mounts `./api:/app`
  into the celery-worker container, so an RCE in the worker persists
  by writing to the host tree.
- **Suggested fix:** Remove the bind mount from the default compose.
  The dev overlay (`docker-compose.dev.yml`, introduced in V-003)
  already holds the mount.
- **Target version:** v1.4.

### V-040 -- API and admin bound to 0.0.0.0 by default
- **Severity:** Medium
- **Category:** Infrastructure
- **Description:** `docker-compose.yml` publishes `5000:5000` and
  `8080:8080` without an interface prefix. On a public-cloud host this
  exposes the API and admin UI to the internet.
- **Suggested fix:** Change to `127.0.0.1:5000:5000` /
  `127.0.0.1:8080:8080`. Deployments that need external access override
  in their own compose.
- **Target version:** v1.4.

### V-041 -- No rate limiting beyond /auth/login
- **Severity:** Medium
- **Category:** DoS
- **Description:** Every other endpoint (sync trigger, credential test,
  receive, pick, etc.) can be hit at full rate by any authenticated
  user. Combined with V-009's (now closed) SSRF surface, unthrottled
  test_connection was a scanning primitive.
- **Suggested fix:** Add Flask-Limiter with per-user and per-IP token
  buckets. Lower limit for connector/test, higher for mobile scan
  paths.
- **Target version:** v1.4.

### V-042 -- No pip-audit / npm-audit in CI
- **Severity:** Medium
- **Category:** Supply-chain
- **Description:** `admin/package.json` uses `^` ranges and the admin
  Dockerfile (post-V-003) uses `npm ci`. Backend uses pinned versions
  but no audit job.
- **Suggested fix:** Add a CI step that runs `pip-audit` and
  `npm audit --audit-level=high` and fails the build on high-severity
  advisories.
- **Target version:** v1.4.

### V-046 -- No Subresource Integrity on admin bundle
- **Severity:** Medium
- **Category:** AI-Pattern
- **Description:** Built asset tags in `admin/index.html` ship without
  `integrity="sha384-..."` attributes. Combined with V-050 (no CSP), a
  static-host compromise or CDN poisoning could rewrite the bundle.
- **Suggested fix:** Enable a Vite SRI plugin in the build.
- **Target version:** v1.4.

### V-051 -- No HSTS header
- **Severity:** Medium
- **Category:** Infrastructure
- **Description:** Response headers do not include
  `Strict-Transport-Security`.
- **Suggested fix:** Set `max-age=31536000; includeSubDomains` when
  TLS terminates at the reverse proxy.
- **Target version:** v1.4.

### V-058 -- Lockout DoS + last-admin delete race chain
- **Severity:** Medium
- **Category:** Chain
- **Description:** Two-step chain combining V-023 (closed) and V-031
  (open). With V-023 closed the first leg no longer fires, but V-031
  alone remains an admin-wipe risk.
- **Suggested fix:** Depends on V-031.
- **Target version:** v1.4 (blocked by V-031).

### V-059 -- CSV formula -> Excel RCE chain
- **Severity:** Medium (closed by V-015)
- **Category:** Chain
- **Description:** Now closed by V-015. Import-side sanitization
  prevents the payload from ever landing in the DB; export-side
  sanitizer was already present in `DataTable`.
- **Target version:** Done.

### V-060 -- update_settings not audited
- **Severity:** Medium
- **Category:** Logic
- **Description:** POST /api/admin/settings updates `app_settings`
  without writing to audit_log. A compromised admin can silently flip
  `allow_over_receipt` or `require_packing_before_shipping`.
- **Suggested fix:** `write_audit_log` before commit with the key,
  old value, and new value. Audit-log tamper resistance (V-025) now
  makes those records durable.
- **Target version:** v1.4.

### V-072 -- Source bind mounts in default compose
- **Severity:** Medium (partially closed by V-003)
- **Category:** Infrastructure
- **Description:** `docker-compose.yml` still bind-mounts `./api:/app`
  into api and celery-worker containers. V-003 closed the admin
  equivalent; the api and celery mounts remain for dev convenience.
- **Suggested fix:** Remove from default compose. Move to
  `docker-compose.dev.yml` only.
- **Target version:** v1.4.

### V-079 -- No MFA for admin
- **Severity:** Medium
- **Category:** Auth
- **Description:** Admin accounts authenticate with username + password
  only. A stolen password is sufficient.
- **Suggested fix:** Add TOTP MFA as an ADMIN-role requirement, with a
  recovery-code flow for lockout.
- **Target version:** v1.4 or v2.0.

### DNS rebinding on connector SSRF guard
- **Severity:** Medium
- **Category:** Injection
- **Description:** V-009's guard resolves the hostname and checks the IP,
  but does not pin the resolved IP for the actual request. A hostname
  that returns a public IP on first lookup and a private IP on second
  lookup could bypass.
- **Suggested fix:** Resolve once, then make the HTTP request against
  the literal IP with the original Host header preserved. Reject when
  the resolution is non-deterministic.
- **Target version:** v1.4.

---

## Lows and Informationals

| V-ID | Title | Suggested fix |
|------|-------|---------------|
| V-011 | `discover()` swallows import errors | Log exceptions instead of silently skipping |
| V-013 | Breaker state is ephemeral per connector instance | Share via Redis keyed on (connector, warehouse) |
| V-017 | Pydantic extra fields ignored, not rejected | `model_config = ConfigDict(extra="forbid")` on all request schemas |
| V-018 | Validation error shape enables field enumeration | Collapse to generic message in production |
| V-019 | No rate limit on validation errors | Add Flask-Limiter pre-validation |
| V-022 | Admin CSV client-side `split(',')` is naive | Replace with Papa Parse |
| V-036 | `wh_filter.replace("warehouse_id", "po.warehouse_id")` pattern | Use a per-query builder instead of string substitution |
| V-037 | `int()` in CSV import -> 500 on non-numeric (now closed by V-015) | Done |
| V-038 | No per-endpoint body-size caps | Set per-route `MAX_CONTENT_LENGTH` |
| V-039 | Username case-normalization mismatch between DB lookup and lockout key | Decide on case-sensitivity policy and normalize in one place |
| V-043 | Default CORS_ORIGINS includes Metro dev server (:8081) | Remove from production defaults |
| V-044 | `users.is_active DEFAULT TRUE` | Change default to FALSE; require explicit activation |
| V-049 | Hardcoded internal IP in mobile preview APK | Parameterize via build-time secret |
| V-052 | N+1 queries in admin dashboard endpoint | Convert to JOINs |
| V-061 | `user_id` mixed int/string across audit log callers | Standardize on username (VARCHAR) or user_id (INT) with schema change |
| V-064 | Flask-CORS does not explicitly reject `Origin: null` | Add explicit denylist |
| V-065 | Gunicorn defaults have no `--timeout` / `--max-requests` | Tune in Dockerfile |
| V-067 | `users.warehouse_ids INT[]` has no length cap | Schema CHECK constraint |
| V-068 | JSONB barcode lookup builds f-string JSON literal | Use `json.dumps([barcode])` for safe JSON construction |
| V-070 | `seed-apartment-lab.sql` mounted at `/seed-data/` even with SKIP_SEED | Only mount in dev compose |
| V-071 | Containers have no `cap_drop` / `read_only` / `security_opt` | Add to prod compose |
| V-073 | `review_adjustments` silently skips invalid decisions | Return per-decision detail |
| V-074 | `direct_adjustment` bypasses approval | Add opt-in setting `require_direct_adjustment_approval` |
| V-075 | Mobile global 401 interceptor force-logs-out | Restrict to whitelist endpoints |
| V-076 | Only 500 handler is consolidated | Add matching handlers for 4xx if observability needs |
| V-077 | Test JWT secret hardcoded in conftest (closed cosmetically) | Rotate per-run via `secrets.token_hex(32)` |

---

## Chain Exploits -- Which Fixes Break Them

All seven chain exploits from the Phase 6 audit have at least one leg
fixed in v1.3.

| Chain | Breaking fix landed in v1.3 | Remaining legs (target) |
|-------|-----------------------------|--------------------------|
| V-053 admin XSS -> JWT exfil -> SSRF -> cloud metadata | V-009, V-014 | V-045 (v1.4), V-050 (v1.4) |
| V-054 admin takeover -> vault decryption -> ERP | V-001, V-002, V-025, V-026 | none -- chain is broken |
| V-055 Redis injection -> Celery task forgery | V-004 | V-010 (v1.4) |
| V-056 sync deadlock -> social engineering | -- | V-012 (v1.4, sync heartbeat) |
| V-057 cycle count inflation -> ghost shipments | -- | Operational control (approval separation setting already present; document) |
| V-058 lockout DoS + admin delete race | V-023 | V-031 (v1.4) |
| V-059 CSV formula -> Excel RCE | V-015 | none -- chain is broken |
