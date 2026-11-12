"""Deterministic restock recommendation. No LLM, no persistence, no payment."""

from fastapi import APIRouter

from app.api.deps import AsOfDep, SessionDep
from app.schemas import ErrorResponse, RestockRecommendationRequest, RestockRecommendationResponse
from app.services.procurement import build_restock_recommendation

router = APIRouter(tags=["restock"])


@router.post(
    "/restock-recommendations",
    response_model=RestockRecommendationResponse,
    responses={503: {"model": ErrorResponse, "description": "Database not seeded"}},
)
def create_restock_recommendation(
    body: RestockRecommendationRequest, session: SessionDep, as_of: AsOfDep
) -> RestockRecommendationResponse:
    """Compute what to reorder, from which supplier, within the budget.

    Always 200 when the request is valid, including when nothing needs reordering or nothing
    is affordable: read `status`, `unfulfilled` and `rejected_options` for the explanation.
    Nothing is saved (the status code is 200, not 201, for that reason).
    """
    recommendation = build_restock_recommendation(session, as_of, body.budget_cents)
    return RestockRecommendationResponse.from_result(recommendation, as_of)
