"""Database tables (Docs/backend_plan.md, Step 2).

Conventions:
- Primary keys are prefixed random strings from ``app.ids.new_id``.
- Money and quantities are NUMERIC, never float.
- Enum columns are text with a CHECK constraint built from ``app.enums``.
- ``review_actions`` and ``audit_events`` are append-only; a trigger in the
  migration rejects UPDATE and DELETE on them.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamped, enum_check
from app.enums import (
    AiProviderName,
    CaseStatus,
    ConnectorKind,
    ConnectorStatus,
    EvidenceSource,
    EvidenceStatus,
    GroundingStatus,
    JobStatus,
    NotificationKind,
    OcrProviderName,
    Priority,
    ReportFormat,
    ReportStatus,
    ReviewDecision,
    Role,
    RuleTemplate,
    Severity,
)

TZ = DateTime(timezone=True)


def _fk(target: str, ondelete: str = "RESTRICT") -> ForeignKey:
    return ForeignKey(target, ondelete=ondelete)


# People ---------------------------------------------------------------------


class Tenant(Timestamped, Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    case_prefix: Mapped[str] = mapped_column(String(8), server_default="PL")
    # Next human case number, e.g. 7 -> "PL-007". Incremented with UPDATE ... RETURNING.
    next_case_number: Mapped[int] = mapped_column(Integer, server_default="1")


class User(Timestamped, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    # Empty until an invited user first signs in (linked by verified email).
    firebase_uid: Mapped[str | None] = mapped_column(String(128), unique=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    name: Mapped[str] = mapped_column(String(200), server_default="")
    phone: Mapped[str] = mapped_column(String(40), server_default="")
    job_title: Mapped[str] = mapped_column(String(120), server_default="")
    active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    avatar_key: Mapped[str | None] = mapped_column(String(300))  # re-encoded JPEG in storage
    # Per-kind on/off plus "push"; missing keys use the defaults in services/notifications.py.
    notification_prefs: Mapped[dict[str, bool]] = mapped_column(JSONB, server_default="{}")


class Membership(Timestamped, Base):
    """A user's role in a tenant. Roles are managed on the server only."""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id"),
        enum_check("role", Role),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(_fk("tenants.id"), index=True)
    user_id: Mapped[str] = mapped_column(_fk("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))


class Device(Timestamped, Base):
    """FCM push token registered by the app (endpoints 2 and 22)."""

    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("user_id", "token"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(_fk("users.id", "CASCADE"), index=True)
    token: Mapped[str] = mapped_column(String(4096))
    platform: Mapped[str] = mapped_column(String(20), server_default="web")


class Customer(Timestamped, Base):
    """A tenant's own customer that cases can be filed under (endpoints 28-30)."""

    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(_fk("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    contact_email: Mapped[str] = mapped_column(String(320), server_default="")


# Cases and evidence ---------------------------------------------------------


class Case(Timestamped, Base):
    __tablename__ = "cases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "reference"),
        enum_check("status", CaseStatus),
        enum_check("priority", Priority),
        Index("ix_cases_tenant_status_created", "tenant_id", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(_fk("tenants.id"))
    reference: Mapped[str] = mapped_column(String(20))  # "PL-001", shown to people
    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(80), server_default="")
    description: Mapped[str] = mapped_column(Text, server_default="")
    status: Mapped[str] = mapped_column(String(20), server_default=CaseStatus.AWAITING_EVIDENCE)
    priority: Mapped[str] = mapped_column(String(10), server_default=Priority.MEDIUM)
    customer_id: Mapped[str | None] = mapped_column(_fk("customers.id"))
    assigned_to: Mapped[str | None] = mapped_column(_fk("users.id"))
    created_by: Mapped[str | None] = mapped_column(_fk("users.id"))
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())
    version: Mapped[int] = mapped_column(Integer, server_default="1")


class CaseMember(Timestamped, Base):
    __tablename__ = "case_members"

    case_id: Mapped[str] = mapped_column(_fk("cases.id", "CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(_fk("users.id", "CASCADE"), primary_key=True, index=True)
    added_by: Mapped[str | None] = mapped_column(_fk("users.id"))


class EvidenceFile(Timestamped, Base):
    __tablename__ = "evidence_files"
    __table_args__ = (
        enum_check("status", EvidenceStatus),
        enum_check("source", EvidenceSource),
        CheckConstraint("byte_size >= 0", name="byte_size_non_negative"),
        Index("ix_evidence_files_case_created", "case_id", "created_at"),
        # The same external file is imported once.
        Index(
            "uq_evidence_files_source_ref",
            "tenant_id",
            "source",
            "source_ref",
            unique=True,
            postgresql_where="source_ref IS NOT NULL",
        ),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(_fk("tenants.id"), index=True)
    case_id: Mapped[str] = mapped_column(_fk("cases.id"))
    object_key: Mapped[str] = mapped_column(String(300), unique=True)  # chosen by the server
    original_filename: Mapped[str] = mapped_column(String(255))
    declared_mime: Mapped[str] = mapped_column(String(120))
    detected_mime: Mapped[str | None] = mapped_column(String(120))
    byte_size: Mapped[int] = mapped_column(Integer)  # declared, then the server-measured size
    page_count: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))  # computed by the server
    evidence_types: Mapped[list[str]] = mapped_column(ARRAY(String(20)), server_default="{}")
    status: Mapped[str] = mapped_column(String(20), server_default=EvidenceStatus.UPLOADING)
    error_class: Mapped[str | None] = mapped_column(String(80))
    parser_version: Mapped[str | None] = mapped_column(String(80))
    uploaded_by: Mapped[str | None] = mapped_column(_fk("users.id"))
    uploaded_at: Mapped[datetime | None] = mapped_column(TZ)
    # Set when the retention job deleted the file and its text; the row stays
    # for the audit trail and the findings that cite it.
    purged_at: Mapped[datetime | None] = mapped_column(TZ)
    source: Mapped[str] = mapped_column(String(20), server_default=EvidenceSource.UPLOAD)
    # The source's own ID for the file (message, Drive file, Odoo attachment).
    source_ref: Mapped[str | None] = mapped_column(String(300))
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class ProcessingJob(Timestamped, Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (enum_check("status", JobStatus),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(_fk("evidence_files.id"), index=True)
    # Same key for every redelivery of the same work, so duplicates are no-ops.
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    status: Mapped[str] = mapped_column(String(20), server_default=JobStatus.QUEUED)
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    lease_until: Mapped[datetime | None] = mapped_column(TZ)
    error_class: Mapped[str | None] = mapped_column(String(80))
    next_retry_at: Mapped[datetime | None] = mapped_column(TZ)
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


# AI output and results --------------------------------------------------------


class ExtractedClaim(Timestamped, Base):
    """What the model said, kept unchanged. Corrections live in review_actions."""

    __tablename__ = "extracted_claims"
    __table_args__ = (enum_check("grounding", GroundingStatus),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(_fk("tenants.id"))
    case_id: Mapped[str] = mapped_column(_fk("cases.id"), index=True)
    evidence_id: Mapped[str] = mapped_column(_fk("evidence_files.id"), index=True)
    field_name: Mapped[str] = mapped_column(String(80))
    # Line items: the item as written; the rule engine normalises it to match documents.
    item_key: Mapped[str | None] = mapped_column(String(200))
    # Where in the document: a cell range, a column label.
    context: Mapped[str] = mapped_column(String(300), server_default="")
    raw_value: Mapped[str | None] = mapped_column(Text)
    normalized_value: Mapped[str | None] = mapped_column(Text)  # decimals kept as strings
    data_type: Mapped[str] = mapped_column(String(20))
    unit: Mapped[str | None] = mapped_column(String(20))
    source_quote: Mapped[str | None] = mapped_column(Text)
    page_index: Mapped[int | None] = mapped_column(Integer)
    span_start: Mapped[int | None] = mapped_column(Integer)
    span_end: Mapped[int | None] = mapped_column(Integer)
    bbox: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    grounding: Mapped[str] = mapped_column(String(20), server_default=GroundingStatus.NEEDS_REVIEW)
    extraction_status: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    # Reproducibility: which provider, model, prompt, schema, OCR and region produced this.
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(40))
    schema_version: Mapped[str] = mapped_column(String(40))
    ocr_version: Mapped[str | None] = mapped_column(String(80))
    region: Mapped[str] = mapped_column(String(40))


class Rule(Timestamped, Base):
    """A tenant's instance of a rule template. Parameters live in rule_versions."""

    __tablename__ = "rules"
    __table_args__ = (
        UniqueConstraint("tenant_id", "template"),
        enum_check("template", RuleTemplate),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(_fk("tenants.id"))
    template: Mapped[str] = mapped_column(String(40))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="true")
    current_version: Mapped[int] = mapped_column(Integer, server_default="1")


class RuleVersion(Timestamped, Base):
    __tablename__ = "rule_versions"
    __table_args__ = (UniqueConstraint("rule_id", "version"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    rule_id: Mapped[str] = mapped_column(_fk("rules.id"))
    version: Mapped[int] = mapped_column(Integer)
    params: Mapped[dict[str, str]] = mapped_column(
        JSONB, server_default="{}"
    )  # decimals as strings
    created_by: Mapped[str | None] = mapped_column(_fk("users.id"))


class Finding(Timestamped, Base):
    __tablename__ = "findings"
    __table_args__ = (
        enum_check("review_status", ReviewDecision),
        enum_check("severity", Severity),
        Index("ix_findings_case_status", "case_id", "review_status"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(_fk("tenants.id"))
    case_id: Mapped[str] = mapped_column(_fk("cases.id"))
    rule_id: Mapped[str] = mapped_column(_fk("rules.id"), index=True)
    rule_version: Mapped[int] = mapped_column(Integer)
    finding_type: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(10))
    input_claim_ids: Mapped[list[str]] = mapped_column(ARRAY(String(40)), server_default="{}")
    expected: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB
    )  # {"value": "100", "unit": "units"}
    observed: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    difference: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    explanation: Mapped[str] = mapped_column(Text, server_default="")
    explanation_source: Mapped[str] = mapped_column(String(20), server_default="template")
    review_status: Mapped[str] = mapped_column(String(10), server_default=ReviewDecision.PENDING)
    # Rule + version + inputs; re-running the same job can't create a second copy.
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class ReviewAction(Timestamped, Base):
    """Append-only. Only people write these, never API keys or the model."""

    __tablename__ = "review_actions"
    __table_args__ = (enum_check("decision", ReviewDecision),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(_fk("tenants.id"))
    case_id: Mapped[str] = mapped_column(_fk("cases.id"), index=True)
    finding_id: Mapped[str] = mapped_column(_fk("findings.id"), index=True)
    actor_user_id: Mapped[str] = mapped_column(_fk("users.id"))
    decision: Mapped[str] = mapped_column(String(10))
    note: Mapped[str] = mapped_column(Text, server_default="")
    correction: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # {field, previous, new}
    request_id: Mapped[str] = mapped_column(String(64))


class Report(Timestamped, Base):
    __tablename__ = "reports"
    __table_args__ = (enum_check("format", ReportFormat), enum_check("status", ReportStatus))

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(_fk("tenants.id"))
    case_id: Mapped[str] = mapped_column(_fk("cases.id"), index=True)
    format: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(10), server_default=ReportStatus.QUEUED)
    title: Mapped[str] = mapped_column(String(200), server_default="")
    object_key: Mapped[str | None] = mapped_column(String(300))
    template_version: Mapped[str] = mapped_column(String(40))
    # What was asked for, e.g. {"type": "detailed", "sections": ["evidence", "findings"]}.
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    sha256: Mapped[str | None] = mapped_column(String(64))
    requested_by: Mapped[str | None] = mapped_column(_fk("users.id"))
    completed_at: Mapped[datetime | None] = mapped_column(TZ)


# Integrations -----------------------------------------------------------------


class ApiKey(Timestamped, Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(_fk("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    prefix: Mapped[str] = mapped_column(String(12))  # first 12 characters, for display
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)  # SHA-256 of the full key
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(30)))
    created_by: Mapped[str | None] = mapped_column(_fk("users.id"))
    last_used_at: Mapped[datetime | None] = mapped_column(TZ)
    revoked_at: Mapped[datetime | None] = mapped_column(TZ)  # revocation is permanent


class WebhookEndpoint(Timestamped, Base):
    __tablename__ = "webhook_endpoints"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(_fk("tenants.id"), index=True)
    url: Mapped[str] = mapped_column(String(2048))
    description: Mapped[str] = mapped_column(String(200), server_default="")
    events: Mapped[list[str]] = mapped_column(ARRAY(String(40)))
    active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    secret_ref: Mapped[str] = mapped_column(String(300))  # Secret Manager name, not the secret
    failing_since: Mapped[datetime | None] = mapped_column(TZ)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (Index("ix_webhook_deliveries_endpoint_at", "endpoint_id", "at"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    endpoint_id: Mapped[str] = mapped_column(_fk("webhook_endpoints.id", "CASCADE"))
    event_id: Mapped[str] = mapped_column(String(40))  # stable across retries
    event: Mapped[str] = mapped_column(String(40))
    attempt: Mapped[int] = mapped_column(Integer)
    status_code: Mapped[int] = mapped_column(Integer)  # 0 when the receiver couldn't be reached
    at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    next_attempt_at: Mapped[datetime | None] = mapped_column(TZ)


class Connector(Base):
    __tablename__ = "connectors"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind"),
        enum_check("kind", ConnectorKind),
        enum_check("status", ConnectorStatus),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(_fk("tenants.id"))
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), server_default=ConnectorStatus.NOT_CONNECTED)
    detail: Mapped[str] = mapped_column(String(500), server_default="")
    secret_ref: Mapped[str | None] = mapped_column(
        String(300)
    )  # credentials stay in Secret Manager
    # Credentials and refresh tokens, encrypted (app.services.secret_box). Never returned.
    secret: Mapped[str | None] = mapped_column(Text)
    # Importer bookmarks: folder IDs, the last Odoo attachment, folder -> case.
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    connected_by: Mapped[str | None] = mapped_column(_fk("users.id"))
    connected_at: Mapped[datetime | None] = mapped_column(TZ)
    last_synced_at: Mapped[datetime | None] = mapped_column(TZ)


class ProviderSettings(Base):
    __tablename__ = "provider_settings"
    __table_args__ = (enum_check("ocr", OcrProviderName), enum_check("ai", AiProviderName))

    tenant_id: Mapped[str] = mapped_column(_fk("tenants.id"), primary_key=True)
    ocr: Mapped[str] = mapped_column(String(40), server_default=OcrProviderName.GOOGLE_DOCUMENT_AI)
    ai: Mapped[str] = mapped_column(String(40), server_default=AiProviderName.VERTEX_GEMINI)
    model: Mapped[str] = mapped_column(String(80))
    region: Mapped[str] = mapped_column(String(40), server_default="asia-southeast1")
    updated_by: Mapped[str | None] = mapped_column(_fk("users.id"))
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


# Reliability and audit ----------------------------------------------------------


class OutboxEvent(Timestamped, Base):
    """Written in the same transaction as the change it describes, then published."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_unpublished", "created_at", postgresql_where="published_at IS NULL"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(_fk("tenants.id"))
    topic: Mapped[str] = mapped_column(String(40))  # "processing" or "webhook"
    type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)  # IDs only, never document content
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    published_at: Mapped[datetime | None] = mapped_column(TZ)


class AuditEvent(Timestamped, Base):
    """Append-only."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_events_case_created", "case_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(_fk("tenants.id"))
    actor_user_id: Mapped[str | None] = mapped_column(_fk("users.id"))
    actor_api_key_id: Mapped[str | None] = mapped_column(_fk("api_keys.id"))
    action: Mapped[str] = mapped_column(String(60))  # e.g. "case.created"
    target_type: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[str] = mapped_column(String(40))
    case_id: Mapped[str | None] = mapped_column(_fk("cases.id"))
    request_id: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


class Notification(Timestamped, Base):
    """In-app notification for one person. Kept 90 days."""

    __tablename__ = "notifications"
    __table_args__ = (
        enum_check("kind", NotificationKind),
        Index("ix_notifications_user_created", "user_id", "created_at"),
        Index("ix_notifications_unread", "user_id", postgresql_where="read_at IS NULL"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(_fk("tenants.id"))
    user_id: Mapped[str] = mapped_column(_fk("users.id", "CASCADE"))
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(500), server_default="")
    case_id: Mapped[str | None] = mapped_column(_fk("cases.id", "CASCADE"))
    link: Mapped[str] = mapped_column(String(300), server_default="")
    read_at: Mapped[datetime | None] = mapped_column(TZ)
