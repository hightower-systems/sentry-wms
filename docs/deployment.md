# Deployment Guide

## Development (Docker Compose)

### Prerequisites

- Docker and Docker Compose
- Node.js 18+ (for mobile app development)

### Setup

```bash
git clone https://github.com/hightower-systems/sentry-wms.git
cd sentry-wms
cp .env.example .env
# Generate the three required secrets and paste them into .env:
#   JWT_SECRET            -- openssl rand -hex 32
#   SENTRY_ENCRYPTION_KEY -- python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#   REDIS_PASSWORD        -- python -c "import secrets; print(secrets.token_hex(32))"
# docker compose will refuse to start if any of these are missing.
docker compose up -d
```

This starts five containers:

- **sentry-db** -- PostgreSQL 16 on port 5432 (bound to localhost only)
- **sentry-api** -- Flask API on port 5000
- **sentry-redis** -- Redis 7 (broker for Celery, no host port)
- **sentry-celery** -- Celery worker for connector sync tasks
- **sentry-admin** -- React admin panel served by nginx on port 8080

For local development with Vite dev-server and hot reload:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

The overlay replaces the nginx admin with the Vite dev-server on port 3000
and mounts `./api` and `./admin` into their containers for live reload.

### Admin Login

Fresh installs seed the admin user as `admin` / `admin` with `must_change_password=true`. Auth middleware blocks every endpoint except change-password and logout until you set a new password from the first-login screen.

To skip the forced change (CI, deterministic dev environments), set `ADMIN_PASSWORD` in your `.env`. The seed uses that value directly and prints it on startup:

```bash
docker compose logs db | grep "Admin password"
```

### Demo Data

The default seed includes 1 warehouse, 6 zones, 16 bins, 20 items, 5 POs, and 20 SOs for testing. To start with a clean system:

```bash
SKIP_SEED=true docker compose up -d
```

### Running Tests

```bash
docker compose exec api python -m pytest tests/ -x -q
```

647 backend tests using transaction-rollback isolation (54 skipped inside the
api container for infrastructure-config assertions; run on the host with
`python -m pytest tests/` to get full coverage).

---

## Upgrading

When upgrading Sentry WMS, you MUST rebuild Docker images after pulling new code. Python or JavaScript dependencies may have changed, and cached images will not include new dependencies.

Correct upgrade procedure:

```bash
git pull
docker compose down
docker compose build
docker compose up -d
```

If you see `ModuleNotFoundError` or similar errors after upgrading, you skipped the build step.

Starting in v1.4.2, `sentry-api` detects this condition at startup: if the Docker image's baked-in version does not match the source code version, the container logs a clear upgrade-procedure message and exits with code 2 rather than crashing a worker with a dependency error.

---

## Production

### Required Environment Variables

All of the following are required. `docker compose` refuses to start if any are missing:

```bash
# Application auth
JWT_SECRET=$(openssl rand -hex 32)

# Connector credential vault (Fernet, base64, 32 bytes)
SENTRY_ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# Redis broker password (Celery)
REDIS_PASSWORD=$(python -c "import secrets; print(secrets.token_hex(32))")

# Database
DATABASE_URL=postgresql://user:pass@db:5432/sentry
POSTGRES_USER=your-db-user
POSTGRES_PASSWORD=your-db-password

# Allowed browser origins for the admin panel / mobile
CORS_ORIGINS=https://your-admin-domain.com
```

`SENTRY_ENCRYPTION_KEY` in particular is load-bearing: rotating it
requires decrypting every row of `connector_credentials` with the old
key and re-encrypting with the new one. Treat it like a master key.
The app does not auto-generate a replacement -- missing values raise
`RuntimeError` at startup.

### Production Docker Compose

Use `docker-compose.prod.yml` which has no source volume mounts:

```bash
docker compose -f docker-compose.prod.yml up -d
```

Key differences from the dev compose:

- No `./api:/app`, `./db:/db`, or `./admin:/app` volume mounts
- `SKIP_SEED=true` by default
- Every secret required via env var (no defaults); hard-fail on missing
- `FLASK_ENV=production` hardcoded
- Redis requires `--requirepass $REDIS_PASSWORD`; Celery broker URL uses
  the authenticated form
- Admin container is a multi-stage nginx build serving the compiled Vite
  bundle; Vite dev-server is unavailable in production

### Required migration

Before running v1.3.0+ against an existing v1.2 database, apply migration
`db/migrations/016_audit_log_tamper_resistance.sql`. It adds the
`prev_hash` / `row_hash` columns on `audit_log`, installs the hash-chain
trigger and the `BEFORE UPDATE / BEFORE DELETE` guards, and exposes
`verify_audit_log_chain()` for periodic integrity checks. v1.4.0 adds
migrations `017_sync_state_running_since.sql` and
`018_sync_state_run_id.sql` for the stale-running sync recovery (V-012)
and sync_state race fix (V-102).

### Infrastructure Notes

- PostgreSQL port is bound to `127.0.0.1:5432` only (not exposed to the network)
- API and admin ports default to `127.0.0.1` (see LAN development access below)
- API runs Gunicorn with 4 workers (not the Flask dev server)
- Container runs as non-root user `appuser`
- `debug=False` is hardcoded in `app.py`

### Content-Security-Policy

A strict CSP header is set by the API and mirrored by nginx in the admin
container. `default-src 'self'`, `script-src 'self'` with per-build SRI
hashes, `style-src 'self' 'unsafe-inline'`, `img-src 'self' data:`,
`font-src 'self'`, `connect-src 'self'`. If you override `CORS_ORIGINS`
you do not need to edit the CSP -- `connect-src 'self'` is same-origin
only and browser requests to the API go through the admin origin.

### Rate limiting

Flask-Limiter is enabled by default, backed by the same Redis broker as
Celery. Global default is `300/minute` per client; sensitive endpoints
(auth, sync-reset, connector test-connection) have tighter per-route
quotas. The limiter reads from the `REDIS_PASSWORD`-authenticated
broker URL; set `RATELIMIT_STORAGE_URI` explicitly if you want a
separate Redis instance.

### Self-hosted fonts

Instrument Sans and JetBrains Mono ship inside `admin/public/fonts/`
under the SIL Open Font License. No third-party font requests are made
at runtime -- relevant if you have strict egress controls on the
warehouse network.

### LAN development access

The API and admin ports are parametrized via `API_BIND_HOST` and
`ADMIN_BIND_HOST`. Both default to `127.0.0.1`, which is the
correct posture for production deployments behind a reverse proxy
and for any cloud-hosted install (V-040). For LAN development
where a phone or handheld scanner on the same network needs to
reach the API directly, override either or both in your local
`.env`:

```
API_BIND_HOST=0.0.0.0
ADMIN_BIND_HOST=0.0.0.0
```

`.env` is gitignored, so the override stays on the dev machine
and does not ship to production.

### Reverse Proxy (HTTPS)

The API serves HTTP only. For HTTPS, put a reverse proxy in front:

```
Browser / mobile app --> HTTPS --> nginx / Caddy / Traefik / ALB --> HTTP --> gunicorn:5000
```

#### TRUST_PROXY (required behind a reverse proxy)

When Sentry runs behind a reverse proxy, set `TRUST_PROXY=true` in the API's environment:

```
TRUST_PROXY=true
```

The Flask app wraps `app.wsgi_app` in Werkzeug's `ProxyFix` when this flag is set, so `request.scheme`, `request.host`, and `request.is_secure` reflect the headers the proxy forwards (`X-Forwarded-Proto`, `X-Forwarded-Host`, `X-Forwarded-For`) instead of the internal `http://127.0.0.1:5000` hop. Without this, cookies issued at login are scoped to the internal hostname, the browser never resubmits them to the public hostname, and every CSRF-protected `POST` / `PUT` / `PATCH` / `DELETE` returns `403 CSRF token missing or invalid` (#107).

> **Security warning.** Only enable `TRUST_PROXY` when Sentry actually runs behind a reverse proxy on a network the proxy controls. If the app is reachable directly (no proxy in front, or a proxy that forwards from the public internet without stripping inbound `X-Forwarded-*` headers), any client can forge its own scheme, hostname, and client IP by sending those headers. `TRUST_PROXY` is opt-in for exactly this reason. The default-off deployment is safe against header forgery.

##### Where to set it, and how to apply the change

`TRUST_PROXY` goes in the `.env` file at the **repo root** (next to `docker-compose.yml`), NOT `api/.env`. `docker-compose.yml` reads `.env` from the Compose project directory; `api/.env` is only picked up by a direct `flask run` from inside the `api/` folder and is not consulted by the containerised deployment.

After editing `.env`, the `api` container must be **recreated**, not just restarted, for the new value to take effect. Compose picks up `.env` changes when it creates a container, not when it starts one. A common footgun:

- `docker compose up -d` -- recreates the container when the config has changed, which is what picks up the new `.env` value.
- `docker compose restart api` -- keeps the existing container and just bounces the process inside it, which does NOT re-read `.env`.

##### Verification

Two checks confirm `TRUST_PROXY` actually reached the Flask app after `docker compose up -d`:

```bash
# 1. Compose forwarded the env var into the container.
docker compose exec api env | grep TRUST_PROXY
# Expected: TRUST_PROXY=true

# 2. Flask read it at startup and wired ProxyFix.
docker compose logs api | grep ProxyFix
# Expected: "ProxyFix active: trusting X-Forwarded-* headers (TRUST_PROXY=true)"
# One line per gunicorn worker (4 lines on the default worker count).
```

Or, from outside the container (e.g. from the reverse proxy itself), hit the health endpoint:

```bash
curl -s https://sentry.yourcompany.com/api/health
# {"status":"ok","service":"sentry-wms","proxy_fix_active":true}
```

A green health response with `"proxy_fix_active": false` behind a reverse proxy means `TRUST_PROXY` did not reach the container. Check `docker-compose.yml` (v1.4.4 shipped without the Compose-side wiring; v1.4.5 added it; #136) and confirm you ran `up -d`, not `restart`, after changing `.env`.

#### nginx

Minimum config. Each header has a specific job; the comments explain why each one is required:

```nginx
server {
    listen 443 ssl;
    server_name sentry.yourcompany.com;

    ssl_certificate     /etc/ssl/certs/your-cert.pem;
    ssl_certificate_key /etc/ssl/private/your-key.pem;

    location / {
        proxy_pass http://127.0.0.1:5000;

        # Public hostname the browser used. ProxyFix rewrites
        # request.host from this so cookies scope to the public
        # hostname instead of 127.0.0.1.
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-Host  $host;

        # Scheme the browser used. ProxyFix rewrites request.scheme
        # and request.is_secure from this so the Secure cookie flag
        # and HSTS header emit correctly.
        proxy_set_header X-Forwarded-Proto $scheme;

        # Real client IP for audit logs and rate limiting.
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    }
}
```

#### Caddy

Caddy's `reverse_proxy` directive sets all the required `X-Forwarded-*` headers automatically:

```caddy
sentry.yourcompany.com {
    reverse_proxy 127.0.0.1:5000
}
```

#### Traefik (v2+)

Traefik sets the forwarded headers by default for any service reached through a router. A minimum dynamic-config snippet:

```yaml
http:
  routers:
    sentry:
      rule: "Host(`sentry.yourcompany.com`)"
      entryPoints: [websecure]
      tls: {}
      service: sentry-api
  services:
    sentry-api:
      loadBalancer:
        servers:
          - url: "http://127.0.0.1:5000"
```

#### AWS ALB and other TLS-terminating load balancers

ALB, GCP HTTPS Load Balancer, Azure Application Gateway, Cloudflare Tunnels, Fly.io, Render, and most other managed edges all send `X-Forwarded-Proto` and `X-Forwarded-For`. `TRUST_PROXY=true` works the same way for all of them.

#### Multi-hop deployments (CDN in front of a proxy)

The default ProxyFix config trusts **one** proxy hop. When Sentry sits behind multiple TLS-terminating proxies (e.g. Cloudflare CDN -> nginx -> Sentry, or ALB -> nginx -> Sentry), increase the hop count in `api/app.py`:

```python
app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=2,    # one entry each from CDN and nginx
    x_proto=2,
    x_host=2,
    x_prefix=0,
)
```

The hop count must match the number of trusted proxies in the chain exactly. Over-counting accepts forged headers from the innermost proxy's client (a request originator can prepend fake `X-Forwarded-*` entries that a too-permissive ProxyFix will trust); under-counting scopes cookies to the wrong hop. Upstream Werkzeug documents this at <https://werkzeug.palletsprojects.com/en/latest/middleware/proxy_fix/>.

---

## Mobile App

### Sideloading the APK

Download the APK from the [GitHub Releases](https://github.com/hightower-systems/sentry-wms/releases) page.

Install via ADB:

```bash
adb install sentry-wms-v1.4.5.apk
```

Or transfer the APK to the device and open it from the file manager.

### First Launch

On first launch, the app prompts for the API server URL:

1. Enter your server's IP and port (e.g., `http://10.0.0.150:5000`)
2. The app runs a health check before accepting the URL
3. Log in with your admin credentials
4. Select a warehouse (shown as a blocking modal after login)

The server URL can be changed later from Settings in the user dropdown menu.

### Broadcast Intent Scanning (Chainway C6000)

For hardware scanners that use Android broadcast intents instead of keyboard wedge:

1. Open Settings from the user dropdown
2. Switch scan mode from KEYBOARD to INTENT
3. Configure the intent action and extra key for your device

Default values for Chainway C6000:

- Intent action: `com.android.scanner.BARCODE_READ`
- Extra key: `barcode_string`

### Expo Go (Development)

For development testing without building an APK:

```bash
cd mobile
npm install
npx expo start --clear
```

Scan the QR code with Expo Go on your device. Set the API URL to your dev machine's IP.
