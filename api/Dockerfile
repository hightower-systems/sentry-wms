FROM python:3.11-slim

# v1.6.0 #171: postgresql-client provides the psql binary required by
# api/tests/test_role_creation_scripts.py (operator-path coverage for
# db/role-*.sql). CI runs pytest on the ubuntu-latest runner where
# psql is provided by the postgres:16 service container; this line
# closes the parity gap so `docker exec sentry-api pytest` exercises
# the same code path locally instead of silently skipping. The test
# itself loud-fails at collection time when psql is missing, so a
# future refactor that drops this line will surface as a test
# failure rather than a silent regression.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Keep packaging tools current enough to clear advisories inherited from the
# Python base image. These are not application runtime dependencies.
RUN python -m pip install --no-cache-dir --upgrade \
    pip==26.2.2 \
    setuptools==84.2.1

# Build context is the repo root (so we can COPY db/mappings alongside
# the api source). The repo-root .dockerignore strips admin/, mobile/,
# docs/, .git, etc. so the context stays small.
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api/ .

# Bake mapping documents (db/mappings/<source>.yaml) into the image so
# Pipe B inbound has its schema available at /app/db/mappings without a
# runtime volume mount. SENTRY_INBOUND_MAPPINGS_DIR=/app/db/mappings in
# managed-platform deploys (ACA, K8s); docker-compose keeps using its
# ./db:/db volume mount and points the env var at /db/mappings instead.
COPY db/mappings /app/db/mappings

# v1.4.2 #73: bake the source version into the image so the app can detect
# an upgrade-without-rebuild at startup (code pulled but image not rebuilt
# leaves stale dependencies -- e.g., flask_limiter missing after v1.3->v1.4).
RUN grep -oP '(?<=__version__ = ")[^"]+' /app/version.py > /app/BUILD_VERSION

RUN adduser --disabled-password --no-create-home appuser
USER appuser

EXPOSE 5000

# --timeout 180: gunicorn's default 30s worker timeout cuts off the
# slowest PO inbound payloads (wide product-line counts) and the
# largest cycle-count approvals (a single bin can carry 300+ varianced
# lines, each applied + event-emitted in one transaction) before the
# write completes. 180s gives the canonical write paths headroom
# without masking a runaway query. nginx proxy_read/send_timeout must
# stay above this so the proxy never severs a still-working backend.
CMD ["gunicorn", "-w", "4", "--timeout", "180", "-b", "0.0.0.0:5000", "app:create_app()"]
