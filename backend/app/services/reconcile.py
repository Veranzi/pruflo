"""Reconcile a case: run the rules over its grounded claims and update its
findings and status (Docs/backend_plan.md, Steps 11 and 12).

Runs after a file finishes processing, once no other file in the case is
still waiting. The case row is locked, so two files finishing together
reconcile one after the other.

- New findings are added with ``review_status = pending``.
- A finding the rules no longer produce (a later delivery note covered the
  shortfall) is removed only if nobody has reviewed it; reviewed findings
  always stay.
- Then the case moves to ``in_review`` if any finding is pending, otherwise
  to ``completed`` (Step 0).
"""

import logging
import re
from collections import defaultdict

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    Case,
    EvidenceFile,
    ExtractedClaim,
    Finding,
    ReviewAction,
)
from app.enums import CaseStatus, EvidenceStatus, GroundingStatus, ReviewDecision, RuleTemplate
from app.ids import new_id
from app.pipeline.claims.schema import DocumentType, item_key
from app.pipeline.rules import templates
from app.pipeline.rules.engine import Doc, Fact, Proposed, evaluate
from app.services import audit
from app.services import cases as case_service

logger = logging.getLogger("pruflo.reconcile")

# Files in these states will change the case's claims, so wait for them.
UNFINISHED = {
    EvidenceStatus.QUEUED,
    EvidenceStatus.EXTRACTING,
    EvidenceStatus.GROUNDING,
    EvidenceStatus.RECONCILING,
    EvidenceStatus.FAILED_RETRYABLE,
}


def documents(db: Session, case_id: str) -> list[Doc]:
    """The case's documents as the rules see them: grounded claims only."""
    rows = db.execute(
        select(ExtractedClaim, EvidenceFile.original_filename)
        .join(EvidenceFile, EvidenceFile.id == ExtractedClaim.evidence_id)
        .where(
            ExtractedClaim.case_id == case_id,
            ExtractedClaim.grounding == GroundingStatus.GROUNDED,
            ExtractedClaim.normalized_value.is_not(None),
        )
        .order_by(ExtractedClaim.evidence_id, ExtractedClaim.created_at, ExtractedClaim.id)
    ).all()
    by_file: dict[str, list[tuple[ExtractedClaim, str]]] = defaultdict(list)
    for claim, name in rows:
        by_file[claim.evidence_id].append((claim, name))
    docs: list[Doc] = []
    for evidence_id, claims in by_file.items():
        kind = next(
            (
                DocumentType(c.normalized_value or DocumentType.OTHER)
                for c, _ in claims
                if c.field_name == "document_type"
            ),
            DocumentType.OTHER,
        )
        if kind == DocumentType.OTHER:
            kind = infer_kind({c.field_name for c, _ in claims})
        doc = Doc(evidence_id, claims[0][1], kind)
        for claim, name in claims:
            doc.facts.append(
                Fact(
                    claim_id=claim.id,
                    evidence_id=evidence_id,
                    file_name=name,
                    field=claim.field_name,
                    value=claim.normalized_value or "",
                    unit=claim.unit,
                    item_key=item_key(claim.item_key) if claim.item_key else None,
                    item_label=claim.item_key or "",
                )
            )
        docs.append(doc)
    return docs


# Fields only one kind of document states. Used when the reader didn't
# report document_type (a model can leave it out even when asked).
_KIND_FIELDS = (
    (DocumentType.DELIVERY_NOTE, {"delivered_quantity", "delivery_date"}),
    (DocumentType.INVOICE, {"invoiced_quantity"}),
    (DocumentType.PURCHASE_ORDER, {"ordered_quantity", "promised_delivery_date"}),
)


def infer_kind(fields: set[str]) -> DocumentType:
    """The document's kind from the fields found in it, if exactly one kind fits."""
    kinds = [kind for kind, markers in _KIND_FIELDS if fields & markers]
    return kinds[0] if len(kinds) == 1 else DocumentType.OTHER


def _rewrite(db: Session, tenant_id: str, proposed: Proposed) -> tuple[str, str]:
    """Optionally let Gemini reword the template, with the tenant's model and
    region. The rewrite must contain exactly the template's numbers, or the
    template is kept."""
    from app.pipeline.claims import gemini, gemini_target
    from app.pipeline.errors import PipelineError

    target = gemini_target(db, tenant_id)
    if not get_settings().gemini_explanations or target is None:
        return proposed.explanation, "template"
    try:
        reply = gemini.generate(
            target,
            "Rewrite this finding for an operations reviewer in one or two plain sentences. "
            "Use only the facts given; keep every number, date, unit and file name exactly; "
            "add nothing.",
            [{"role": "user", "parts": [{"text": proposed.explanation}]}],
            {"temperature": 0, "maxOutputTokens": 300},
        )
        text = str(reply["candidates"][0]["content"]["parts"][0]["text"]).strip()
    except (PipelineError, KeyError, IndexError, TypeError):
        return proposed.explanation, "template"
    numbers = re.compile(r"\d[\d.,:-]*\d|\d")
    if (
        not text
        or len(text) > 600
        or sorted(numbers.findall(text)) != sorted(numbers.findall(proposed.explanation))
    ):
        return proposed.explanation, "template"
    return text, "machine_generated"


def reconcile_case(db: Session, case_id: str, *, force: bool = False) -> dict[str, int] | None:
    """Returns counts, or None when files in the case are still processing."""
    case = db.scalar(select(Case).where(Case.id == case_id).with_for_update())
    if case is None:
        return None
    waiting = db.scalar(
        select(func.count())
        .select_from(EvidenceFile)
        .where(EvidenceFile.case_id == case_id, EvidenceFile.status.in_(UNFINISHED))
    )
    if waiting and not force:
        db.commit()
        return None

    rules = {RuleTemplate(r.template): r for r in templates.ensure_rules(db, case.tenant_id)}
    params = {t: templates.current_params(db, r) for t, r in rules.items() if r.enabled}
    proposed = evaluate(documents(db, case_id), params)

    existing = {
        f.dedupe_key: f for f in db.scalars(select(Finding).where(Finding.case_id == case_id))
    }
    wanted: dict[str, Proposed] = {}
    for p in proposed:
        rule = rules[p.template]
        wanted.setdefault(p.dedupe_key(rule.id, rule.current_version), p)

    added = 0
    for key, p in wanted.items():
        if key in existing:
            continue
        rule = rules[p.template]
        text, source = _rewrite(db, case.tenant_id, p)
        db.add(
            Finding(
                id=new_id("fnd"),
                tenant_id=case.tenant_id,
                case_id=case_id,
                rule_id=rule.id,
                rule_version=rule.current_version,
                finding_type=p.finding_type,
                severity=p.severity,
                input_claim_ids=sorted(set(p.claim_ids)),
                expected=p.expected,
                observed=p.observed,
                difference=p.difference,
                explanation=text,
                explanation_source=source,
                review_status=ReviewDecision.PENDING,
                dedupe_key=key,
            )
        )
        added += 1

    stale = [f for key, f in existing.items() if key not in wanted]
    reviewed = set(
        db.scalars(
            select(ReviewAction.finding_id).where(
                ReviewAction.finding_id.in_([f.id for f in stale])
            )
        )
    )
    removable = [f.id for f in stale if f.id not in reviewed]
    if removable:
        db.execute(delete(Finding).where(Finding.id.in_(removable)))
    db.flush()

    pending = (
        db.scalar(
            select(func.count())
            .select_from(Finding)
            .where(Finding.case_id == case_id, Finding.review_status == ReviewDecision.PENDING)
        )
        or 0
    )
    if added or removable:
        audit.record_system(
            db,
            case.tenant_id,
            "case.reconciled",
            "case",
            case.id,
            case_id=case.id,
            details={
                "title": f"Checked by the rules: {added} new finding(s)",
                "added": added,
                "removed": len(removable),
                "pending": pending,
            },
        )
    if pending:
        case_service.transition(
            db, case, CaseStatus.IN_REVIEW, reason="findings pending", pending_findings=pending
        )
    else:
        case_service.transition(db, case, CaseStatus.COMPLETED, reason="no finding pending")
    db.commit()
    logger.info("case reconciled", extra={"case_id": case_id})
    return {"added": added, "removed": len(removable), "pending": pending}


def reconcile_waiting(db: Session) -> int:
    """Safety net: cases still awaiting evidence although every file is done
    (a worker crashed between processing and reconciling). Returns how many
    were reconciled."""
    finished = (
        select(EvidenceFile.case_id)
        .where(EvidenceFile.status != EvidenceStatus.UPLOADING)
        .group_by(EvidenceFile.case_id)
        .having(func.count().filter(EvidenceFile.status.in_(UNFINISHED)) == 0)
    )
    case_ids = db.scalars(
        select(Case.id).where(Case.status == CaseStatus.AWAITING_EVIDENCE, Case.id.in_(finished))
    ).all()
    done = 0
    for case_id in case_ids:
        if reconcile_case(db, case_id) is not None:
            done += 1
    return done
