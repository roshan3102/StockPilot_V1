"""Read-only queries over products, sales and supplier offers.

Pure data access: no reorder maths here (that is the M2 domain layer). Functions take a
SQLAlchemy `Session` as the first argument so callers (API, agent, tests) control the
database and the transaction.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    MerchantSettings,
    POStatus,
    Product,
    PurchaseOrder,
    PurchaseOrderLine,
    Sale,
    Supplier,
    SupplierProduct,
)


@dataclass(frozen=True)
class InventoryRow:
    sku: str
    name: str
    category: str
    on_hand: int
    max_stock: int
    on_order: int  # units on PAID purchase orders (there is no receiving workflow)
    units_sold_lookback: int  # units sold in the last `lookback_days` days before `as_of`


@dataclass(frozen=True)
class SupplierOffer:
    supplier_id: int
    supplier_name: str
    unit_price_cents: int
    pack_size: int
    min_order_cents: int
    lead_time_days: int


class DataNotSeededError(RuntimeError):
    """The database has no merchant settings row, i.e. it was never seeded."""


class UnknownSkuError(LookupError):
    """No product has the requested SKU."""


@dataclass(frozen=True)
class SalesDay:
    sold_on: date
    quantity: int


@dataclass(frozen=True)
class SalesHistory:
    sku: str
    name: str
    start_date: date  # first day of the window (inclusive)
    end_date: date  # last day of the window (inclusive); the day before `as_of`
    days: int
    total_units: int
    daily: list[SalesDay]  # only days that had sales, oldest first; quiet days have no row


def get_settings_row(session: Session) -> MerchantSettings:
    """The single merchant-settings row (budget, cover target, lookback window)."""
    settings = session.scalars(select(MerchantSettings)).one_or_none()
    if settings is None:
        raise DataNotSeededError("merchant_settings is empty; seed the database first")
    return settings


def get_sales_history(
    session: Session, sku: str, as_of: date, days: int | None = None
) -> SalesHistory:
    """Daily sales for one product over `[as_of - days, as_of)`.

    `days` defaults to the merchant's `lookback_days`. Same window convention as
    `get_inventory`, so the numbers here add up to that endpoint's sales figures.
    Raises UnknownSkuError for a SKU that does not exist (a known SKU with no sales
    returns an empty `daily` list instead).
    """
    product = session.scalars(select(Product).where(Product.sku == sku)).one_or_none()
    if product is None:
        raise UnknownSkuError(sku)
    if days is None:
        days = get_settings_row(session).lookback_days

    window_start = as_of - timedelta(days=days)
    sales = session.scalars(
        select(Sale)
        .where(Sale.product_id == product.id, Sale.sold_on >= window_start, Sale.sold_on < as_of)
        .order_by(Sale.sold_on)
    ).all()
    return SalesHistory(
        sku=product.sku,
        name=product.name,
        start_date=window_start,
        end_date=as_of - timedelta(days=1),
        days=days,
        total_units=sum(s.quantity for s in sales),
        daily=[SalesDay(s.sold_on, s.quantity) for s in sales],
    )


def _on_order_units():
    """Correlated subquery: units of the current `Product` row on PAID purchase orders.
    Shared so that inventory screens and PO validation can never define "on order" differently."""
    return (
        select(func.coalesce(func.sum(PurchaseOrderLine.quantity), 0))
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.po_id)
        .where(PurchaseOrderLine.product_id == Product.id, PurchaseOrder.status == POStatus.PAID)
        .scalar_subquery()
    )


@dataclass(frozen=True)
class StockLevel:
    product_id: int
    sku: str
    name: str
    on_hand: int
    max_stock: int
    on_order: int


def get_stock_levels(session: Session, skus: Sequence[str]) -> dict[str, StockLevel]:
    """Current stock position of the given SKUs, keyed by SKU. Unknown SKUs are absent."""
    rows = session.execute(
        select(Product, _on_order_units()).where(Product.sku.in_(skus))
    ).all()
    return {
        p.sku: StockLevel(p.id, p.sku, p.name, p.on_hand, p.max_stock, on_order)
        for p, on_order in rows
    }


def get_inventory(session: Session, as_of: date) -> list[InventoryRow]:
    """Every product with stock, on-order units and recent sales, ordered by SKU.

    The window is `[as_of - lookback_days, as_of)`: `as_of` itself is excluded because
    "today" is not finished yet.
    """
    window_start = as_of - timedelta(days=get_settings_row(session).lookback_days)

    # A correlated scalar subquery runs once per product row, referencing that row's id.
    # coalesce(..., 0) turns "no matching rows" (SUM -> NULL) into 0.
    sold = (
        select(func.coalesce(func.sum(Sale.quantity), 0))
        .where(Sale.product_id == Product.id, Sale.sold_on >= window_start, Sale.sold_on < as_of)
        .scalar_subquery()
    )
    rows = session.execute(
        select(Product, _on_order_units(), sold).order_by(Product.sku)
    ).all()
    return [
        InventoryRow(
            sku=p.sku, name=p.name, category=p.category, on_hand=p.on_hand,
            max_stock=p.max_stock, on_order=on_order_units, units_sold_lookback=sold_units,
        )
        for p, on_order_units, sold_units in rows
    ]


# 52 weeks, not 365 days: comparing a Saturday with a Saturday keeps weekly patterns aligned.
YEAR_AGO = timedelta(weeks=52)
MAX_SEASONAL_HORIZON_DAYS = 364  # beyond a year, "last year's coming weeks" overlap this year


@dataclass(frozen=True)
class LastYearSales:
    horizon_units: int  # sold in [as_of - 52w, as_of - 52w + horizon): last year's coming weeks
    lookback_units: int  # sold in [as_of - 52w - lookback, as_of - 52w): to compare with now


def get_last_year_sales(
    session: Session, as_of: date, horizon_days: int, lookback_days: int
) -> dict[str, LastYearSales]:
    """Last year's sales around the same date, for products whose history reaches back far
    enough to cover both windows. Products with a shorter history are absent: their forecast
    falls back to the recent rate rather than reading "no rows" as "no sales"."""
    if horizon_days > MAX_SEASONAL_HORIZON_DAYS:
        return {}
    pivot = as_of - YEAR_AGO
    lookback_start = pivot - timedelta(days=lookback_days)
    horizon_end = pivot + timedelta(days=horizon_days)

    def window(start: date, end: date):
        return func.coalesce(func.sum(Sale.quantity).filter(Sale.sold_on >= start,
                                                             Sale.sold_on < end), 0)

    rows = session.execute(
        select(Product.sku, func.min(Sale.sold_on), window(pivot, horizon_end),
               window(lookback_start, pivot))
        .join(Sale, Sale.product_id == Product.id)
        .group_by(Product.sku)
    ).all()
    return {
        sku: LastYearSales(horizon_units=horizon, lookback_units=lookback)
        for sku, first_sale, horizon, lookback in rows
        if first_sale <= lookback_start
    }


def list_supplier_offers(session: Session, sku: str) -> list[SupplierOffer]:
    """Every supplier that sells `sku`, cheapest unit price first.

    Returns an empty list both for an unknown SKU and for a SKU nobody supplies.
    """
    rows = session.execute(
        select(SupplierProduct, Supplier)
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .join(Product, Product.id == SupplierProduct.product_id)
        .where(Product.sku == sku)
        .order_by(SupplierProduct.unit_price_cents, Supplier.name)
    ).all()
    return [
        SupplierOffer(
            supplier_id=s.id, supplier_name=s.name, unit_price_cents=o.unit_price_cents,
            pack_size=o.pack_size, min_order_cents=s.min_order_cents,
            lead_time_days=s.lead_time_days,
        )
        for o, s in rows
    ]
