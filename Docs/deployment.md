# Deploying PruFlo: hand-off guide

Updated 2026-10-18. Follow it top to bottom; each step says what to click or run, what you should see, and what to do if you don't. Background on *why* it's set up this way is in `Docs/fix_plan.md`.

PruFlo runs on Firebase and Google Cloud, across **two projects on purpose**:

| Project | Holds | Billing |
| --- | --- | --- |
| `login-register-125ef` (Pruflo, Firebase) | Hosting (`pruflo.web.app`), Firebase Authentication, Firestore `(default)` (`nam5`), the Cloud SQL instance `login-register-125ef-instance` (`us-east4`, database `pruflo`) | **None: stays on the free Spark plan** |
| `gen-lang-client-0422375595` ("Gemini API") | Cloud Run service `pruflo-api` (`us-east4`), Cloud Build, Artifact Registry, Secret Manager, Cloud Scheduler, Gemini on Vertex AI | Linked |

Linking billing to the Firebase project would upgrade it to Blaze, which turns the SQL Connect trial database into a paid Cloud SQL instance (from about $9 a month). Keeping it on Spark keeps the database free for the trial's 90 days. The Cloud Run service's identity, `pruflo-api@gen-lang-client-0422375595.iam.gserviceaccount.com`, is granted only what it needs in the Firebase project: Cloud SQL client, Firestore user, and Firebase Auth viewer.

| Piece | Service | Notes |
| --- | --- | --- |
| Web app | Firebase Hosting: https://pruflo.web.app (and https://undercurrent-cb9f8.web.app) | Deployed by GitHub Actions on every merge to `main` |
| Sign-in | Firebase Authentication | Email/Password and Google |
| API and worker | **Cloud Run**, one service (`backend/Dockerfile.cloudrun`) | Files are processed right after upload; scales to zero when idle |
| Scheduled work | **Cloud Scheduler** job `pruflo-tick`, every 6 hours | Retries, imports, retention, Monday summary |
| Structured records | **Cloud SQL for PostgreSQL** (SQL Connect no-cost trial, 90 days) | Cases, claims, findings, review and audit trail |
| Unstructured content | **Firestore** | Uploaded files (chunked) and each file's extracted page text and blocks |
| Reading documents | **Gemini on Vertex AI**, through the service's identity | A Gemini API key is optional |

## 0. What you need

- Access to the GitHub repo `Veranzi/pruflo` with **admin** rights (for repository variables and secrets), or someone who has them.
- Owner access to both Google Cloud projects.
- On your machine: `git`, Python 3.12, the Google Cloud CLI (`gcloud`) signed in with `gcloud auth login`, and Git Bash on Windows. Docker isn't needed: Cloud Build builds the image.
- For the one-off database steps (step 4), the backend's dependencies:

  ```bash
  git clone https://github.com/Veranzi/pruflo.git && cd pruflo/backend
  python3.12 -m venv .venv && source .venv/bin/activate
  pip install -r requirements-dev.txt
  ```

Throughout, **never commit secrets**. Git already ignores `backend/.env*` (except `.env.example`) and `*-service-account*.json`.

## 1. Firebase project (about 15 minutes)

In https://console.firebase.google.com:

1. **The project:** the existing **Pruflo** project, ID **`login-register-125ef`**. It stays on the free **Spark** plan; don't upgrade it (see above).
   - Its Firestore already holds `users` and `conversations` collections from an earlier app. PruFlo only uses `pruflo_objects` and `pruflo_documents`, so they don't clash.
2. **Register the web app:** ⚙ Project settings → General → "Your apps" → **Web** (`</>`) → nickname `pruflo-web`, leave "Firebase Hosting" **unticked** → Register.
   - Copy these five values from the `firebaseConfig` shown: `apiKey`, `authDomain`, `projectId`, `messagingSenderId`, `appId`.
   - They're public by design (the API enforces access), and they go into GitHub in step 7.
3. **Firestore:** Build → **Firestore Database** → Create database → location **`nam5` (United States)** → **Start in production mode** → Create.
   - Production mode denies all browser access, which is what we want: only the backend (Cloud Run's service account) reads and writes files.
   - If you're asked for rules, use:

     ```
     rules_version = '2';
     service cloud.firestore {
       match /databases/{database}/documents {
         match /{document=**} { allow read, write: if false; }
       }
     }
     ```
4. **Sign-in:** Build → **Authentication** → Get started.
   - Sign-in method: enable **Email/Password** and **Google**. For Google, choose a support email.
   - Then Authentication → **Settings → Authorized domains → Add domain**, twice: `pruflo.web.app` and `pruflo.firebaseapp.com`.
   - Without these, sign-in on the live site fails with `auth/unauthorized-domain`.
5. **Web push key:** ⚙ Project settings → **Cloud Messaging** → "Web Push certificates" → **Generate key pair** → copy the key (public; it goes to GitHub as `FIREBASE_VAPID_KEY`).

No service-account key file is needed: Cloud Run uses its own identity. A key is only needed to run the backend outside Google Cloud (for example on your laptop against the live Firestore); if one exists and nobody uses it, delete it under ⚙ Project settings → Service accounts.

## 2. Cloud SQL for PostgreSQL, through Firebase SQL Connect (about 15 minutes)

Structured records (cases, evidence metadata, claims, findings, review actions, audit) live in Google's **Cloud SQL for PostgreSQL**. Firebase SQL Connect gives a Spark project one **no-cost Cloud SQL instance for 90 days, with no billing account**: 1 vCPU, ~628 MB memory, 10 GB storage, about 8,000 operations a day.

1. **Create the instance:** Firebase console → the Pruflo project → **SQL Connect** → Get started → choose the **no-cost trial** instance.
   - Region: **`us-east4` (Northern Virginia)**, where Cloud Run runs, near Firestore `nam5`.
   - Accept the default service and database names; the wizard's own schema isn't used by PruFlo.
2. **Open it in Google Cloud:** https://console.cloud.google.com/sql, project `login-register-125ef`. Then:
   - **Databases → Create database** → name `pruflo`. PruFlo uses its own database so it never touches SQL Connect's schema.
   - **Users →** user `postgres` → **Change password** → generate a strong one and **save it in a password manager**. It's called `<POSTGRES_PASSWORD>` below.
   - **Connections → Networking:** under **Public IP → Authorized networks**, add only **your own IP** (for the admin commands in steps 4 and 6). Cloud Run doesn't need an entry: it connects through Cloud SQL's built-in connection, authorised by its identity. Never add `0.0.0.0/0`.
   - **Connections → Security:** set SSL mode to **Allow only SSL connections**.
   - Copy the instance's **Public IP address** from Overview. It's called `<SQL_IP>` below.
3. **Connection string** for the admin commands (the backend adds the driver name itself):

   ```
   postgresql://postgres:<POSTGRES_PASSWORD>@<SQL_IP>:5432/pruflo?sslmode=require
   ```

## 3. Gemini

Cloud Run calls **Gemini on Vertex AI** in the billed project, with its own identity: nothing to set up here. New organisations use it by default.

Optional: an organisation can use a **Gemini API key** instead (Integrations → Providers → Gemini API). Create one at https://aistudio.google.com → **Get API key**, in a project with billing so it's a paid-tier key, and put it in Secret Manager as `pruflo-gemini-api-key` (step 5.2).

## 4. Set up the database once, from your machine (about 10 minutes)

PruFlo uses two database roles (`Docs/backend_plan.md`, Step 2):
- `pruflo_migrator` owns the schema and runs migrations.
- `pruflo_app` is what the API uses; it can't change the schema or rewrite the audit trail.

1. **Choose two new passwords** (letters and digits only, so they don't need URL-escaping), e.g.:

   ```bash
   python -c "import secrets; print(secrets.token_hex(16)); print(secrets.token_hex(16))"
   ```

2. **Create `backend/.env.deploy`** (git-ignored) with your values:

   ```bash
   # Cloud SQL, as postgres (from step 2.3)
   ADMIN_URL='postgresql://postgres:<POSTGRES_PASSWORD>@<SQL_IP>:5432/pruflo?sslmode=require'
   MIGRATOR_PASSWORD='<first password>'
   RUNTIME_PASSWORD='<second password>'
   MIGRATION_URL='postgresql://pruflo_migrator:<first password>@<SQL_IP>:5432/pruflo?sslmode=require'
   APP_URL='postgresql://pruflo_app:<second password>@<SQL_IP>:5432/pruflo?sslmode=require'
   ```

3. **Create the roles, run the migrations, and check** (in `backend/`, with the virtualenv active):

   ```bash
   set -a; source .env.deploy; set +a

   # Create both roles (run as Cloud SQL's postgres user).
   PRUFLO_ADMIN_DATABASE_URL="$ADMIN_URL" PRUFLO_MIGRATOR_PASSWORD="$MIGRATOR_PASSWORD" \
     PRUFLO_RUNTIME_PASSWORD="$RUNTIME_PASSWORD" python -m app.cli db-roles

   # Create the tables, as the migration role.
   PRUFLO_MIGRATION_DATABASE_URL="$MIGRATION_URL" alembic upgrade head

   # Apply the grants to the new tables (safe to repeat).
   PRUFLO_ADMIN_DATABASE_URL="$ADMIN_URL" python -m app.cli db-roles --no-create
   ```

   You should see `Roles ready: ...` twice, and Alembic should end at the latest migration.

4. **Check the app's role can connect:**

   ```bash
   PRUFLO_DATABASE_URL="$APP_URL" python -c "from app.db.session import get_engine; from sqlalchemy import text; print(get_engine().connect().execute(text('select count(*) from tenants')).scalar())"
   ```

   It prints `0`.

After this, migrations run on every deploy (step 5), so this step is only needed once.

**If a step fails:**
- `connection timed out`: your IP isn't in the instance's authorized networks (step 2.2).
- `permission denied to create role`: create the two users in the Cloud SQL console (**Users → Add user account**, built-in authentication) with the passwords above, then run `db-roles --no-create`.

## 5. Cloud Run (about 10 minutes, mostly waiting)

`backend/deploy/cloudrun/setup.sh` does the whole deployment, and it's safe to run again after every code change. It never creates or changes the database server, the database or the role passwords.

1. **Billing:** `gen-lang-client-0422375595` must be linked to the billing account (it is). Set a budget alert, e.g. $5: Billing → Budgets & alerts.
2. **Secrets.** Put the existing passwords in Secret Manager in the **Cloud Run project**. In Git Bash, run each line, paste the value when it waits, and press Enter (nothing is shown or kept in history):

   ```bash
   P=gen-lang-client-0422375595 R=us-east4
   read -rs V && printf '%s' "$V" | gcloud secrets create pruflo-db-migrator-password --project $P --replication-policy=user-managed --locations=$R --data-file=- && unset V
   read -rs V && printf '%s' "$V" | gcloud secrets create pruflo-db-runtime-password  --project $P --replication-policy=user-managed --locations=$R --data-file=- && unset V
   ```

   Optional, the same way:
   - `pruflo-db-admin-password` (`<POSTGRES_PASSWORD>`): lets each deploy re-apply table grants after new migrations.
   - `pruflo-gemini-api-key`: for organisations set to the Gemini API provider.

   The script generates the rest the first time: `pruflo-secrets-key` (encrypts webhook secrets and connector credentials), `pruflo-storage-signing-key` (signs upload and view links) and `pruflo-tick-token` (protects the scheduled-work endpoint).
3. **Run it** (Git Bash, from `backend/`):

   ```bash
   ./deploy/cloudrun/setup.sh
   ```

   It checks billing; turns on the APIs it needs in both projects; checks the Cloud SQL instance and Firestore; creates the `pruflo-api` service account and grants its roles in both projects; builds the image with Cloud Build; runs the migrations as the Cloud Run job `pruflo-db-migrate`; deploys `pruflo-api`; schedules `pruflo-tick`; and checks `/readyz`. It ends by printing the API's URL, called `<API_URL>` below.
4. **Check it:**

   ```bash
   curl <API_URL>/readyz    # {"status":"ready","release":"<commit>"}: the database answers
   curl <API_URL>/v1/me     # 401: the API requires Firebase sign-in
   ```

   Cloud Run reserves `/healthz`, so it answers with Google's own 404 page; use `/readyz`.

How it behaves in production:
- The service runs with `--min-instances 0` (nothing runs, or costs, while idle) and `--no-cpu-throttling` (a file keeps processing after its upload request returns). The first request after an idle spell starts an instance in a few seconds.
- Migrations run in `pruflo-db-migrate` before each new revision, so a failed migration never reaches the live service.
- `/docs` is off in `prod`, and development sign-in (`dev:` tokens) is refused.

## 6. Create the organisation and the first admin (5 minutes)

On your machine, in `backend/`, against the production database:

```bash
set -a; source .env.deploy; set +a
export PRUFLO_DATABASE_URL="$APP_URL"
python -m app.cli bootstrap --tenant "<Company name>" --admin-email <your sign-in email> --admin-name "<Your name>"
python -m app.cli invite --tenant ten_... --email teammate@example.com --role reviewer   # optional
```

Run `bootstrap` **once**: each run creates another organisation. The admin signs in on pruflo.web.app with that exact email (Google, or Email/Password after verifying it). New organisations use Gemini on Vertex AI; switch under **Integrations → Providers**.

## 7. GitHub settings, then go live (10 minutes)

In the repo → **Settings → Secrets and variables → Actions**:

**Variables** (Variables tab):

| Name | Value |
| --- | --- |
| `API_BASE_URL` | `<API_URL>` (the `https://…run.app` URL, no trailing slash) |
| `FIREBASE_API_KEY` | `apiKey` |
| `FIREBASE_APP_ID` | `appId` |
| `FIREBASE_MESSAGING_SENDER_ID` | `messagingSenderId` |
| `FIREBASE_PROJECT_ID` | `login-register-125ef` |
| `FIREBASE_AUTH_DOMAIN` | `login-register-125ef.firebaseapp.com` |
| `FIREBASE_VAPID_KEY` | the Web Push key from step 1.5 |

**Secret** (Secrets tab), optional: `PRUFLO_TICK_TOKEN` = the value of `pruflo-tick-token` (`gcloud secrets versions access latest --secret pruflo-tick-token --project gen-lang-client-0422375595`). It's only for the "Backend scheduled work (run now)" button; Cloud Scheduler already runs the work every 6 hours.

Then **merge to `main`** with a pull request once its checks pass. Merging runs `firebase-hosting-merge.yml`, which rebuilds pruflo.web.app with the Firebase config and `API_BASE_URL`. The API itself is deployed by `setup.sh` (step 5), not by the merge.

## 8. Check it end to end (15 minutes)

1. **Make the demo documents**: a PDF purchase order, an Excel delivery note and a Word invoice that disagree on purpose. In `backend/`:

   ```bash
   python - <<'PY'
   from pathlib import Path
   from tests.test_claims_rules import _po_pdf, _dn_xlsx, _invoice_docx
   out = Path("demo-documents"); out.mkdir(exist_ok=True)
   (out / "purchase-order.pdf").write_bytes(_po_pdf())
   (out / "delivery-note.xlsx").write_bytes(_dn_xlsx())
   (out / "invoice.docx").write_bytes(_invoice_docx())
   print("written to", out.resolve())
   PY
   ```

2. Open https://pruflo.web.app → sign in with the admin email → **Cases → New case** → open it → **Evidence** → upload the three files.
3. Within a minute or two, the files show **processed**, and the case moves to **In review** with three findings:
   - quantity shortfall: Steel bolts M8, 100 ordered vs 80 delivered, **20 pcs short**
   - amount mismatch: SGD 76.30 vs 83.70, **9.7% more**
   - late delivery: due 5 Oct, delivered 8 Oct, **3 days late**
4. In the **Review** workspace, approve or reject each one (reject needs a note). The case becomes **Completed**.
5. **Reports**: generate a detailed report and download it, and export JSON and CSV.
6. **Submission check** (from the repo root):

   ```bash
   python scripts/check_submission.py --api <API_URL> --video <demo video link>
   ```

   Every line should say PASS (WARN is advice).

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| `setup.sh` stops: missing secret | It prints the command to add it (step 5.2); then run it again |
| `setup.sh` stops: an API "not enabled" or "has not been used" | A newly enabled API takes a few minutes to answer; run the script again |
| `pruflo-db-migrate` fails | Cloud Run console (project `gen-lang-client-0422375595`) → Jobs → `pruflo-db-migrate` → latest execution → Logs. A password error means a secret doesn't match its role; "Can't locate revision" means the code is older than the database |
| Revision fails to start | Cloud Run → `pruflo-api` → Logs. `start.sh: no such file or directory` means Windows line endings; the image build strips them, so rebuild |
| `/readyz` 503 | The database didn't answer: check the instance is running and the secrets match the roles |
| `Permission denied` on Firestore or sign-in | IAM page of `login-register-125ef`: `pruflo-api@gen-lang-client-0422375595.iam.gserviceaccount.com` needs Cloud SQL Client, Cloud Datastore User and Firebase Authentication Viewer; `setup.sh` grants them |
| Sign-in on the site: `auth/unauthorized-domain` | Step 1.4: add `pruflo.web.app` to Authorized domains |
| Sign-in works, then the API says `not_invited` | That email wasn't invited: run `python -m app.cli invite` (step 6) with the exact email |
| API returns `401` for every call after sign-in | `FIREBASE_PROJECT_ID` in GitHub isn't `login-register-125ef` |
| Browser console shows a CORS error | `API_BASE_URL` has a trailing slash, or the site isn't one of the allowed origins (add one with `PRUFLO_CORS_ORIGINS`, a JSON list, on the Cloud Run service) |
| Upload stays "queued" | The next `pruflo-tick` (within 6 hours) processes it, or run "Backend scheduled work (run now)" in GitHub Actions |
| Findings missing; claims show "ambiguous" or "ungrounded" | Expected for a layout the structured reader can't tie to labels; check Gemini is on, or use the demo documents |
| Deploy workflow says "Set these repository variables" | One of the step 7 variables is missing |

## Limits

- **Cloud Run:** free tier at demo volume; 1 vCPU and 1 GiB per instance, at most 2 instances.
- **Firestore:** free quota of 1 GiB stored, 20,000 writes and 50,000 reads a day. A 25 MB file uses ~37 writes, so about 40 files at the maximum size fill the storage.
- **Cloud SQL trial:** about 8,000 operations a day and 10 GB. After 90 days the instance is archived unless the project moves to Blaze (then it's billed at Cloud SQL rates). Before a demo, take a copy:

  ```bash
  pg_dump "$ADMIN_URL" --no-owner -Fc -f pruflo-$(date +%F).dump
  ```

- **Gemini on Vertex AI:** pay per use, about $0.02 per five-document case.

## Data and privacy

- **Location:** data is stored and processed in the **United States** (Cloud Run and Cloud SQL `us-east4`, Firestore `nam5`; Gemini in the region set under Integrations → Providers). Firebase Auth keeps sign-in records (email, name) with Google.
- **Gemini:** Vertex AI doesn't use prompts to train models. A Gemini API key from a project without billing is a free-tier key, whose prompts Google may use; use only synthetic documents with one.
- **Security:** Firestore rules deny all browser access; upload and view links are signed and expire after 10 minutes. The API never returns secrets, and no service-account key is stored anywhere.

## Follow-ups (not needed to go live)

1. **App Check:** create a reCAPTCHA v3 site key in the Firebase console, set `FIREBASE_APP_CHECK_SITE_KEY` in GitHub, and set `PRUFLO_APP_CHECK_REQUIRED=true` on the Cloud Run service (`gcloud run services update pruflo-api --update-env-vars ...`).
2. **OCR for scanned documents:** scans end as "needs review". Document AI in the billed project would read them.
3. **Separate worker:** move processing to `Dockerfile.worker` behind Pub/Sub (`PRUFLO_QUEUE_BACKEND=pubsub`), so uploads never compete with API requests (`Docs/backend_plan.md`, Step 3).
4. **Retention periods and an email service** are still open decisions (`Docs/backend_plan.md`, "Open questions").
