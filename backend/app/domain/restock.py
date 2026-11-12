"""The restocking engine: forecast -> quote offers -> pick suppliers -> fit the budget.

`recommend_restock` is the only public entry point. It never raises for "business" problems
(nothing to order, supplier unavailable, budget too small); those come back as data in the
result so a caller can show them. It raises ValueError only for malformed input.

Pipeline
  1. forecast_demand() for every product -> which products are short, and by how much.
  2. quote_offer() for every (short product, supplier offer) -> priced line or rejection.
  3. _assign(): each product goes to its cheapest quote; any supplier whose resulting
     subtotal is under its minimum order is excluded and its products are reassigned.
  4. Budget: _fit_budget() builds the order up most-urgent-first and keeps it within budget.

Every step is a plain loop over integers, so the result is reproducible and explainable.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.domain.constraints import (
    Offer,
    QuotedLine,
    RejectedOption,
    RejectionCode,
    SupplierTerms,
    quote_offer,
)
from app.domain.forecasting import DemandForecast, ProductSnapshot, RestockConfig, forecast_demand


class RestockStatus(StrEnum):
    NO_RESTOCK_NEEDED = "NO_RESTOCK_NEEDED"  # every product has enough stock for the horizon
    COMPLETE = "COMPLETE"  # every short product is covered by the orders
    PARTIAL = "PARTIAL"  # some orders, but some short products could not be ordered
    NOTHING_ORDERED = "NOTHING_ORDERED"  # products are short but no valid order exists


class UnfulfilledCode(StrEnum):
    NO_SUPPLIER_OFFERS = "NO_SUPPLIER_OFFERS"  # nobody lists this product
    NO_VIABLE_SUPPLIER = "NO_VIABLE_SUPPLIER"  # offers exist but every one was rejected
    OVER_BUDGET = "OVER_BUDGET"  # dropped so the total fits the budget


@dataclass(frozen=True)
class UnfulfilledItem:
    """A product that is short on stock but is NOT in any recommended order, and why."""

    sku: str
    shortfall_units: int
    code: UnfulfilledCode
    message: str


@dataclass(frozen=True)
class RecommendedLine:
    quote: QuotedLine  # quantity, prices, totals (all computed by the engine)
    forecast: DemandForecast  # the evidence behind the quantity
    reason: str  # a plain-English, number-for-number explanation


@dataclass(frozen=True)
class SupplierOrder:
    supplier_id: int
    supplier_name: str
    min_order_cents: int
    lines: tuple[RecommendedLine, ...]
    subtotal_cents: int


@dataclass(frozen=True)
class RestockRecommendation:
    status: RestockStatus
    orders: tuple[SupplierOrder, ...]  # one per supplier (plan decision D2)
    total_cents: int
    budget_cents: int
    remaining_budget_cents: int
    forecasts: tuple[DemandForecast, ...]  # one per input product, including "fine" ones
    unfulfilled: tuple[UnfulfilledItem, ...]
    rejected_options: tuple[RejectedOption, ...]
    assumptions: tuple[str, ...]


def _usd(cents: int) -> str:
    """Format integer cents as dollars without ever touching a float."""
    return f"${cents // 100:,}.{cents % 100:02d}"


def _price_key(quote: QuotedLine) -> tuple[int, int, int]:
    """Sort key for "cheapest": total line cost, then unit price, then supplier id.
    The last element makes ties deterministic, so equal inputs always give equal output."""
    return (quote.line_total_cents, quote.unit_price_cents, quote.supplier_id)


def _assign(
    skus: Sequence[str],
    quotes: dict[str, list[QuotedLine]],
    suppliers: dict[int, SupplierTerms],
) -> tuple[dict[str, QuotedLine], dict[int, int]]:
    """Pick one quote per sku, honouring each supplier's minimum order.

    Returns (chosen quote per sku, {excluded supplier id: subtotal that fell short}).
    A sku missing from `chosen` has no quote left because all its suppliers were excluded.

    Each pass: give every sku its cheapest quote among non-excluded suppliers, total up each
    supplier, and if any are under their minimum, exclude the one with the smallest
    subtotal (one at a time, because dropping it can change the others), then go again. The excluded set only grows, so this ends.
    Known limit: it is greedy. It never tries to *add* products to a supplier to lift it
    over its minimum, so it can miss a feasible combination.
    """
    excluded: dict[int, int] = {}
    while True:
        chosen: dict[str, QuotedLine] = {}
        for sku in skus:
            eligible = [q for q in quotes[sku] if q.supplier_id not in excluded]
            if eligible:
                chosen[sku] = min(eligible, key=_price_key)

        subtotals: dict[int, int] = defaultdict(int)
        for quote in chosen.values():
            subtotals[quote.supplier_id] += quote.line_total_cents
        short = {
            sid: total for sid, total in subtotals.items() if total < suppliers[sid].min_order_cents
        }
        if not short:
            return chosen, excluded
        worst = min(short, key=lambda sid: (short[sid], sid))
        excluded[worst] = short[worst]


def _fit_budget(
    urgency_order: Sequence[str],
    quotes: dict[str, list[QuotedLine]],
    suppliers: dict[int, SupplierTerms],
    budget_cents: int,
) -> tuple[list[str], dict[str, str]]:
    """Choose which products to buy by BUILDING UP the order, most urgent product first.

    Returns (wanted skus, {skus left out: reason}). The cost of the plan for `wanted`
    (computed by `_assign`, so supplier minimums are honoured) never exceeds the budget.

    For each product in urgency order, tentatively add it and re-plan:
      * It fits the budget  -> keep it. A product that is blocked only because its supplier's
        minimum is not reached stays in `wanted` anyway ("pending"): it costs nothing yet, and
        a later product from the same supplier can lift the supplier over its minimum.
      * It does not fit     -> normally leave it out. Exception, "unlock": if adding it would
        make a more urgent pending product orderable, evict the LEAST urgent accepted products
        (only ones less urgent than that pending product) until the plan fits. If that cannot
        be done without losing an urgent product, leave it out instead.

    Why not simply shed the least urgent product until the total fits? Shedding can strip a
    supplier of the lines it needs to reach its minimum and lose the most urgent product.
    """
    rank = {sku: i for i, sku in enumerate(urgency_order)}  # 0 = most urgent

    def plan(skus: list[str]) -> tuple[dict[str, QuotedLine], int]:
        chosen, _ = _assign(skus, quotes, suppliers)
        return chosen, sum(q.line_total_cents for q in chosen.values())

    def left_out(sku: str, why: str) -> str:
        cheapest = min(quotes[sku], key=_price_key).line_total_cents
        return (
            f"Cheapest valid line is {_usd(cheapest)}; {why} the {_usd(budget_cents)} budget. "
            f"Products are added most urgent first"
        )

    wanted: list[str] = []
    dropped: dict[str, str] = {}
    for sku in urgency_order:
        trial = [*wanted, sku]
        chosen_trial, cost = plan(trial)
        if cost <= budget_cents:
            wanted = trial
            continue

        chosen_before, _ = plan(wanted)
        unlocked = [s for s in chosen_trial if s not in chosen_before and s != sku]
        if unlocked:
            target = min(unlocked, key=rank.__getitem__)  # most urgent product this unlocks
            # Anything at least as urgent as `target` that is placed today must stay placed.
            protected = [s for s in chosen_before if rank[s] <= rank[target]] + [target]
            evictable = sorted((s for s in wanted if rank[s] > rank[target]),
                               key=rank.__getitem__, reverse=True)  # least urgent first
            kept = list(trial)
            evicted: list[str] = []
            for victim in evictable:
                kept.remove(victim)
                evicted.append(victim)
                chosen_k, cost_k = plan(kept)
                if not all(s in chosen_k for s in protected):
                    break  # evicting this one hurt something urgent: give up on unlocking
                if cost_k <= budget_cents:
                    wanted = kept
                    for v in evicted:
                        dropped[v] = left_out(
                            v, f"displaced to make room for the more urgent {target} within"
                        )
                    break
            if sku in wanted:
                continue
        dropped[sku] = left_out(sku, "it would not fit within")
    return wanted, dropped


def _line_reason(
    forecast: DemandForecast, quote: QuotedLine, config: RestockConfig, valid_offer_count: int
) -> str:
    packs = quote.quantity // quote.pack_size
    parts = [
        f"Sells {forecast.avg_daily_sales:.2f}/day, so {forecast.demand_units} units over "
        f"{config.horizon_days} days; {forecast.available_units} available; "
        f"short by {forecast.shortfall_units}.",
        f"Order {packs} case(s) of {quote.pack_size} = {quote.quantity} units "
        f"at {_usd(quote.unit_price_cents)} = {_usd(quote.line_total_cents)}.",
    ]
    if quote.capped_by_max_stock:
        parts.append("Quantity capped by max_stock, so the shortfall is only partly covered.")
    if valid_offer_count > 1:
        parts.append(f"Cheapest total of {valid_offer_count} valid supplier offers.")
    return " ".join(parts)


def _validate_inputs(
    products: Sequence[ProductSnapshot],
    suppliers: Sequence[SupplierTerms],
    offers: Sequence[Offer],
    budget_cents: int,
) -> None:
    if budget_cents < 0:
        raise ValueError("budget_cents cannot be negative")
    if len({p.sku for p in products}) != len(products):
        raise ValueError("duplicate SKU in products")
    if len({s.supplier_id for s in suppliers}) != len(suppliers):
        raise ValueError("duplicate supplier id in suppliers")
    known = {s.supplier_id for s in suppliers}
    if len({(o.supplier_id, o.sku) for o in offers}) != len(offers):
        raise ValueError("duplicate (supplier, sku) offer")
    unknown = {o.supplier_id for o in offers} - known
    if unknown:
        raise ValueError(f"offers reference unknown supplier ids: {sorted(unknown)}")


def _assumptions(config: RestockConfig) -> tuple[str, ...]:
    return (
        f"Average daily sales = units sold in the last {config.lookback_days} days / "
        f"{config.lookback_days}.",
        f"Forecast horizon is {config.horizon_days} days; demand = average daily sales x "
        f"horizon, rounded up to whole units.",
        "Incoming (paid, not yet received) stock is counted as available."
        if config.count_incoming_stock
        else "Incoming stock is ignored when sizing orders (it still uses shelf room).",
        "Supplier lead time is not modelled; the horizon is used exactly as given.",
        "Quantities are whole cases, capped to the room left under max_stock.",
        "The cheapest supplier is the lowest total line cost after case rounding "
        "(ties: lower unit price, then lower supplier id).",
        "A supplier whose subtotal is below its minimum order is excluded and its products "
        "move to the next-cheapest supplier.",
        "Within the budget, products are added most urgent first (fewest days of cover). A "
        "product that does not fit is skipped and smaller, less urgent ones may still be "
        "ordered; a line is ordered in full or not at all.",
        "A product blocked only by its supplier's minimum can still be ordered if a later "
        "product lifts that supplier over the minimum, even if that means displacing less "
        "urgent lines.",
    )


def recommend_restock(
    products: Sequence[ProductSnapshot],
    suppliers: Sequence[SupplierTerms],
    offers: Sequence[Offer],
    budget_cents: int,
    config: RestockConfig,
) -> RestockRecommendation:
    """Compute what to reorder, from whom, and for how much, within `budget_cents`.

    Offers for SKUs that are not in `products` are ignored (they are not being restocked).
    """
    _validate_inputs(products, suppliers, offers, budget_cents)
    suppliers_by_id = {s.supplier_id: s for s in suppliers}
    offers_by_sku: dict[str, list[Offer]] = defaultdict(list)
    for offer in sorted(offers, key=lambda o: o.supplier_id):
        offers_by_sku[offer.sku].append(offer)

    # 1. Forecast.
    ordered_products = sorted(products, key=lambda p: p.sku)
    forecasts = {p.sku: forecast_demand(p, config) for p in ordered_products}
    short_products = [p for p in ordered_products if forecasts[p.sku].needs_restock]

    # 2. Quote every offer for every short product.
    quotes: dict[str, list[QuotedLine]] = {}
    rejected: list[RejectedOption] = []
    unfulfilled: dict[str, UnfulfilledItem] = {}
    for product in short_products:
        shortfall = forecasts[product.sku].shortfall_units
        quotes[product.sku] = []
        for offer in offers_by_sku[product.sku]:
            result = quote_offer(product, shortfall, offer, suppliers_by_id[offer.supplier_id])
            if isinstance(result, QuotedLine):
                quotes[product.sku].append(result)
            else:
                rejected.append(result)
        if not offers_by_sku[product.sku]:
            unfulfilled[product.sku] = UnfulfilledItem(
                product.sku, shortfall, UnfulfilledCode.NO_SUPPLIER_OFFERS,
                "No supplier lists this product",
            )
        elif not quotes[product.sku]:
            unfulfilled[product.sku] = UnfulfilledItem(
                product.sku, shortfall, UnfulfilledCode.NO_VIABLE_SUPPLIER,
                "Every supplier offer was rejected (see rejected_options)",
            )

    # 3 + 4. Build the order up most-urgent-first within the budget, then assign suppliers.
    # Urgency = fewest days of cover. Cover is never None here (a product with a shortfall
    # must have sales). Ties break on SKU so the order is deterministic.
    urgency_order = sorted(
        (sku for sku, q in quotes.items() if q), key=lambda sku: (forecasts[sku].days_of_cover, sku)
    )
    active, over_budget = _fit_budget(urgency_order, quotes, suppliers_by_id, budget_cents)
    for sku, why in over_budget.items():
        unfulfilled[sku] = UnfulfilledItem(
            sku, forecasts[sku].shortfall_units, UnfulfilledCode.OVER_BUDGET, why
        )
    chosen, excluded = _assign(active, quotes, suppliers_by_id)

    # Products still active but without a quote lost every supplier to a minimum-order exclusion.
    for sku in active:
        if sku not in chosen:
            unfulfilled[sku] = UnfulfilledItem(
                sku, forecasts[sku].shortfall_units, UnfulfilledCode.NO_VIABLE_SUPPLIER,
                "Every supplier able to sell this product was excluded: none could reach its "
                "minimum order value within the available budget",
            )

    # Explain every offer that lost, for the products that were considered.
    for sku in active:
        winner = chosen.get(sku)
        for quote in quotes[sku]:
            if quote is winner:
                continue
            name = suppliers_by_id[quote.supplier_id].name
            below_min = quote.supplier_id in excluded and (
                winner is None or _price_key(quote) < _price_key(winner)
            )
            if below_min:
                rejected.append(RejectedOption(
                    sku, quote.supplier_id, RejectionCode.BELOW_MINIMUM_ORDER,
                    f"{name} was excluded: its order would total "
                    f"{_usd(excluded[quote.supplier_id])}, below its "
                    f"{_usd(suppliers_by_id[quote.supplier_id].min_order_cents)} minimum",
                ))
            else:
                rejected.append(RejectedOption(
                    sku, quote.supplier_id, RejectionCode.NOT_CHEAPEST,
                    f"{name} would cost {_usd(quote.line_total_cents)} "
                    f"({quote.quantity} units at {_usd(quote.unit_price_cents)})"
                    + (f" vs {_usd(winner.line_total_cents)} chosen" if winner else ""),
                ))

    # Build the orders, grouped by supplier.
    lines_by_supplier: dict[int, list[RecommendedLine]] = defaultdict(list)
    for sku, quote in sorted(chosen.items()):
        reason = _line_reason(forecasts[sku], quote, config, len(quotes[sku]))
        lines_by_supplier[quote.supplier_id].append(RecommendedLine(quote, forecasts[sku], reason))
    orders = tuple(
        SupplierOrder(
            supplier_id=sid,
            supplier_name=suppliers_by_id[sid].name,
            min_order_cents=suppliers_by_id[sid].min_order_cents,
            lines=tuple(lines),
            subtotal_cents=sum(line.quote.line_total_cents for line in lines),
        )
        for sid, lines in sorted(lines_by_supplier.items())
    )

    if not short_products:
        status = RestockStatus.NO_RESTOCK_NEEDED
    elif not orders:
        status = RestockStatus.NOTHING_ORDERED
    elif unfulfilled:
        status = RestockStatus.PARTIAL
    else:
        status = RestockStatus.COMPLETE

    total_cents = sum(order.subtotal_cents for order in orders)
    return RestockRecommendation(
        status=status,
        orders=orders,
        total_cents=total_cents,
        budget_cents=budget_cents,
        remaining_budget_cents=budget_cents - total_cents,
        forecasts=tuple(forecasts.values()),
        unfulfilled=tuple(sorted(unfulfilled.values(), key=lambda u: u.sku)),
        rejected_options=tuple(rejected),
        assumptions=_assumptions(config),
    )
