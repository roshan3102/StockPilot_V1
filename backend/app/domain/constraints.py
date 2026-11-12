"""Per-offer rules: can this supplier sell us this product, and in what quantity and cost?

An *offer* is one supplier's price and case size for one product. `quote_offer` turns a
shortfall (units we are missing) into either a priced `QuotedLine` or a `RejectedOption`
that says why the offer cannot be used. Supplier-level rules that need several lines at
once (the minimum order value) live in restock.py.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.domain.forecasting import ProductSnapshot, ceil_div


class RejectionCode(StrEnum):
    SUPPLIER_UNAVAILABLE = "SUPPLIER_UNAVAILABLE"
    EXCEEDS_MAX_STOCK = "EXCEEDS_MAX_STOCK"  # not even one case fits under max_stock
    BELOW_MINIMUM_ORDER = "BELOW_MINIMUM_ORDER"  # supplier's order minimum was not reached
    NOT_CHEAPEST = "NOT_CHEAPEST"  # a valid offer that lost the price comparison


@dataclass(frozen=True)
class SupplierTerms:
    supplier_id: int
    name: str
    min_order_cents: int
    # The database has no availability column yet. This lets the engine (and its tests) model
    # "supplier is out of business / not delivering" without a schema change.
    available: bool = True

    def __post_init__(self) -> None:
        if self.min_order_cents < 0:
            raise ValueError(f"supplier {self.supplier_id}: min_order_cents cannot be negative")


@dataclass(frozen=True)
class Offer:
    supplier_id: int
    sku: str
    unit_price_cents: int
    pack_size: int  # the case size: we may only buy whole multiples of this

    def __post_init__(self) -> None:
        if self.unit_price_cents <= 0 or self.pack_size <= 0:
            raise ValueError(f"{self.sku}@{self.supplier_id}: price and pack_size must be positive")


@dataclass(frozen=True)
class QuotedLine:
    """A valid, fully priced candidate line. All numbers are computed here, never typed in."""

    sku: str
    supplier_id: int
    unit_price_cents: int
    pack_size: int
    shortfall_units: int  # what we were trying to cover
    quantity: int  # what we would actually buy: a whole number of cases
    line_total_cents: int
    capped_by_max_stock: bool  # True when `quantity` < shortfall because stock would overflow

    @property
    def overage_units(self) -> int:
        """Units bought beyond the shortfall because of case rounding (0 if capped)."""
        return max(0, self.quantity - self.shortfall_units)


@dataclass(frozen=True)
class RejectedOption:
    sku: str
    supplier_id: int | None  # None = the rejection is about the product, not one supplier
    code: RejectionCode
    message: str


def quote_offer(
    product: ProductSnapshot,
    shortfall_units: int,
    offer: Offer,
    supplier: SupplierTerms,
) -> QuotedLine | RejectedOption:
    """Round `shortfall_units` up to whole cases, then cap to the room left under max_stock."""
    if not supplier.available:
        return RejectedOption(
            product.sku, supplier.supplier_id, RejectionCode.SUPPLIER_UNAVAILABLE,
            f"{supplier.name} is marked unavailable",
        )

    # Incoming units will arrive and take shelf space, so they always count against the cap
    # even when the forecast ignores them (config.count_incoming_stock=False).
    room = max(0, product.max_stock - product.on_hand - product.incoming)
    max_packs = room // offer.pack_size
    if max_packs == 0:
        return RejectedOption(
            product.sku, supplier.supplier_id, RejectionCode.EXCEEDS_MAX_STOCK,
            f"{supplier.name} sells {product.sku} in cases of {offer.pack_size} but only "
            f"{room} units of room are left under max_stock {product.max_stock}",
        )

    packs_needed = ceil_div(shortfall_units, offer.pack_size)
    packs = min(packs_needed, max_packs)
    quantity = packs * offer.pack_size
    return QuotedLine(
        sku=product.sku,
        supplier_id=supplier.supplier_id,
        unit_price_cents=offer.unit_price_cents,
        pack_size=offer.pack_size,
        shortfall_units=shortfall_units,
        quantity=quantity,
        line_total_cents=quantity * offer.unit_price_cents,
        capped_by_max_stock=packs < packs_needed,
    )
