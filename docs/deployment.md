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
docker compose up -d
```

This starts three containers:

- **sentry-db** - PostgreSQL 16 on port 5432 (bound to localhost only)
- **sentry-api** - Flask API on port 5000
- **sentry-admin** - React admin panel on port 3000

### Finding the Admin Password

On first run, the seed script generates a random admin password:

```bash
docker compose logs api | grep "Admin password"
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

307 tests using transaction rollback isolation. Runs in about 13 seconds.

---

## Production

### Required Environment Variables

Both `JWT_SECRET` and `DATABASE_URL` are required. The app raises `RuntimeError` on startup if either is missing.

```bash
# Generate a secret
openssl rand -hex 32

# .env
JWT_SECRET=your-generated-secret-here
DATABASE_URL=postgresql://user:pass@db:5432/sentry
POSTGRES_USER=your-db-user
POSTGRES_PASSWORD=your-db-password
CORS_ORIGINS=https://your-admin-domain.com
```

### Production Docker Compose

Use `docker-compose.prod.yml` which has no source volume mounts:

```bash
docker compose -f docker-compose.prod.yml up -d
```

Key differences from the dev compose:

- No `./api:/app` or `./db:/db` volume mounts
- `SKIP_SEED=true` by default
- All credentials required via env vars (no defaults)
- `FLASK_ENV=production` hardcoded

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
adb install sentry-wms-v1.2.0.apk
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
