"""The purchase-order state machine: which status may follow which.

This module is the single source of truth for the lifecycle (docs/MVP_PLAN.md section 3).
It knows nothing about the database; the service layer calls `assert_transition` before every
status change, and `models.py` re-exports `POStatus` for the table's CHECK constraint.

    DRAFT --> APPROVED --> PAYMENT_PENDING --> PAID
      |           |              |
      |           |              +--> PAYMENT_FAILED --> PAYMENT_PENDING (retry)
      +-----------+--> CANCELLED

PAID and CANCELLED are terminal. A PO whose payment failed can only be retried, not cancelled
(a plan decision: it has been approved and may have a payment attempt on record).
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class POStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    PAID = "PAID"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    CANCELLED = "CANCELLED"


# MappingProxyType is a read-only view: nothing can add a transition at runtime by accident.
ALLOWED_TRANSITIONS: Mapping[POStatus, frozenset[POStatus]] = MappingProxyType({
    POStatus.DRAFT: frozenset({POStatus.APPROVED, POStatus.CANCELLED}),
    POStatus.APPROVED: frozenset({POStatus.PAYMENT_PENDING, POStatus.CANCELLED}),
    POStatus.PAYMENT_PENDING: frozenset({POStatus.PAID, POStatus.PAYMENT_FAILED}),
    POStatus.PAYMENT_FAILED: frozenset({POStatus.PAYMENT_PENDING}),
    POStatus.PAID: frozenset(),
    POStatus.CANCELLED: frozenset(),
})


class InvalidTransitionError(ValueError):
    """The requested status change is not an edge of the state machine."""

    def __init__(self, from_status: str, to_status: str):
        allowed = sorted(ALLOWED_TRANSITIONS[POStatus(from_status)])
        super().__init__(
            f"Cannot move a purchase order from {from_status} to {to_status} "
            f"(allowed from {from_status}: {', '.join(allowed) or 'none, it is final'})"
        )
        self.from_status = from_status
        self.to_status = to_status


def can_transition(from_status: str, to_status: str) -> bool:
    return POStatus(to_status) in ALLOWED_TRANSITIONS[POStatus(from_status)]


def assert_transition(from_status: str, to_status: str) -> None:
    """Raise InvalidTransitionError unless `from_status -> to_status` is allowed.

    Unknown status strings raise ValueError (from the enum lookup), which is correct: that
    is a bug or corrupt data, not a business rule.
    """
    if not can_transition(from_status, to_status):
        raise InvalidTransitionError(from_status, to_status)
