"""Purchase-order endpoints: list, read, edit a draft, approve, cancel.

Each route: validate the body (Pydantic) -> ONE service call -> commit -> reshape. No purchasing
logic lives here; the services decide and the routes only translate. Payment endpoints live
in api/payments.py.

Error mapping (the HTTP status says who must act, the code says exactly what happened):

  404 PO_NOT_FOUND         no such purchase order
  409 PO_NOT_APPROVABLE    approve on a PO that is not a DRAFT (also a double click)
  409 PO_NOT_CANCELLABLE   cancel on a PO that is not DRAFT or APPROVED
  409 PO_NOT_EDITABLE      edit on a PO that is not a DRAFT
  409 STALE_DRAFT          the draft changed since the client loaded it (version mismatch)
  409 TOTAL_MISMATCH       the approved total differs from the server's total
  409 PRICES_CHANGED       supplier prices or case sizes changed since the draft was priced
  422 ORDER_REJECTED       a purchasing rule fails; details.violations lists every one
  422 INVALID_ORDER        structurally impossible request (e.g. unknown supplier id)

409 means "your request was fine but the PO's current state conflicts with it: reload and look
again". 422 means "this order, as specified, is not allowed". Stored data that contradicts
itself (OrderIntegrityError) is a server fault, so it falls through to the generic 500.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import SessionDep
from app.api.errors import ApiError
from app.schemas import (
    ApprovePurchaseOrderRequest,
    CancelPurchaseOrderRequest,
    EditPurchaseOrderRequest,
    ErrorResponse,
    PurchaseOrderListResponse,
    PurchaseOrderOut,
    PurchaseOrderSummaryOut,
)
from app.services.po_lifecycle import (
    InvalidTransitionError,
    NotEditableError,
    OrderRejectedError,
    PricesChangedError,
    PurchaseOrderNotFoundError,
    StaleDraftError,
    TotalMismatchError,
    approve_purchase_order,
    cancel_purchase_order,
    edit_draft_purchase_order,
    get_purchase_order,
    list_purchase_orders,
)
from app.services.purchase_orders import InvalidOrderError, LineRequest

router = APIRouter(prefix="/purchase-orders", tags=["purchase-orders"])

MAX_LIST_LIMIT = 200

PoId = Annotated[int, Path(ge=1, description="Purchase order id")]

_CONFLICT = {409: {"model": ErrorResponse, "description": "The PO's state conflicts with the request"}}
_REJECTED = {422: {"model": ErrorResponse, "description": "Validation or purchasing-rule failure"}}
_NOT_FOUND = {404: {"model": ErrorResponse, "description": "No such purchase order"}}


@contextmanager
def po_errors(not_in_state_code: str) -> Iterator[None]:
    """Translate lifecycle exceptions into the API's error envelope.

    `not_in_state_code` is the code used when the state machine refuses the move: it differs
    per endpoint (PO_NOT_APPROVABLE for approve, PO_NOT_CANCELLABLE for cancel). Anything not
    listed here is a bug or a server fault and propagates to the generic 500 handler.
    """
    try:
        yield
    except PurchaseOrderNotFoundError as exc:
        raise ApiError(404, "PO_NOT_FOUND", f"No purchase order with id {exc}", {"po_id": exc.args[0]})
    except InvalidTransitionError as exc:
        raise ApiError(409, not_in_state_code, str(exc), {
            "from_status": exc.from_status, "to_status": exc.to_status})
    except NotEditableError as exc:
        raise ApiError(409, "PO_NOT_EDITABLE", str(exc))
    except StaleDraftError as exc:
        raise ApiError(409, "STALE_DRAFT", str(exc), {
            "expected_version": exc.expected_version, "actual_version": exc.actual_version})
    except TotalMismatchError as exc:
        raise ApiError(409, "TOTAL_MISMATCH", str(exc), {
            "approved_total_cents": exc.approved_total_cents,
            "server_total_cents": exc.server_total_cents})
    except PricesChangedError as exc:
        raise ApiError(409, "PRICES_CHANGED", str(exc), {
            "changes": exc.changes,
            "draft_total_cents": exc.draft_total_cents,
            "current_total_cents": exc.current_total_cents})
    except OrderRejectedError as exc:
        raise ApiError(422, "ORDER_REJECTED", str(exc), {
            "violations": [
                {"code": v.code.value, "sku": v.sku, "message": v.message} for v in exc.violations
            ]})
    except InvalidOrderError as exc:
        raise ApiError(422, "INVALID_ORDER", str(exc))


@router.get(
    "", response_model=PurchaseOrderListResponse,
    summary="Order history: the most recent purchase orders, newest first",
)
def list_orders(
    session: SessionDep, limit: Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)] = 50
) -> PurchaseOrderListResponse:
    return PurchaseOrderListResponse(
        items=[PurchaseOrderSummaryOut.from_po(po) for po in list_purchase_orders(session, limit=limit)]
    )


@router.get(
    "/{po_id}", response_model=PurchaseOrderOut, responses={**_NOT_FOUND},
    summary="A purchase order with its lines and audit events",
)
def read_purchase_order(po_id: PoId, session: SessionDep) -> PurchaseOrderOut:
    with po_errors("PO_NOT_APPROVABLE"):
        return PurchaseOrderOut.from_po(get_purchase_order(session, po_id))


@router.put(
    "/{po_id}", response_model=PurchaseOrderOut,
    responses={**_NOT_FOUND, **_CONFLICT, **_REJECTED},
    summary="Replace the lines of a DRAFT (re-priced from the database)",
)
def edit_purchase_order(
    po_id: PoId, body: EditPurchaseOrderRequest, session: SessionDep
) -> PurchaseOrderOut:
    """Send the COMPLETE new list of lines and the `version` you were looking at. Prices and
    totals are never accepted from the client. Sending the same lines again re-prices the
    draft after a supplier price change."""
    with po_errors("PO_NOT_APPROVABLE"):
        po = edit_draft_purchase_order(
            session, po_id,
            expected_version=body.expected_version,
            lines=[LineRequest(line.sku, line.quantity, line.reason) for line in body.lines],
            supplier_id=body.supplier_id,
        )
        session.commit()
        return PurchaseOrderOut.from_po(po)


@router.post(
    "/{po_id}/approve", response_model=PurchaseOrderOut,
    responses={**_NOT_FOUND, **_CONFLICT, **_REJECTED},
    summary="Approve a DRAFT: the exact order the merchant reviewed",
)
def approve(
    po_id: PoId, body: ApprovePurchaseOrderRequest, session: SessionDep
) -> PurchaseOrderOut:
    """Recomputes the order from current database values and refuses unless it is still valid
    and still equals what the merchant saw. On success the order is frozen as a snapshot."""
    with po_errors("PO_NOT_APPROVABLE"):
        po = approve_purchase_order(
            session, po_id,
            approved_total_cents=body.approved_total_cents,
            expected_version=body.expected_version,
        )
        session.commit()
        return PurchaseOrderOut.from_po(po)


@router.post(
    "/{po_id}/cancel", response_model=PurchaseOrderOut,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Reject a DRAFT or cancel an APPROVED order (before payment starts)",
)
def cancel(
    po_id: PoId, session: SessionDep, body: CancelPurchaseOrderRequest | None = None
) -> PurchaseOrderOut:
    """The body is optional; send `{"reason": "..."}` to record why."""
    with po_errors("PO_NOT_CANCELLABLE"):
        po = cancel_purchase_order(session, po_id, reason=body.reason if body else None)
        session.commit()
        return PurchaseOrderOut.from_po(po)
