"""Claim extraction (Docs/backend_plan.md, Step 9).

An extractor proposes values with the quote they came from; code validates
them (``schema``) and grounding checks every quote against the page text
(``app.pipeline.grounding``) before any rule may use them.

- ``GeminiExtractor``: when ``PRUFLO_GEMINI_ENABLED`` is on, with the
  tenant's provider (``vertex_gemini`` or ``gemini_api``), model and region
  from the provider settings.
- ``StructuredExtractor``: otherwise. Reads labelled fields and line-item
  tables without a model.
"""

from typing import TYPE_CHECKING, Protocol

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import ProviderSettings
from app.pipeline.claims.schema import RawClaim
from app.pipeline.document import Page

if TYPE_CHECKING:
    from app.pipeline.claims.gemini import Target


class ClaimExtractor(Protocol):
    name: str  # provider, recorded on every claim
    model: str
    prompt_version: str

    def extract(self, pages: list[Page], file_name: str = "") -> list[RawClaim]: ...


def gemini_target(db: Session, tenant_id: str) -> "Target | None":
    """Where this tenant's Gemini calls go, or None when Gemini isn't usable."""
    from app.pipeline.claims.gemini import Target

    settings = get_settings()
    providers = db.get(ProviderSettings, tenant_id)
    if not settings.gemini_enabled or providers is None:
        return None
    if providers.ai == "gemini_api":
        if not settings.gemini_api_key:
            return None
        return Target("gemini_api", providers.model, api_key=settings.gemini_api_key)
    if providers.ai == "vertex_gemini":
        return Target("vertex_gemini", providers.model, settings.gcp_project, providers.region)
    return None


def extractor_for(db: Session, tenant_id: str) -> tuple[ClaimExtractor, str]:
    """The extractor for this tenant, and where it runs (recorded on claims)."""
    from app.pipeline.claims.gemini import GeminiExtractor
    from app.pipeline.claims.structured import StructuredExtractor

    target = gemini_target(db, tenant_id)
    if target is not None:
        return GeminiExtractor(target), target.label
    # The structured reader runs inside the worker, so in the worker's region.
    settings = get_settings()
    return StructuredExtractor(day_first=settings.dates_day_first), settings.region
