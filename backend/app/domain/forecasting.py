"""Step 1 of restocking: how many units do we expect to need, and how many are we short?

Two transparent methods, chosen per product:

  SEASONAL (when the product has a year of history): last year's sales over the same coming
  weeks, scaled by how this year is running compared with last year:

    trend     = units_sold_in_lookback / units_sold_in_the_same_weeks_last_year
                (clamped to [1/2, 2], so one odd month cannot double or halve a season)
    demand    = ceil(last_year_units_in_the_coming_horizon * trend)

    This is what sees a holiday coming: if last November and December sold three times the
    usual rate, so will the coming weeks (adjusted for this year's growth).

  RECENT (no usable last-year history): the recent average rate over the horizon:

    demand    = ceil(units_sold_in_lookback * horizon_days / lookback_days)

  Either way:  shortfall = demand - on_hand - incoming

We multiply before dividing and use integers and Fractions so every result is exact (no
0.1 + 0.2 style float errors) and the same on every machine.
"""

from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction

MIN_TREND = Fraction(1, 2)
MAX_TREND = Fraction(2)


class ForecastMethod(StrEnum):
    SEASONAL = "SEASONAL"  # last year's same weeks, scaled by this year's trend
    RECENT = "RECENT"  # the recent average rate


@dataclass(frozen=True)
class RestockConfig:
    """Every tunable assumption in one place. Nothing about the forecast is hard-coded."""

    horizon_days: int  # how many days of future sales we want covered
    lookback_days: int  # length of the sales window that `units_sold_lookback` covers
    # There are no arrival dates in the schema, so "relevant" incoming stock is all of it.
    # Set False to ignore incoming stock and size orders from on-hand stock alone.
    count_incoming_stock: bool = True

    def __post_init__(self) -> None:
        if self.horizon_days <= 0:
            raise ValueError("horizon_days must be positive")
        if self.lookback_days <= 0:
            raise ValueError("lookback_days must be positive")


@dataclass(frozen=True)
class ProductSnapshot:
    """What we know about one product right now. Mirrors services.inventory.InventoryRow
    (this layer may not import services, so the service maps one to the other)."""

    sku: str
    name: str
    on_hand: int
    max_stock: int
    units_sold_lookback: int  # total units sold over the last `lookback_days` days
    incoming: int = 0  # units already ordered and paid for but not yet on the shelf
    # Last year's sales, when the product has a full year of history (None otherwise):
    # over the same `horizon_days` that are about to come, and over the same `lookback_days`
    # this year's `units_sold_lookback` covers. Both windows are exactly 52 weeks back.
    last_year_horizon_units: int | None = None
    last_year_lookback_units: int | None = None

    def __post_init__(self) -> None:
        if min(self.on_hand, self.incoming, self.units_sold_lookback) < 0:
            raise ValueError(f"{self.sku}: on_hand, incoming and sales cannot be negative")
        if (self.last_year_horizon_units is None) != (self.last_year_lookback_units is None):
            raise ValueError(f"{self.sku}: give both last-year figures or neither")
        if min(self.last_year_horizon_units or 0, self.last_year_lookback_units or 0) < 0:
            raise ValueError(f"{self.sku}: last year's sales cannot be negative")
        if self.max_stock <= 0:
            raise ValueError(f"{self.sku}: max_stock must be positive")


@dataclass(frozen=True)
class DemandForecast:
    sku: str
    on_hand: int  # echoed from the input so a result can show its own evidence
    incoming: int
    avg_daily_sales: float  # for display ONLY; every decision uses the integer fields
    demand_units: int  # units expected to sell over the horizon (rounded up)
    available_units: int  # on_hand plus (optionally) incoming
    shortfall_units: int  # demand - available, never below 0
    # Days the available stock lasts at the average sales rate. None when nothing sells.
    # A Fraction is an exact ratio, so sorting products by cover has no rounding surprises.
    days_of_cover: Fraction | None
    method: ForecastMethod = ForecastMethod.RECENT
    horizon_days: int = 0
    last_year_horizon_units: int | None = None  # SEASONAL only: the evidence behind `demand`
    trend: Fraction | None = None  # SEASONAL only, after clamping

    @property
    def needs_restock(self) -> bool:
        return self.shortfall_units > 0

    @property
    def horizon_cover(self) -> Fraction | None:
        """Days the available stock lasts at the rate the forecast expects for the horizon
        (which, in a holiday season, can be much faster than the recent rate)."""
        if self.demand_units == 0:
            return None
        return Fraction(self.available_units * self.horizon_days, self.demand_units)


def ceil_div(numerator: int, denominator: int) -> int:
    """Integer ceiling division. `-(-a // b)` works because // rounds toward minus infinity:
    negating first turns "round down" into "round up". Avoids math.ceil(a / b) floats."""
    return -(-numerator // denominator)


def seasonal_trend(units_sold_lookback: int, last_year_lookback_units: int) -> Fraction:
    """This year's recent sales relative to the same weeks last year, clamped."""
    trend = Fraction(units_sold_lookback, last_year_lookback_units)
    return min(MAX_TREND, max(MIN_TREND, trend))


def forecast_demand(product: ProductSnapshot, config: RestockConfig) -> DemandForecast:
    ly_horizon, ly_lookback = product.last_year_horizon_units, product.last_year_lookback_units
    trend = None
    if ly_horizon is not None and ly_lookback:  # last year sold something in the same weeks
        trend = seasonal_trend(product.units_sold_lookback, ly_lookback)
        demand = ceil_div(ly_horizon * trend.numerator, trend.denominator)
    else:
        demand = ceil_div(product.units_sold_lookback * config.horizon_days, config.lookback_days)
    available = product.on_hand + (product.incoming if config.count_incoming_stock else 0)
    cover = (
        Fraction(available * config.lookback_days, product.units_sold_lookback)
        if product.units_sold_lookback > 0
        else None
    )
    return DemandForecast(
        sku=product.sku,
        on_hand=product.on_hand,
        incoming=product.incoming,
        avg_daily_sales=product.units_sold_lookback / config.lookback_days,
        demand_units=demand,
        available_units=available,
        shortfall_units=max(0, demand - available),
        days_of_cover=cover,
        method=ForecastMethod.RECENT if trend is None else ForecastMethod.SEASONAL,
        horizon_days=config.horizon_days,
        last_year_horizon_units=None if trend is None else ly_horizon,
        trend=trend,
    )
