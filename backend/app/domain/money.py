"""Converting between integer cents (our storage format) and decimal strings (PayPal's).

PayPal sends and expects amounts as strings such as "153.60". Floats must never touch money
(0.1 + 0.2 != 0.3), so both directions use integer arithmetic or `Decimal`, which is exact.
USD only (docs/MVP_PLAN.md assumptions): two decimal places.
"""

from decimal import Decimal, InvalidOperation


def format_cents(cents: int) -> str:
    """15360 -> "153.60". Negative amounts are a bug, so they raise."""
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise TypeError("cents must be an int")
    if cents < 0:
        raise ValueError("an amount cannot be negative")
    dollars, remainder = divmod(cents, 100)
    return f"{dollars}.{remainder:02d}"


def parse_amount_to_cents(value: str) -> int:
    """"153.60" -> 15360. Raises ValueError for anything that is not a plain non-negative
    amount with at most two decimal places (so "1.234", "-1.00", "1e3" and " 1.00" all fail
    instead of being silently rounded or misread)."""
    if not isinstance(value, str) or not value.isascii() or not value.replace(".", "", 1).isdigit():
        raise ValueError(f"not a valid amount: {value!r}")
    try:
        amount = Decimal(value)
    except InvalidOperation:  # pragma: no cover - guarded by the check above
        raise ValueError(f"not a valid amount: {value!r}") from None
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"more than two decimal places: {value!r}")
    return int(cents)
