"""Purchasing rules for ONE purchase order, as a pure function.

`validate_order` is the single place these rules live. The service layer calls it with
numbers it just read from the database (never numbers a client sent), at three moments:
when a draft is created, when it is edited, and again when it is approved.

Rules (docs/MVP_PLAN.md section 6):
  - the order has at least one line, and each SKU appears once
  - the supplier currently offers every SKU
  - quantity is a positive whole number of cases (multiple of pack size)
  - quantity fits in the room left under max_stock (max_stock - on_hand - incoming), except
    for the eligible products of a promotion the order claims
  - the supplier's minimum order value is met
  - this order plus the other live orders of the same run stay within the budget

It reports EVERY violation, not just the first, so a merchant (or later the agent) can fix
them all in one pass. Equality is allowed at both edges: spending exactly the budget or
exactly the minimum is fine.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum


class ViolationCode(StrEnum):
    EMPTY_ORDER = "EMPTY_ORDER"
    DUPLICATE_SKU = "DUPLICATE_SKU"
    OFFER_UNAVAILABLE = "OFFER_UNAVAILABLE"  # supplier does not (or no longer) sell the SKU
    INVALID_QUANTITY = "INVALID_QUANTITY"  # zero or negative
    NOT_PACK_MULTIPLE = "NOT_PACK_MULTIPLE"
    EXCEEDS_MAX_STOCK = "EXCEEDS_MAX_STOCK"
    BELOW_MINIMUM_ORDER = "BELOW_MINIMUM_ORDER"
    BUDGET_NOT_SET = "BUDGET_NOT_SET"
    OVER_BUDGET = "OVER_BUDGET"
    # Only for an order that claims a promotion (see domain.promotions).
    PROMO_NOT_AVAILABLE = "PROMO_NOT_AVAILABLE"  # archived, expired, or another supplier's
    PROMO_NOT_WHOLE_CASES = "PROMO_NOT_WHOLE_CASES"
    PROMO_WRONG_CASE_COUNT = "PROMO_WRONG_CASE_COUNT"
    PROMO_TOO_FEW_VARIANTS = "PROMO_TOO_FEW_VARIANTS"


@dataclass(frozen=True)
class RequestedLine:
    """What is being asked for. Only a SKU and a quantity: no price, no total."""

    sku: str
    quantity: int


@dataclass(frozen=True)
class TrustedOffer:
    """Facts read from the database right now about one SKU at the order's supplier."""

    unit_price_cents: int
    pack_size: int
    on_hand: int
    max_stock: int
    incoming: int  # units on PAID purchase orders


@dataclass(frozen=True)
class OrderRules:
    min_order_cents: int
    budget_cents: int | None  # None = no budget was recorded: the order cannot be approved
    # Spend of the OTHER live (non-cancelled) orders in the same run, which shares the budget.
    other_committed_cents: int = 0


@dataclass(frozen=True)
class Violation:
    code: ViolationCode
    message: str
    sku: str | None = None  # None when the problem is about the whole order


@dataclass(frozen=True)
class OrderCheck:
    # Computed here from trusted prices. Lines without a usable offer or with a non-positive
    # quantity contribute nothing, so this number is only meaningful when `ok` is True.
    total_cents: int
    violations: tuple[Violation, ...]

    @property
    def ok(self) -> bool:
        return not self.violations


def _usd(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"


def validate_order(
    lines: Sequence[RequestedLine],
    offers: Mapping[str, TrustedOffer],
    rules: OrderRules,
    *,
    exempt_from_max_stock: Collection[str] = (),
) -> OrderCheck:
    """`exempt_from_max_stock`: SKUs allowed past max_stock, i.e. the eligible products of a
    promotion the order claims (buying ahead on a deal that will not come back soon)."""
    violations: list[Violation] = []
    total = 0

    if not lines:
        violations.append(Violation(ViolationCode.EMPTY_ORDER, "An order needs at least one line"))

    seen: set[str] = set()
    for line in lines:
        if line.sku in seen:
            violations.append(Violation(
                ViolationCode.DUPLICATE_SKU, f"{line.sku} appears more than once", line.sku))
            continue  # judge the first occurrence only, so the total is not double counted
        seen.add(line.sku)

        offer = offers.get(line.sku)
        if offer is None:
            violations.append(Violation(
                ViolationCode.OFFER_UNAVAILABLE,
                f"The supplier does not offer {line.sku}", line.sku))
            continue
        if line.quantity <= 0:
            violations.append(Violation(
                ViolationCode.INVALID_QUANTITY,
                f"{line.sku}: quantity must be positive, got {line.quantity}", line.sku))
            continue

        total += line.quantity * offer.unit_price_cents
        if line.quantity % offer.pack_size != 0:
            violations.append(Violation(
                ViolationCode.NOT_PACK_MULTIPLE,
                f"{line.sku}: quantity {line.quantity} is not a multiple of the case size "
                f"{offer.pack_size}", line.sku))
        room = max(0, offer.max_stock - offer.on_hand - offer.incoming)
        if line.quantity > room and line.sku not in exempt_from_max_stock:
            violations.append(Violation(
                ViolationCode.EXCEEDS_MAX_STOCK,
                f"{line.sku}: quantity {line.quantity} exceeds the {room} units of room left "
                f"under max_stock {offer.max_stock}", line.sku))

    if lines and total < rules.min_order_cents:
        violations.append(Violation(
            ViolationCode.BELOW_MINIMUM_ORDER,
            f"Order total {_usd(total)} is below the supplier minimum "
            f"{_usd(rules.min_order_cents)}"))

    if rules.budget_cents is None:
        violations.append(Violation(
            ViolationCode.BUDGET_NOT_SET, "No budget is recorded for this order"))
    elif total + rules.other_committed_cents > rules.budget_cents:
        violations.append(Violation(
            ViolationCode.OVER_BUDGET,
            f"Order total {_usd(total)} plus {_usd(rules.other_committed_cents)} already "
            f"committed in this run exceeds the {_usd(rules.budget_cents)} budget"))

    return OrderCheck(total_cents=total, violations=tuple(violations))
