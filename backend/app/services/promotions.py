"""Supplier rebate promotions: from pasted text to a confirmed record to an order.

    draft_promotion_from_text   AI transcribes the text; this module matches it to the catalogue
                                and returns a DRAFT for the merchant to confirm (nothing saved)
    create_promotion            save the confirmed terms, after checking them against the catalogue
    evaluate_promotion          which flavors and sizes the cases should go to (domain.promotions)
    promotion_violations        the promotion rules an order claiming it must pass (used by
                                po_lifecycle at draft, edit and approval)

Like po_lifecycle, functions flush but never commit; the caller owns the transaction.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.llm_client import StructuredLLMClient
from app.agent.promotion_extraction import MAX_PROMOTION_TEXT_CHARS, extract_promotion
from app.domain.po_validation import RequestedLine, Violation, ViolationCode
from app.domain.promotions import (
    PromoCandidate,
    PromotionAllocation,
    PromotionTerms,
    allocate_promotion,
    check_promotion_lines,
)
from app.domain.units import find_sizes_ml, format_size
from app.domain.forecasting import RestockConfig
from app.models import (
    Product,
    Promotion,
    PromotionProduct,
    PromotionStatus,
    PurchaseOrder,
    Supplier,
    SupplierProduct,
)
from app.services.inventory import (
    MAX_SEASONAL_HORIZON_DAYS,
    get_inventory,
    get_last_year_sales,
    get_settings_row,
)

# The merchant's rule: a case of spirits is 12 bottles unless the promotion says otherwise.
DEFAULT_UNITS_PER_CASE = 12
MAX_ELIGIBLE_PRODUCTS = 50


class PromotionNotFoundError(LookupError):
    """No promotion has this id."""


class SupplierNotFoundError(LookupError):
    """No supplier has this id."""


class InvalidPromotionError(ValueError):
    """The promotion's terms do not fit the catalogue or do not make sense."""


# ---------------------------------------------------------------------------------------------
# Catalogue views the confirmation form needs
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogOffer:
    sku: str
    name: str
    brand: str | None
    variant: str | None
    size_ml: int | None
    unit_price_cents: int
    pack_size: int


def list_suppliers(session: Session) -> list[Supplier]:
    return list(session.scalars(select(Supplier).order_by(Supplier.name)))


def list_supplier_catalog(session: Session, supplier_id: int) -> list[CatalogOffer]:
    if session.get(Supplier, supplier_id) is None:
        raise SupplierNotFoundError(supplier_id)
    rows = session.execute(
        select(SupplierProduct, Product)
        .join(Product, Product.id == SupplierProduct.product_id)
        .where(SupplierProduct.supplier_id == supplier_id)
        .order_by(Product.brand, Product.variant, Product.size_ml, Product.sku)
    ).all()
    return [
        CatalogOffer(p.sku, p.name, p.brand, p.variant, p.size_ml, o.unit_price_cents, o.pack_size)
        for o, p in rows
    ]


# ---------------------------------------------------------------------------------------------
# Step 1: text -> draft (nothing is saved)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PromotionDraft:
    title: str
    kind: str
    supplier_id: int | None
    supplier_name_as_written: str | None
    brand: str | None
    required_cases: int | None
    units_per_case: int
    rebate_cents: int | None
    min_distinct_variants: int | None
    max_bundles: int | None
    sizes_ml: tuple[int, ...]
    eligible_skus: tuple[str, ...]
    valid_from: date | None
    valid_until: date | None
    ambiguities: tuple[str, ...]  # what the AI says the merchant should confirm
    warnings: tuple[str, ...]  # what WE could not match or had to assume
    source_text: str


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _match_supplier(suppliers: Sequence[Supplier], written: str | None) -> Supplier | None:
    if not written:
        return None
    wanted = _words(written)
    hits = [s for s in suppliers if wanted and wanted <= _words(s.name)]
    if len(hits) != 1:
        first = written.lower().split()[0]
        hits = [s for s in suppliers if s.name.lower().split()[0] == first]
    return hits[0] if len(hits) == 1 else None


def _dollars_to_cents(text: str | None) -> int | None:
    if not text:
        return None
    try:
        cents = Decimal(text.replace("$", "").replace(",", "").strip()) * 100
    except InvalidOperation:
        return None
    if not cents.is_finite() or cents <= 0 or cents != cents.to_integral_value():
        return None
    return int(cents)


def _sizes(written: Sequence[str]) -> tuple[int, ...]:
    sizes: list[int] = []
    for text in written:
        for size in find_sizes_ml(text):
            if size not in sizes:
                sizes.append(size)
    return tuple(sizes)


def _brand_products(session: Session, brand: str) -> list[tuple[Product, SupplierProduct]]:
    wanted = brand.strip().lower()
    rows = session.execute(
        select(Product, SupplierProduct).join(SupplierProduct, SupplierProduct.product_id == Product.id)
    ).all()
    return [
        (p, o) for p, o in rows
        if (p.brand and p.brand.lower() == wanted) or wanted in p.name.lower()
    ]


def draft_promotion_from_text(
    session: Session, llm: StructuredLLMClient, text: str, as_of: date
) -> PromotionDraft:
    """Raises LLMError when the AI service fails. Everything else is reported in `warnings`."""
    extracted = extract_promotion(llm, text, as_of)
    warnings: list[str] = []

    if extracted.kind != "REBATE":
        warnings.append(
            f"This reads like a {extracted.kind.replace('_', ' ').lower()} offer. StockPilot "
            f"handles rebates (pay list price, money back later); check the terms."
        )

    suppliers = list_suppliers(session)
    supplier = _match_supplier(suppliers, extracted.supplier_name)
    sizes = _sizes(extracted.sizes)
    units_per_case = extracted.units_per_case or DEFAULT_UNITS_PER_CASE
    if extracted.units_per_case is None:
        warnings.append(f"Assumed {DEFAULT_UNITS_PER_CASE} bottles per case.")

    eligible: list[str] = []
    if extracted.brand:
        candidates = _brand_products(session, extracted.brand)
        if supplier is None:
            carriers = {o.supplier_id for _, o in candidates}
            if len(carriers) == 1:
                supplier = session.get(Supplier, carriers.pop())
                warnings.append(
                    f"Supplier {extracted.supplier_name or '(not named)'} is not in the catalogue; "
                    f"matched {supplier.name}, the only supplier carrying {extracted.brand}."
                )
        if supplier is not None:
            candidates = [(p, o) for p, o in candidates if o.supplier_id == supplier.id]
        if sizes:
            stocked = {p.size_ml for p, _ in candidates}
            for size in sizes:
                if size not in stocked:
                    warnings.append(f"No {extracted.brand} product at {format_size(size)} in the catalogue.")
            candidates = [(p, o) for p, o in candidates if p.size_ml in sizes]
        for p, o in candidates:
            if p.sku not in eligible:
                eligible.append(p.sku)
            if o.pack_size != units_per_case:
                warnings.append(
                    f"{p.sku} is sold in cases of {o.pack_size}, not {units_per_case}."
                )
        if not candidates:
            warnings.append(f"No {extracted.brand} products matched; choose them below.")
    else:
        warnings.append("The text names no brand; choose the eligible products below.")
    if supplier is None:
        warnings.append("Choose the supplier offering this promotion.")

    rebate_cents = _dollars_to_cents(extracted.amount_usd)
    if extracted.amount_usd and rebate_cents is None:
        warnings.append(f"Could not read the amount {extracted.amount_usd!r}; enter it below.")

    return PromotionDraft(
        title=extracted.title.strip() or "Supplier promotion",
        kind=extracted.kind,
        supplier_id=supplier.id if supplier else None,
        supplier_name_as_written=extracted.supplier_name,
        brand=extracted.brand,
        required_cases=extracted.required_cases,
        units_per_case=units_per_case,
        rebate_cents=rebate_cents,
        min_distinct_variants=extracted.min_distinct_flavors,
        max_bundles=extracted.max_bundles,
        sizes_ml=sizes,
        eligible_skus=tuple(eligible),
        valid_from=extracted.valid_from,
        valid_until=extracted.valid_until,
        ambiguities=tuple(extracted.ambiguities),
        warnings=tuple(dict.fromkeys(warnings)),
        source_text=text,
    )


# ---------------------------------------------------------------------------------------------
# Step 2: confirm and save
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PromotionInput:
    supplier_id: int
    title: str
    required_cases: int
    units_per_case: int
    rebate_cents: int
    min_distinct_variants: int
    eligible_skus: Sequence[str]
    valid_from: date | None = None
    valid_until: date | None = None
    source_text: str | None = None
    max_bundles: int | None = None  # None: rebates stack without a limit
    buy_through: date | None = None  # stock up for demand until this date


def variant_of(product: Product) -> str:
    """Products without a flavor count as their own variant."""
    return product.variant or product.sku


def create_promotion(session: Session, data: PromotionInput) -> Promotion:
    """Save a confirmed promotion. Raises InvalidPromotionError with a readable reason."""
    supplier = session.get(Supplier, data.supplier_id)
    if supplier is None:
        raise InvalidPromotionError(f"There is no supplier #{data.supplier_id}")
    title = data.title.strip()
    if not title:
        raise InvalidPromotionError("The promotion needs a title")
    try:
        terms = PromotionTerms(
            data.required_cases, data.units_per_case, data.rebate_cents, data.min_distinct_variants,
            data.max_bundles,
        )
    except ValueError as exc:
        raise InvalidPromotionError(str(exc)) from exc
    if data.valid_from and data.valid_until and data.valid_from > data.valid_until:
        raise InvalidPromotionError("The start date is after the end date")
    if data.buy_through and data.valid_from and data.buy_through <= data.valid_from:
        raise InvalidPromotionError("The stock-up date must be after the start date")

    skus = list(dict.fromkeys(data.eligible_skus))
    if not skus:
        raise InvalidPromotionError("Choose at least one eligible product")
    if len(skus) > MAX_ELIGIBLE_PRODUCTS:
        raise InvalidPromotionError(f"At most {MAX_ELIGIBLE_PRODUCTS} eligible products")

    rows = {
        p.sku: (p, o) for p, o in session.execute(
            select(Product, SupplierProduct)
            .join(SupplierProduct, SupplierProduct.product_id == Product.id)
            .where(SupplierProduct.supplier_id == supplier.id, Product.sku.in_(skus))
        )
    }
    missing = [sku for sku in skus if sku not in rows]
    if missing:
        raise InvalidPromotionError(f"{supplier.name} does not sell: {', '.join(missing)}")
    wrong_case = [sku for sku in skus if rows[sku][1].pack_size != terms.units_per_case]
    if wrong_case:
        raise InvalidPromotionError(
            f"Not sold in cases of {terms.units_per_case} by {supplier.name}: {', '.join(wrong_case)}"
        )
    variants = {variant_of(rows[sku][0]) for sku in skus}
    if len(variants) < terms.min_distinct_variants:
        raise InvalidPromotionError(
            f"The eligible products cover {len(variants)} flavor(s), fewer than the "
            f"{terms.min_distinct_variants} the promotion requires"
        )

    promotion = Promotion(
        supplier_id=supplier.id, title=title, kind="REBATE", status=PromotionStatus.ACTIVE.value,
        required_cases=terms.required_cases, units_per_case=terms.units_per_case,
        rebate_cents=terms.rebate_cents, min_distinct_variants=terms.min_distinct_variants,
        max_bundles=terms.max_bundles, buy_through=data.buy_through,
        valid_from=data.valid_from, valid_until=data.valid_until, source_text=data.source_text,
        products=[
            PromotionProduct(product_id=rows[sku][0].id, supplier_id=supplier.id) for sku in skus
        ],
    )
    session.add(promotion)
    session.flush()
    return promotion


# ---------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------


def get_promotion(session: Session, promotion_id: int) -> Promotion:
    promotion = session.get(Promotion, promotion_id)
    if promotion is None:
        raise PromotionNotFoundError(promotion_id)
    return promotion


def list_promotions(session: Session) -> list[Promotion]:
    return list(session.scalars(select(Promotion).order_by(Promotion.id.desc())))


def unavailable_reason(promotion: Promotion, as_of: date) -> str | None:
    """Why the promotion cannot be used on `as_of`, or None if it can."""
    if promotion.status != PromotionStatus.ACTIVE:
        return f"Promotion #{promotion.id} is {promotion.status.lower()}"
    if promotion.valid_from and as_of < promotion.valid_from:
        return f"Promotion #{promotion.id} starts on {promotion.valid_from.isoformat()}"
    if promotion.valid_until and as_of > promotion.valid_until:
        return f"Promotion #{promotion.id} ended on {promotion.valid_until.isoformat()}"
    return None


def active_promotions(session: Session, as_of: date) -> list[Promotion]:
    return [p for p in list_promotions(session) if unavailable_reason(p, as_of) is None]


def archive_promotion(session: Session, promotion_id: int) -> Promotion:
    promotion = get_promotion(session, promotion_id)
    promotion.status = PromotionStatus.ARCHIVED.value
    session.flush()
    return promotion


def promotion_terms(promotion: Promotion) -> PromotionTerms:
    return PromotionTerms(
        promotion.required_cases, promotion.units_per_case, promotion.rebate_cents,
        promotion.min_distinct_variants, promotion.max_bundles,
    )


def promotion_eligible_skus(session: Session, promotion_id: int) -> frozenset[str]:
    promotion = session.get(Promotion, promotion_id)
    return frozenset(pp.product.sku for pp in promotion.products) if promotion else frozenset()


@dataclass(frozen=True)
class PromotionClaim:
    """Whether a PO's current lines still earn the rebate it claims."""

    meets_terms: bool
    rebate_cents: int  # 0 when the terms are not met
    message: str


def promotion_claim(
    promotion: Promotion, lines: Sequence[RequestedLine], as_of: date | None = None
) -> PromotionClaim:
    """Advisory: does this set of lines earn the rebate? Never blocks an order on its own."""
    if as_of is not None:
        reason = unavailable_reason(promotion, as_of)
        if reason:
            return PromotionClaim(False, 0, f"Does not meet rebate requirements: {reason}.")
    terms = promotion_terms(promotion)
    variants = {pp.product.sku: variant_of(pp.product) for pp in promotion.products}
    violations = check_promotion_lines(terms, variants, lines)
    if violations:
        detail = "; ".join(v.message for v in violations)
        return PromotionClaim(False, 0, f"Does not meet rebate requirements: {detail}.")
    eligible_units = sum(line.quantity for line in lines if line.sku in variants)
    bundles = terms.bundles_in(eligible_units)
    rebate = bundles * terms.rebate_cents
    plural = "s" if bundles != 1 else ""
    return PromotionClaim(
        True, rebate,
        f"Meets rebate requirements: {bundles} bundle{plural}, "
        f"${rebate // 100:,}.{rebate % 100:02d} back from the supplier.",
    )


def promotion_claim_for_po(po: PurchaseOrder, as_of: date | None = None) -> PromotionClaim:
    if po.promotion is None:
        raise ValueError("purchase order does not claim a promotion")
    lines = [RequestedLine(line.product.sku, line.quantity) for line in po.lines]
    return promotion_claim(po.promotion, lines, as_of)


def expected_rebate_cents(promotion: Promotion, po: PurchaseOrder) -> int:
    """The rebate a PO claiming `promotion` earns today, or 0 if the terms are not met."""
    return promotion_claim_for_po(po).rebate_cents


def stock_up_horizon_days(promotion: Promotion, as_of: date, default_days: int) -> int:
    """Days of demand a promotion order should cover: until the "stock up through" date,
    or the store's cover target when the promotion has none (or it has passed)."""
    if promotion.buy_through is None or promotion.buy_through <= as_of:
        return default_days
    return min((promotion.buy_through - as_of).days, MAX_SEASONAL_HORIZON_DAYS)


# ---------------------------------------------------------------------------------------------
# Evaluation and the order rules
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PromotionEvaluation:
    promotion: Promotion
    allocation: PromotionAllocation
    unavailable_reason: str | None
    stock_up_through: date  # the end of the demand window the allocation covers


def evaluate_promotion(session: Session, promotion_id: int, as_of: date) -> PromotionEvaluation:
    """Recommend how many bundles to buy and which eligible products the cases go to, from
    today's stock and the demand expected until the promotion's stock-up date (last year's
    sales for those weeks, where the history exists)."""
    promotion = get_promotion(session, promotion_id)
    settings = get_settings_row(session)
    config = RestockConfig(
        horizon_days=stock_up_horizon_days(promotion, as_of, settings.target_cover_days),
        lookback_days=settings.lookback_days,
    )
    last_year = get_last_year_sales(session, as_of, config.horizon_days, config.lookback_days)
    eligible = {pp.product.sku: pp.product for pp in promotion.products}
    prices = dict(session.execute(
        select(Product.sku, SupplierProduct.unit_price_cents)
        .join(SupplierProduct, SupplierProduct.product_id == Product.id)
        .where(SupplierProduct.supplier_id == promotion.supplier_id, Product.sku.in_(list(eligible)))
    ).all())
    candidates = [
        PromoCandidate(
            sku=row.sku, name=row.name, variant=variant_of(eligible[row.sku]),
            size_ml=eligible[row.sku].size_ml, on_hand=row.on_hand, incoming=row.on_order,
            max_stock=row.max_stock, units_sold_lookback=row.units_sold_lookback,
            unit_price_cents=prices[row.sku],
            last_year_horizon_units=ly.horizon_units if ly else None,
            last_year_lookback_units=ly.lookback_units if ly else None,
        )
        for row in get_inventory(session, as_of)
        if row.sku in eligible and row.sku in prices
        for ly in [last_year.get(row.sku)]
    ]
    allocation = allocate_promotion(promotion_terms(promotion), candidates, config)
    return PromotionEvaluation(promotion, allocation, unavailable_reason(promotion, as_of),
                               as_of + timedelta(days=config.horizon_days))


def promotion_violations(
    session: Session,
    promotion_id: int,
    supplier_id: int,
    lines: Sequence[RequestedLine],
    as_of: date,
) -> tuple[Violation, ...]:
    promotion = session.get(Promotion, promotion_id)
    if promotion is None:
        return (Violation(ViolationCode.PROMO_NOT_AVAILABLE, f"There is no promotion #{promotion_id}"),)
    if promotion.supplier_id != supplier_id:
        return (Violation(
            ViolationCode.PROMO_NOT_AVAILABLE,
            f"Promotion #{promotion.id} is offered by {promotion.supplier.name}, not this supplier",
        ),)
    reason = unavailable_reason(promotion, as_of)
    if reason:
        return (Violation(ViolationCode.PROMO_NOT_AVAILABLE, reason),)
    variants = {pp.product.sku: variant_of(pp.product) for pp in promotion.products}
    return check_promotion_lines(promotion_terms(promotion), variants, lines)
