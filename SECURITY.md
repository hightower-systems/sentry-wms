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
