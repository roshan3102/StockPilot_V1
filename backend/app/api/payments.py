"""PayPal payment endpoints for an APPROVED purchase order (docs/MVP_PLAN.md section 5).

    POST /purchase-orders/{id}/paypal/orders    create (or resume) the PayPal order
    POST /purchase-orders/{id}/paypal/capture   verify with PayPal, capture, verify again

There is no amount anywhere in a request: the server derives it from the approved snapshot.
The capture body is only the PayPal order id, and unknown fields are rejected.

Error mapping. The status says who must act, the code says exactly what happened:

  404 PO_NOT_FOUND              no such purchase order
  409 PO_NOT_PAYABLE            create on a PO that is not APPROVED / PAYMENT_FAILED / resumable
  409 PAYPAL_ORDER_MISMATCH     that PayPal order id is not one of this PO's payments
  409 PAYMENT_NOT_CAPTURABLE    the payment already failed, or the PO is not awaiting payment
  409 BUYER_APPROVAL_REQUIRED   PayPal says the buyer has not approved yet; nothing was captured
  402 PAYMENT_DECLINED          declined/voided; PO is PAYMENT_FAILED; create a NEW order to retry
  422 VERIFICATION_FAILED       amount/reference mismatch; the PO is NOT marked paid
  502 PAYPAL_ERROR              PayPal refused the request (details.issue, details.debug_id)
  503 PAYPAL_UNAVAILABLE        PayPal did not answer clearly; retry the SAME request (idempotent)
  503 PAYPAL_NOT_CONFIGURED     credentials missing or not the Sandbox (server configuration)

Success: capture returns 200 when PAID and 202 when PayPal accepted the payment but has not
finalised it (POST the same capture again later to re-check).
"""

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Response

from app.api.deps import PayPalDep, SessionDep
from app.api.errors import ApiError
from app.api.purchase_orders import PoId, po_errors
from app.schemas import (
    CapturePayPalOrderRequest,
    CapturePayPalOrderResponse,
    CreatePayPalOrderResponse,
    ErrorResponse,
    PaymentOut,
)
from app.services.payments import (
    BuyerApprovalRequiredError,
    CaptureOutcome,
    PayPalApiError,
    PaymentNotCapturableError,
    PayPalOrderMismatchError,
    PayPalUncertainError,
    PoNotPayableError,
    capture_paypal_order,
    create_paypal_order,
)

router = APIRouter(prefix="/purchase-orders", tags=["payments"])

_ERR = {"model": ErrorResponse}
_COMMON = {404: _ERR, 409: _ERR, 502: _ERR, 503: _ERR}


@contextmanager
def paypal_errors() -> Iterator[None]:
    """Translate payment-service exceptions (cases where nothing was changed)."""
    try:
        yield
    except PoNotPayableError as exc:
        raise ApiError(409, "PO_NOT_PAYABLE", str(exc))
    except PayPalOrderMismatchError as exc:
        raise ApiError(409, "PAYPAL_ORDER_MISMATCH", str(exc))
    except PaymentNotCapturableError as exc:
        raise ApiError(409, "PAYMENT_NOT_CAPTURABLE", str(exc))
    except BuyerApprovalRequiredError as exc:
        raise ApiError(409, "BUYER_APPROVAL_REQUIRED", str(exc), {"paypal_status": exc.paypal_status})
    except PayPalApiError as exc:
        raise ApiError(502, "PAYPAL_ERROR", f"PayPal refused the request: {exc.issue}", {
            "issue": exc.issue, "debug_id": exc.debug_id})
    except PayPalUncertainError as exc:
        # Raised only when nothing was recorded, so repeating the request is safe.
        raise ApiError(503, "PAYPAL_UNAVAILABLE", str(exc), {"retryable": True})


@router.post(
    "/{po_id}/paypal/orders", status_code=201, response_model=CreatePayPalOrderResponse,
    responses={200: {"model": CreatePayPalOrderResponse, "description": "An open order was resumed"},
               **_COMMON},
    summary="Create (or resume) the PayPal order for an APPROVED purchase order",
)
def create_paypal_order_endpoint(
    po_id: PoId, session: SessionDep, paypal: PayPalDep, response: Response
) -> CreatePayPalOrderResponse:
    """No body: the amount is read from the approved snapshot. Calling this again while the
    buyer has an open, unpaid order returns that same order (200) instead of creating another."""
    with po_errors("PO_NOT_PAYABLE"), paypal_errors():
        result = create_paypal_order(session, paypal, po_id)
        session.commit()
        if result.resumed:
            response.status_code = 200
        payment = result.payment
        return CreatePayPalOrderResponse(
            paypal_order_id=payment.paypal_order_id, amount_cents=payment.amount_cents,
            payment_status=payment.status, approve_url=result.approve_url, resumed=result.resumed,
        )


@router.post(
    "/{po_id}/paypal/capture", response_model=CapturePayPalOrderResponse,
    responses={202: {"model": CapturePayPalOrderResponse, "description": "Accepted, not final"},
               402: _ERR, 422: _ERR, **_COMMON},
    summary="Verify with PayPal, capture an approved order, and verify the result",
)
def capture_paypal_order_endpoint(
    po_id: PoId, body: CapturePayPalOrderRequest, session: SessionDep, paypal: PayPalDep,
    response: Response,
) -> CapturePayPalOrderResponse:
    """Idempotent. Also the way to re-check a PENDING or uncertain payment: PayPal is asked
    first, and an order that is already captured is verified, never captured twice."""
    with po_errors("PAYMENT_NOT_CAPTURABLE"), paypal_errors():
        result = capture_paypal_order(session, paypal, po_id, body.paypal_order_id)
        session.commit()  # commit BEFORE reporting: declines, flags and uncertainty are audited
        payment = PaymentOut.from_payment(result.payment)
        po_status = result.po.status

    details = {"po_status": po_status, "payment_id": payment.id, "reasons": list(result.reasons)}
    match result.outcome:
        case CaptureOutcome.PAID | CaptureOutcome.PENDING:
            if result.outcome == CaptureOutcome.PENDING:
                response.status_code = 202
            return CapturePayPalOrderResponse(
                outcome=result.outcome.value, po_status=po_status, payment=payment
            )
        case CaptureOutcome.DECLINED:
            raise ApiError(402, "PAYMENT_DECLINED",
                           "The payment was not completed. Create a new PayPal order to retry.",
                           details)
        case CaptureOutcome.VERIFICATION_FAILED:
            raise ApiError(422, "VERIFICATION_FAILED",
                           "PayPal's record does not match the approved order. The purchase order "
                           "was NOT marked paid.", details)
        case _:  # CaptureOutcome.UNCERTAIN
            raise ApiError(503, "PAYPAL_UNAVAILABLE",
                           "PayPal did not give a clear answer, so the outcome is unknown. "
                           "Repeat this same request: PayPal is checked before anything is captured.",
                           {**details, "outcome_unknown": True, "retryable": True})
