# PruFlo backend

The API and processing worker behind PruFlo. People upload evidence for a case, the worker reads the documents, rules flag discrepancies as findings, and a named reviewer approves or rejects each one. Everything runs on Google Cloud in `asia-southeast1` (Singapore).

The core rule: **code calculates, AI reads documents and drafts wording, and people decide.**

- [Build plan and progress](../Docs/backend_plan.md)
- [API contract, endpoints 1–33](../Docs/API_Contract.md) and [integrations contract, 34–50](../Docs/Integrations_API_Contract.md)
- Interactive API docs: `/docs` on a running API (`local`, `test` and `dev` only)

## Contents

- [Architecture](#architecture)
- [Requirements](#requirements)
- [Run locally](#run-locally)
- [Configuration](#configuration)
- [Authentication and access](#authentication-and-access)
- [Processing pipeline](#processing-pipeline)
- [Operator commands](#operator-commands)
- [Testing and checks](#testing-and-checks)
- [Database migrations](#database-migrations)
- [Deploying to Google Cloud](#deploying-to-google-cloud)
- [Security notes](#security-notes)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)

## Architecture

```
Flutter app ──HTTPS──▶ API (Cloud Run, FastAPI) ──▶ Cloud SQL (PostgreSQL)
     │                    │   └─ outbox ──▶ Pub/Sub ──push──▶ Worker (Cloud Run)
     └─ PUT (signed URL) ─┴──────────────▶ Cloud Storage ◀────────┘   │
                                                       Document AI ◀──┤ (OCR)
                                                       webhooks ◀─────┘ (signed POSTs)
```

| Piece | Code | What it does |
| --- | --- | --- |
| API | `app/main.py`, `app/api/v1/` | Endpoints 1–50: auth, permissions, cases, uploads, review, reports, admin, integrations |
| Worker | `app/worker/main.py` | Receives Pub/Sub pushes: processes evidence, renders reports, delivers webhooks |
| Pipeline | `app/pipeline/` | Text extraction (PDF, Word, Excel, CSV, TXT), OCR adapter, rule templates |
| Outbox | `app/services/outbox.py`, `app/worker/queue.py` | Events are written with the change that caused them, then published, so nothing is lost if a publish fails |

The API and worker are the same codebase in two Cloud Run services with separate service accounts. Locally, the API runs jobs itself right after responding, so no Pub/Sub emulator is needed.

## Requirements

- Python 3.12
- Docker (for the local PostgreSQL)
- For deployment: a GCP project with Cloud Run, Cloud SQL, Cloud Storage, Pub/Sub, Cloud Scheduler, Secret Manager and (optionally) Document AI enabled, and a Firebase project with Authentication

## Run locally

```bash
cd backend
cp .env.example .env                 # set PRUFLO_DB_PASSWORD (openssl rand -hex 16)
docker compose up -d postgres        # PostgreSQL on 127.0.0.1:5433
python3.12 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
alembic upgrade head
python -m app.cli bootstrap --tenant "Acme Ltd" --admin-email you@example.com --admin-name "Your Name"
uvicorn app.main:app --reload --port 8000 --no-access-log
```

Check it's up at http://localhost:8000/healthz, and browse the endpoints at http://localhost:8000/docs.

Locally, sign in by sending `Authorization: Bearer dev:<any-id>:<invited email>` (only accepted when `PRUFLO_ENV=local`):

```bash
curl -H "Authorization: Bearer dev:me1:you@example.com" http://localhost:8000/v1/me
```

The Flutter app does this for you when it's built without Firebase settings. See [`frontend/README.md`](../frontend/README.md).

Uploaded files are stored under `backend/.local-storage/` and served through signed links from the API itself.

## Configuration

All settings are environment variables with the `PRUFLO_` prefix, read from `backend/.env` locally. On Cloud Run, set them on the service and mount secrets from Secret Manager.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PRUFLO_ENV` | `local` | `local`, `test`, `dev`, `staging` or `prod`. Development-only behaviour is refused outside `local`/`test` |
| `PRUFLO_DATABASE_URL` | – | Full SQLAlchemy URL (`postgresql+psycopg://...`) for the **runtime** role. Use on Cloud Run, from Secret Manager |
| `PRUFLO_MIGRATION_DATABASE_URL` | `PRUFLO_DATABASE_URL` | URL for the **migration** role, used only by Alembic (see [Database migrations](#database-migrations)) |
| `PRUFLO_DB_PASSWORD` | – | Local database password; with `PRUFLO_DB_USER`/`HOST`/`PORT`/`NAME` it builds the URL when `PRUFLO_DATABASE_URL` is empty |
| `PRUFLO_GCP_PROJECT` | `pruflo-local` | GCP project for Storage, Pub/Sub and Document AI |
| `PRUFLO_FIREBASE_PROJECT` | `PRUFLO_GCP_PROJECT` | Firebase project whose ID tokens are accepted (the project from `Docs/deployment.md`) |
| `PRUFLO_FIREBASE_CHECK_REVOKED` | `true` | Also check that the session wasn't revoked. Needs the runtime identity to have Firebase Authentication admin access on the Firebase project; set `false` only locally |
| `PRUFLO_REGION` | `asia-southeast1` | Deployment region |
| `PRUFLO_STORAGE_BACKEND` | `local` | `firestore` (files in Firestore, free Firebase plan), `gcs` (Cloud Storage, needs billing) or `local` (disk, local/test only) |
| `PRUFLO_STORAGE_SIGNING_KEY` | – | Signs the API's upload and view links (`firestore` and `local`). Required outside `local`/`test` |
| `PRUFLO_FIREBASE_CREDENTIALS` | – | The Firebase service-account key as JSON text (or `PRUFLO_FIREBASE_CREDENTIALS_FILE` with a path). Needed outside Google Cloud for sign-in checks, push and Firestore |
| `PRUFLO_GEMINI_API_KEY` | – | Gemini API key for organisations on the `gemini_api` provider. A free-tier key is for synthetic documents only |
| `PRUFLO_TICK_TOKEN` | – | Turns on `POST /v1/internal/tick` (scheduled work without a worker service) |
| `PRUFLO_EVIDENCE_BUCKET` | `pruflo-evidence-local` | Private bucket for evidence and reports |
| `PRUFLO_SIGNED_URL_MINUTES` | `10` | Lifetime of upload, view and download links |
| `PRUFLO_SIGNING_SERVICE_ACCOUNT` | – | Sign URLs through IAM signBlob as this account when the credentials can't sign (e.g. a gcloud login running `check-storage`). Not needed on Cloud Run |
| `PRUFLO_PUBLIC_BASE_URL` | `http://localhost:8000` | Base of local storage links (local only) |
| `PRUFLO_PUBLIC_APP_URL` | `https://pruflo.web.app` | The web app; push notifications link into it |
| `PRUFLO_QUEUE_BACKEND` | `inline` | `pubsub` on Cloud Run; `inline` runs jobs in the API process |
| `PRUFLO_PUBSUB_TOPIC` | `evidence-jobs` | Topic the outbox publishes to |
| `PRUFLO_WORKER_PUSH_AUDIENCE` | – | Worker URL; OIDC tokens must be issued for it |
| `PRUFLO_WORKER_INVOKER_SERVICE_ACCOUNTS` | `[]` | JSON list of service accounts allowed to call the worker (Pub/Sub push, Cloud Scheduler) |
| `PRUFLO_DOCUMENTAI_LOCATION` | – | Document AI location. A location outside the region sends documents abroad; get sign-off first |
| `PRUFLO_DOCUMENTAI_PROCESSOR_ID` | – | OCR processor. Without it, scans and images are marked `needs_review` |
| `PRUFLO_GEMINI_ENABLED` | `false` | Read documents with Gemini on Vertex AI (the tenant's model and region from endpoint 49). Off: the structured reader. Needs Vertex AI User for the worker |
| `PRUFLO_GEMINI_EXPLANATIONS` | `false` | Also let Gemini reword finding explanations (`machine_generated`) |
| `PRUFLO_DATES_DAY_FIRST` | `true` | Read 03/10/2026 as 3 October |
| `PRUFLO_EVIDENCE_RETENTION_DAYS`, `PRUFLO_REPORT_RETENTION_DAYS` | `0` | Delete evidence files (of completed cases) and reports after this many days; 0 keeps them |
| `PRUFLO_DB_POOL_SIZE`, `PRUFLO_DB_MAX_OVERFLOW` | `5`, `5` | Connections per instance. Keep (pool + overflow) × (API + worker max instances) under the Cloud SQL limit |
| `PRUFLO_RELEASE` | `dev` | The deployed commit, shown by `/healthz` |
| `PRUFLO_SECRETS_KEY` | – | Fernet key that encrypts webhook signing secrets and document-source credentials, and signs OAuth state. Required outside `local`/`test` |
| `PRUFLO_INBOUND_EMAIL_DOMAIN` | – | Domain of the email inbox addresses (`evidence+<tag>@<domain>`). With the token, turns the email inbox source on |
| `PRUFLO_INBOUND_EMAIL_TOKEN` | – | Shared secret the mail service sends to `/v1/inbound/email` (header `X-Pruflo-Inbound-Token` or `?token=`) |
| `PRUFLO_INBOUND_AUTHSERV_ID` | – | The mail service's name in `Authentication-Results` (e.g. `mx.sendgrid.net`); when set, only that header is trusted |
| `PRUFLO_GOOGLE_OAUTH_CLIENT_ID`, `_SECRET` | – | OAuth web client for Google Drive (scope `drive.readonly`). Turns the Drive source on |
| `PRUFLO_MICROSOFT_OAUTH_CLIENT_ID`, `_SECRET` | – | Entra ID app for SharePoint (`Sites.Selected`, `offline_access`). Turns the SharePoint source on |
| `PRUFLO_CONNECTOR_SYNC_MINUTES` | `15` | How often polled sources (Odoo, Drive, SharePoint) are synced |
| `PRUFLO_CORS_ORIGINS` | `["https://pruflo.web.app","https://pruflo.firebaseapp.com"]` | Browser origins allowed to call the API; any localhost port is also allowed in `local`/`test` |
| `PRUFLO_AUTH_DEV_MODE` | `false` | Accept `dev:` tokens. Ignored outside `local`/`test` |
| `PRUFLO_APP_CHECK_REQUIRED` | `true` | Require a Firebase App Check token from the app. Skipped in `local`/`test` |

Limits shared with the app: 25 MB per file, 200 PDF pages, 20 uploads in progress per case (`app/config.py`).

## Authentication and access

- **People** sign in with Firebase. The API verifies the ID token (with revocation) and the App Check token, then loads their membership. Access is invite-only: an admin invites an email address, and the person's Firebase account is linked on first sign-in with that verified address. Anyone else gets `403 not_invited`.
- **Other systems** use API keys (`Authorization: Bearer pf_live_...`), created by admins with scopes. Keys are stored as SHA-256 hashes and rate-limited to 600 requests a minute per instance.

| Action | investigator | reviewer | admin | viewer | API key |
| --- | --- | --- | --- | --- | --- |
| View cases, evidence and findings | ✓ | ✓ | ✓ | ✓ | `cases:read` |
| Create and edit cases, upload evidence | ✓ | | ✓ | | `cases:write`, `evidence:write` |
| Decide on findings | | ✓ | ✓ | | never |
| Reports and export | ✓ | ✓ | ✓ | | `reports:read`, `findings:read` |
| Admin and integrations | | | ✓ | | never |

People only see the cases they're members of (admins see all cases in their organisation). A case you can't see returns `404`.

## Processing pipeline

1. **Upload** (endpoints 7, 8): the client PUTs the file to a signed URL. On `complete`, the API checks the real file type from its content, the size and the PDF page count, computes the SHA-256, and writes a job plus an outbox event in one transaction.
2. **Extract** (worker): PDF text layer as lines with page positions, Word paragraphs and tables, Excel sheets with cell references, CSV and TXT. Pages without a text layer, and images, go to Document AI OCR if configured.
3. **Retry and failure**: jobs are claimed with a lease and are idempotent, so a redelivered message changes nothing. Unreadable files fail at once; temporary errors retry up to 5 times.
4. **Claims** (Step 9, `app/pipeline/claims/`): an extractor proposes values with the line they came from; code checks them against the schema and parses numbers, dates (day first) and currencies. With `PRUFLO_GEMINI_ENABLED`, Gemini on Vertex AI reads the document (strict JSON schema, no tools, the tenant's model and region). Otherwise the structured reader takes the document type, labelled fields ("PO No:", "Delivery Date:", "Grand Total:") and line-item tables from PDF, Word, Excel and CSV.
   - **Grounding** (Step 10): each quote is found again on its page, ignoring only whitespace and line wraps. One match is `grounded` (span and box saved), several are `ambiguous`, none is `ungrounded`, and a bare number without its label is `ambiguous`. Only grounded claims reach the rules.
   - **Rules** (Step 11): when a case's last file is done, the four templates run over its documents, grouped by purchase order: `quantity_shortfall`, `amount_mismatch`, `delivery_date_window` and `missing_document`. Arithmetic is Decimal; different units or currencies give a `data_quality` finding instead. Unreviewed findings the rules no longer produce are removed. The case moves to `in_review` or `completed`.
   - **Explanations** (Step 12): fixed templates; an optional Gemini rewrite (`PRUFLO_GEMINI_EXPLANATIONS`) is kept only if its numbers match.
5. **Review** (15): a reviewer approves, rejects (note required) or sets a finding back to pending. The case becomes `completed` when nothing is pending.
6. **Reports** (17–19): the worker renders a PDF from stored data only.
7. **Webhooks** (37–42): events are signed with HMAC-SHA256 and retried after 1 min, 5 min, 30 min, 2 h, 6 h and 12 h. An endpoint failing for 3 days is paused.
8. **Notifications** (51–53): the following create an in-app notification for the people concerned:
   - evidence finished processing (for the uploader);
   - a case assigned to someone;
   - a case ready for review (for reviewers and admins on the case);
   - a report ready (for whoever requested it);
   - an optional Monday summary.

   Each person switches kinds on or off in their profile (`PATCH /v1/me`). If push is on and they've registered a device (2), the worker sends it with Firebase Cloud Messaging and removes tokens FCM reports as dead. Notifications are kept 90 days.
9. **Case Q&A** (20, optional): Gemini answers from the case's findings, claims and timeline through three read-only tools; citations are checked, and an answer without one is withheld. Returns `503 assistant_unavailable` until Gemini is enabled.
10. **Profile photos** (54–56): uploaded like evidence, then re-encoded server-side as a JPEG of at most 512 px. This strips metadata such as GPS location.
11. **Document sources** (43–45, `app/connectors/`): imported files go through `services.evidence.import_file`, the same types, limits, checksum and queue as an upload; each source's file ID is stored so nothing is imported twice. A source is listed as `coming_soon` until this deployment has its settings.
    - **Email inbox**: the mail service posts each message to `POST /v1/inbound/email`. The sender must pass SPF or DKIM aligned with the From domain (or DMARC) in the topmost `Authentication-Results` header and be a member who may upload. `[PL-001]` in the subject adds to that case; otherwise a new case is opened.
    - **Odoo**: login checked on save (JSON-RPC, read-only user). Attachments on purchase orders, receipts and vendor bills added after connecting are imported, grouped by purchase order into one case.
    - **Google Drive / SharePoint**: OAuth consent, then each subfolder of the connected folder becomes a case; loose files open their own case unless their name mentions a case reference. A new version of a file comes in as new evidence.

## Operator commands

```bash
python -m app.cli bootstrap --tenant "Acme Ltd" --admin-email admin@acme.com --admin-name "Ada"
python -m app.cli invite --tenant ten_... --email someone@acme.com --role reviewer
python -m app.cli remove-user --email someone@acme.com   # keeps the audit trail, clears the email
python -m app.worker drain                               # process anything waiting, now
python -m app.worker weekly-summary                      # send this week's summaries now
python -m app.worker sync-connectors                     # import from Odoo, Drive, SharePoint now
python -m app.worker reconcile [--case case_...]         # run the rules for waiting cases
python -m app.worker retention                           # delete files past their retention period
python -m app.loadtest --api URL --token T --case ID     # uploads at 200 pages / 25 MB, with timings
python -m app.cli test-push --email someone@acme.com     # real FCM push to their browsers
python -m app.cli check-storage                          # real bucket: region, privacy, signed URLs, size limit
python -m app.cli db-roles                               # migration and runtime roles (see below)
```

## Testing and checks

```bash
docker compose up -d               # tests use a separate pruflo_test database, recreated each run
ruff check . && ruff format --check .
mypy app
pytest
```

The same checks run in GitHub Actions (`.github/workflows/backend-ci.yml`) on pull requests that touch `backend/`. `tests/test_gcp_emulators.py` runs the upload flow through the real Google client libraries against the fake GCS server and the Pub/Sub emulator; it's skipped locally if they aren't running, and required in CI.

## Database migrations

After changing `app/db/models.py`:

```bash
alembic revision --autogenerate -m "describe the change"
alembic check                       # models and migrations agree
```

Review the generated file before committing. Run migrations as a release step with the migration identity, never at app startup. `review_actions` and `audit_events` are append-only: a database trigger rejects updates and deletes.

### Database roles

Two roles keep the app away from the schema (`app/db/roles.py`):

- **`pruflo_migrator`** owns the schema and is the only one that runs migrations (`PRUFLO_MIGRATION_DATABASE_URL`).
- **`pruflo_app`** is what the API and worker log in as (`PRUFLO_DATABASE_URL`). It can read and write data, but can't create, alter or drop tables, can't truncate, and can only insert into `review_actions` and `audit_events`. Not being the owner, it can't switch their triggers off either.

Set them up once per database, as an admin, after the first migration:

```bash
# Local: creates both roles
PRUFLO_ADMIN_DATABASE_URL=postgresql+psycopg://pruflo:...@localhost:5433/pruflo \
PRUFLO_MIGRATOR_PASSWORD=... PRUFLO_RUNTIME_PASSWORD=... python -m app.cli db-roles

# Cloud SQL: create the users first, then run it as the postgres user
gcloud sql users create pruflo_migrator --instance=pruflo --password=...
gcloud sql users create pruflo_app --instance=pruflo --password=...
PRUFLO_ADMIN_DATABASE_URL=... python -m app.cli db-roles --no-create
```

It's safe to run again; do so after a migration that adds an append-only table. Tables added by later migrations get the runtime grants automatically.

## Deploying for free (Firebase + Render + Supabase)

The current deployment, with no Google Cloud billing (see `Docs/fix_plan.md`):

| Piece | Runs on |
| --- | --- |
| Web app, sign-in, push | Firebase Hosting, Auth and Cloud Messaging (Spark plan) |
| Uploaded files | Firestore in the same Firebase project (`PRUFLO_STORAGE_BACKEND=firestore`) |
| API and worker | One Render free web service (`render.yaml`, `Dockerfile.render`); files are processed in the API process right after upload (`PRUFLO_QUEUE_BACKEND=inline`) |
| Scheduled work | `.github/workflows/tick.yml` calls `POST /v1/internal/tick` every 30 minutes |
| Postgres | Supabase (session pooler URL); `python -m app.cli db-roles` keeps Supabase's API roles away from our tables |
| Document reading | Gemini API key (`gemini_api` provider), or the structured reader without one |

`start-render.sh` runs the migrations as `pruflo_migrator` before starting the API. Firestore's security rules deny all browser access: only the backend's service account reads and writes files.

## Deploying to Google Cloud

For a deployment with billing or credits. Plan Step 3 covers this; it hasn't been done yet. The pieces, all in `asia-southeast1`:

| Resource | Notes |
| --- | --- |
| Cloud SQL for PostgreSQL 16 | Private IP; separate migration and runtime users (`python -m app.cli db-roles`) |
| Cloud Storage bucket | Uniform access, public access prevention; holds evidence and reports |
| Artifact Registry | `Dockerfile` (API) and `Dockerfile.worker` (worker, includes fonts for PDFs) |
| Cloud Run `pruflo-api` | `PRUFLO_STORAGE_BACKEND=gcs`, `PRUFLO_QUEUE_BACKEND=pubsub`, public, min instances 0; startup probe `/readyz`, `PRUFLO_RELEASE` set to the commit |
| Cloud Run `pruflo-worker` | No unauthenticated access, concurrency 1, same image settings plus the worker variables |
| Pub/Sub topic `evidence-jobs` | Push subscription to `https://<worker>/pubsub/push` with an OIDC token; retry backoff 10–600 s; dead-letter topic after 5 deliveries |
| Cloud Scheduler | OIDC calls to the worker: `POST /tasks/dispatch-outbox` and `POST /tasks/retry-webhooks` every minute; `POST /tasks/sync-connectors` and `POST /tasks/reconcile` every 15 minutes; `POST /tasks/retention` daily; `POST /tasks/weekly-summary` on Mondays at 07:00 Asia/Singapore |
| Secret Manager | `PRUFLO_DATABASE_URL`, `PRUFLO_MIGRATION_DATABASE_URL`, `PRUFLO_SECRETS_KEY`, `PRUFLO_INBOUND_EMAIL_TOKEN`, the OAuth client secrets |
| Inbound mail (optional) | SendGrid Inbound Parse (raw) or a Mailgun route posting to `https://<api>/v1/inbound/email?token=...`; MX for `PRUFLO_INBOUND_EMAIL_DOMAIN` |
| OAuth apps (optional) | Google OAuth web client and an Entra ID app, both with redirect URI `https://<api>/v1/connectors/oauth/callback` |
| Document AI | OCR processor, optional |

Give the API's service account Storage object admin on the bucket, Pub/Sub publisher, Cloud SQL client, Secret Manager accessor, and Service Account Token Creator on itself (for signed URLs). Give the worker the same (it also publishes follow-up events), plus Document AI API user, Vertex AI User (for Gemini) and Firebase Cloud Messaging admin (`roles/firebasecloudmessaging.admin`, for push). If the Firebase project isn't the GCP project the services run in, also grant both service accounts **Firebase Authentication Admin** on the Firebase project, for the revocation check and push. Don't download service-account keys.

## Security notes

- No secrets in the repository. Local credentials live in the git-ignored `backend/.env`; cloud secrets in Secret Manager.
- Signed URLs expire after 10 minutes and are never logged. uvicorn runs with `--no-access-log`; the app logs one JSON line per request without the query string.
- Logs never contain document text, tokens or keys.
- Webhook and Odoo URLs must be HTTPS and resolve to public addresses, checked on save and before each call (SSRF protection). Redirects aren't followed.
- Document-source credentials and refresh tokens are encrypted at rest and never returned. OAuth `state` is signed, expires in 15 minutes and works once; the return URL must be a page of the web app.
- Inbound mail is accepted only with the shared token, from a verified sender who is a member allowed to upload; the address alone isn't a credential.
- CSV exports neutralise cells that spreadsheet apps would run as formulas.
- Uploaded documents are untrusted: they're parsed as data and never executed. Macro-enabled Office files and encrypted PDFs are refused.

## Project layout

```
backend/
  app/
    api/v1/        routes, one module per area
    auth/          Firebase, App Check, API keys, permission table
    db/            models and Alembic migrations
    pipeline/      parsers, OCR adapter, rule templates
    reports/       PDF rendering
    services/      business logic shared by routes and the worker
    storage/       Cloud Storage and local storage, file-type detection
    worker/        worker service, job runner, outbox dispatcher
    cli.py         operator commands
  tests/           pytest suite (runs against PostgreSQL)
  Dockerfile       API image
  Dockerfile.worker
  docker-compose.yml
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `docker compose up` says `PRUFLO_DB_PASSWORD` is missing | Copy `.env.example` to `.env` and set it |
| `password authentication failed` | The password in `.env` doesn't match the database volume. Set it with `docker compose exec postgres psql -U pruflo -c "ALTER USER pruflo WITH PASSWORD '...'"` |
| `403 not_invited` | Invite the email with `python -m app.cli invite` (or `bootstrap` for a new organisation) |
| Files stay `queued` locally | Run `python -m app.worker drain`; check `PRUFLO_QUEUE_BACKEND=inline` |
| Scans end as `needs_review` (`ocr_not_configured`) | Expected without Document AI; set the two `PRUFLO_DOCUMENTAI_*` variables |
| The browser reports a CORS error | Add the app's origin to `PRUFLO_CORS_ORIGINS` |
