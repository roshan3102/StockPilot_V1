"""Deterministic liquor-store seed data for "Barrel & Bottle".

Usage (from `backend/`):

    uv run python -m app.seed                    # wipe the dev DB, reseed relative to today
    uv run python -m app.seed --as-of 2026-10-15 # same, with a fixed "today"

Reproducibility: every number here is either a literal in the tables below or comes from
`random.Random` seeded with a fixed string per SKU. Same `as_of` date => identical rows.
Sales dates are relative to `as_of` so the data never looks stale in a demo.

Scenario (what the data is designed to exercise):
- Several SKUs are nearly out of stock; a few are healthy and should NOT be reordered.
- Some SKUs have 2-3 suppliers with different prices/case sizes; some have exactly one.
- Supplier minimums are tight against the budget: the three minimums sum to $2,650 but the
  budget is $2,500, so the merchant cannot meet every supplier's minimum in one run.
- Summit's big cases are cheaper per unit but sometimes exceed the room left under
  `max_stock` (see WINE-ROSE), which the validator must handle.

Promotion scenario: Harbor carries Svedka in six flavors at 750ml and 1.5L, 12 bottles a case.
Demand is deliberately uneven (Original and Vanilla sell fast, Peach barely moves), so a
"10 mixed cases, at least 3 flavors" rebate has a clearly better and a clearly worse split.
Grey Goose comes in 375ml, 750ml and 1L, so a question about a size that does not exist
(e.g. "70ml") has real sizes to point to.

Seasonal scenario: 450 days of history (so every product has last year's same weeks plus
the four weeks before them) with a holiday curve. Wine sells ~2-3x in Nov/Dec and sparkling
(Prosecco, Champagne) ~4x in December; spirits and mixers rise less; beer peaks in summer.
The store grows ~10% a year. So a wine & champagne rebate offered in early November should
stock up for the holidays, not just for the next two weeks. Aug-Oct are 1.0 everywhere, so
the recent weeks before mid-October (the test suite's date) are unaffected by the curve.
"""

import argparse
import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_engine, reset_schema
from app.models import MerchantSettings, Product, Sale, Supplier, SupplierProduct

SEED = "stockpilot-v1"
HISTORY_DAYS = 56  # two lookback windows of sales history

# Relative sales by weekday (Mon..Sun). Averages to 1.0 so `daily_mean` stays the true mean.
WEEKDAY_FACTOR = (0.9, 0.9, 0.9, 0.9, 1.2, 1.3, 0.9)


@dataclass(frozen=True)
class ProductSeed:
    sku: str
    name: str
    category: str
    daily_mean: float  # average units sold per day (drives generated sales history)
    on_hand: int
    max_stock: int
    brand: str | None = None
    variant: str | None = None  # flavor, for products that come in several
    size_ml: int | None = None
    season: str | None = None  # key into Catalog.seasonality; default: the category


@dataclass(frozen=True)
class SupplierSeed:
    key: str
    name: str
    min_order_cents: int
    lead_time_days: int


# (supplier key, unit price in cents, pack size) per SKU
OfferSeed = tuple[str, int, int]


@dataclass(frozen=True)
class Catalog:
    settings: dict[str, int]
    suppliers: list[SupplierSeed]
    products: list[ProductSeed]
    offers: dict[str, list[OfferSeed]]
    history_days: int = HISTORY_DAYS
    # Sales multiplier per calendar month (Jan..Dec), by ProductSeed.season or category.
    seasonality: dict[str, tuple[float, ...]] = field(default_factory=dict)
    # Business growth per year: sales older than the recent HISTORY_DAYS are scaled down by
    # 1 / (1 + growth * age_in_years), so this year runs ahead of last year by about that much.
    yearly_growth: float = 0.0


_SEASONALITY: dict[str, tuple[float, ...]] = {
    #           Jan  Feb  Mar  Apr  May  Jun  Jul  Aug  Sep  Oct  Nov  Dec
    "Wine":     (0.8, 1.0, 0.9, 0.9, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.8, 2.8),
    "Sparkling": (0.6, 1.2, 0.9, 0.9, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.6, 4.0),
    "Spirits":  (0.85, 0.9, 0.9, 0.9, 1.0, 1.0, 1.1, 1.0, 1.0, 1.0, 1.3, 1.8),
    "Beer":     (0.8, 0.8, 0.9, 1.0, 1.2, 1.3, 1.4, 1.0, 1.0, 1.0, 1.0, 1.1),
    "Mixers":   (0.8, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.0, 1.0, 1.0, 1.2, 1.6),
}

_SVEDKA = [
    # (sku code, flavor, daily mean 750ml, on hand 750ml, daily mean 1.5L, on hand 1.5L)
    ("ORIG", "Original", 2.6, 14, 1.6, 10),
    ("VAN", "Vanilla", 1.4, 6, 0.6, 5),
    ("RASP", "Raspberry", 1.3, 12, 0.5, 9),
    ("STRL", "Strawberry Lemonade", 1.0, 5, 0.4, 3),
    ("MNGP", "Mango Pineapple", 0.6, 10, 0.2, 6),
    ("PCH", "Peach", 0.25, 11, 0.1, 7),
]
_SVEDKA_PRODUCTS = [
    product
    for code, flavor, mean_750, on_hand_750, mean_1500, on_hand_1500 in _SVEDKA
    for product in (
        ProductSeed(f"SVD-{code}-750", f"Svedka {flavor} Vodka 750ml", "Spirits", mean_750,
                    on_hand=on_hand_750, max_stock=96, brand="Svedka", variant=flavor, size_ml=750),
        ProductSeed(f"SVD-{code}-1500", f"Svedka {flavor} Vodka 1.5L", "Spirits", mean_1500,
                    on_hand=on_hand_1500, max_stock=48, brand="Svedka", variant=flavor, size_ml=1500),
    )
]
_SVEDKA_OFFERS: dict[str, list[OfferSeed]] = {
    p.sku: [("harbor", 1099 if p.size_ml == 750 else 1799, 12)] for p in _SVEDKA_PRODUCTS
}

LIQUOR = Catalog(
    settings={"default_budget_cents": 250_000, "target_cover_days": 14, "lookback_days": 28},
    suppliers=[
        # Broad catalogue (beer, wine, spirits, mixers), moderate prices, small cases.
        SupplierSeed("harbor", "Harbor Wine & Spirits Distributors", min_order_cents=50_000, lead_time_days=3),
        # Beer only, fast delivery.
        SupplierSeed("brew", "Craft Brew Collective", min_order_cents=35_000, lead_time_days=2),
        # Cheapest per unit but large cases, high minimum, slow delivery.
        SupplierSeed("summit", "Summit Beverage Wholesale", min_order_cents=180_000, lead_time_days=5),
    ],
    products=[
        # --- Beer ---
        ProductSeed("BEER-IPA", "Hazy IPA 6-pack 12oz", "Beer", 6.0, on_hand=18, max_stock=180),
        ProductSeed("BEER-LAGER", "Premium Lager 12-pack 12oz", "Beer", 8.0, on_hand=140, max_stock=240),
        ProductSeed("BEER-STOUT", "Irish Stout 4-pack 14.9oz", "Beer", 2.0, on_hand=6, max_stock=60),
        ProductSeed("BEER-SELTZER", "Hard Seltzer Variety 12-pack", "Beer", 5.0, on_hand=20, max_stock=150),
        # --- Wine ---
        ProductSeed("WINE-CAB", "Cabernet Sauvignon 750ml", "Wine", 4.0, on_hand=12, max_stock=120, size_ml=750),
        ProductSeed("WINE-PINOT-G", "Pinot Grigio 750ml", "Wine", 3.5, on_hand=60, max_stock=120, size_ml=750),
        ProductSeed("WINE-PROSECCO", "Prosecco 750ml", "Wine", 2.0, on_hand=5, max_stock=60, size_ml=750,
                    season="Sparkling"),
        ProductSeed("WINE-ROSE", "Provence Rose 750ml", "Wine", 1.5, on_hand=14, max_stock=36, size_ml=750),
        ProductSeed("CHMP-MOET-BRUT", "Moet & Chandon Imperial Brut Champagne 750ml", "Wine", 0.7,
                    on_hand=8, max_stock=48, brand="Moet & Chandon", variant="Imperial Brut",
                    size_ml=750, season="Sparkling"),
        ProductSeed("CHMP-MOET-ROSE", "Moet & Chandon Rose Imperial Champagne 750ml", "Wine", 0.3,
                    on_hand=6, max_stock=24, brand="Moet & Chandon", variant="Rose Imperial",
                    size_ml=750, season="Sparkling"),
        ProductSeed("CHMP-VEUVE", "Veuve Clicquot Yellow Label Brut Champagne 750ml", "Wine", 0.5,
                    on_hand=6, max_stock=36, brand="Veuve Clicquot", variant="Yellow Label",
                    size_ml=750, season="Sparkling"),
        # --- Spirits ---
        ProductSeed("SPR-VODKA", "Premium Vodka 750ml", "Spirits", 3.0, on_hand=10, max_stock=72, size_ml=750),
        ProductSeed("SPR-BOURBON", "Kentucky Straight Bourbon 750ml", "Spirits", 2.0, on_hand=9, max_stock=48,
                    size_ml=750),
        ProductSeed("SPR-TEQUILA", "Blanco Tequila 750ml", "Spirits", 1.5, on_hand=40, max_stock=60, size_ml=750),
        ProductSeed("SPR-GIN", "London Dry Gin 750ml", "Spirits", 1.0, on_hand=4, max_stock=24, size_ml=750),
        ProductSeed("GG-375", "Grey Goose Vodka 375ml", "Spirits", 0.8, on_hand=16, max_stock=36,
                    brand="Grey Goose", variant="Original", size_ml=375),
        ProductSeed("GG-750", "Grey Goose Vodka 750ml", "Spirits", 1.8, on_hand=22, max_stock=48,
                    brand="Grey Goose", variant="Original", size_ml=750),
        ProductSeed("GG-1000", "Grey Goose Vodka 1L", "Spirits", 0.9, on_hand=9, max_stock=36,
                    brand="Grey Goose", variant="Original", size_ml=1000),
        *_SVEDKA_PRODUCTS,
        # --- Mixers ---
        ProductSeed("MIX-TONIC", "Tonic Water 4-pack", "Mixers", 3.0, on_hand=8, max_stock=80),
        ProductSeed("MIX-LIME", "Lime Juice 1L", "Mixers", 1.0, on_hand=3, max_stock=24),
        ProductSeed("MIX-ICE", "Party Ice 10lb", "Mixers", 6.0, on_hand=40, max_stock=120),
        ProductSeed("MIX-CUPS", "Plastic Cups 50ct", "Mixers", 1.5, on_hand=45, max_stock=60),
    ],
    offers={
        "BEER-IPA": [("harbor", 899, 4), ("brew", 845, 4), ("summit", 790, 24)],
        "BEER-LAGER": [("harbor", 1299, 2), ("brew", 1250, 2), ("summit", 1150, 12)],
        "BEER-STOUT": [("brew", 1050, 6)],  # single-supplier SKU
        "BEER-SELTZER": [("harbor", 1450, 2), ("brew", 1399, 2), ("summit", 1320, 10)],
        "WINE-CAB": [("harbor", 1150, 12), ("summit", 985, 36)],
        "WINE-PINOT-G": [("harbor", 875, 12), ("summit", 760, 36)],
        "WINE-PROSECCO": [("harbor", 1095, 12)],  # single-supplier SKU
        "CHMP-MOET-BRUT": [("harbor", 4599, 12)],
        "CHMP-MOET-ROSE": [("harbor", 5599, 12)],
        "CHMP-VEUVE": [("harbor", 5299, 12)],
        # Summit's case of 24 exceeds the room under max_stock (36 - 14 = 22); Harbor's 6 fits.
        "WINE-ROSE": [("harbor", 1495, 6), ("summit", 1290, 24)],
        "SPR-VODKA": [("harbor", 1899, 6), ("summit", 1699, 24)],
        "SPR-BOURBON": [("harbor", 2899, 6), ("summit", 2650, 12)],
        "SPR-TEQUILA": [("harbor", 2499, 6), ("summit", 2290, 12)],
        "SPR-GIN": [("harbor", 2199, 6)],  # single-supplier SKU
        "GG-375": [("harbor", 1799, 12), ("summit", 1650, 24)],
        "GG-750": [("harbor", 2999, 12), ("summit", 2799, 24)],
        "GG-1000": [("harbor", 3699, 12), ("summit", 3450, 24)],
        **_SVEDKA_OFFERS,
        "MIX-TONIC": [("harbor", 499, 6), ("summit", 425, 24)],
        "MIX-LIME": [("harbor", 399, 6)],  # single-supplier SKU
        "MIX-ICE": [("harbor", 249, 10), ("summit", 199, 20)],
        "MIX-CUPS": [("harbor", 450, 12), ("summit", 380, 48)],
    },
    history_days=450,
    seasonality=_SEASONALITY,
    yearly_growth=0.10,
)

# Convenience aliases used by tests.
PRODUCTS = LIQUOR.products
SUPPLIERS = LIQUOR.suppliers


def _daily_quantity(rng: random.Random, mean: float, weekday: int) -> int:
    """One day's units sold: noisy around the weekday-adjusted mean, never negative."""
    expected = mean * WEEKDAY_FACTOR[weekday]
    # Gaussian with variance ~ mean approximates Poisson-like count noise without numpy.
    return max(0, round(rng.gauss(expected, math.sqrt(expected))))


def seed_database(session: Session, as_of: date, catalog: Catalog = LIQUOR) -> None:
    """Insert all seed rows into an EMPTY schema and commit. Call `reset_schema` first."""
    session.add(MerchantSettings(id=1, **catalog.settings))

    suppliers = {s.key: Supplier(name=s.name, min_order_cents=s.min_order_cents,
                                 lead_time_days=s.lead_time_days) for s in catalog.suppliers}
    session.add_all(suppliers.values())

    products: dict[str, Product] = {}
    for p in catalog.products:
        products[p.sku] = Product(sku=p.sku, name=p.name, category=p.category,
                                  on_hand=p.on_hand, max_stock=p.max_stock,
                                  brand=p.brand, variant=p.variant, size_ml=p.size_ml)
    session.add_all(products.values())
    # flush() sends the INSERTs now (inside the open transaction) so each object gets its
    # auto-generated `id`, which the rows below need for their foreign keys.
    session.flush()

    for sku, offers in catalog.offers.items():
        for supplier_key, price, pack in offers:
            session.add(SupplierProduct(
                supplier_id=suppliers[supplier_key].id, product_id=products[sku].id,
                unit_price_cents=price, pack_size=pack))

    sales = []
    for p in catalog.products:
        profile = catalog.seasonality.get(p.season or p.category)

        def mean_on(day: date, days_ago: int, p: ProductSeed = p, profile=profile) -> float:
            mean = p.daily_mean * (profile[day.month - 1] if profile else 1.0)
            if days_ago > HISTORY_DAYS:
                mean /= 1 + catalog.yearly_growth * days_ago / 364
            return mean

        # A string seed per SKU: adding/removing a product never changes another's history.
        # The recent HISTORY_DAYS and the older history use separate streams, so a longer
        # history never changes the recent weeks.
        older = random.Random(f"{SEED}:{p.sku}:older")
        recent = random.Random(f"{SEED}:{p.sku}")
        for days_ago in range(catalog.history_days, 0, -1):
            day = as_of - timedelta(days=days_ago)
            rng = recent if days_ago <= HISTORY_DAYS else older
            quantity = _daily_quantity(rng, mean_on(day, days_ago), day.weekday())
            if quantity > 0:  # no row on zero-sale days
                sales.append({"product_id": products[p.sku].id, "sold_on": day, "quantity": quantity})
    if sales:
        session.execute(insert(Sale), sales)

    session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset the dev database and load liquor seed data.")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None,
                        help="treat this date (YYYY-MM-DD) as 'today'; default is the real today")
    args = parser.parse_args()

    settings = get_settings()
    if settings.app_env != "dev":
        # reset_schema DROPS ALL TABLES. Refuse to do that anywhere but a dev environment.
        raise SystemExit(f"Refusing to reset the database when APP_ENV={settings.app_env!r}")

    as_of = args.as_of or date.today()
    engine = get_engine()
    reset_schema(engine)
    with Session(engine) as session:
        seed_database(session, as_of, LIQUOR)
    print(f"Seeded {settings.database_url} with the liquor catalogue as of {as_of}: "
          f"{len(LIQUOR.products)} products, {len(LIQUOR.suppliers)} suppliers.")


if __name__ == "__main__":
    main()
