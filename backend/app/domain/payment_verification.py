"""Deciding what a PayPal order MEANS, from facts we trust and facts PayPal reports.

Pure code: no HTTP, no database. `integrations/paypal.py` turns PayPal's JSON into the
`ObservedOrder` below; `services/payments.py` builds `ExpectedPayment` from OUR stored
approved snapshot; `assess_order` compares the two. Nothing a browser or an LLM says is an input.

Why this exists (docs/MVP_PLAN.md section 3): a successful popup, an HTTP 201, or a status
field read in isolation is not proof of payment. In particular PayPal answers a processor
decline with HTTP 201 and a capture `status: DECLINED`. We only call a PO paid when the order
is the one we created, for exactly the approved amount, with a capture whose status is COMPLETED.
"""

from dataclasses import dataclass
from enum import StrEnum

# PayPal order statuses we act on (Orders v2). Anything else is treated as unrecognised.
_AWAITING_BUYER = frozenset({"CREATED", "SAVED", "PAYER_ACTION_REQUIRED"})


class Verdict(StrEnum):
    AWAITING_BUYER = "AWAITING_BUYER"      # order exists, buyer has not approved it yet
    READY_TO_CAPTURE = "READY_TO_CAPTURE"  # buyer approved; money has not moved
    PAID = "PAID"                          # exactly one capture, COMPLETED, right amount
    PENDING = "PENDING"                    # capture accepted but not final (e.g. under review)
    DECLINED = "DECLINED"                  # the capture was declined or failed
    VOIDED = "VOIDED"                      # PayPal voided/expired the order; it can never be paid
    MISMATCH = "MISMATCH"                  # something does not match what we approved


@dataclass(frozen=True)
class ExpectedPayment:
    """What we know to be true, taken from the approved snapshot and our own payment row."""

    paypal_order_id: str
    amount_cents: int
    currency: str
    reference: str  # binds the order to this PO and this exact approved snapshot


@dataclass(frozen=True)
class ObservedCapture:
    id: str
    status: str  # COMPLETED, PENDING, DECLINED, FAILED, REFUNDED, PARTIALLY_REFUNDED
    amount_cents: int
    currency: str
    custom_id: str | None = None
    status_reason: str | None = None  # PayPal's `status_details.reason` for PENDING captures


@dataclass(frozen=True)
class ObservedOrder:
    """The parts of a PayPal order response that verification needs."""

    id: str
    status: str
    intent: str | None  # absent from capture responses, present on GET
    unit_count: int
    reference_id: str | None
    custom_id: str | None
    amount_cents: int
    currency: str
    captures: tuple[ObservedCapture, ...] = ()
    approve_url: str | None = None


@dataclass(frozen=True)
class Assessment:
    verdict: Verdict
    reasons: tuple[str, ...] = ()
    capture: ObservedCapture | None = None


def assess_order(expected: ExpectedPayment, order: ObservedOrder) -> Assessment:
    """Compare PayPal's view of an order with what we approved. Never raises."""
    problems = _identity_problems(expected, order)
    if problems:
        return Assessment(Verdict.MISMATCH, tuple(problems))

    if order.captures:  # money may have moved: judge the capture, whatever the order status says
        return _assess_capture(expected, order)

    if order.status in _AWAITING_BUYER:
        return Assessment(Verdict.AWAITING_BUYER)
    if order.status == "APPROVED":
        return Assessment(Verdict.READY_TO_CAPTURE)
    if order.status == "VOIDED":
        return Assessment(Verdict.VOIDED)
    if order.status == "COMPLETED":
        return Assessment(Verdict.MISMATCH, ("order is COMPLETED but has no capture",))
    return Assessment(Verdict.MISMATCH, (f"unrecognised order status {order.status!r}",))


def _identity_problems(expected: ExpectedPayment, order: ObservedOrder) -> list[str]:
    problems = []
    if order.id != expected.paypal_order_id:
        problems.append(f"order id {order.id!r} is not {expected.paypal_order_id!r}")
    if order.unit_count != 1:
        problems.append(f"expected exactly 1 purchase unit, found {order.unit_count}")
    if order.intent not in (None, "CAPTURE"):
        problems.append(f"order intent is {order.intent!r}, expected 'CAPTURE'")
    # reference_id is echoed back in every PayPal response, so it is REQUIRED to match.
    # custom_id is checked wherever PayPal shows it (it is not guaranteed on every response).
    if order.reference_id != expected.reference:
        problems.append(f"reference {order.reference_id!r} does not match {expected.reference!r}")
    if order.custom_id not in (None, expected.reference):
        problems.append(f"custom_id {order.custom_id!r} does not match {expected.reference!r}")
    if order.amount_cents != expected.amount_cents:
        problems.append(
            f"order amount {order.amount_cents} cents, approved {expected.amount_cents} cents"
        )
    if order.currency != expected.currency:
        problems.append(f"order currency {order.currency!r}, approved {expected.currency!r}")
    return problems


def _assess_capture(expected: ExpectedPayment, order: ObservedOrder) -> Assessment:
    if len(order.captures) != 1:
        return Assessment(
            Verdict.MISMATCH, (f"expected exactly 1 capture, found {len(order.captures)}",)
        )
    capture = order.captures[0]
    problems = []
    if capture.amount_cents != expected.amount_cents:
        problems.append(
            f"captured {capture.amount_cents} cents, approved {expected.amount_cents} cents"
        )
    if capture.currency != expected.currency:
        problems.append(f"captured currency {capture.currency!r}, approved {expected.currency!r}")
    if capture.custom_id not in (None, expected.reference):
        problems.append(f"capture custom_id {capture.custom_id!r} does not match")
    if problems:
        return Assessment(Verdict.MISMATCH, tuple(problems), capture)

    if capture.status == "COMPLETED":
        return Assessment(Verdict.PAID, capture=capture)
    if capture.status == "PENDING":
        reason = capture.status_reason or "no reason given"
        return Assessment(Verdict.PENDING, (f"capture is pending: {reason}",), capture)
    if capture.status in ("DECLINED", "FAILED"):
        return Assessment(Verdict.DECLINED, (f"capture was {capture.status.lower()}",), capture)
    return Assessment(
        Verdict.MISMATCH, (f"unexpected capture status {capture.status!r}",), capture
    )
