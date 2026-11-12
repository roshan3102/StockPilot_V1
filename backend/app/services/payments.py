"""Paying an APPROVED purchase order through PayPal (docs/MVP_PLAN.md sections 3, 5, 6).

Two operations, each safe to repeat:

  create_paypal_order   APPROVED / PAYMENT_FAILED -> PAYMENT_PENDING, or resume an open order
  capture_paypal_order  PAYMENT_PENDING -> PAID | stays pending | PAYMENT_FAILED

Rules this module enforces (each has a test):

* The amount comes from the frozen approved snapshot, after `verify_approved_snapshot`. No
  function here accepts a total, and nothing the LLM or browser sends can change it.
* We never ask PayPal to capture an order unless PayPal itself reports it as APPROVED by the
  buyer, and unless its id, reference, amount and currency match our records first.
* A PO becomes PAID only when `domain.payment_verification` says PAID: one COMPLETED capture
  with exactly the approved amount. An HTTP 2xx or a redirect proves nothing.
* A timeout or 5xx during capture is "unknown", never "failed": the PO stays PAYMENT_PENDING,
  and calling capture again looks the order up before doing anything. PayPal-Request-Id values
  are derived from stable data, so a repeated call can never create a second order or charge.
* If money may have moved but verification fails, the PO is NOT marked failed (that would let
  the buyer pay twice). It stays PAYMENT_PENDING with the payment flagged for a human.

Like `po_lifecycle`, functions here flush but never commit; the caller owns the transaction.
Results that change state or must be audited are RETURNED (so the caller commits them);
errors where nothing changed are RAISED (so the caller's rollback discards them).

Concurrency note: two simultaneous captures are made safe by PayPal's idempotent
PayPal-Request-Id plus the one-live-payment-per-PO unique index. The check-then-write pattern
here shares the single-writer assumption documented in `po_lifecycle`.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache

from sqlalchemy.orm import Session

from app.config import get_settings
from app.domain.payment_verification import Assessment, ExpectedPayment, Verdict, assess_order
from app.domain.po_state import POStatus
from app.integrations.paypal import (
    PayPalApiError,
    PayPalClient,
    PayPalConfigError,
    PayPalError,
    PayPalGateway,
    PayPalUncertainError,
)
from app.models import Payment, PaymentStatus, PurchaseOrder
from app.services.po_lifecycle import (
    OrderIntegrityError,
    get_approved_snapshot,
    get_purchase_order,
    record_event,
    transition_purchase_order,
    verify_approved_snapshot,
)

__all__ = [
    "BuyerApprovalRequiredError", "CaptureOutcome", "CaptureResult", "CreateResult",
    "PayPalApiError", "PayPalConfigError", "PayPalError", "PayPalGateway",
    "PayPalOrderMismatchError", "PayPalUncertainError", "PaymentNotCapturableError",
    "PoNotPayableError", "capture_paypal_order", "create_paypal_order", "get_paypal_client",
]

# Stable random namespace: the same input text always gives the same UUID (uuid5), which is
# what makes our PayPal-Request-Id values repeatable across retries and server restarts.
_REQUEST_ID_NAMESPACE = uuid.UUID("6f1c3b0e-5a51-4c1a-9a53-0d6f7a0c9e11")

# PayPal's documented "the buyer must fund this differently" refusals. On these the order is
# dead for us: the payment fails and a retry creates a fresh order (plan section 3).
FUNDING_FAILURE_ISSUES = frozenset({
    "INSTRUMENT_DECLINED",
    "TRANSACTION_REFUSED",
    "REDIRECT_PAYER_FOR_ALTERNATE_FUNDING",
    "MAX_NUMBER_OF_PAYMENT_ATTEMPTS_EXCEEDED",
})

_LIVE = (PaymentStatus.CREATED, PaymentStatus.PENDING, PaymentStatus.CAPTURED)
_DESCRIPTION_MAX = 127  # PayPal truncates purchase-unit descriptions beyond this


# ---------------------------------------------------------------------------------------------
# Errors where NOTHING changed. The API layer maps each to an HTTP status and error code.
# ---------------------------------------------------------------------------------------------


class PoNotPayableError(ValueError):
    """The PO is not in a state where a PayPal order may be created (or resumed)."""


class PayPalOrderMismatchError(ValueError):
    """The PayPal order id does not belong to this purchase order."""


class PaymentNotCapturableError(ValueError):
    """The payment is not open: it already failed, or the PO is not awaiting payment."""


class BuyerApprovalRequiredError(ValueError):
    """PayPal says the buyer has not approved the order. We did not call capture."""

    def __init__(self, paypal_status: str):
        self.paypal_status = paypal_status
        super().__init__(f"The buyer has not approved this PayPal order (PayPal status {paypal_status})")


# ---------------------------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CreateResult:
    payment: Payment
    approve_url: str | None
    resumed: bool  # True when an already-open PayPal order was returned instead of a new one


class CaptureOutcome(StrEnum):
    PAID = "PAID"                                  # verified; PO is PAID
    PENDING = "PENDING"                            # PayPal accepted but has not finalised
    DECLINED = "DECLINED"                          # PO is PAYMENT_FAILED; retry makes a new order
    VERIFICATION_FAILED = "VERIFICATION_FAILED"    # amount/reference mismatch; NOT paid
    UNCERTAIN = "UNCERTAIN"                        # PayPal did not answer clearly; ask again


@dataclass(frozen=True)
class CaptureResult:
    outcome: CaptureOutcome
    po: PurchaseOrder
    payment: Payment
    reasons: tuple[str, ...] = ()


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


@lru_cache
def get_paypal_client() -> PayPalGateway:
    """The one real client, built from environment settings on first use. Raises
    PayPalConfigError when credentials are missing or the URL is not the Sandbox."""
    settings = get_settings()
    return PayPalClient(
        settings.paypal_client_id,
        settings.paypal_client_secret.get_secret_value(),
        settings.paypal_base_url,
    )


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _reference(po: PurchaseOrder) -> str:
    """Sent to PayPal as reference_id/custom_id and required back. It names this PO and the
    exact approved snapshot, so an order made for any other PO or version cannot verify."""
    return f"po-{po.id}-{po.approved_snapshot_sha256[:16]}"


def _create_request_id(po: PurchaseOrder, attempt: int) -> str:
    # Includes the snapshot hash (which contains the approval time), so /dev/reset followed by
    # a fresh approval can never collide with an order PayPal remembers from an earlier run.
    return str(uuid.uuid5(_REQUEST_ID_NAMESPACE, f"create:{po.approved_snapshot_sha256}:{attempt}"))


def _capture_request_id(paypal_order_id: str) -> str:
    return str(uuid.uuid5(_REQUEST_ID_NAMESPACE, f"capture:{paypal_order_id}"))


def _approved_total_cents(po: PurchaseOrder) -> int:
    """The only source of a payable amount: the verified approved snapshot."""
    verify_approved_snapshot(po)  # OrderIntegrityError if anything was tampered with
    return get_approved_snapshot(po)["total_cents"]


def _expected(po: PurchaseOrder, payment: Payment) -> ExpectedPayment:
    total = _approved_total_cents(po)
    if payment.amount_cents != total or payment.currency != po.currency:
        raise OrderIntegrityError(
            f"Payment {payment.id} ({payment.amount_cents} {payment.currency}) does not match "
            f"the approved order ({total} {po.currency})"
        )
    return ExpectedPayment(
        paypal_order_id=payment.paypal_order_id,
        amount_cents=total,
        currency=po.currency,
        reference=_reference(po),
    )


def _live_payment(po: PurchaseOrder) -> Payment | None:
    return next((p for p in po.payments if p.status in _LIVE), None)


# ---------------------------------------------------------------------------------------------
# Step 1: create (or resume) the PayPal order
# ---------------------------------------------------------------------------------------------


def create_paypal_order(
    session: Session, gateway: PayPalGateway, po_id: int, *, now: datetime | None = None
) -> CreateResult:
    """Start payment for an approved PO.

    * APPROVED or PAYMENT_FAILED: create a PayPal order for the snapshot total, record a CREATED
      payment, and move the PO to PAYMENT_PENDING.
    * PAYMENT_PENDING with an open, unpaid order (the buyer closed the popup): ask PayPal about
      that order and return it again, so the buyer resumes instead of paying twice.
    * Anything else raises PoNotPayableError. PayPal errors propagate with nothing written.
    """
    now = now or _now()
    po = get_purchase_order(session, po_id)

    if po.status == POStatus.PAYMENT_PENDING:
        resumed = _resume(session, gateway, po, now)
        if resumed is not None:
            return resumed

    if po.status not in (POStatus.APPROVED, POStatus.PAYMENT_FAILED):
        raise PoNotPayableError(
            f"Purchase order {po.id} is {po.status}; only an APPROVED order can be paid"
        )

    total = _approved_total_cents(po)
    attempt = len(po.payments) + 1
    created = gateway.create_order(
        amount_cents=total,
        currency=po.currency,
        reference=_reference(po),
        description=f"StockPilot PO #{po.id} - {po.supplier.name}"[:_DESCRIPTION_MAX],
        request_id=_create_request_id(po, attempt),
    )

    payment = Payment(
        po_id=po.id, paypal_order_id=created.id, status=PaymentStatus.CREATED,
        amount_cents=total, currency=po.currency,
    )
    po.payments.append(payment)
    session.flush()
    transition_purchase_order(session, po.id, POStatus.PAYMENT_PENDING, detail={
        "payment_id": payment.id, "paypal_order_id": created.id, "amount_cents": total,
        "currency": po.currency, "attempt": attempt, "snapshot_sha256": po.approved_snapshot_sha256,
    })
    return CreateResult(payment, created.approve_url, resumed=False)


def _resume(
    session: Session, gateway: PayPalGateway, po: PurchaseOrder, now: datetime
) -> CreateResult | None:
    """Return the open order, or None after retiring a dead one so the caller makes a new one."""
    live = _live_payment(po)
    if live is None:
        raise PoNotPayableError(f"Purchase order {po.id} is awaiting payment but has no open payment")
    if live.status != PaymentStatus.CREATED:
        raise PoNotPayableError(
            f"Purchase order {po.id} already has a payment in state {live.status}; "
            f"call the capture endpoint to check its status"
        )
    order = gateway.get_order(live.paypal_order_id)
    assessment = assess_order(_expected(po, live), order)
    if assessment.verdict in (Verdict.AWAITING_BUYER, Verdict.READY_TO_CAPTURE):
        return CreateResult(live, order.approve_url, resumed=True)
    if assessment.verdict == Verdict.VOIDED:
        _fail(session, po, live, ("PayPal voided or expired the order",))
        return None
    raise PoNotPayableError(
        f"The open PayPal order for purchase order {po.id} is not resumable "
        f"({assessment.verdict.value}); call the capture endpoint to check its status"
    )


# ---------------------------------------------------------------------------------------------
# Step 3: capture, with verification (also the way to re-check a pending/uncertain payment)
# ---------------------------------------------------------------------------------------------


def capture_paypal_order(
    session: Session, gateway: PayPalGateway, po_id: int, paypal_order_id: str,
    *, now: datetime | None = None,
) -> CaptureResult:
    """Verify with PayPal, capture if (and only if) the buyer approved, verify the result.

    Safe to call repeatedly: for an already-paid PO it returns the stored result without
    contacting PayPal; for an order PayPal already captured it verifies instead of capturing.

    Raises (nothing changed): PayPalOrderMismatchError, PaymentNotCapturableError,
    BuyerApprovalRequiredError, PayPalApiError (a refusal that is not a decline),
    PayPalUncertainError (the lookup itself failed), OrderIntegrityError.
    """
    now = now or _now()
    po = get_purchase_order(session, po_id)
    payment = next((p for p in po.payments if p.paypal_order_id == paypal_order_id), None)
    if payment is None:
        raise PayPalOrderMismatchError(
            f"PayPal order {paypal_order_id} does not belong to purchase order {po.id}"
        )
    if po.status == POStatus.PAID and payment.status == PaymentStatus.CAPTURED:
        return CaptureResult(CaptureOutcome.PAID, po, payment)  # duplicate request: no PayPal call
    if po.status != POStatus.PAYMENT_PENDING or payment.status not in (
        PaymentStatus.CREATED, PaymentStatus.PENDING
    ):
        raise PaymentNotCapturableError(
            f"Payment for PayPal order {paypal_order_id} is {payment.status} and purchase order "
            f"{po.id} is {po.status}; it cannot be captured. Create a new PayPal order to retry."
        )
    expected = _expected(po, payment)

    # Ask PayPal what is true NOW, before doing anything. The browser's "approved" is a hint.
    order = gateway.get_order(paypal_order_id)
    assessment = assess_order(expected, order)
    capture_attempted = False

    if assessment.verdict == Verdict.AWAITING_BUYER:
        raise BuyerApprovalRequiredError(order.status)

    if assessment.verdict == Verdict.READY_TO_CAPTURE:
        capture_attempted = True
        try:
            order = gateway.capture_order(
                paypal_order_id, request_id=_capture_request_id(paypal_order_id)
            )
        except PayPalUncertainError as exc:
            record_event(session, po, "PAYMENT_CAPTURE_UNCERTAIN", {
                "payment_id": payment.id, "paypal_order_id": paypal_order_id, "error": str(exc),
            })
            return CaptureResult(CaptureOutcome.UNCERTAIN, po, payment, (str(exc),))
        except PayPalApiError as exc:
            if exc.issue == "ORDER_ALREADY_CAPTURED":
                order = gateway.get_order(paypal_order_id)  # someone (a retry) got there first
            elif exc.issue in FUNDING_FAILURE_ISSUES:
                reasons = (f"PayPal declined the payment: {exc.issue}",)
                _fail(session, po, payment, reasons)
                return CaptureResult(CaptureOutcome.DECLINED, po, payment, reasons)
            elif exc.issue == "ORDER_NOT_APPROVED":
                raise BuyerApprovalRequiredError("not approved") from exc
            else:
                raise
        assessment = assess_order(expected, order)

    return _settle(session, po, payment, assessment, capture_attempted or bool(order.captures), now)


def _settle(
    session: Session, po: PurchaseOrder, payment: Payment, assessment: Assessment,
    money_may_have_moved: bool, now: datetime,
) -> CaptureResult:
    # A concurrent request may have finished while we talked to PayPal: look again before writing.
    session.refresh(po)
    session.refresh(payment)
    if po.status == POStatus.PAID and payment.status == PaymentStatus.CAPTURED:
        return CaptureResult(CaptureOutcome.PAID, po, payment)

    verdict, capture = assessment.verdict, assessment.capture

    if verdict == Verdict.PAID:
        payment.status = PaymentStatus.CAPTURED
        payment.paypal_capture_id = capture.id
        payment.verified_at = now
        payment.error = None
        session.flush()
        transition_purchase_order(session, po.id, POStatus.PAID, detail={
            "payment_id": payment.id, "paypal_order_id": payment.paypal_order_id,
            "paypal_capture_id": capture.id, "amount_cents": capture.amount_cents,
            "currency": capture.currency, "snapshot_sha256": po.approved_snapshot_sha256,
        })
        return CaptureResult(CaptureOutcome.PAID, po, payment)

    if verdict == Verdict.PENDING:
        newly_pending = payment.status != PaymentStatus.PENDING
        payment.status = PaymentStatus.PENDING
        payment.paypal_capture_id = capture.id
        session.flush()
        if newly_pending:
            record_event(session, po, "PAYMENT_CAPTURE_PENDING", {
                "payment_id": payment.id, "paypal_capture_id": capture.id,
                "reason": capture.status_reason,
            })
        return CaptureResult(CaptureOutcome.PENDING, po, payment, assessment.reasons)

    if verdict in (Verdict.DECLINED, Verdict.VOIDED):
        reasons = assessment.reasons or ("PayPal voided or expired the order",)
        _fail(session, po, payment, reasons)
        return CaptureResult(CaptureOutcome.DECLINED, po, payment, reasons)

    # MISMATCH, or an order state that makes no sense right after a capture.
    reasons = assessment.reasons or ("unexpected order state after capture",)
    if not money_may_have_moved:
        # Caught BEFORE capture: nothing was charged. Fail safe and let the merchant retry.
        _fail(session, po, payment, reasons)
        return CaptureResult(CaptureOutcome.VERIFICATION_FAILED, po, payment, reasons)

    # Money may have moved but the details are wrong. Do NOT mark the payment failed: that
    # would allow a second payment. Keep the PO pending and flag it for a human to reconcile.
    flagged = "VERIFICATION_FAILED: " + "; ".join(reasons)
    if payment.status == PaymentStatus.CREATED:
        payment.status = PaymentStatus.PENDING
    changed = payment.error != flagged
    payment.error = flagged
    session.flush()
    if changed:
        record_event(session, po, "PAYMENT_VERIFICATION_FAILED", {
            "payment_id": payment.id, "paypal_order_id": payment.paypal_order_id,
            "reasons": list(reasons),
        })
    return CaptureResult(CaptureOutcome.VERIFICATION_FAILED, po, payment, reasons)


def _fail(session: Session, po: PurchaseOrder, payment: Payment, reasons: tuple[str, ...]) -> None:
    """Payment attempt is over and no money moved: PAYMENT_FAILED, so a retry may start."""
    payment.status = PaymentStatus.FAILED
    payment.error = "; ".join(reasons)[:500]
    session.flush()
    transition_purchase_order(session, po.id, POStatus.PAYMENT_FAILED, detail={
        "payment_id": payment.id, "paypal_order_id": payment.paypal_order_id,
        "reasons": list(reasons),
    })
