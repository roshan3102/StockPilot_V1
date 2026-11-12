"""Questions about stock, sales and promotions, answered by the read-only agent.

    POST /assistant/questions   e.g. "how much grey goose 750ml do we have?"; nothing is stored

Error mapping:

  422 VALIDATION_ERROR     empty or too-long question
  502 LLM_UNAVAILABLE      the AI service failed (details.reason)
  503 LLM_NOT_CONFIGURED   no API key on the server
  503 NOT_SEEDED           the database has no merchant settings
"""

from fastapi import APIRouter

from app.api.deps import AsOfDep, LLMDep, SessionDep
from app.api.errors import ApiError
from app.schemas import AskQuestionRequest, AskQuestionResponse, ErrorResponse
from app.services.assistant import answer_question

router = APIRouter(prefix="/assistant", tags=["assistant"])

_ERR = {"model": ErrorResponse}


@router.post(
    "/questions", response_model=AskQuestionResponse,
    responses={422: _ERR, 502: _ERR, 503: _ERR},
    summary="Ask about stock, sales or promotions (read-only)",
)
def ask_question(
    body: AskQuestionRequest, session: SessionDep, llm: LLMDep, as_of: AsOfDep
) -> AskQuestionResponse:
    answer = answer_question(session, llm, question=body.question, as_of=as_of)
    if answer.llm_failed:
        raise ApiError(502, "LLM_UNAVAILABLE", answer.answer, {"reason": answer.error_code})
    return AskQuestionResponse.from_answer(answer)
