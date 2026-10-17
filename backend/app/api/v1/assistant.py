"""Case Q&A: endpoint 20 (Docs/backend_plan.md, Step 18). Optional and
read-only; see app.services.assistant."""

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.permissions import Permission
from app.auth.principal import Principal, load_case, require
from app.db.session import get_db
from app.errors import ApiError
from app.pipeline.errors import PipelineError
from app.services import assistant, audit

router = APIRouter(prefix="/cases/{case_id}", tags=["review"])


class QuestionIn(BaseModel):
    question: str = Field(min_length=3, max_length=500)


class Citation(BaseModel):
    type: Literal["finding", "claim"]
    id: str


class AnswerOut(BaseModel):
    answer: str
    citations: list[Citation]
    source: Literal["machine_generated"] = "machine_generated"


@router.post("/qa", response_model=AnswerOut)
def ask(
    case_id: str,
    body: QuestionIn,
    principal: Principal = Depends(require(Permission.VIEW_CASES)),
    db: Session = Depends(get_db),
) -> AnswerOut:
    """20: an answer that cites the case's findings and claims. Never decides."""
    case = load_case(db, principal, case_id)
    try:
        answer = assistant.ask(db, case, body.question.strip())
    except assistant.AssistantUnavailable as exc:
        raise ApiError(503, "assistant_unavailable", str(exc)) from exc
    except PipelineError as exc:
        raise ApiError(503, "assistant_unavailable", "The assistant couldn't answer now.") from exc
    # The question itself isn't stored: it may quote document content.
    audit.record(
        db,
        principal,
        "case.question_asked",
        "case",
        case.id,
        case_id=case.id,
        details={"citations": len(answer.citations)},
    )
    db.commit()
    return AnswerOut(
        answer=answer.text,
        citations=[Citation(**c) for c in answer.citations],
    )
