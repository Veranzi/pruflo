# Pruflo web app (Flutter)

The Flutter client for Pruflo. People use it to create cases, upload evidence, review findings, record decisions and generate reports. Admins also manage users and integrations.

The app has no data of its own. Every screen reads from and writes to the Pruflo API (`backend/`) and shows a loading, empty or error state while it waits. The API is described in [`Docs/API_Contract.md`](../Docs/API_Contract.md) and [`Docs/Integrations_API_Contract.md`](../Docs/Integrations_API_Contract.md).

- Production: https://pruflo.web.app (Firebase Hosting in project `undercurrent-cb9f8`; sign-in, push and files in the Firebase project from `Docs/deployment.md`)
- Targets: web (primary). Android and iOS build from the same code but aren't released yet.

## Requirements

- Flutter 3.47 (stable) with Dart 3.13. Check with `flutter --version`.
- A running Pruflo API: locally from `backend/` (see [`backend/README.md`](../backend/README.md)), or a deployed Cloud Run URL.
- For real sign-in: a Firebase web app in the Firebase project, with the Email/Password, Google and (optionally) Microsoft providers enabled.

## Configuration

All settings are passed at build time with `--dart-define=NAME=value`. None of them are secrets: Firebase web config is public by design, and access is enforced by Firebase Auth and the API.

| Name | Default | What it does |
| --- | --- | --- |
| `API_BASE_URL` | `http://localhost:8000` | The Pruflo API, e.g. `https://pruflo-api-xxxx.a.run.app` |
| `FIREBASE_API_KEY` | *(empty)* | Turns on Firebase Auth. Without it the app uses development sign-in (below) |
| `FIREBASE_APP_ID` | *(empty)* | Firebase web app ID, `1:...:web:...` |
| `FIREBASE_MESSAGING_SENDER_ID` | *(empty)* | Firebase sender ID |
| `FIREBASE_PROJECT_ID` | *(required)* | Firebase project for sign-in and push |
| `FIREBASE_AUTH_DOMAIN` | `<project id>.firebaseapp.com` | Firebase auth domain |
| `FIREBASE_APP_CHECK_SITE_KEY` | *(empty)* | reCAPTCHA v3 site key. When set, every API call carries an App Check token |
| `FIREBASE_VAPID_KEY` | *(empty)* | Web push key (Firebase console → Cloud Messaging → Web Push certificates). With the Firebase config, the app asks for notification permission after sign-in and registers the browser for push. Empty: in-app notifications only |

You can find the Firebase values in the Firebase console under **Project settings → Your apps → Web app → SDK setup and configuration**.

The API requires an App Check token in production unless it runs with `PRUFLO_APP_CHECK_REQUIRED=false`. Set `FIREBASE_APP_CHECK_SITE_KEY` for production builds, or relax that API setting until App Check is registered.

## Run locally against the local API

1. Start the API and create an organisation and an admin (details in `backend/README.md`):

   ```bash
   cd backend
   cp .env.example .env    # set PRUFLO_DB_PASSWORD: openssl rand -hex 16
   docker compose up -d postgres
   python3.12 -m venv .venv && source .venv/bin/activate
   pip install -r requirements-dev.txt
   alembic upgrade head
   python -m app.cli bootstrap --tenant "My Org" --admin-email you@example.com --admin-name "Your Name"
   uvicorn app.main:app --reload --port 8000 --no-access-log
   ```

   To invite more people: `python -m app.cli invite --tenant ten_... --email someone@example.com --role reviewer`, or use the Admin screen in the app.

2. Run the app:

   ```bash
   cd frontend
   flutter pub get
   flutter run -d chrome
   ```

3. Sign in with an invited email.

**Real Firebase sign-in locally.** Put the build settings in `frontend/dart_defines.local.json` (git-ignored; the values are in Firebase console → Project settings → Your apps):

```json
{
  "API_BASE_URL": "http://localhost:8000",
  "FIREBASE_API_KEY": "...",
  "FIREBASE_APP_ID": "...",
  "FIREBASE_MESSAGING_SENDER_ID": "..."
}
```

Then run `flutter run -d chrome --dart-define-from-file=dart_defines.local.json`. Run the API with `PRUFLO_FIREBASE_PROJECT=<your project id>` and `PRUFLO_FIREBASE_CHECK_REVOKED=false` (see `backend/.env.example`). Sign in with an invited email: Google sign-in links immediately; email/password needs the verification link first.

**Development sign-in.** Builds without `FIREBASE_API_KEY` show a yellow "Development sign-in" banner and ask only for an email address. The app then sends the API's development token (`dev:<uid>:<email>`). The API accepts it only when it runs with `PRUFLO_ENV=local` and `PRUFLO_AUTH_DEV_MODE=true`, so this mode can't sign anyone in to a deployed API. The email is remembered in the browser, so reloading the page keeps you signed in on the page you were on; signing out forgets it, and if the API stops accepting the email (for example, it was removed) the app forgets it and shows the sign-in page.

## Notifications and profile photo

- **Bell.** The bell in the top bar shows how many notifications are unread. The count is checked every minute while you're signed in. Opening it lists recent notifications; tapping one marks it read and opens the case or report it's about, and "Mark all as read" clears the count (endpoints 51–53).
- **What you get** is set under **Profile → Notifications**: evidence processing finished or failed, a case assigned to you, a case ready for your review, a report ready, and a Monday summary (off by default). Each switch saves straight away (`PATCH /v1/me`).
- **Push.** With `FIREBASE_VAPID_KEY` set, the same notifications can also arrive as browser push messages, controlled by the "Also send to my devices" switch. The service worker FCM needs, `web/firebase-messaging-sw.js`, is committed: it shows the notification straight from the push payload and opens its link on click, so it needs no Firebase config. Without the key, push is skipped and nothing else changes. To check a real send once the key is set and someone has allowed notifications: `python -m app.cli test-push --email ...` in `backend/`.
- **Profile photo.** **Profile → Change photo** takes a PNG or JPEG up to 5 MB. It's uploaded through a signed URL (endpoints 54–55); the server checks the file and re-encodes it at most 512 × 512 px, which also strips location data. Photo links expire after about 10 minutes, so the app falls back to initials if a link has expired, and gets a fresh one the next time it loads your profile.

Admins also have **Admin → Customers** (cases can be filed under a customer, chosen when creating or editing a case), **Admin → Audit log**, and a **Members** section on each case to choose who can see it.

## Build for production

```bash
flutter build web --release \
  --dart-define=API_BASE_URL=https://pruflo-api-xxxx.a.run.app \
  --dart-define=FIREBASE_API_KEY=... \
  --dart-define=FIREBASE_APP_ID=... \
  --dart-define=FIREBASE_MESSAGING_SENDER_ID=... \
  --dart-define=FIREBASE_APP_CHECK_SITE_KEY=...
```

The output is in `build/web`.

Also add the hosting domain (`pruflo.web.app`) to **Firebase Authentication → Settings → Authorized domains**, and to the API's `PRUFLO_CORS_ORIGINS`.

## Deploy (Firebase Hosting)

Deployment is automated by two GitHub Actions workflows in `.github/workflows/`:

| Workflow | When | What |
| --- | --- | --- |
| `firebase-hosting-pull-request.yml` | A pull request to `main` | Builds the app and deploys a preview channel |
| `firebase-hosting-merge.yml` | A push to `main` | Builds the app and deploys to the live channel, including https://pruflo.web.app |

Hosting settings are in `firebase.json`: `build/web` is served with SPA rewrites to `index.html` and `Cache-Control: no-cache`, so browsers pick up new releases straight away.

Both workflows pass the build settings from GitHub repository variables (**Settings → Secrets and variables → Actions → Variables**):

| Variable | Required | Value |
| --- | --- | --- |
| `API_BASE_URL` | yes | The deployed API, `https://...` |
| `FIREBASE_API_KEY` | yes | From the Firebase web app config |
| `FIREBASE_APP_ID` | yes | From the Firebase web app config |
| `FIREBASE_MESSAGING_SENDER_ID` | yes | From the Firebase web app config |
| `FIREBASE_APP_CHECK_SITE_KEY` | if the API requires App Check | reCAPTCHA v3 site key |
| `FIREBASE_VAPID_KEY` | only for push | Web push key. Both workflows pass it; set the repository variable to turn push on |

A **Check build settings** step stops the live deploy if a required variable is missing or `API_BASE_URL` isn't `https`, so a misconfigured build never replaces the live site. On pull requests, missing variables only skip the preview (with a warning): the app is still built, so compile errors fail the check.

To deploy by hand, run `firebase deploy --only hosting:web --project pruflo` from `frontend/`. The project alias is in `frontend/.firebaserc`.

## Project structure

```
lib/
  main.dart                 starts the app and checks the saved session
  app/                      router (go_router) and theme
  core/
    config.dart             --dart-define settings
    api_client.dart         HTTP, auth headers, the API's error envelope
    api.dart                PrufloApi: profile, cases, evidence, findings, reports, admin
    integrations_api.dart   API keys, webhooks, sources, rules, providers
    auth_service.dart       Firebase or development sign-in; the user from /v1/me;
                            the development session is kept in shared_preferences
    push_service.dart       optional web push (FIREBASE_VAPID_KEY), POST /v1/devices
    models.dart             API models (fromJson), wire values for enums
    integration_models.dart Integrations models
    download*.dart          browser downloads and opening signed links
  features/                 one folder per screen
  widgets/                  shared widgets, including AsyncView (loading/error/empty),
                            the notifications bell, user avatars and the customer picker
test/
  support/fake_api.dart     a fake API returning contract-shaped JSON
```

What each role can do is decided by the API. The app only hides actions a role can't take: viewers can't decide or export, and only admins see Admin and Integrations.

## Test

```bash
flutter analyze
flutter test
```

The tests run against `test/support/fake_api.dart`, which answers with JSON copied from the API contracts. They cover:

- every route rendering at phone and desktop widths;
- data from the API reaching the screens;
- empty organisations showing empty states;
- sign-in errors;
- review decision rules;
- role-based hiding.

If you change the API contract, update `models.dart` and the fake together.
