"""Checks PruFlo against the AI Builder Cup submission rules.

Run before submitting, and again during the evaluation window, because the
rules give no retries if anything is down:

    python scripts/check_submission.py --api https://pruflo-api-....run.app \
        --video https://youtu.be/...

Uses only the standard library. With the Google Cloud CLI signed in, it also
checks the deployed configuration (Firestore, Gemini, Cloud SQL) directly.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = {
    "site": "https://pruflo.web.app",
    "repo": "Veranzi/pruflo",
    "project": "login-register-125ef",  # Firebase: Auth, Firestore, Cloud SQL
    "run_project": "gen-lang-client-0422375595",  # Cloud Run and Vertex Gemini
    "region": "us-east4",
    "service": "pruflo-api",
    "deck": ROOT / "Docs" / "PruFlo_Submission_AI_Builder_Cup.pptx",
}


@dataclass
class Result:
    requirement: str
    status: str  # PASS, FAIL, WARN or SKIP
    detail: str


results: list[Result] = []


def record(requirement: str, ok: bool | None, detail: str, warn_only: bool = False) -> None:
    if ok is None:
        status = "SKIP"
    elif ok:
        status = "PASS"
    else:
        status = "WARN" if warn_only else "FAIL"
    results.append(Result(requirement, status, detail))


def fetch(url: str, timeout: float = 20) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "pruflo-submission-check"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(2_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(200_000).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        return 0, str(exc)


def gcloud(*args: str) -> str | None:
    exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not exe:
        return None
    proc = subprocess.run([exe, *args], capture_output=True, text=True, timeout=120)
    return proc.stdout.strip() if proc.returncode == 0 else None


def check_site(site: str, project: str) -> None:
    code, body = fetch(site)
    record("Prototype link is live", code == 200, f"{site} answered {code or body}")
    if code != 200:
        return
    code, js = fetch(site.rstrip("/") + "/main.dart.js", timeout=60)
    if code != 200:
        record("Web app uses Firebase Auth", None, "couldn't download main.dart.js")
        return
    record("Web app uses Firebase Auth", project in js,
           f"Firebase project {project} {'is' if project in js else 'is NOT'} built into the app")
    uses_mock = "Incorrect email or password." in js and "provider-" in js
    record("Web app signs in for real (not the mock)", not uses_mock,
           "mock sign-in code found in the build" if uses_mock else "no mock sign-in found",
           warn_only=True)


def check_api(api: str | None, site: str) -> None:
    if not api:
        record("API runs on Cloud Run", None, "pass --api <Cloud Run URL>")
        return
    record("API runs on Cloud Run", ".run.app" in api, api)
    # Cloud Run reserves /healthz; /readyz also proves the database answers.
    code, body = fetch(api.rstrip("/") + "/readyz")
    record("API answers and database connected (/readyz)", code == 200,
           body[:120].strip())
    code, _ = fetch(api.rstrip("/") + "/v1/me")
    record("API requires Firebase sign-in", code == 401, f"/v1/me without a token answered {code}")
    code, js = fetch(site.rstrip("/") + "/main.dart.js", timeout=60)
    if code == 200:
        record("Web app points at this API", api.rstrip("/") in js,
               "API_BASE_URL is built into the app" if api.rstrip("/") in js
               else "rebuild the web app with API_BASE_URL set to this URL")


def check_cloud(project: str, run_project: str, region: str, service: str) -> None:
    raw = gcloud("run", "services", "describe", service, "--project", run_project,
                 "--region", region, "--format=json")
    if raw is None:
        record("Cloud configuration (via gcloud)", None,
               "gcloud not installed or not signed in; skipped")
        return
    container = json.loads(raw)["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value", "(secret)") for e in container.get("env", [])}
    annotations = json.loads(raw)["spec"]["template"]["metadata"].get("annotations", {})
    record("Firestore holds uploaded files", env.get("PRUFLO_STORAGE_BACKEND") == "firestore",
           f"PRUFLO_STORAGE_BACKEND={env.get('PRUFLO_STORAGE_BACKEND')}")
    record("Gemini is switched on", env.get("PRUFLO_GEMINI_ENABLED") == "true",
           f"PRUFLO_GEMINI_ENABLED={env.get('PRUFLO_GEMINI_ENABLED')}")
    record("Cloud SQL attached", "run.googleapis.com/cloudsql-instances" in annotations,
           annotations.get("run.googleapis.com/cloudsql-instances", "none"))
    firestore = gcloud("firestore", "databases", "describe", "--database=(default)",
                       "--project", project, "--format=value(locationId)")
    record("Firestore database exists", bool(firestore), firestore or "not found")
    enabled = gcloud("services", "list", "--enabled", "--project", run_project,
                     "--format=value(config.name)") or ""
    record("Vertex AI (Gemini) API enabled", "aiplatform.googleapis.com" in enabled,
           "aiplatform.googleapis.com")
    logs = gcloud("logging", "read",
                  f'resource.type="cloud_run_revision" AND resource.labels.service_name="{service}" '
                  'AND "evidence processed"', "--project", run_project, "--limit=1",
                  "--freshness=7d", "--format=value(timestamp)")
    record("Documents processed on Cloud Run (last 7 days)", bool(logs),
           f"last at {logs}" if logs else "none yet: upload the demo documents to a case",
           warn_only=True)


def check_repo(repo: str) -> None:
    code, body = fetch(f"https://api.github.com/repos/{repo}")
    if code != 200:
        record("GitHub repository is public", False, f"GitHub answered {code}")
        return
    info = json.loads(body)
    record("GitHub repository is public", not info.get("private", True),
           f"https://github.com/{repo} (default branch {info.get('default_branch')})")
    branch = info.get("default_branch", "main")
    code, _ = fetch(f"https://raw.githubusercontent.com/{repo}/{branch}/backend/deploy/cloudrun/setup.sh")
    record("Cloud Run deployment is on the default branch", code == 200,
           f"backend/deploy/cloudrun/setup.sh on {branch}: {code}", warn_only=True)


def check_video(video: str | None) -> None:
    if not video:
        record("Demo video link", False, "pass --video <link>; the video must be 3-4 minutes")
        return
    code, _ = fetch(video)
    record("Demo video link opens", code == 200, f"{video} answered {code}")


def check_deck(deck: Path) -> None:
    if not deck.exists():
        record("Pitch deck", False, f"{deck} not found")
        return
    text = ""
    with zipfile.ZipFile(deck) as z:
        for name in z.namelist():
            if name.startswith("ppt/slides/slide") and name.endswith(".xml"):
                text += z.read(name).decode("utf-8", "replace")
    placeholders = [p for p in ("ADD VIDEO LINK", "Pending") if p in text]
    record("Pitch deck has no placeholders", not placeholders,
           "found: " + ", ".join(placeholders) if placeholders else deck.name)
    pdf = deck.with_suffix(".pdf")
    record("Pitch deck exported to PDF", pdf.exists() and pdf.stat().st_mtime >= deck.stat().st_mtime,
           pdf.name if pdf.exists() else "export the deck to PDF")
    for tech in ("Firebase", "Firestore", "Cloud Run", "Gemini"):
        record(f"Deck mentions {tech}", tech in text, tech)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", help="Cloud Run URL of the API")
    parser.add_argument("--video", help="demo video link")
    parser.add_argument("--site", default=DEFAULTS["site"])
    parser.add_argument("--repo", default=DEFAULTS["repo"])
    parser.add_argument("--project", default=DEFAULTS["project"])
    parser.add_argument("--run-project", default=DEFAULTS["run_project"])
    parser.add_argument("--region", default=DEFAULTS["region"])
    parser.add_argument("--service", default=DEFAULTS["service"])
    parser.add_argument("--deck", type=Path, default=DEFAULTS["deck"])
    args = parser.parse_args()

    check_site(args.site, args.project)
    check_api(args.api, args.site)
    check_cloud(args.project, args.run_project, args.region, args.service)
    check_repo(args.repo)
    check_video(args.video)
    check_deck(args.deck)

    width = max(len(r.requirement) for r in results)
    for r in results:
        print(f"{r.status:<5} {r.requirement:<{width}}  {r.detail}")
    failed = [r for r in results if r.status == "FAIL"]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks not failing.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
