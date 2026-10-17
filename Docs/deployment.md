# Deploying PruFlo (free tier): hand-off guide

Written 2026-10-17 for the developer finishing the deployment. Follow it top to bottom; each step says what to click or run, what you should see, and what to do if you don't. Allow about 1–1.5 hours.

Background on *why* it's set up this way is in `Docs/fix_plan.md`. In short: there's no Google Cloud billing account, so everything runs on free tiers with no card:

| Piece | Service | Notes |
| --- | --- | --- |
| Web app | Firebase Hosting: https://pruflo.web.app (project `login-register-125ef`) and https://undercurrent-cb9f8.web.app | Deployed to both by GitHub Actions on every merge to `main` |
| Sign-in, push, uploaded files | **A new Firebase project** (Spark plan), which you create in step 1 | Files are stored in **Firestore**, in chunks |
| API and worker | **Render**, one free web service (`render.yaml`) | Files are processed in the API process right after upload |
| Scheduled work | GitHub Actions `tick.yml` every 30 min | Retries, imports, retention, Monday summary; also wakes Render |
| Structured records | **Cloud SQL for PostgreSQL** (Firebase SQL Connect no-cost trial, 90 days) | Cases, claims, findings, review and audit trail |
| Unstructured content | **Firestore** | Uploaded files (chunked) and each file's extracted page text and blocks |
| Reading documents | **Gemini API**, free key | Synthetic demo documents only (see "Data and privacy") |

## What's done and what's left

**Done, in the code on the `backend` branch** (not yet on `main`; PR #15 merged an earlier state):
- Everything in `Docs/backend_plan.md`: uploads, extraction, grounding, the four rules, review, reports, export, integrations, notifications and Q&A.
- The free-tier pieces: Firestore file storage, the Gemini API-key provider, the scheduled-work endpoint, the Firestore document-content store, separate migration and runtime database roles, and the Render image, blueprint and start script.
- 213 backend tests and 54 Flutter tests pass. The Render image was built and started locally: `/healthz` answers, and it runs as a non-root user.

**Left (this guide):** create the accounts, put the secrets in Render and GitHub, run the database setup once, merge to `main`, and check it end to end. No code changes are needed.

## 0. What you need

- Access to the GitHub repo `Veranzi/pruflo` with **admin** rights (needed for repository variables and secrets), or someone who has them.
- A Google account for the new Firebase project (no card is needed anywhere).
- On your machine: `git`, Python 3.12, and optionally the GitHub CLI (`gh`). Docker isn't needed.
- The repo checked out on the `backend` branch, with the backend's dependencies installed:

  ```bash
  git clone https://github.com/Veranzi/pruflo.git && cd pruflo
  git checkout backend
  cd backend && python3.12 -m venv .venv && source .venv/bin/activate
  pip install -r requirements-dev.txt
  ```

Throughout, **never commit secrets**. Git already ignores `backend/.env*` (except `.env.example`) and `*-service-account*.json`.

## 1. Firebase project (about 15 minutes)

In https://console.firebase.google.com with the new Google account:

1. **The project:** use the existing **Pruflo** project, ID **`login-register-125ef`** (the account's project quota is used up, so no new project). It's called `<PROJECT_ID>` below.
   - It stays on the free **Spark** plan.
   - Its Firestore already holds `users` and `conversations` collections from an earlier app. PruFlo only uses `pruflo_objects` and `pruflo_documents`, so they don't clash.
2. **Register the web app:** ⚙ Project settings → General → "Your apps" → **Web** (`</>`) → nickname `pruflo-web`, leave "Firebase Hosting" **unticked** → Register.
   - Copy these five values from the `firebaseConfig` shown: `apiKey`, `authDomain`, `projectId`, `messagingSenderId`, `appId`.
   - They're public by design (the API enforces access), and they go into GitHub in step 6.
3. **Firestore:** Build → **Firestore Database** → Create database → location **`nam5` (United States)** → **Start in production mode** → Create.
   - Production mode denies all browser access, which is what we want: only the backend (with the service-account key) reads and writes files.
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
6. **Service-account key (secret):** ⚙ Project settings → **Service accounts** → **Generate new private key** → Generate key.
   - Save the file as `backend/firebase-service-account.json`.
   - The backend uses it to verify sign-ins, send push and read and write Firestore. Treat it like a password; if it leaks, delete it on the same page and generate a new one.

## 2. Cloud SQL for PostgreSQL, through Firebase SQL Connect (about 15 minutes)

Structured records (cases, evidence metadata, claims, findings, review actions, audit) live in Google's **Cloud SQL for PostgreSQL**. Firebase SQL Connect (formerly Data Connect) gives a Spark project one **no-cost Cloud SQL instance for 90 days, with no billing account or card**: PostgreSQL 17, 1 vCPU, ~628 MB memory, 10 GB storage, about 8,000 operations a day. Unstructured content (uploaded files, and each file's extracted page text and blocks) lives in Firestore (step 1.3).

1. **Create the instance:** Firebase console → the Pruflo project → **SQL Connect** → Get started → choose the **no-cost trial** instance.
   - Region: **`us-east4` (Northern Virginia)**, next to Render's Virginia region and Firestore `nam5`.
   - Accept the default service and database names; the wizard's own schema isn't used by PruFlo.
2. **Open it in Google Cloud:** the same page links to the instance ("View in Google Cloud"), or go to https://console.cloud.google.com/sql with the same Google account and pick project `login-register-125ef`. Then:
   - **Databases → Create database** → name `pruflo`. PruFlo uses its own database so it never touches SQL Connect's schema.
   - **Users →** user `postgres` → **Change password** → generate a strong one and **save it in a password manager**. It's called `<POSTGRES_PASSWORD>` below.
   - **Connections → Networking:** under **Public IP → Authorized networks**, add your own IP (for step 4) and **Render's outbound IPs** (Render dashboard → your service → **Connect → Outbound**; add each range). Never add `0.0.0.0/0`.
   - **Connections → Security:** set SSL mode to **Allow only SSL connections**.
   - Copy the instance's **Public IP address** from Overview. It's called `<SQL_IP>` below.
3. **Connection string** (the backend adds the driver name itself):

   ```
   postgresql://postgres:<POSTGRES_PASSWORD>@<SQL_IP>:5432/pruflo?sslmode=require
   ```

**If the trial blocks a step** (for example, authorized networks or creating a database): note what the console says. Enabling billing on the project lifts the limits and keeps the trial's 90 free days.

## 3. Gemini API key (2 minutes)

https://aistudio.google.com with the **same new Google account** → **Get API key → Create API key** → pick the new Firebase project → copy the key.

It's a free-tier key. Use it only with the synthetic demo documents (see "Data and privacy"). If you'd rather have no AI, skip this: the structured reader, which needs no model, handles the demo documents.

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

   You should see `Roles ready: ...` twice, and Alembic should end with `Running upgrade 0008 -> 0009, document content in firestore`.

4. **Check the app's role can connect:**

   ```bash
   PRUFLO_DATABASE_URL="$APP_URL" python -c "from app.db.session import get_engine; from sqlalchemy import text; print(get_engine().connect().execute(text('select count(*) from tenants')).scalar())"
   ```

   It prints `0`.

**If a step fails:**
- `connection timed out`: your IP (or Render's) isn't in the instance's authorized networks (step 2.2).
- `permission denied to create role`: create the two users in the Cloud SQL console (**Users → Add user account**, built-in authentication) with the passwords above, then run `db-roles --no-create`. If that isn't possible either: **Fallback:** use `ADMIN_URL` for both `MIGRATION_URL` and `APP_URL` below. It works, but without the separate-roles protection; note it as a follow-up.

## 5. Render (about 15 minutes, plus the first build)

1. **Make the encryption key** (it encrypts webhook secrets and connector credentials) and keep the output handy:

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

2. **Turn the service-account key into one line** (Render takes it as an environment variable):

   ```bash
   python -c "import json; print(json.dumps(json.load(open('firebase-service-account.json'))))"
   ```

3. In https://dashboard.render.com (sign in with GitHub): **New → Blueprint** → connect `Veranzi/pruflo` → Render reads `render.yaml` and shows the service `pruflo-api` (Docker, free, Virginia, branch **`main`**).
   - Render deploys from `main`. Until the `backend` branch is merged into `main` (step 7), either merge first or change the branch to `backend` in the service's Settings after creating it.
4. Render asks for the values marked `sync: false`. Fill them in:

   | Variable | Value |
   | --- | --- |
   | `PRUFLO_FIREBASE_PROJECT` | `<PROJECT_ID>` from step 1 |
   | `PRUFLO_SECRETS_KEY` | the Fernet key from 5.1 |
   | `PRUFLO_DATABASE_URL` | `APP_URL` from step 4 |
   | `PRUFLO_MIGRATION_DATABASE_URL` | `MIGRATION_URL` from step 4 |
   | `PRUFLO_FIREBASE_CREDENTIALS` | the one-line JSON from 5.2 |
   | `PRUFLO_GEMINI_API_KEY` | the key from step 3 (or leave empty for no AI) |

   `PRUFLO_STORAGE_SIGNING_KEY` and `PRUFLO_TICK_TOKEN` are generated by Render. The rest are fixed in `render.yaml`:

   | Variable | Value |
   | --- | --- |
   | `PRUFLO_ENV` | `prod` |
   | `PRUFLO_STORAGE_BACKEND` | `firestore` |
   | `PRUFLO_QUEUE_BACKEND` | `inline` |
   | `PRUFLO_GEMINI_ENABLED` | `true` |
   | `PRUFLO_APP_CHECK_REQUIRED` | `false` |
   | `PRUFLO_DB_POOL_SIZE` / `PRUFLO_DB_MAX_OVERFLOW` | `3` / `2` |

5. **Apply.** The first build takes 5–10 minutes. In the service's **Logs** you should see, in order:
   - `Running upgrade ...` lines (or nothing, if step 4 already migrated)
   - `Uvicorn running on http://0.0.0.0:10000`
6. Copy the service URL (top of the page, e.g. `https://pruflo-api-xxxx.onrender.com`). It's called `<API_URL>` below. Check it:

   ```bash
   curl <API_URL>/healthz   # {"status":"ok","release":"<commit>"}
   curl <API_URL>/readyz    # {"status":"ready",...}: the database answers
   ```

7. In the service → **Environment**, copy the generated value of **`PRUFLO_TICK_TOKEN`**; it goes into GitHub in step 6.

How it behaves in production:
- `start-render.sh` runs `alembic upgrade head` as `pruflo_migrator` before each start. Render's free plan has no pre-deploy step, and there's one instance, so it's safe.
- `PRUFLO_PUBLIC_BASE_URL` (used in upload links) defaults to Render's own URL; `PRUFLO_RELEASE` to the deployed commit.
- `/docs` is off in `prod`, and development sign-in (`dev:` tokens) is refused.
- The free service **sleeps after 15 minutes** without traffic; the first request then takes about a minute. The tick workflow wakes it every 30 minutes.

## 6. Create the organisation and the first admin (5 minutes)

On your machine, in `backend/`, against the production database:

```bash
set -a; source .env.deploy; set +a
export PRUFLO_DATABASE_URL="$APP_URL"
export PRUFLO_GEMINI_API_KEY='<key from step 3>'   # new organisations then use Gemini
python -m app.cli bootstrap --tenant "<Company name>" --admin-email <your sign-in email> --admin-name "<Your name>"
python -m app.cli invite --tenant ten_... --email teammate@example.com --role reviewer   # optional
```

The admin signs in on pruflo.web.app with that exact email (Google, or Email/Password after verifying it). Without the Gemini key in the environment the organisation starts on Vertex, which isn't configured, so the structured reader is used. Switch it later in the app under **Integrations → Providers → Gemini API (paid key)**.

## 7. GitHub settings, then go live (10 minutes)

In the repo → **Settings → Secrets and variables → Actions**:

**Variables** (Variables tab):

| Name | Value |
| --- | --- |
| `API_BASE_URL` | `<API_URL>` (no trailing slash) |
| `FIREBASE_API_KEY` | `apiKey` |
| `FIREBASE_APP_ID` | `appId` |
| `FIREBASE_MESSAGING_SENDER_ID` | `messagingSenderId` |
| `FIREBASE_PROJECT_ID` | `projectId` |
| `FIREBASE_AUTH_DOMAIN` | `authDomain` |
| `FIREBASE_VAPID_KEY` | the Web Push key from step 1.5 |

**Secret** (Secrets tab): `PRUFLO_TICK_TOKEN` = the value copied in step 5.7.

The same with the GitHub CLI:

```bash
gh variable set API_BASE_URL --body "<API_URL>"
gh variable set FIREBASE_API_KEY --body "..."
gh variable set FIREBASE_APP_ID --body "..."
gh variable set FIREBASE_MESSAGING_SENDER_ID --body "..."
gh variable set FIREBASE_PROJECT_ID --body "..."
gh variable set FIREBASE_AUTH_DOMAIN --body "..."
gh variable set FIREBASE_VAPID_KEY --body "..."
gh secret set PRUFLO_TICK_TOKEN   # paste the value when asked
```

Then **open a pull request from `backend` to `main` and merge it** once its checks pass (`gh pr create --base main --head backend --fill`, then merge on GitHub). Merging:
- runs `firebase-hosting-merge.yml`, which rebuilds pruflo.web.app with the new Firebase config and `API_BASE_URL`
- deploys Render (it follows `main`)
- turns on `tick.yml` (scheduled workflows only run from `main`)

Check the Actions tab: "Deploy PruFlo to Firebase Hosting on merge" must be green. Then run "Backend scheduled work" once by hand (Actions → the workflow → **Run workflow**). Its log ends with a JSON line such as `{"processing":{...},"connectors":{...},...}`.

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
3. Within a minute or two (longer if Render was asleep), the files show **processed**, and the case moves to **In review** with three findings:
   - quantity shortfall: Steel bolts M8, 100 ordered vs 80 delivered, **20 pcs short**
   - amount mismatch: SGD 76.30 vs 83.70, **9.7% more**
   - late delivery: due 5 Oct, delivered 8 Oct, **3 days late**
4. In the **Review** workspace, approve or reject each one (reject needs a note). The case becomes **Completed**.
5. **Reports**: generate a detailed report and download the PDF, and export JSON and CSV.
6. Optional:
   - the case's **Ask** tab answers questions, if the Gemini key is set
   - a teammate with the reviewer role gets the "ready for review" notification

If all of that works, the deployment is done.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| Render build fails at `pip install` | Rare network issue: Render → Manual Deploy → "Clear build cache & deploy" |
| Render logs: `connection to server ... failed` / `/readyz` 503 | Wrong database URL: must be the **session pooler** host, with `.<REF>` after the role name, the right password and `?sslmode=require` |
| Render logs: `Set PRUFLO_STORAGE_SIGNING_KEY` | The variable is missing: Environment → add it with any long random value |
| Render logs: `Set PRUFLO_FIREBASE_CREDENTIALS` or a JSON error | Paste the **one-line** JSON from step 5.2, without quotes around it |
| Sign-in on the site: `auth/unauthorized-domain` | Step 1.4: add `pruflo.web.app` to Authorized domains |
| Sign-in works, then the API says `not_invited` | That email wasn't invited: run `python -m app.cli invite` (step 6) with the exact email |
| API returns `401` for every call after sign-in | `PRUFLO_FIREBASE_PROJECT` on Render doesn't match `FIREBASE_PROJECT_ID` in GitHub |
| Browser console shows a CORS error | `API_BASE_URL` has a trailing slash, or the site isn't `pruflo.web.app`/`pruflo.firebaseapp.com` (to add another origin, set `PRUFLO_CORS_ORIGINS` on Render as a JSON list) |
| Upload stays "queued" | Render went to sleep mid-job: the next tick (or `Run workflow` on "Backend scheduled work") processes it |
| Files fail with `storage_unavailable` | Firestore wasn't created (step 1.3), or the service-account key belongs to another project |
| Findings missing; claims show "ambiguous" or "ungrounded" | Expected for a layout the structured reader can't tie to labels; set the Gemini key, or use the demo documents |
| Deploy workflow says "Set these repository variables" | One of the step 7 variables is missing |

## Free-tier limits

- **Render:** 512 MB RAM and 0.1 CPU. It sleeps after 15 idle minutes (about a minute to wake), and a 200-page PDF takes minutes.
- **Firestore:** 1 GiB stored, 20,000 writes and 50,000 reads a day. A 25 MB file uses ~37 writes, so about 40 files at the maximum size fill the storage.
- **Cloud SQL trial:** about 8,000 operations a day and 10 GB. After 90 days the instance is suspended (30-day grace) unless billing is enabled. Before a demo, take a copy:

  ```bash
  pg_dump "$ADMIN_URL" --no-owner -Fc -f pruflo-$(date +%F).dump
  ```

- **Gemini free tier:** rate limits apply; heavy use returns 429 and the file is retried by the next tick.

## Data and privacy

- **Location:** data is stored in the **United States** (Render Virginia, Cloud SQL `us-east4`, Firestore `nam5`). Firebase Auth keeps sign-in records (email, name) with Google.
- **Gemini free tier:** Google may use free-tier prompts to improve its products and have people review them. **Upload only synthetic documents** (like the demo set) while the key is a free one. A paid-tier key needs a billing account.
- **Security:** Firestore rules deny all browser access; upload and view links are signed and expire after 10 minutes. The API never returns secrets.

## Follow-ups (not needed to go live)

1. **App Check:** create a reCAPTCHA v3 site key in the Firebase console, set `FIREBASE_APP_CHECK_SITE_KEY` in GitHub, and set `PRUFLO_APP_CHECK_REQUIRED=true` on Render.
2. **OCR for scanned documents:** none on this plan (scans end as "needs review"). Options: a paid Gemini tier reading the page images, or Google Cloud Document AI once there's billing.
3. **Hackathon rule:** the AI Builder Cup asks for a prototype built on Google Cloud. Firebase, Firestore, Cloud SQL and Gemini are Google, but Render isn't. With billing, move the API to Cloud Run (and optionally Firebase Storage + Vertex Gemini) by changing settings (`Docs/fix_plan.md`, "Moving to Google Cloud later"); no code changes are needed.
4. **Retention periods and an email service** are still open decisions (`Docs/backend_plan.md`, "Open questions").
