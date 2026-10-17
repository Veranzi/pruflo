"""The single source of wire values shared with the frontend.

Every model, schema and test imports these. Values match
frontend/lib/core/models.dart and Docs/backend_plan.md (Step 0).
"""

from enum import StrEnum


class CaseStatus(StrEnum):
    AWAITING_EVIDENCE = "awaiting_evidence"
    IN_REVIEW = "in_review"
    COMPLETED = "completed"


class ReviewDecision(StrEnum):
    """Every finding starts as PENDING."""

    APPROVE = "approve"
    REJECT = "reject"
    PENDING = "pending"


class Role(StrEnum):
    INVESTIGATOR = "investigator"
    REVIEWER = "reviewer"
    ADMIN = "admin"
    VIEWER = "viewer"


class Priority(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class EvidenceStatus(StrEnum):
    """Processing state of one evidence file, separate from the case status."""

    UPLOADING = "uploading"
    QUEUED = "queued"
    EXTRACTING = "extracting"
    GROUNDING = "grounding"
    RECONCILING = "reconciling"
    PROCESSED = "processed"
    NEEDS_REVIEW = "needs_review"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"


class GroundingStatus(StrEnum):
    GROUNDED = "grounded"
    AMBIGUOUS = "ambiguous"
    UNGROUNDED = "ungrounded"
    NEEDS_REVIEW = "needs_review"


class Severity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ApiKeyScope(StrEnum):
    CASES_READ = "cases:read"
    CASES_WRITE = "cases:write"
    EVIDENCE_WRITE = "evidence:write"
    FINDINGS_READ = "findings:read"
    REPORTS_READ = "reports:read"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"


class ReportFormat(StrEnum):
    PDF = "pdf"
    JSON = "json"
    CSV = "csv"


class ReportStatus(StrEnum):
    QUEUED = "queued"
    READY = "ready"
    FAILED = "failed"


class RuleTemplate(StrEnum):
    QUANTITY_SHORTFALL = "quantity_shortfall"
    AMOUNT_MISMATCH = "amount_mismatch"
    DELIVERY_DATE_WINDOW = "delivery_date_window"
    MISSING_DOCUMENT = "missing_document"


class WebhookEvent(StrEnum):
    EVIDENCE_PROCESSED = "evidence.processed"
    EVIDENCE_FAILED = "evidence.failed"
    CASE_IN_REVIEW = "case.in_review"
    FINDING_DECIDED = "finding.decided"
    CASE_COMPLETED = "case.completed"
    REPORT_READY = "report.ready"
    PING = "ping"


class ConnectorKind(StrEnum):
    EMAIL_INBOX = "email_inbox"
    GOOGLE_DRIVE = "google_drive"
    SHAREPOINT = "sharepoint"
    ODOO = "odoo"
    SAP = "sap"


class EvidenceSource(StrEnum):
    """Where an evidence file came from. Every source goes through the same checks."""

    UPLOAD = "upload"
    EMAIL_INBOX = "email_inbox"
    GOOGLE_DRIVE = "google_drive"
    SHAREPOINT = "sharepoint"
    ODOO = "odoo"


class ConnectorStatus(StrEnum):
    NOT_CONNECTED = "not_connected"
    CONNECTED = "connected"
    ERROR = "error"
    COMING_SOON = "coming_soon"


class OcrProviderName(StrEnum):
    GOOGLE_DOCUMENT_AI = "google_document_ai"
    AZURE_DOCUMENT_INTELLIGENCE = "azure_document_intelligence"
    AWS_TEXTRACT = "aws_textract"
    SELF_HOSTED = "self_hosted"


class AiProviderName(StrEnum):
    VERTEX_GEMINI = "vertex_gemini"
    GEMINI_API = "gemini_api"  # Gemini API with a paid-tier key (AI Studio)
    VERTEX_CLAUDE = "vertex_claude"
    AZURE_OPENAI = "azure_openai"
    SELF_HOSTED = "self_hosted"


class NotificationKind(StrEnum):
    EVIDENCE_PROCESSED = "evidence_processed"
    CASE_ASSIGNED = "case_assigned"
    CASE_IN_REVIEW = "case_in_review"
    REPORT_READY = "report_ready"
    WEEKLY_SUMMARY = "weekly_summary"
