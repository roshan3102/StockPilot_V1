"""Finding products the way a merchant names them ("grey goose 750", "svedka vanilla 1.5L").

Deterministic text matching, no AI: every word of the query (minus filler like "how much")
must appear in the product's name, brand, flavor, SKU or category. A size in the query is
matched exactly; if no matching product has that size, the result says so and lists the sizes
that do exist, so nobody has to guess what "70ml" meant. Stock and sales numbers come from the
same inventory overview the dashboard uses. Read-only.
"""

import re
from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.units import find_sizes_ml, strip_sizes
from app.models import Product
from app.services.procurement import get_inventory_overview

MAX_MATCHES = 25

_FILLER = frozenset("""
    a an and any are at bottle bottles case cases count do does for from have how i in inventory
    is it left many me much number of on our please show stock stocked the there tell units we
    what whats which with you
""".split())


@dataclass(frozen=True)
class ProductMatch:
    sku: str
    name: str
    category: str
    brand: str | None
    variant: str | None
    size_ml: int | None
    on_hand: int
    on_order: int
    max_stock: int
    avg_daily_sales: float  # display only
    days_of_cover: Fraction | None


@dataclass(frozen=True)
class ProductSearchResult:
    query: str
    terms: tuple[str, ...]  # the words that had to match
    requested_size_ml: int | None
    # None: no size asked. False: products matched the words but none has that size; `matches`
    # then lists every size that does exist.
    size_found: bool | None
    matches: tuple[ProductMatch, ...]


def _terms(query: str) -> tuple[str, ...]:
    words = re.findall(r"[a-z0-9]+", strip_sizes(query).lower())
    return tuple(dict.fromkeys(w for w in words if w not in _FILLER))


def search_products(session: Session, as_of: date, query: str) -> ProductSearchResult:
    terms = _terms(query)
    sizes = find_sizes_ml(query)
    requested = sizes[0] if sizes else None

    attributes = {p.sku: p for p in session.scalars(select(Product))}
    word_matches = []
    for row in get_inventory_overview(session, as_of):
        product = attributes[row.sku]
        haystack = " ".join(
            filter(None, [row.name, row.sku, row.category, product.brand, product.variant])
        ).lower()
        if terms and all(term in haystack for term in terms):
            word_matches.append(ProductMatch(
                sku=row.sku, name=row.name, category=row.category, brand=product.brand,
                variant=product.variant, size_ml=product.size_ml, on_hand=row.on_hand,
                on_order=row.on_order, max_stock=row.max_stock,
                avg_daily_sales=row.avg_daily_sales, days_of_cover=row.days_of_cover,
            ))

    size_found = None
    matches = word_matches
    if requested is not None:
        exact = [m for m in word_matches if m.size_ml == requested]
        size_found = bool(exact)
        matches = exact or word_matches
    matches.sort(key=lambda m: (m.brand or "", m.variant or "", m.size_ml or 0, m.sku))
    return ProductSearchResult(
        query=query, terms=terms, requested_size_ml=requested, size_found=size_found,
        matches=tuple(matches[:MAX_MATCHES]),
    )
