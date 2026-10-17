#!/bin/sh
# Start script for Render (Dockerfile.render).
set -eu

# Render provides these; the app reads its own names.
export PRUFLO_PUBLIC_BASE_URL="${PRUFLO_PUBLIC_BASE_URL:-${RENDER_EXTERNAL_URL:-}}"
export PRUFLO_RELEASE="${PRUFLO_RELEASE:-${RENDER_GIT_COMMIT:-dev}}"

# One instance on the free plan and no pre-deploy step there, so migrations
# run here, as the migration role (PRUFLO_MIGRATION_DATABASE_URL), before the
# API starts. Alembic takes a lock, so a restart mid-migration is safe.
if [ "${PRUFLO_RUN_MIGRATIONS:-true}" = "true" ]; then
  alembic upgrade head
fi

# --no-access-log: app.middleware writes one structured line per request,
# and uvicorn's access log would include query strings (signed links).
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}" --no-access-log \
  --proxy-headers --forwarded-allow-ips '*'
