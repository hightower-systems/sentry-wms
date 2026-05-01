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

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# v1.4.2 #73: bake the source version into the image so the app can detect
# an upgrade-without-rebuild at startup (code pulled but image not rebuilt
# leaves stale dependencies -- e.g., flask_limiter missing after v1.3->v1.4).
RUN grep -oP '(?<=__version__ = ")[^"]+' /app/version.py > /app/BUILD_VERSION

RUN adduser --disabled-password --no-create-home appuser
USER appuser

EXPOSE 5000

CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:create_app()"]
