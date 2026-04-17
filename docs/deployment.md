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

### Finding the Admin Password

On first run, the seed script generates a random admin password:

```bash
docker compose logs db | grep "Admin password"
```

Set `ADMIN_PASSWORD` in your `.env` to override the auto-generated password.

### Demo Data

The default seed includes 1 warehouse, 6 zones, 16 bins, 20 items, 5 POs, and 20 SOs for testing. To start with a clean system:

```bash
SKIP_SEED=true docker compose up -d
```

### Running Tests

```bash
docker compose exec api python -m pytest tests/ -x -q
```

570 backend tests using transaction-rollback isolation. Runs in about 18 seconds.
24 of those are infrastructure-config assertions and correctly skip when the
suite runs inside the api container; run on the host (`python -m pytest tests/`)
to get full coverage.

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

Before running v1.3.0 against an existing v1.2 database, apply migration
`db/migrations/016_audit_log_tamper_resistance.sql`. It adds the
`prev_hash` / `row_hash` columns on `audit_log`, installs the hash-chain
trigger and the `BEFORE UPDATE / BEFORE DELETE` guards, and exposes
`verify_audit_log_chain()` for periodic integrity checks.

### Infrastructure Notes

- PostgreSQL port is bound to `127.0.0.1:5432` only (not exposed to the network)
- API runs Gunicorn with 4 workers (not the Flask dev server)
- Container runs as non-root user `appuser`
- `debug=False` is hardcoded in `app.py`

### Reverse Proxy (HTTPS)

The API serves HTTP only. For HTTPS, put a reverse proxy in front:

```
Mobile app --> HTTPS --> nginx/Caddy --> HTTP --> gunicorn:5000
```

A minimal nginx config:

```nginx
server {
    listen 443 ssl;
    server_name api.yourcompany.com;

    ssl_certificate /etc/ssl/certs/your-cert.pem;
    ssl_certificate_key /etc/ssl/private/your-key.pem;

    location / {
        proxy_pass http://localhost:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

## Mobile App

### Sideloading the APK

Download the APK from the [GitHub Releases](https://github.com/hightower-systems/sentry-wms/releases) page.

Install via ADB:

```bash
adb install sentry-wms-v1.3.0.apk
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
