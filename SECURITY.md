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

## Security Practices

- JWT authentication with live database validation on every request
- User role, warehouse access, and active status verified per-request (not cached in token)
- Deactivated users and permission changes take effect immediately
- Warehouse authorization middleware on all endpoints
- All SQL queries use parameterized bindings
- Login lockout after 5 failed attempts
- bcrypt password hashing
- CORS restricted to configured origins
- Role-based access control (ADMIN/USER)
- Full audit trail on every warehouse action
- Request body size limited to 10MB
- Pagination capped to prevent memory exhaustion
