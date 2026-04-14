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
