"""Supplier rebate promotions, as pure functions.

A rebate promotion: buy a bundle of `required_cases` cases of the eligible products from one
supplier, covering at least `min_distinct_variants` different variants (flavors), and the
supplier pays `rebate_cents` back later. Bundles stack: 2 bundles (20 cases) earn 2 rebates,
up to `max_bundles` when the supplier sets a limit. The order itself is paid at list price.

Two questions live here:

  check_promotion_lines   does this order meet the terms? (the validator's promotion rules)
  allocate_promotion      how many bundles, and which eligible products get the cases?

Allocation, step by step (integers and exact fractions only, so it is reproducible):
  1. Forecast each product's demand over the stock-up horizon (until the promotion's "stock
     up through" date): last year's sales for the same weeks, scaled by this year's trend,
     when the history exists. This is what sees a holiday season coming.
  2. Bundles = enough to cover the products' combined shortfall over that horizon, rounded
     up, at least 1 and at most `max_bundles`. A deal that is not repeated soon is worth
     buying ahead for; max_stock is deliberately not a limit here (the merchant chose to
     allow it), but rows show how far an order goes over it.
  3. Hand out the cases one at a time. Each goes to the product whose stock would run out
     soonest at its expected rate, counting the cases already given to it. This spreads the
     cases in proportion to expected sales. Products with no expected sales go last.
  4. If fewer than `min_distinct_variants` variants received a case, move single cases from
     the most over-covered product of an already-repeated variant to the most urgent product
     of a missing variant, until the requirement is met.
  5. If more than `min_distinct_variants` variants received a case, consolidate: empty the
     least-needed extras (fewest cases, then lowest demand) onto the flavors that remain,
     so the order hits the mix-and-match requirement exactly rather than spreading further.
  6. Count the bottles that go beyond each product's need over the horizon, so the merchant
     can see how much of the deal is stock they would have bought anyway.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction

from app.domain.forecasting import (
    DemandForecast,
    ForecastMethod,
    ProductSnapshot,
    RestockConfig,
    ceil_div,
    forecast_demand,
)
from app.domain.po_validation import RequestedLine, Violation, ViolationCode


def _usd(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"


@dataclass(frozen=True)
class PromotionTerms:
    required_cases: int  # per bundle
    units_per_case: int
    rebate_cents: int  # per bundle
    min_distinct_variants: int  # across the whole order, however many bundles
    max_bundles: int | None = None  # None: the supplier sets no limit

    def __post_init__(self) -> None:
        if min(self.required_cases, self.units_per_case, self.rebate_cents) <= 0:
            raise ValueError("required_cases, units_per_case and rebate_cents must be positive")
        if not 1 <= self.min_distinct_variants <= self.required_cases:
            raise ValueError("min_distinct_variants must be between 1 and required_cases")
        if self.max_bundles is not None and self.max_bundles < 1:
            raise ValueError("max_bundles must be at least 1")

    @property
    def required_units(self) -> int:
        """Units in one bundle."""
        return self.required_cases * self.units_per_case

    def bundles_in(self, units: int) -> int:
        """Rebates earned by `units` eligible units: whole bundles, capped at max_bundles.
        Only meaningful for an order that passed `check_promotion_lines`."""
        bundles = units // self.required_units
        return bundles if self.max_bundles is None else min(bundles, self.max_bundles)


def net_after_rebate(list_total_cents: int, rebate_cents: int) -> int:
    """What an order really costs once the supplier pays the rebate back. Display only: the
    order and its payment are always the list total."""
    return list_total_cents - rebate_cents


# --- the rule check -------------------------------------------------------------------------


def check_promotion_lines(
    terms: PromotionTerms,
    variant_by_sku: Mapping[str, str],
    lines: Sequence[RequestedLine],
) -> tuple[Violation, ...]:
    """Promotion violations of an order. `variant_by_sku` holds the ELIGIBLE products only;
    other lines are allowed on the order (e.g. to reach the supplier minimum) and ignored here."""
    violations: list[Violation] = []
    promo_lines = [line for line in lines if line.sku in variant_by_sku and line.quantity > 0]

    for line in promo_lines:
        if line.quantity % terms.units_per_case:
            violations.append(Violation(
                ViolationCode.PROMO_NOT_WHOLE_CASES,
                f"{line.sku}: {line.quantity} units is not a whole number of "
                f"{terms.units_per_case}-bottle cases", line.sku))

    units = sum(line.quantity for line in promo_lines)
    bundles, leftover = divmod(units, terms.required_units)
    too_many = terms.max_bundles is not None and bundles > terms.max_bundles
    if bundles == 0 or leftover or too_many:
        limit = "" if terms.max_bundles is None else f", at most {terms.max_bundles}"
        violations.append(Violation(
            ViolationCode.PROMO_WRONG_CASE_COUNT,
            f"The order has {units} units ({units / terms.units_per_case:g} cases) of eligible "
            f"products; the promotion needs whole bundles of {terms.required_cases} cases "
            f"({terms.required_units} units){limit}"))

    variants = {variant_by_sku[line.sku] for line in promo_lines}
    if len(variants) < terms.min_distinct_variants:
        violations.append(Violation(
            ViolationCode.PROMO_TOO_FEW_VARIANTS,
            f"The order covers {len(variants)} different flavor(s); the promotion needs at "
            f"least {terms.min_distinct_variants}"))
    return tuple(violations)


# --- allocation -----------------------------------------------------------------------------


@dataclass(frozen=True)
class PromoCandidate:
    """One eligible product with the facts allocation needs (read from the database)."""

    sku: str
    name: str
    variant: str
    size_ml: int | None
    on_hand: int
    incoming: int
    max_stock: int
    units_sold_lookback: int
    unit_price_cents: int
    # Last year's sales over the horizon and over the lookback, 52 weeks back (see
    # ProductSnapshot). None when the product's history does not reach that far.
    last_year_horizon_units: int | None = None
    last_year_lookback_units: int | None = None


@dataclass(frozen=True)
class AllocationRow:
    sku: str
    name: str
    variant: str
    size_ml: int | None
    cases: int  # 0 for products that get none
    units: int
    unit_price_cents: int
    line_total_cents: int
    avg_daily_sales: float  # recent rate, display only
    demand_units: int  # expected sales over the horizon
    forecast_method: ForecastMethod
    last_year_units: int | None  # SEASONAL: what sold in the same weeks last year
    trend: Fraction | None  # SEASONAL: this year's sales relative to last year
    shortfall_units: int  # need over the horizon, before this order
    excess_units: int  # units of this order beyond that need
    over_max_stock_units: int  # how far stock would go over max_stock after the order
    cover_before: Fraction | None  # days, at the expected rate; None: no expected sales
    cover_after: Fraction | None


class AllocationStatus(StrEnum):
    OK = "OK"
    INFEASIBLE = "INFEASIBLE"  # not enough flavors among the eligible products


class DealVerdict(StrEnum):
    ALL_NEEDED = "ALL_NEEDED"  # every unit is within the products' need over the horizon
    EXTRA_STOCK = "EXTRA_STOCK"  # some units go beyond it; the merchant weighs the rebate


@dataclass(frozen=True)
class PromotionAllocation:
    status: AllocationStatus
    rows: tuple[AllocationRow, ...]  # every candidate, most cases first
    list_total_cents: int  # what the order costs (and what PayPal charges)
    bundles: int  # rebates earned
    rebate_per_bundle_cents: int
    rebate_cents: int  # bundles x rebate_per_bundle_cents
    net_cents: int  # list total minus the rebate the supplier pays back later
    total_units: int
    distinct_variants: int
    demand_units: int  # expected sales of the eligible products over the horizon
    need_units: int  # what they need over the horizon, before this order
    excess_units: int  # units of this order beyond each product's need
    horizon_days: int
    seasonal: bool  # at least one product's forecast used last year's sales
    verdict: DealVerdict | None
    message: str


def _cover(available: int, forecast: DemandForecast) -> Fraction | None:
    """Days `available` units last at the rate expected over the horizon."""
    if forecast.demand_units == 0:
        return None
    return Fraction(available * forecast.horizon_days, forecast.demand_units)


def _urgency(cover: Fraction | None) -> tuple[int, Fraction]:
    """Sort key: lower = more urgent. Products with no expected sales come after all others."""
    return (1, Fraction(0)) if cover is None else (0, cover)


def allocate_promotion(
    terms: PromotionTerms, candidates: Sequence[PromoCandidate], config: RestockConfig
) -> PromotionAllocation:
    """`config.horizon_days` is the stock-up horizon: the days until the promotion's "stock
    up through" date (or the store's cover target when the promotion has none)."""
    if len({c.sku for c in candidates}) != len(candidates):
        raise ValueError("duplicate SKU in candidates")
    upc = terms.units_per_case
    by_sku = {c.sku: c for c in sorted(candidates, key=lambda c: c.sku)}
    forecasts = {
        sku: forecast_demand(
            ProductSnapshot(sku, c.name, c.on_hand, c.max_stock, c.units_sold_lookback,
                            c.incoming, c.last_year_horizon_units, c.last_year_lookback_units),
            config,
        )
        for sku, c in by_sku.items()
    }
    cases = dict.fromkeys(by_sku, 0)

    def cover_with(sku: str, n: int) -> Fraction | None:
        return _cover(forecasts[sku].available_units + n * upc, forecasts[sku])

    def key(sku: str) -> tuple:
        # Most urgent first; ties: bigger expected demand, then SKU, so equal inputs give
        # equal output.
        return (_urgency(cover_with(sku, cases[sku])), -forecasts[sku].demand_units, sku)

    need_units = sum(f.shortfall_units for f in forecasts.values())
    bundles = max(1, ceil_div(need_units, terms.required_units))
    if terms.max_bundles is not None:
        bundles = min(bundles, terms.max_bundles)

    variants = {c.variant for c in by_sku.values()}
    problem = None
    if len(variants) < terms.min_distinct_variants:
        problem = (f"Only {len(variants)} flavor(s) are eligible; the promotion needs at least "
                   f"{terms.min_distinct_variants}.")

    if problem is None:
        # 3. One case at a time to the most urgent product.
        for _ in range(bundles * terms.required_cases):
            cases[min(by_sku, key=key)] += 1

        def used_variants() -> set[str]:
            return {by_sku[s].variant for s, n in cases.items() if n}

        def variant_cases(variant: str) -> int:
            return sum(n for s, n in cases.items() if by_sku[s].variant == variant)

        def variant_demand(variant: str) -> int:
            return sum(forecasts[s].demand_units for s in by_sku if by_sku[s].variant == variant)

        # 4. Repair the flavor requirement (too few).
        while len(used_variants()) < terms.min_distinct_variants:
            used = used_variants()
            receiver = min((s for s in by_sku if by_sku[s].variant not in used), key=key)
            donors = [s for s, n in cases.items() if n and variant_cases(by_sku[s].variant) >= 2]
            # The donor is the product whose cover after the order is highest (most to spare).
            donor = max(donors, key=lambda s: (_urgency(cover_with(s, cases[s] - 1)), s))
            cases[donor] -= 1
            cases[receiver] += 1

        # 5. Consolidate extras down to exactly min_distinct_variants. Drop the flavors that
        # urgency liked least (fewest cases, then lowest demand); give those cases to the
        # most urgent product among the flavors that remain.
        while len(used_variants()) > terms.min_distinct_variants:
            used = used_variants()
            drop = min(used, key=lambda v: (variant_cases(v), variant_demand(v), v))
            keep = used - {drop}
            donor = max((s for s, n in cases.items() if n and by_sku[s].variant == drop),
                        key=lambda s: (_urgency(cover_with(s, cases[s] - 1)), s))
            receiver = min((s for s in by_sku if by_sku[s].variant in keep), key=key)
            cases[donor] -= 1
            cases[receiver] += 1

    def row(sku: str, c: PromoCandidate) -> AllocationRow:
        f, units = forecasts[sku], cases[sku] * upc
        return AllocationRow(
            sku=sku, name=c.name, variant=c.variant, size_ml=c.size_ml,
            cases=cases[sku], units=units, unit_price_cents=c.unit_price_cents,
            line_total_cents=units * c.unit_price_cents,
            avg_daily_sales=f.avg_daily_sales, demand_units=f.demand_units,
            forecast_method=f.method, last_year_units=f.last_year_horizon_units, trend=f.trend,
            shortfall_units=f.shortfall_units, excess_units=max(0, units - f.shortfall_units),
            over_max_stock_units=max(0, c.on_hand + c.incoming + units - c.max_stock),
            cover_before=cover_with(sku, 0), cover_after=cover_with(sku, cases[sku]),
        )

    rows = tuple(sorted((row(sku, c) for sku, c in by_sku.items()),
                        key=lambda r: (-r.cases, r.sku)))
    horizon = config.horizon_days
    demand_units = sum(f.demand_units for f in forecasts.values())
    seasonal = any(f.method == ForecastMethod.SEASONAL for f in forecasts.values())

    if problem is not None:
        return PromotionAllocation(
            status=AllocationStatus.INFEASIBLE, rows=rows, list_total_cents=0, bundles=0,
            rebate_per_bundle_cents=terms.rebate_cents, rebate_cents=0, net_cents=0,
            total_units=0, distinct_variants=0, demand_units=demand_units,
            need_units=need_units, excess_units=0, horizon_days=horizon, seasonal=seasonal,
            verdict=None, message=problem,
        )

    list_total = sum(r.line_total_cents for r in rows)
    rebate = bundles * terms.rebate_cents
    net = net_after_rebate(list_total, rebate)
    total_units = sum(r.units for r in rows)
    excess = sum(r.excess_units for r in rows)
    basis = ("based on last year's sales for the same weeks, adjusted for this year's trend"
             if seasonal else "at the recent sales rate")
    outlook = (f"Over the next {horizon} days these products are expected to sell "
               f"{demand_units} units ({basis}); after current stock they need {need_units}.")
    plural = "s" if bundles != 1 else ""
    capped = (terms.max_bundles is not None and bundles == terms.max_bundles
              and need_units > bundles * terms.required_units)
    order = (f"{bundles} bundle{plural} of {terms.required_cases} cases = "
             f"{bundles * terms.required_cases} cases ({total_units} units)"
             + (f", the supplier's limit of {terms.max_bundles}" if capped else "") + ".")
    costs = (f"The order is {_usd(list_total)} at list price (what you pay now) and "
             f"{_usd(net)} after the supplier's {_usd(rebate)} rebate"
             + (f" ({bundles} x {_usd(terms.rebate_cents)})" if bundles > 1 else "") + ".")
    if excess == 0:
        verdict = DealVerdict.ALL_NEEDED
        verdict_text = "Every unit is stock these products need, so the rebate is pure savings."
    else:
        verdict = DealVerdict.EXTRA_STOCK
        verdict_text = (f"{excess} of the {total_units} units go beyond that need; the split "
                        f"keeps the extra on the fastest sellers.")
    return PromotionAllocation(
        status=AllocationStatus.OK, rows=rows, list_total_cents=list_total, bundles=bundles,
        rebate_per_bundle_cents=terms.rebate_cents, rebate_cents=rebate, net_cents=net,
        total_units=total_units, distinct_variants=len({r.variant for r in rows if r.cases}),
        demand_units=demand_units, need_units=need_units, excess_units=excess,
        horizon_days=horizon, seasonal=seasonal, verdict=verdict,
        message=f"{outlook} {order} {verdict_text} {costs}",
    )
