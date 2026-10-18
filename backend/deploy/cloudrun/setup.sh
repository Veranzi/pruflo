#!/usr/bin/env bash
# Deploys the PruFlo API to Cloud Run across two projects:
#   RUN_PROJECT  (billed): Cloud Run, Cloud Build, Secret Manager,
#                Cloud Scheduler and Gemini on Vertex AI.
#   DATA_PROJECT (Firebase, free Spark plan): Firebase Auth, Firestore and the
#                Cloud SQL instance (SQL Connect trial). Linking billing to it
#                would turn the trial database into a paid one, so it stays free.
# The Cloud Run identity is granted only what it needs in DATA_PROJECT.
#
# It never creates or changes the database server, the database or its
# roles' passwords. Those already exist (Docs/deployment.md, step 2); their
# passwords go into Secret Manager once (Docs/deployment.md, step 5).
#
# Safe to run again. Secret values are never printed or written to disk.
#
# Usage (Git Bash, from backend/, signed in with `gcloud auth login`):
#   ADMIN_EMAIL=you@example.com ./deploy/cloudrun/setup.sh
set -euo pipefail
# Never stop to ask a question (e.g. "enable this API?"): fail and say why.
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

RUN_PROJECT="${RUN_PROJECT:-gen-lang-client-0422375595}"
DATA_PROJECT="${DATA_PROJECT:-login-register-125ef}"
PROJECT="$RUN_PROJECT"  # where everything this script creates lives
# Same region as the Cloud SQL instance; Firestore (nam5) is in the US too.
REGION="${REGION:-us-east4}"
SERVICE="${SERVICE:-pruflo-api}"
SQL_INSTANCE="${SQL_INSTANCE:-login-register-125ef-instance}"
DB_NAME="${DB_NAME:-pruflo}"
REPO="${REPO:-pruflo}"
SA_NAME="${SA_NAME:-pruflo-api}"
TENANT_NAME="${TENANT_NAME:-PruFlo Demo}"
ADMIN_EMAIL="${ADMIN_EMAIL:-}"
ADMIN_NAME="${ADMIN_NAME:-}"

SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
RELEASE="$(git rev-parse --short HEAD 2>/dev/null || echo manual)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/api:${RELEASE}"

step() { printf '\n== %s\n' "$*"; }
exists() { "$@" >/dev/null 2>&1; }
rand_hex() { python -c "import secrets; print(secrets.token_hex(24))"; }
fernet_key() { python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"; }
has_secret() { exists gcloud secrets describe "$1" --project "$PROJECT"; }
secret() { gcloud secrets versions access latest --secret "$1" --project "$PROJECT"; }

# A secret someone must provide (an existing password). Stops with how to add it.
need_secret() {
  if ! has_secret "$1"; then
    cat <<EOF

Missing secret $1: $2
Add it without it appearing in your shell history:
  read -rs V && printf '%s' "\$V" | gcloud secrets create $1 --project $PROJECT \\
    --replication-policy=user-managed --locations=$REGION --data-file=- && unset V
Then run this script again.
EOF
    exit 1
  fi
}
# A secret the script may generate the first time (nothing depends on it yet).
ensure_secret() {
  if ! has_secret "$1"; then
    "$2" | tr -d '\r\n' | gcloud secrets create "$1" --project "$PROJECT" \
      --replication-policy=user-managed --locations="$REGION" --data-file=- >/dev/null
    echo "generated secret $1"
  fi
}
# A secret computed from others (connection URLs); updated only if it changed.
put_secret() {
  local name="$1" value="$2" current=""
  if ! has_secret "$name"; then
    gcloud secrets create "$name" --project "$PROJECT" \
      --replication-policy=user-managed --locations="$REGION" >/dev/null
  fi
  current="$(secret "$name" 2>/dev/null || true)"
  if [ "$current" != "$value" ]; then
    printf '%s' "$value" | gcloud secrets versions add "$name" --project "$PROJECT" --data-file=- >/dev/null
    echo "updated secret $name"
  fi
}

step "Cloud Run in $RUN_PROJECT ($REGION); data in $DATA_PROJECT"
gcloud config set project "$PROJECT" >/dev/null
if [ "$(gcloud billing projects describe "$PROJECT" --format='value(billingEnabled)')" != "True" ]; then
  echo "Billing isn't enabled on $PROJECT; Cloud Run needs it, even within its free tier."
  exit 1
fi

step "Enabling APIs"
gcloud services enable --project "$PROJECT" \
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  sqladmin.googleapis.com secretmanager.googleapis.com firestore.googleapis.com \
  aiplatform.googleapis.com cloudscheduler.googleapis.com iam.googleapis.com \
  identitytoolkit.googleapis.com sql-component.googleapis.com
# The Cloud SQL connector, Firestore and Firebase Auth calls also need their
# APIs on in the Firebase project (free APIs; no billing needed there).
gcloud services enable --project "$DATA_PROJECT" \
  sqladmin.googleapis.com firestore.googleapis.com identitytoolkit.googleapis.com

# A newly enabled API can refuse calls for a few minutes; wait until it answers.
for i in $(seq 1 18); do
  if gcloud secrets list --project "$PROJECT" --limit 1 >/dev/null 2>&1 \
     && gcloud run services list --project "$PROJECT" --region "$REGION" >/dev/null 2>&1; then
    break
  fi
  [ "$i" = 18 ] && { echo "APIs still not answering after 3 minutes; run the script again shortly."; exit 1; }
  echo "waiting for the newly enabled APIs..."; sleep 10
done

step "Existing Cloud SQL instance and Firestore"
CONN="$(gcloud sql instances describe "$SQL_INSTANCE" --project "$DATA_PROJECT" --format='value(connectionName)')"
SQL_REGION="$(gcloud sql instances describe "$SQL_INSTANCE" --project "$DATA_PROJECT" --format='value(region)')"
gcloud sql databases describe "$DB_NAME" --instance "$SQL_INSTANCE" --project "$DATA_PROJECT" >/dev/null
echo "Cloud SQL $CONN, database $DB_NAME"
echo "Firestore $(gcloud firestore databases describe --database='(default)' --project "$DATA_PROJECT" --format='value(locationId)')"
[ "$SQL_REGION" = "$REGION" ] || echo "Note: Cloud Run ($REGION) and Cloud SQL ($SQL_REGION) are in different regions."

step "Secrets"
need_secret pruflo-db-migrator-password "the password of the existing database role pruflo_migrator"
need_secret pruflo-db-runtime-password "the password of the existing database role pruflo_app"
# Encrypts stored webhook secrets and connector credentials. Generated once; if
# it's ever replaced, values encrypted with the old key can't be read.
ensure_secret pruflo-secrets-key fernet_key
ensure_secret pruflo-storage-signing-key rand_hex
ensure_secret pruflo-tick-token rand_hex

SOCKET="/cloudsql/${CONN}"
# Builds a connection URL from a role and its password (read from Secret
# Manager at run time; nothing is stored in this file).
url() {
  local login="$1:$2"
  echo "postgresql+psycopg://${login}@/${DB_NAME}?host=${SOCKET}"
}
put_secret pruflo-db-migration-url "$(url pruflo_migrator "$(secret pruflo-db-migrator-password)")"
put_secret pruflo-db-url "$(url pruflo_app "$(secret pruflo-db-runtime-password)")"

step "Service account $SA"
if ! exists gcloud iam service-accounts describe "$SA" --project "$PROJECT"; then
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" \
    --display-name "PruFlo API (Cloud Run)"
fi
# Here: Vertex Gemini, its secrets, logs, and calling APIs billed to this project.
for role in roles/aiplatform.user roles/secretmanager.secretAccessor \
            roles/logging.logWriter roles/serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" \
    --role "$role" --condition=None >/dev/null
done
# In the Firebase project: the database socket, Firestore, and reading sign-in
# accounts (session-revocation checks). No key file is ever created.
for role in roles/cloudsql.client roles/datastore.user roles/firebaseauth.viewer; do
  gcloud projects add-iam-policy-binding "$DATA_PROJECT" --member "serviceAccount:$SA" \
    --role "$role" --condition=None >/dev/null
done
echo "roles granted"

step "Building the image with Cloud Build"
if ! exists gcloud artifacts repositories describe "$REPO" --location "$REGION" --project "$PROJECT"; then
  gcloud artifacts repositories create "$REPO" --repository-format=docker \
    --location "$REGION" --project "$PROJECT"
fi
# New projects build as the Compute Engine default account, which may lack
# permission to read the upload and push the image.
BUILD_SA="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')-compute@developer.gserviceaccount.com"
for role in roles/cloudbuild.builds.builder roles/artifactregistry.writer roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$BUILD_SA" \
    --role "$role" --condition=None >/dev/null
done
gcloud builds submit . --project "$PROJECT" --region "$REGION" \
  --config deploy/cloudrun/cloudbuild.yaml --substitutions "_IMAGE=$IMAGE"

COMMON=(--project "$PROJECT" --region "$REGION" --image "$IMAGE"
        --service-account "$SA" --set-cloudsql-instances "$CONN")

step "Migrations (Cloud Run job pruflo-db-migrate)"
MIGRATE="alembic upgrade head"
MIGRATE_SECRETS="PRUFLO_MIGRATION_DATABASE_URL=pruflo-db-migration-url:latest,PRUFLO_DATABASE_URL=pruflo-db-url:latest"
# Grants on new tables need the admin role; only if its password was provided.
if has_secret pruflo-db-admin-password; then
  put_secret pruflo-db-admin-url "$(url postgres "$(secret pruflo-db-admin-password)")"
  MIGRATE="$MIGRATE && python -m app.cli db-roles --no-create"
  MIGRATE_SECRETS+=",PRUFLO_ADMIN_DATABASE_URL=pruflo-db-admin-url:latest"
fi
gcloud run jobs deploy pruflo-db-migrate "${COMMON[@]}" \
  --command sh --args=-c,"$MIGRATE" --set-env-vars PRUFLO_ENV=prod \
  --set-secrets "$MIGRATE_SECRETS" --max-retries 0 --task-timeout 600 >/dev/null
gcloud run jobs execute pruflo-db-migrate --project "$PROJECT" --region "$REGION" --wait

step "API service $SERVICE"
# Gemini (Vertex) runs in this project; sign-in and Firestore are the Firebase project's.
ENV_VARS="PRUFLO_ENV=prod,PRUFLO_REGION=${REGION},PRUFLO_GCP_PROJECT=${PROJECT}"
ENV_VARS+=",PRUFLO_FIREBASE_PROJECT=${DATA_PROJECT},PRUFLO_RELEASE=${RELEASE}"
ENV_VARS+=",PRUFLO_STORAGE_BACKEND=firestore,PRUFLO_CONTENT_BACKEND=firestore"
ENV_VARS+=",PRUFLO_QUEUE_BACKEND=inline,PRUFLO_GEMINI_ENABLED=true"
ENV_VARS+=",PRUFLO_APP_CHECK_REQUIRED=false,PRUFLO_RUN_MIGRATIONS=false"
# The trial instance allows few connections: keep the pool small.
ENV_VARS+=",PRUFLO_DB_POOL_SIZE=2,PRUFLO_DB_MAX_OVERFLOW=2"
SECRETS="PRUFLO_DATABASE_URL=pruflo-db-url:latest,PRUFLO_SECRETS_KEY=pruflo-secrets-key:latest"
SECRETS+=",PRUFLO_STORAGE_SIGNING_KEY=pruflo-storage-signing-key:latest,PRUFLO_TICK_TOKEN=pruflo-tick-token:latest"
# Optional: a Gemini API key, for organisations set to the gemini_api provider.
has_secret pruflo-gemini-api-key && SECRETS+=",PRUFLO_GEMINI_API_KEY=pruflo-gemini-api-key:latest"

# --no-cpu-throttling: files are processed in this service right after the
# upload request returns, so the CPU must stay on between requests. With
# --min-instances 0 it still scales to zero when nobody is using it.
gcloud run deploy "$SERVICE" "${COMMON[@]}" \
  --allow-unauthenticated --port 8080 \
  --cpu 1 --memory 1Gi --no-cpu-throttling --min-instances 0 --max-instances 2 \
  --concurrency 20 --timeout 300 \
  --set-env-vars "$ENV_VARS" --set-secrets "$SECRETS"

URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.url)')"
# Upload and view links point back at this service.
gcloud run services update "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --update-env-vars "PRUFLO_PUBLIC_BASE_URL=${URL}" >/dev/null

# Every 6 hours: each call starts an instance, which then stays up (and is
# billed) for up to ~15 minutes. Uploads don't wait for it; it only runs
# retries, imports, retention and the Monday summary.
step "Scheduled work (Cloud Scheduler, every 6 hours)"
TICK_HEADERS="X-Pruflo-Tick-Token=$(secret pruflo-tick-token)"
if exists gcloud scheduler jobs describe pruflo-tick --location "$REGION" --project "$PROJECT"; then
  gcloud scheduler jobs update http pruflo-tick --location "$REGION" --project "$PROJECT" \
    --schedule "0 */6 * * *" --uri "${URL}/v1/internal/tick" --http-method POST \
    --update-headers "$TICK_HEADERS" >/dev/null
else
  gcloud scheduler jobs create http pruflo-tick --location "$REGION" --project "$PROJECT" \
    --schedule "0 */6 * * *" --uri "${URL}/v1/internal/tick" --http-method POST \
    --headers "$TICK_HEADERS" >/dev/null
fi
echo "pruflo-tick scheduled"

if [ -n "$ADMIN_EMAIL" ]; then
  step "First organisation and admin ($ADMIN_EMAIL)"
  gcloud run jobs deploy pruflo-cli "${COMMON[@]}" \
    --command python --args=-m,app.cli,bootstrap,--tenant,"$TENANT_NAME",--admin-email,"$ADMIN_EMAIL",--admin-name,"$ADMIN_NAME" \
    --set-env-vars PRUFLO_ENV=prod --set-secrets "PRUFLO_DATABASE_URL=pruflo-db-url:latest" \
    --max-retries 0 --task-timeout 120 >/dev/null
  gcloud run jobs execute pruflo-cli --project "$PROJECT" --region "$REGION" --wait \
    || echo "Bootstrap failed; usually the organisation already exists (see the job's logs)."
fi

step "Checking the deployment"
# Cloud Run reserves /healthz (Google answers 404), so check /readyz.
curl -fsS "${URL}/readyz" && echo

cat <<EOF

Done. The API is at:
  ${URL}

Next (Docs/deployment.md, steps 7 and 8):
  1. Set the GitHub repository variable API_BASE_URL to ${URL}
  2. Merge to main so pruflo.web.app is rebuilt against this API.
  3. python ../scripts/check_submission.py --api ${URL}
EOF
