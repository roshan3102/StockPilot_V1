"""Agent restock runs (docs/MVP_PLAN.md section 5). Synchronous: POST returns when the run ends.

    POST /restock-runs        run the agent on one request; DRAFT POs only, nothing is approved
    GET  /restock-runs/{id}   the run, its POs with evidence, and the full tool trace

Error mapping:

  404 RUN_NOT_FOUND        no such run
  422 VALIDATION_ERROR     empty or too-long message, bad budget, unknown fields
  502 LLM_UNAVAILABLE      the AI service failed mid-run; the failed run is saved with no POs
                           (details.run_id to fetch it, details.reason for the specific cause)
  503 LLM_NOT_CONFIGURED   no API key on the server; no run is recorded
  503 NOT_SEEDED           the database has no merchant settings
"""

from typing import Annotated

from fastapi import APIRouter, Path

from app.api.deps import AsOfDep, LLMDep, SessionDep
from app.api.errors import ApiError
from app.config import get_settings
from app.schemas import CreateRestockRunRequest, ErrorResponse, RestockRunDetailOut, RestockRunOut
from app.services.restock_runs import (
    RestockRunNotFoundError,
    RunStatus,
    get_restock_run,
    start_restock_run,
)

router = APIRouter(prefix="/restock-runs", tags=["restock-runs"])

RunId = Annotated[int, Path(ge=1, description="Restock run id")]
_ERR = {"model": ErrorResponse}


@router.post(
    "", status_code=201, response_model=RestockRunOut,
    responses={422: _ERR, 502: _ERR, 503: _ERR},
    summary="Ask the agent to restock; returns the draft purchase orders it created",
)
def create_restock_run(
    body: CreateRestockRunRequest, session: SessionDep, llm: LLMDep, as_of: AsOfDep
) -> RestockRunOut:
    """201 also covers "nothing to order" (zero POs, explained in `summary`) and a run stopped
    early by a step limit (its valid drafts are kept; see `warnings`)."""
    view = start_restock_run(
        session, llm, message=body.message, budget_cents=body.budget_cents, as_of=as_of,
        model=get_settings().llm_model,
    )
    run = view.run
    if run.status == RunStatus.FAILED:
        raise ApiError(502, "LLM_UNAVAILABLE", run.summary or run.error_message or "", {
            "run_id": run.id, "reason": run.error_code})
    return RestockRunOut.from_view(view)


@router.get(
    "/{run_id}", response_model=RestockRunDetailOut, responses={404: _ERR},
    summary="A restock run with its purchase orders and tool trace",
)
def read_restock_run(run_id: RunId, session: SessionDep, as_of: AsOfDep) -> RestockRunDetailOut:
    try:
        view = get_restock_run(session, run_id, as_of)
    except RestockRunNotFoundError:
        raise ApiError(404, "RUN_NOT_FOUND", f"No restock run with id {run_id}", {"run_id": run_id})
    return RestockRunDetailOut.from_view(view)
