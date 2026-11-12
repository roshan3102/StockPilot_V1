"""Glue between the database and the pure restocking engine (app.domain).

This is the only place that knows both worlds. It reads rows (via services.inventory),
translates them into the plain domain types, and calls `recommend_restock`. It contains NO
purchasing arithmetic of its own: every quantity, price and total comes from the domain layer.
Read-only: it writes nothing, so it never needs to commit.
"""

from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from sqlalchemy.orm import Session

from app.domain.constraints import (
    Offer,
    QuotedLine,
    RejectedOption,
    SupplierTerms,
    quote_offer,
)
from app.domain.forecasting import DemandForecast, ProductSnapshot, RestockConfig, forecast_demand
from app.domain.restock import RestockRecommendation, recommend_restock
from app.models import MerchantSettings
from app.services.inventory import (
    InventoryRow,
    LastYearSales,
    get_inventory,
    get_last_year_sales,
    get_settings_row,
    list_supplier_offers,
)


def restock_config(
    settings: MerchantSettings, horizon_days: int | None = None
) -> RestockConfig:
    """Forecast horizon = `horizon_days` when given, else the store's `target_cover_days`.
    Supplier lead time is deliberately not added (see the assumptions in every result)."""
    return RestockConfig(
        horizon_days=settings.target_cover_days if horizon_days is None else horizon_days,
        lookback_days=settings.lookback_days,
    )


def snapshot(row: InventoryRow, last_year: LastYearSales | None = None) -> ProductSnapshot:
    return ProductSnapshot(
        sku=row.sku,
        name=row.name,
        on_hand=row.on_hand,
        max_stock=row.max_stock,
        units_sold_lookback=row.units_sold_lookback,
        incoming=row.on_order,  # units on PAID purchase orders
        last_year_horizon_units=last_year.horizon_units if last_year else None,
        last_year_lookback_units=last_year.lookback_units if last_year else None,
    )


def snapshots(
    session: Session, as_of: date, config: RestockConfig
) -> tuple[list[InventoryRow], dict[str, ProductSnapshot]]:
    """Every product's row and its forecast input, with last year's sales for the same
    coming `config.horizon_days` wherever the history reaches back that far."""
    rows = get_inventory(session, as_of)
    last_year = get_last_year_sales(session, as_of, config.horizon_days, config.lookback_days)
    return rows, {row.sku: snapshot(row, last_year.get(row.sku)) for row in rows}


@dataclass(frozen=True)
class InventoryOverviewRow:
    """One product as the inventory screen shows it (docs/MVP_PLAN.md section 5)."""

    sku: str
    name: str
    category: str
    on_hand: int
    max_stock: int
    on_order: int
    avg_daily_sales: float  # display only
    days_of_cover: Fraction | None  # None when the product has no sales in the window
    forecast: DemandForecast  # the target-cover forecast the restock engine uses


def get_inventory_overview(session: Session, as_of: date) -> list[InventoryOverviewRow]:
    """Every product with its stock, incoming units, sales rate and days of cover.

    The sales rate and cover come from the same `forecast_demand` the restock engine uses,
    so the dashboard and the recommendation can never disagree about them.
    """
    config = restock_config(get_settings_row(session))
    rows, inputs = snapshots(session, as_of, config)
    overview = []
    for row in rows:
        forecast = forecast_demand(inputs[row.sku], config)
        overview.append(
            InventoryOverviewRow(
                sku=row.sku, name=row.name, category=row.category, on_hand=row.on_hand,
                max_stock=row.max_stock, on_order=row.on_order,
                avg_daily_sales=forecast.avg_daily_sales, days_of_cover=forecast.days_of_cover,
                forecast=forecast,
            )
        )
    return overview


def build_restock_recommendation(
    session: Session,
    as_of: date,
    budget_cents: int | None = None,
    horizon_days: int | None = None,
) -> RestockRecommendation:
    """Recommend a restock for every product, using the merchant's stored settings.

    - `as_of` is passed in (not read from the clock) so results are reproducible; the
      sales window is the `lookback_days` before it, as in `get_inventory`.
    - `budget_cents` overrides `merchant_settings.default_budget_cents` for this run. The
      plan makes the request's budget authoritative, hence the explicit parameter.
    - `horizon_days` overrides `target_cover_days` for this run (e.g. 7 for "next week").
    """
    settings = get_settings_row(session)
    config = restock_config(settings, horizon_days)
    rows, inputs = snapshots(session, as_of, config)
    products = [inputs[row.sku] for row in rows]

    # One query per SKU (about 17 in the seed data). Fine at this size; if it ever matters,
    # replace with a single joined query rather than adding caching.
    suppliers: dict[int, SupplierTerms] = {}
    offers: list[Offer] = []
    for row in rows:
        for o in list_supplier_offers(session, row.sku):
            # A supplier appears once per SKU it sells; keep the first SupplierTerms we see.
            # `available` is always True: the database has no availability column yet.
            suppliers.setdefault(
                o.supplier_id,
                SupplierTerms(o.supplier_id, o.supplier_name, o.min_order_cents),
            )
            offers.append(
                Offer(o.supplier_id, row.sku, o.unit_price_cents, o.pack_size)
            )

    budget = settings.default_budget_cents if budget_cents is None else budget_cents
    return recommend_restock(products, list(suppliers.values()), offers, budget, config)


@dataclass(frozen=True)
class OfferComparison:
    """One supplier's offer for one product, priced against today's shortfall."""

    supplier_id: int
    supplier_name: str
    unit_price_cents: int
    pack_size: int
    min_order_cents: int  # supplier-wide: the whole PO must reach this, not just this line
    lead_time_days: int
    # Exactly one of these is set when the product is short; both are None when it is not.
    quote: QuotedLine | None  # whole cases covering the shortfall, capped by max_stock
    rejection: RejectedOption | None  # why this offer cannot be used at all


@dataclass(frozen=True)
class SkuComparison:
    sku: str
    name: str
    shortfall_units: int  # 0 means no restock is needed right now
    offers: tuple[OfferComparison, ...]  # cheapest unit price first (as list_supplier_offers)


def compare_supplier_offers(
    session: Session,
    as_of: date,
    skus: list[str],
    horizon_days: int | None = None,
) -> list[SkuComparison]:
    """Every supplier offer for each SKU, with the quantity and cost that would cover the
    current shortfall. Same forecast and `quote_offer` as the restock engine, so a
    comparison and a recommendation can never disagree.

    SKUs that do not exist are simply absent from the result (the same convention as
    `get_stock_levels`); callers compare against what they asked for. Read-only.
    """
    config = restock_config(get_settings_row(session), horizon_days)
    all_rows, inputs = snapshots(session, as_of, config)
    rows = {row.sku: row for row in all_rows}
    comparisons = []
    for sku in skus:
        row = rows.get(sku)
        if row is None:
            continue
        product = inputs[sku]
        shortfall = forecast_demand(product, config).shortfall_units
        offers = []
        for o in list_supplier_offers(session, sku):
            result = None
            if shortfall > 0:
                result = quote_offer(
                    product, shortfall, Offer(o.supplier_id, sku, o.unit_price_cents, o.pack_size),
                    SupplierTerms(o.supplier_id, o.supplier_name, o.min_order_cents),
                )
            offers.append(OfferComparison(
                supplier_id=o.supplier_id, supplier_name=o.supplier_name,
                unit_price_cents=o.unit_price_cents, pack_size=o.pack_size,
                min_order_cents=o.min_order_cents, lead_time_days=o.lead_time_days,
                quote=result if isinstance(result, QuotedLine) else None,
                rejection=result if isinstance(result, RejectedOption) else None,
            ))
        comparisons.append(SkuComparison(sku, row.name, shortfall, tuple(offers)))
    return comparisons
