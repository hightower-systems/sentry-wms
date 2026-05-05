#!/bin/bash
# v1.7.0: load schema.sql into the sentry_test database created by
# 00-create-test-db.sql. The conftest TRUNCATEs tables at session
# start; without the schema there are no tables to wipe.
#
# Runs once during postgres image first-init via
# /docker-entrypoint-initdb.d/. Idempotent on a single first-init
# run; existing volumes need `docker compose down -v` to re-trigger.

set -e

psql -U "$POSTGRES_USER" -d sentry_test -f /seed-data/schema-for-test-db.sql
echo "v1.7.0 test-db init: schema.sql loaded into sentry_test"
