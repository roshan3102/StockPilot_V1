"""Supplier promotions (rebates) and the supplier catalog the confirm form needs.

    GET  /suppliers                     suppliers, by name
    GET  /suppliers/{id}/offers         what one supplier sells (sku, flavor, size, case size)
    POST /promotions/parse              AI reads promotion text into a draft; nothing is saved
    POST /promotions                    save a promotion the merchant confirmed
    GET  /promotions                    all promotions, newest first
    GET  /promotions/{id}               one promotion
    GET  /promotions/{id}/evaluation    the system's split of the cases, from stock and sales
    POST /promotions/{id}/archive       stop offering it to the agent

Error mapping:

  404 SUPPLIER_NOT_FOUND / PROMOTION_NOT_FOUND
  422 INVALID_PROMOTION    terms the catalog cannot satisfy (details.reason)
  422 VALIDATION_ERROR     malformed body
  502 LLM_UNAVAILABLE      the AI service failed while reading the text
  503 LLM_NOT_CONFIGURED   no API key on the server
"""

from typing import Annotated

from fastapi import APIRouter, Path

from app.api.deps import AsOfDep, SessionDep, StructuredLLMDep
from app.api.errors import ApiError
from app.schemas import (
    CreatePromotionRequest,
    ErrorResponse,
    ParsePromotionRequest,
    PromotionDraftOut,
    PromotionEvaluationOut,
    PromotionListResponse,
    PromotionOut,
    SupplierCatalogResponse,
    SupplierListResponse,
    SupplierOut,
    SupplierRef,
)
from app.services.promotions import (
    InvalidPromotionError,
    PromotionInput,
    PromotionNotFoundError,
    SupplierNotFoundError,
    archive_promotion,
    create_promotion,
    draft_promotion_from_text,
    evaluate_promotion,
    get_promotion,
    list_promotions,
    list_supplier_catalog,
    list_suppliers,
)
from app.services.restock_runs import LLMError

router = APIRouter(tags=["promotions"])

PromotionId = Annotated[int, Path(ge=1, description="Promotion id")]
SupplierId = Annotated[int, Path(ge=1, description="Supplier id")]
_ERR = {"model": ErrorResponse}


def _not_found(promotion_id: int) -> ApiError:
    return ApiError(404, "PROMOTION_NOT_FOUND", f"No promotion with id {promotion_id}",
                    {"promotion_id": promotion_id})


@router.get("/suppliers", response_model=SupplierListResponse, summary="All suppliers")
def read_suppliers(session: SessionDep) -> SupplierListResponse:
    return SupplierListResponse(items=[
        SupplierOut(id=s.id, name=s.name, min_order_cents=s.min_order_cents)
        for s in list_suppliers(session)
    ])


@router.get(
    "/suppliers/{supplier_id}/offers", response_model=SupplierCatalogResponse,
    responses={404: _ERR}, summary="What one supplier sells",
)
def read_supplier_offers(supplier_id: SupplierId, session: SessionDep) -> SupplierCatalogResponse:
    try:
        offers = list_supplier_catalog(session, supplier_id)
    except SupplierNotFoundError:
        raise ApiError(404, "SUPPLIER_NOT_FOUND", f"No supplier with id {supplier_id}",
                       {"supplier_id": supplier_id})
    supplier = next(s for s in list_suppliers(session) if s.id == supplier_id)
    return SupplierCatalogResponse.from_offers(SupplierRef(id=supplier.id, name=supplier.name),
                                               offers)


@router.post(
    "/promotions/parse", response_model=PromotionDraftOut,
    responses={422: _ERR, 502: _ERR, 503: _ERR},
    summary="Read promotion text into a draft for the merchant to confirm (nothing is saved)",
)
def parse_promotion(
    body: ParsePromotionRequest, session: SessionDep, llm: StructuredLLMDep, as_of: AsOfDep
) -> PromotionDraftOut:
    try:
        draft = draft_promotion_from_text(session, llm, body.text, as_of)
    except LLMError as exc:
        raise ApiError(502, "LLM_UNAVAILABLE",
                       "The AI service could not read the promotion. Try again, or enter the "
                       "terms by hand.", {"reason": exc.code})
    return PromotionDraftOut.from_draft(draft)


@router.post(
    "/promotions", status_code=201, response_model=PromotionOut, responses={422: _ERR},
    summary="Save a confirmed promotion",
)
def create_promotion_route(
    body: CreatePromotionRequest, session: SessionDep, as_of: AsOfDep
) -> PromotionOut:
    try:
        promotion = create_promotion(session, PromotionInput(**body.model_dump()))
    except InvalidPromotionError as exc:
        raise ApiError(422, "INVALID_PROMOTION", str(exc), {"reason": str(exc)})
    session.commit()
    return PromotionOut.from_promotion(get_promotion(session, promotion.id), as_of)


@router.get("/promotions", response_model=PromotionListResponse, summary="All promotions")
def read_promotions(session: SessionDep, as_of: AsOfDep) -> PromotionListResponse:
    return PromotionListResponse(
        items=[PromotionOut.from_promotion(p, as_of) for p in list_promotions(session)]
    )


@router.get(
    "/promotions/{promotion_id}", response_model=PromotionOut, responses={404: _ERR},
    summary="One promotion",
)
def read_promotion(promotion_id: PromotionId, session: SessionDep, as_of: AsOfDep) -> PromotionOut:
    try:
        return PromotionOut.from_promotion(get_promotion(session, promotion_id), as_of)
    except PromotionNotFoundError:
        raise _not_found(promotion_id)


@router.get(
    "/promotions/{promotion_id}/evaluation", response_model=PromotionEvaluationOut,
    responses={404: _ERR}, summary="Which flavors and sizes to take, from stock and sales",
)
def read_promotion_evaluation(
    promotion_id: PromotionId, session: SessionDep, as_of: AsOfDep
) -> PromotionEvaluationOut:
    try:
        evaluation = evaluate_promotion(session, promotion_id, as_of)
    except PromotionNotFoundError:
        raise _not_found(promotion_id)
    return PromotionEvaluationOut.from_evaluation(evaluation, as_of)


@router.post(
    "/promotions/{promotion_id}/archive", response_model=PromotionOut, responses={404: _ERR},
    summary="Archive a promotion so the agent no longer uses it",
)
def archive_promotion_route(
    promotion_id: PromotionId, session: SessionDep, as_of: AsOfDep
) -> PromotionOut:
    try:
        promotion = archive_promotion(session, promotion_id)
    except PromotionNotFoundError:
        raise _not_found(promotion_id)
    session.commit()
    return PromotionOut.from_promotion(promotion, as_of)
