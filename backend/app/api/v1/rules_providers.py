"""Reconciliation rules (46-47) and processing providers (48-49).

Admins only (Docs/Integrations_API_Contract.md §4 and §5).
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.permissions import Permission
from app.auth.principal import Principal, require
from app.db.models import ProviderSettings, Rule, RuleVersion
from app.db.session import get_db
from app.enums import AiProviderName, OcrProviderName, RuleTemplate
from app.errors import ApiError, bad_request, not_found
from app.ids import new_id
from app.pipeline.rules.templates import TEMPLATES, current_params, ensure_rules, validate_params
from app.services import audit

router = APIRouter(prefix="/admin", tags=["integrations"])
ADMIN = Depends(require(Permission.ADMIN))

# Providers with a working adapter today. Others are listed in the UI so the
# setting is ready, but return 400 provider_not_available until built.
OCR_ADAPTERS = {OcrProviderName.GOOGLE_DOCUMENT_AI}
AI_ADAPTERS = {AiProviderName.VERTEX_GEMINI, AiProviderName.GEMINI_API}
# No free-text model names ever reach Gemini.
MODEL_ALLOWLIST = {"gemini-2.5-flash", "gemini-2.5-pro"}
REGIONS = {"asia-southeast1", "asia-southeast2"}  # Singapore, Jakarta
DEFAULT_MODEL = "gemini-2.5-flash"


# Rules ---------------------------------------------------------------------------


class ParamOut(BaseModel):
    key: str
    label: str
    value: str
    suffix: str


class RuleOut(BaseModel):
    id: str
    template: RuleTemplate
    name: str
    description: str
    version: int
    enabled: bool
    params: list[ParamOut]


class RuleList(BaseModel):
    items: list[RuleOut]


class RulePatch(BaseModel):
    enabled: bool | None = None
    params: dict[str, str] | None = Field(default=None, max_length=10)


def _rule_out(db: Session, rule: Rule) -> RuleOut:
    template = RuleTemplate(rule.template)
    spec = TEMPLATES[template]
    values = current_params(db, rule)
    return RuleOut(
        id=rule.id,
        template=template,
        name=spec.name,
        description=spec.description,
        version=rule.current_version,
        enabled=rule.enabled,
        params=[
            ParamOut(key=p.key, label=p.label, value=values.get(p.key, p.default), suffix=p.suffix)
            for p in spec.params
        ],
    )


@router.get("/rules", response_model=RuleList)
def list_rules(principal: Principal = ADMIN, db: Session = Depends(get_db)) -> RuleList:
    """46"""
    rules = ensure_rules(db, principal.tenant_id)
    db.commit()
    return RuleList(items=[_rule_out(db, r) for r in rules])


@router.patch("/rules/{rule_id}", response_model=RuleOut)
def update_rule(
    rule_id: str, body: RulePatch, principal: Principal = ADMIN, db: Session = Depends(get_db)
) -> RuleOut:
    """47: switching on or off keeps the version; changing a parameter adds one."""
    rule = db.get(Rule, rule_id)
    if rule is None or rule.tenant_id != principal.tenant_id:
        raise not_found("Rule")
    if body.enabled is not None and body.enabled != rule.enabled:
        rule.enabled = body.enabled
        audit.record(
            db,
            principal,
            "rule.toggled",
            "rule",
            rule.id,
            details={"enabled": body.enabled, "template": rule.template},
        )
    if body.params:
        try:
            clean = validate_params(RuleTemplate(rule.template), body.params)
        except ValueError as exc:
            raise bad_request(str(exc), field="params") from exc
        current = current_params(db, rule)
        merged = {**current, **clean}
        if merged != current:
            rule.current_version += 1
            db.add(
                RuleVersion(
                    id=new_id("rv"),
                    rule_id=rule.id,
                    version=rule.current_version,
                    params=merged,
                    created_by=principal.user_id,
                )
            )
            audit.record(
                db,
                principal,
                "rule.updated",
                "rule",
                rule.id,
                details={"version": rule.current_version, "params": merged},
            )
    db.commit()
    return _rule_out(db, rule)


# Providers -----------------------------------------------------------------------


class ProvidersOut(BaseModel):
    ocr: OcrProviderName
    ai: AiProviderName
    model: str
    region: str


def _settings(db: Session, tenant_id: str) -> ProviderSettings:
    settings = db.get(ProviderSettings, tenant_id)
    if settings is None:
        settings = ProviderSettings(tenant_id=tenant_id, model=DEFAULT_MODEL)
        db.add(settings)
        db.flush()
        db.refresh(settings)
    return settings


def _providers_out(s: ProviderSettings) -> ProvidersOut:
    return ProvidersOut(
        ocr=OcrProviderName(s.ocr), ai=AiProviderName(s.ai), model=s.model, region=s.region
    )


@router.get("/providers", response_model=ProvidersOut)
def get_providers(principal: Principal = ADMIN, db: Session = Depends(get_db)) -> ProvidersOut:
    """48"""
    settings = _settings(db, principal.tenant_id)
    db.commit()
    return _providers_out(settings)


@router.put("/providers", response_model=ProvidersOut)
def put_providers(
    body: ProvidersOut, principal: Principal = ADMIN, db: Session = Depends(get_db)
) -> ProvidersOut:
    """49: applies to evidence processed afterwards. Never takes or returns secrets."""
    if body.ocr not in OCR_ADAPTERS:
        raise ApiError(400, "provider_not_available", f"{body.ocr} isn't available yet.", "ocr")
    if body.ai not in AI_ADAPTERS:
        raise ApiError(400, "provider_not_available", f"{body.ai} isn't available yet.", "ai")
    if body.model not in MODEL_ALLOWLIST:
        raise bad_request(f"Choose one of: {', '.join(sorted(MODEL_ALLOWLIST))}.", "model")
    if body.region not in REGIONS:
        raise bad_request(f"Choose one of: {', '.join(sorted(REGIONS))}.", "region")
    settings = _settings(db, principal.tenant_id)
    before = _providers_out(settings).model_dump()
    settings.ocr, settings.ai, settings.model, settings.region = (
        body.ocr,
        body.ai,
        body.model,
        body.region,
    )
    settings.updated_by = principal.user_id
    if body.model_dump() != before:
        audit.record(
            db,
            principal,
            "providers.updated",
            "tenant",
            principal.tenant_id,
            details={"before": before, "after": body.model_dump()},
        )
    db.commit()
    return _providers_out(settings)
