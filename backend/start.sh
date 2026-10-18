#!/bin/sh
# Start script for the Cloud Run container (Dockerfile.cloudrun).
set -eu

# On Cloud Run, migrations run before each deploy in the Cloud Run job
# pruflo-db-migrate (deploy/cloudrun/setup.sh), which sets this to false for
# the service. Set PRUFLO_RUN_MIGRATIONS=true to run them here instead, as the
# migration role (PRUFLO_MIGRATION_DATABASE_URL); Alembic takes a lock.
if [ "${PRUFLO_RUN_MIGRATIONS:-false}" = "true" ]; then
  alembic upgrade head
fi

# --no-access-log: app.middleware writes one structured line per request,
# and uvicorn's access log would include query strings (signed links).
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}" --no-access-log \
  --proxy-headers --forwarded-allow-ips '*'
