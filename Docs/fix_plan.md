# PruFlo: deployment plan (fix plan)

Updated 2026-10-18. PruFlo is being submitted to the Google Cloud AI Builder Cup, which asks for Firebase, Firestore, Cloud Run and Gemini. This plan says where each part of PruFlo runs, why, what changed in the code, and what's left. It changes *where things run*, not what the product does.

Read with `Docs/backend_plan.md` (what's built) and the two API contracts. Where they disagree about infrastructure, this plan wins. **The step-by-step deployment instructions are in `Docs/deployment.md`.**

✅ done · 🟠 partly done · ❌ not started

## The constraint, checked

There's one billing account, linked to the "Gemini API" project (`gen-lang-client-0422375595`). The Firebase project (`login-register-125ef`) stays on the free **Spark** plan, because Firebase SQL Connect gives a Spark project a no-cost Cloud SQL instance for 90 days, and upgrading that project to Blaze would convert it to a paid instance (from about $9 a month; Firebase SQL Connect pricing, checked 2026-10-18).

| Need | Where | Cost |
| --- | --- | --- |
| Hosting, Authentication, Cloud Messaging | Firebase project, Spark | Free |
| **Cloud Firestore** (files and extracted text) | Firebase project, Spark | Free quota: 1 GiB stored, 50,000 reads and 20,000 writes a day |
| **Cloud SQL for PostgreSQL** | Firebase project: SQL Connect no-cost trial | Free for 90 days; about 8,000 operations a day |
| **Cloud Run** (API and worker) | Billed project | Free tier at demo volume; scales to zero |
| **Gemini on Vertex AI** | Billed project | Pay per use, about $0.02 per five-document case |
| Secret Manager, Cloud Build, Artifact Registry, Cloud Scheduler | Billed project | Free tier at demo volume |

The Cloud Run service account (in the billed project) is granted Cloud SQL Client, Cloud Datastore User and Firebase Authentication Viewer in the Firebase project. No service-account key exists anywhere.

## Decisions

| # | Decision | Choice |
| --- | --- | --- |
| D1 | Firebase project | **`login-register-125ef`** (display name Pruflo), on Spark. It hosts `pruflo.web.app`; `undercurrent-cb9f8.web.app` gets the same build |
| D2 | API and worker | **Cloud Run** service `pruflo-api` in the billed project, `us-east4`, one container for both (`Dockerfile.cloudrun`), min instances 0, CPU always allocated so uploads finish processing |
| D3 | Database | **Cloud SQL for PostgreSQL** (SQL Connect no-cost trial, `us-east4`) for structured records: cases, claims, findings, review actions, audit. Cloud Run connects through the built-in Cloud SQL connection; only an admin's own IP is on the authorized networks |
| D4 | Unstructured content | **Firestore** (location `nam5`): uploaded files in ~700 KB chunks, and each file's extracted page text and text blocks (`PRUFLO_CONTENT_BACKEND=firestore`, migration 0009) |
| D5 | Reading documents | **Gemini on Vertex AI** (provider `vertex_gemini`) with the service's identity; the Gemini API key (`gemini_api`) stays as an option, and the structured reader (no model) as the fallback |
| D6 | Queue and schedules | Files are processed in the API process right after upload (`PRUFLO_QUEUE_BACKEND=inline`); **Cloud Scheduler** (`pruflo-tick`) calls `POST /v1/internal/tick` every 6 hours for retries and scheduled tasks (more often keeps an instance up, and billed, much of the day) |
| D7 | OCR | None for now; scans end as `needs_review`. Document AI in the billed project is the follow-up |
| D8 | Data region | United States (Cloud Run and Cloud SQL `us-east4`, Firestore `nam5`), stated in the docs and privacy notes |

## How it fits together

```
Browser (pruflo.web.app, Firebase Hosting)
  │  Firebase Auth ID token (login-register-125ef)
  ▼
Cloud Run: pruflo-api (FastAPI + inline worker)  ──►  Gemini on Vertex AI
  │            │                    ▲                 (billed project)
  │            ▼                    │ POST /v1/internal/tick (every 6 hours)
  │     Firestore (files + extracted   Cloud Scheduler: pruflo-tick
  │      page text and blocks)
  ▼
Cloud SQL for PostgreSQL (structured records; pruflo_migrator owns the
schema, pruflo_app at runtime), through the Cloud SQL connection
```

- **Uploads:** endpoint 7 returns a link to `/v1/objects/...` on the API, HMAC-signed, valid 10 minutes, with the type and the 25 MB limit in the signature. The browser PUTs the file there; the API checks it while it streams in and stores it in Firestore. Endpoint 8 then checks the real type, pages and checksum as before.
- **Views** (endpoint 11) are signed `/v1/objects/...` GET links that stream the file back with `Cache-Control: private, no-store`.
- **Firestore rules** deny all browser access; only the backend's service account reads and writes.

## Work items

### Code

| Item | Status |
| --- | --- |
| Gemini API-key provider (`gemini_api`, migration 0008) for extraction, explanation rewrites and Q&A; Vertex stays the default | ✅ tested |
| Firestore with the runtime identity when no key is set, in the Firebase project even when Cloud Run is in another one | ✅ tested |
| `FirestoreStorage`: chunked files, checksum-verified reads, signed upload and view links through `/v1/objects` | ✅ tested (in-memory Firestore that enforces the 1 MiB limit) |
| `POST /v1/internal/tick` (token-protected): drain waiting files and retries, reconcile, webhook retries, connector sync, retention, Monday summary | ✅ tested |
| Extracted page text and blocks in Firestore (`app/storage/content.py`; migration 0009 drops `document_pages` and `text_blocks`) | ✅ tested |
| `Dockerfile.cloudrun` + `start.sh`, and `backend/deploy/cloudrun/setup.sh` (service account, secrets, Cloud Build, migration job, service, scheduler) | ✅ deployed 2026-10-18 |
| `.github/workflows/tick.yml`: manual "run now" button for the scheduled work, calling the Cloud Run URL | ✅ |
| `scripts/check_submission.py`: checks the live deployment and the deck against the submission rules | ✅ |
| Frontend: Firebase config from the build (no default project), the `gemini_api` provider option; deploy workflows pass `FIREBASE_PROJECT_ID` and `FIREBASE_AUTH_DOMAIN` | ✅ |
| Docs: `Docs/deployment.md` (hand-off guide), README deploy section | ✅ |

### Accounts and settings

| Step | Status |
| --- | --- |
| Firebase project: Firestore (`nam5`) | ✅ |
| Firebase project: web app, Email/Password + Google sign-in, authorised domains, Web Push key | 🟠 check in the Firebase console (`Docs/deployment.md`, step 1) |
| Cloud SQL trial instance via SQL Connect (`us-east4`), database `pruflo`, roles, SSL only, only an admin IP on authorized networks | ✅ |
| Billing on `gen-lang-client-0422375595`; database passwords and keys in its Secret Manager | ✅ |
| Cloud Run `pruflo-api`, job `pruflo-db-migrate`, scheduler `pruflo-tick` | ✅ https://pruflo-api-201197112958.us-east4.run.app |
| GitHub variables: `API_BASE_URL` = the Cloud Run URL, and the Firebase web config | ❌ |
| Merge to `main`, so pruflo.web.app is rebuilt against the API | ❌ |

**Done when:** sign in on `pruflo.web.app`, upload the synthetic purchase order, delivery note and invoice, and the case reaches `in_review` with the three findings; then a decision, the export and the report work, and `python scripts/check_submission.py --api <URL>` passes.

## Limits to know

- Cloud Run starts an instance on the first request after an idle spell (a few seconds); at most 2 instances.
- Firestore free quota: 1 GiB stored and 20,000 writes a day. A 25 MB file uses about 37 writes and 25 MB, so roughly 40 files at the maximum size fill the free storage.
- Cloud SQL trial: about 8,000 operations a day and 10 GB; archived after 90 days unless the project moves to Blaze. Take a `pg_dump` before demos.
- Data is stored in the United States (Cloud Run and Cloud SQL `us-east4`, Firestore `nam5`). Firebase Auth keeps sign-in records in Google's infrastructure.

## Later

- **After the trial:** move the Firebase project to Blaze (the instance becomes a paid Cloud SQL instance), or move the database to the billed project.
- **Full Google Cloud layout** (`backend_plan.md` Step 3): Cloud Storage for files (`PRUFLO_STORAGE_BACKEND=gcs`), Pub/Sub with a separate worker (`PRUFLO_QUEUE_BACKEND=pubsub`, `Dockerfile.worker`), Document AI for OCR. Nothing in the product code changes.
