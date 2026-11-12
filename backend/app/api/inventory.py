"""Read-only inventory and sales endpoints. Each route: validate -> one service call -> reshape."""

from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import AsOfDep, SessionDep
from app.api.errors import ApiError
from app.schemas import ErrorResponse, InventoryResponse, SalesHistoryResponse
from app.services.inventory import UnknownSkuError, get_sales_history
from app.services.procurement import get_inventory_overview

router = APIRouter(tags=["inventory"])

_NOT_SEEDED = {503: {"model": ErrorResponse, "description": "Database not seeded"}}


@router.get("/inventory", response_model=InventoryResponse, responses=_NOT_SEEDED)
def list_inventory(session: SessionDep, as_of: AsOfDep) -> InventoryResponse:
    """Every product with stock, incoming units, average daily sales and days of cover."""
    return InventoryResponse.from_rows(get_inventory_overview(session, as_of), as_of)


@router.get(
    "/inventory/{sku}/sales",
    response_model=SalesHistoryResponse,
    responses={404: {"model": ErrorResponse}, **_NOT_SEEDED},
)
def read_sales_history(
    session: SessionDep,
    as_of: AsOfDep,
    sku: Annotated[str, Path(min_length=1, max_length=32)],
    days: Annotated[
        int | None,
        Query(ge=1, le=365, description="Window length in days. Defaults to the lookback setting."),
    ] = None,
) -> SalesHistoryResponse:
    """Daily sales for one product over the last `days` days (today excluded)."""
    try:
        history = get_sales_history(session, sku, as_of, days)
    except UnknownSkuError:
        raise ApiError(404, "PRODUCT_NOT_FOUND", f"No product with SKU {sku!r}", {"sku": sku})
    return SalesHistoryResponse.from_history(history)
