# PruFlo: free deployment plan (fix plan)

Updated 2026-10-17. PruFlo is being submitted to the Google Cloud AI Builder Cup (prototype deadline 18 October 2026), with **no Google Cloud billing account and no credits**. This plan says where each part of PruFlo runs under that constraint, what changed in the code, and what's left. It changes *where things run*, not what the product does.

Read with `Docs/backend_plan.md` (what's built) and the two API contracts. Where they disagree about infrastructure, this plan wins. **The step-by-step deployment instructions are in `Docs/deployment.md`.**

✅ done · 🟠 partly done · ❌ not started

## The constraint, checked

With no billing account, a Firebase project stays on the free **Spark** plan. Checked against Firebase's documentation on 2026-10-17:

| Need | On Spark | Consequence |
| --- | --- | --- |
| Hosting, Authentication, Cloud Messaging, App Check | ✅ free | Used as before |
| **Cloud Firestore** | ✅ free quota (1 GiB stored, 50,000 reads and 20,000 writes a day) | **Holds the uploaded files**, in chunks |
| Cloud Storage | ❌ needs Blaze since 3 Feb 2026 | Not used |
| Cloud Functions, App Hosting, Cloud Run | ❌ need billing | The Python API can't run on Google |
| Gemini paid tier | ❌ needs billing | The **free-tier Gemini API key** is used, for synthetic demo documents only (Google may use free-tier prompts to improve its products and have people review them) |
| Postgres | ❌ Cloud SQL needs billing | Supabase (free) |

**Rule risk, accepted by the team:** the hackathon asks for a prototype "built on Google Cloud". Firebase (Hosting, Auth, Firestore, push) and Gemini are Google, but the API and worker run on Render and the database on Supabase. If a Google Cloud credit coupon turns up, switch back to the Google Cloud setup in `backend_plan.md` Step 3 (the code still supports Cloud Storage, Pub/Sub, Cloud Run and Vertex through settings).

## Decisions (made 2026-10-17)

| # | Decision | Choice |
| --- | --- | --- |
| D1 | Firebase project | A **new project in a separate Google account**, on Spark (replaces `login-register-125ef`). Hosting stays at `pruflo.web.app` (project `undercurrent-cb9f8`); that domain is authorised in the new project for sign-in |
| D2 | API and worker | **Render free web service** (Docker, region Virginia), one container for both |
| D3 | Database | **Supabase Postgres** (free, US East), through the session pooler (IPv4) |
| D4 | Uploaded files | **Firestore** in the new Firebase project (location `nam5`, United States), ~700 KB chunks per file |
| D5 | Reading documents | **Gemini API, free key** (provider `gemini_api`), synthetic demo documents only; the structured reader (no model) is the fallback |
| D6 | Queue and schedules | Files are processed in the API process right after upload (`PRUFLO_QUEUE_BACKEND=inline`); a GitHub Actions cron calls `POST /v1/internal/tick` every 30 minutes for retries and scheduled tasks |
| D7 | OCR | None for now (Document AI needs billing; Tesseract doesn't fit in 512 MB). Scans end as `needs_review` |
| D8 | Data region | United States, stated in the docs and privacy notes |

## How it fits together

```
Browser (pruflo.web.app, Firebase Hosting)
  │  Firebase Auth ID token (new project)
  ▼
Render: pruflo-api (FastAPI + inline worker)  ──►  Gemini API (free key)
  │            │                    ▲
  │            ▼                    │ POST /v1/internal/tick (every 30 min)
  │     Firestore (files, chunked)  GitHub Actions
  ▼
Supabase Postgres (pruflo_migrator owns the schema, pruflo_app at runtime)
```

- **Uploads:** endpoint 7 returns a link to `/v1/objects/...` on the API, HMAC-signed, valid 10 minutes, with the type and the 25 MB limit in the signature. The browser PUTs the file there; the API checks it while it streams in and stores it in Firestore. Endpoint 8 then checks the real type, pages and checksum as before.
- **Views** (endpoint 11) are signed `/v1/objects/...` GET links that stream the file back with `Cache-Control: private, no-store`.
- **Firestore rules** deny all browser access; only the backend's service account reads and writes.

## Work items

### Code

| Item | Status |
| --- | --- |
| Gemini API-key provider (`gemini_api`, migration 0008) for extraction, explanation rewrites and Q&A; Vertex stays as an option | ✅ tested |
| Firebase service-account key from `PRUFLO_FIREBASE_CREDENTIALS` (sign-in checks, push, Firestore) | ✅ |
| `FirestoreStorage`: chunked files, checksum-verified reads, signed upload and view links through `/v1/objects` | ✅ tested (in-memory Firestore that enforces the 1 MiB limit) |
| `POST /v1/internal/tick` (token-protected): drain waiting files and retries, reconcile, webhook retries, connector sync, retention, Monday summary | ✅ tested |
| Database roles safe on Supabase: no database-wide revoke, and `anon`/`authenticated` lose every privilege on our tables | ✅ tested |
| `Dockerfile.render` + `start-render.sh` (migrations as `pruflo_migrator` at start, then uvicorn) and `render.yaml` | ✅ written; first real run at deploy |
| `.github/workflows/tick.yml` | ✅ written; runs once it's on `main` |
| Frontend: Firebase config from the build (no default project), the `gemini_api` provider option; deploy workflows pass `FIREBASE_PROJECT_ID` and `FIREBASE_AUTH_DOMAIN` | ✅ |
| Docs: `Docs/deployment.md` (hand-off guide), README deploy section | ✅ |

### Accounts and settings (the team)

| Step | Who | Status |
| --- | --- | --- |
| New Firebase project: web app, Firestore (`nam5`, production mode), Email/Password + Google sign-in, authorised domains, Web Push key, service-account key | Team | ❌ |
| Supabase project (US East), session pooler URL, Data API off | Team | ❌ |
| Gemini API key (AI Studio, new account) | Team | ❌ |
| Render account linked to GitHub | Team | ❌ |
| `backend/.env.deploy` with the database URL and Gemini key (git-ignored) | Team | ❌ |

### Deploy (in order)

1. **Database:** create the roles and run the migrations against Supabase from this machine (`python -m app.cli db-roles`, then `alembic upgrade head` as `pruflo_migrator`).
2. **Render:** New → Blueprint → this repository (`render.yaml`, branch `backend`). Paste the secrets from the generated `backend/.env.render` (git-ignored) with **Add from .env**. Wait for `/readyz` to answer.
3. **Organisation:** create it and its first admin with `python -m app.cli bootstrap` against the Supabase database.
4. **GitHub:**
   - variables: `API_BASE_URL` = the Render URL, and the new Firebase web config (`FIREBASE_API_KEY`, `FIREBASE_APP_ID`, `FIREBASE_MESSAGING_SENDER_ID`, `FIREBASE_PROJECT_ID`, `FIREBASE_AUTH_DOMAIN`, `FIREBASE_VAPID_KEY`)
   - secret: `PRUFLO_TICK_TOKEN`
5. **Frontend:** merge to `main` so the live site rebuilds with the new config and `API_BASE_URL`; the tick workflow starts running from `main`.
6. **Check:** sign in on `pruflo.web.app` and upload the synthetic purchase order, delivery note and invoice. The case must reach `in_review` with the three findings, then a decision, the export and the PDF report must work, and the next tick must succeed.

**Done when:** that check passes on the live site with nothing running on a paid service.

## Limits to know (free tiers)

- Render sleeps after 15 idle minutes; the first request after that takes about a minute. With 0.1 CPU and 512 MB, a 200-page PDF takes minutes, not seconds.
- Firestore free quota: 1 GiB stored and 20,000 writes a day. A 25 MB file uses about 37 writes and 25 MB, so roughly 40 files at the maximum size fill the free storage.
- Supabase free: 500 MB database; a project pauses after 7 days with no database requests (the 30-minute tick prevents that). No point-in-time backups: take a `pg_dump` before demos.
- Gemini free tier: rate limits, and prompts may be used by Google. Synthetic documents only.
- Data is stored in the United States (Render Virginia, Supabase East US, Firestore `nam5`). Firebase Auth keeps sign-in records in Google's infrastructure.

## Moving to Google Cloud later

With billing or credits: set `PRUFLO_STORAGE_BACKEND=gcs` (Firebase Storage bucket), `PRUFLO_QUEUE_BACKEND=pubsub` with the worker on Cloud Run, Cloud SQL for Postgres, and `vertex_gemini` for in-region Gemini. Nothing in the product code changes.
