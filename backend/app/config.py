"""Settings read from environment variables prefixed with PRUFLO_.

On Cloud Run, secrets (such as the database URL) are mounted from Secret
Manager as environment variables, so the code reads them the same way.
"""

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

# Upload limits shared with the frontend (Docs/backend_plan.md, Step 0).
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_PAGES = 200
MAX_FILES_PER_BATCH = 20


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PRUFLO_", env_file=".env", extra="ignore")

    env: Literal["local", "test", "dev", "staging", "prod"] = "local"
    # On Cloud Run the full URL comes from Secret Manager as PRUFLO_DATABASE_URL.
    # Locally it's built from the parts below; the password lives only in the
    # git-ignored backend/.env, which docker-compose.yml reads too.
    database_url: str = ""
    # Migrations log in as a separate role that owns the schema; the API and
    # worker use database_url (the runtime role, which can't change the schema
    # or rewrite append-only tables). Falls back to the runtime URL locally.
    migration_database_url: str = ""
    db_user: str = "pruflo"
    db_password: str = ""
    db_host: str = "localhost"
    db_port: int = 5433
    db_name: str = "pruflo"
    # Connections per instance: pool + overflow. Keep
    # (pool + overflow) x (API max instances + worker max instances) below the
    # Cloud SQL connection limit, minus a few for migrations and admin.
    db_pool_size: int = 5
    db_max_overflow: int = 5
    # The deployed commit (the deploy step sets it), shown by /healthz.
    release: str = "dev"
    gcp_project: str = "pruflo-local"
    # The Firebase project whose sign-in tokens are accepted (Docs/deployment.md).
    # Defaults to gcp_project when they are the same project.
    firebase_project: str = ""
    # Also ask Firebase whether the session was revoked (needs the runtime
    # identity to have Firebase Auth admin access on that project). Turn off
    # only for local development.
    firebase_check_revoked: bool = True
    # Outside Google Cloud there's no runtime identity: firebase-admin (sign-in
    # checks, push, Firestore file storage) uses the project's service-account
    # key, given as the JSON text (a host secret) or a path to the file.
    firebase_credentials: str = ""
    firebase_credentials_file: str = ""
    region: str = "asia-southeast1"
    evidence_bucket: str = "pruflo-evidence-local"
    pubsub_topic: str = "evidence-jobs"
    signed_url_minutes: int = 10
    # Signs URLs through the IAM signBlob API when the credentials can't sign
    # themselves (e.g. a person's gcloud login running check-storage; they need
    # Service Account Token Creator on it). On Cloud Run the runtime service
    # account is used automatically.
    signing_service_account: str = ""
    # "firestore": files in Firestore (free Firebase plan); "local": on disk
    # (local and test only); "gcs": Cloud Storage (needs Google Cloud billing).
    storage_backend: Literal["firestore", "local", "gcs"] = "local"
    local_storage_dir: str = ".local-storage"
    firestore_collection: str = "pruflo_objects"
    # Signs the upload and view links this API hands out (Firestore and local
    # storage). Required outside local and test.
    storage_signing_key: str = Field(
        default="",
        validation_alias=AliasChoices("PRUFLO_STORAGE_SIGNING_KEY", "PRUFLO_LOCAL_SIGNING_KEY"),
    )
    public_base_url: str = "http://localhost:8000"
    # The web app, for links in push notifications.
    public_app_url: str = "https://pruflo.web.app"
    # "pubsub" on Cloud Run. "inline" runs jobs in this process right after the
    # request (local development), so no Pub/Sub emulator is needed.
    queue_backend: Literal["pubsub", "inline"] = "inline"
    # The worker accepts calls only with a Google-signed OIDC token for this
    # audience (the worker's URL) from one of these service accounts
    # (the Pub/Sub push and Cloud Scheduler identities).
    # Enables POST /v1/internal/tick (scheduled work on hosts with no worker
    # service or Cloud Scheduler). Sent as X-Pruflo-Tick-Token.
    tick_token: str = ""
    worker_push_audience: str = ""
    worker_invoker_service_accounts: list[str] = []
    # Document AI OCR. Leave the processor empty to run without OCR: scanned
    # pages and images are then marked needs_review. There's no default
    # location on purpose: choosing one outside asia-southeast1 sends documents
    # out of the region, which needs sign-off (Docs/backend_plan.md, Step 0).
    documentai_location: str = ""
    documentai_processor_id: str = ""
    # Gemini on Vertex AI reads documents for claims, with each tenant's model
    # and region (endpoint 49). Off: the structured reader is used instead.
    gemini_enabled: bool = False
    # For tenants on the "gemini_api" provider: a paid-tier Gemini API key
    # (from Secret Manager on Cloud Run). Free-tier keys must not be used for
    # real documents: Google may use free-tier prompts to improve its products.
    gemini_api_key: str = ""
    # Also let Gemini reword finding explanations (labelled machine_generated).
    gemini_explanations: bool = False
    # Numeric dates such as 03/10/2026 are read day first, as in Singapore,
    # Malaysia and Indonesia.
    dates_day_first: bool = True

    # Document sources (Docs/Integrations_API_Contract.md §3). Each one is
    # offered only once its settings are present.
    # Email inbox: the inbound mail service (SendGrid Inbound Parse, Mailgun
    # routes, or any relay) posts each message to /v1/inbound/email with this
    # token. Addresses look like evidence+<tag>@<inbound_email_domain>.
    inbound_email_domain: str = ""
    inbound_email_token: str = ""
    # The authserv-id the mail service writes into Authentication-Results
    # (e.g. mx.sendgrid.net). When set, only that header is trusted.
    inbound_authserv_id: str = ""
    # OAuth apps for Google Drive (read-only) and SharePoint (Sites.Selected).
    # The redirect URI to register is <public_base_url>/v1/connectors/oauth/callback.
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    microsoft_oauth_client_id: str = ""
    microsoft_oauth_client_secret: str = ""
    # How often Cloud Scheduler should call /tasks/sync-connectors; a source
    # synced more recently than this is skipped.
    connector_sync_minutes: int = 15
    # Retention (days; 0 keeps forever). Evidence counts from when its case was
    # completed; reports from when they were made. Notifications (90 days) and
    # webhook delivery logs (30 days) have fixed periods.
    evidence_retention_days: int = 0
    report_retention_days: int = 0

    # Encrypts secrets the API must read back, such as webhook signing secrets
    # (a Fernet key: python -c "from cryptography.fernet import Fernet;
    # print(Fernet.generate_key().decode())"). Required outside local and test.
    secrets_key: str = ""

    # Browser origins allowed to call the API (the Flutter web app).
    cors_origins: list[str] = ["https://pruflo.web.app", "https://pruflo.firebaseapp.com"]

    # Local only: accept "Bearer dev:<uid>:<email>" in place of a Firebase token.
    # Refused outside local and test environments (see app.auth.firebase).
    auth_dev_mode: bool = False
    # App Check is skipped in local and test, and enforced everywhere else.
    app_check_required: bool = True

    @property
    def migration_url(self) -> str:
        if self.migration_database_url:
            return _psycopg(self.migration_database_url)
        return self.sqlalchemy_url

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return _psycopg(self.database_url)
        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=self.db_password or None,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        ).render_as_string(hide_password=False)


def _psycopg(url: str) -> str:
    """Hosts such as Supabase and Render hand out ``postgres://`` or
    ``postgresql://`` URLs; SQLAlchemy needs the psycopg driver named."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
