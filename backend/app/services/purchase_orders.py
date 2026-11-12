"""Creating purchase orders.

Scope note: this is only the *structural* writer. It guarantees an internally consistent
DRAFT PO (prices from the database, totals computed in one place). The business rules
(supplier minimum, budget, max_stock, ...) live in domain.po_validation and are applied by
services.po_lifecycle BEFORE it calls this module; application code should create and edit
POs through po_lifecycle, not by calling the functions here directly.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import POStatus, Product, PurchaseOrder, PurchaseOrderLine, Supplier, SupplierProduct


class InvalidOrderError(ValueError):
    """The requested order is structurally impossible (bad supplier, SKU, or quantity)."""


@dataclass(frozen=True)
class LineRequest:
    """What a caller may specify for a line. Note: NO price and NO total. Those come from the DB."""

    sku: str
    quantity: int
    reason: str | None = None


def build_po_lines(
    session: Session, supplier_id: int, lines: Sequence[LineRequest]
) -> list[PurchaseOrderLine]:
    """Turn requested (sku, quantity) pairs into line rows priced from the database.

    Raises InvalidOrderError for: no lines, duplicate SKUs, unknown supplier, a SKU the
    supplier does not offer, or a quantity that is not a positive multiple of the pack size.
    Adds nothing to the session; the caller attaches the lines to a PO.
    """
    if not lines:
        raise InvalidOrderError("A purchase order needs at least one line")
    skus = [line.sku for line in lines]
    if len(set(skus)) != len(skus):
        raise InvalidOrderError("Each SKU may appear only once per purchase order")
    if session.get(Supplier, supplier_id) is None:
        raise InvalidOrderError(f"Unknown supplier id {supplier_id}")

    # Resolve each SKU to this supplier's offer. Price and pack size are read here, from
    # the database, and copied onto the line as a snapshot.
    new_lines: list[PurchaseOrderLine] = []
    for line in lines:
        offer = session.execute(
            select(SupplierProduct)
            .join(Product, Product.id == SupplierProduct.product_id)
            .where(SupplierProduct.supplier_id == supplier_id, Product.sku == line.sku)
        ).scalar_one_or_none()
        if offer is None:
            raise InvalidOrderError(f"Supplier {supplier_id} does not offer SKU {line.sku!r}")
        if line.quantity <= 0 or line.quantity % offer.pack_size != 0:
            raise InvalidOrderError(
                f"{line.sku}: quantity {line.quantity} must be a positive multiple "
                f"of the pack size {offer.pack_size}"
            )
        new_lines.append(
            PurchaseOrderLine(
                supplier_id=supplier_id,
                product_id=offer.product_id,
                quantity=line.quantity,
                unit_price_cents=offer.unit_price_cents,
                pack_size=offer.pack_size,
                line_total_cents=line.quantity * offer.unit_price_cents,
                reason=line.reason,
            )
        )
    return new_lines


def create_draft_purchase_order(
    session: Session,
    supplier_id: int,
    lines: Sequence[LineRequest],
    rationale: str | None = None,
    *,
    budget_cents: int | None = None,
    run_id: int | None = None,
    promotion_id: int | None = None,
) -> PurchaseOrder:
    """Create a DRAFT PO and its lines. Flushes but does NOT commit; the caller owns the
    transaction, so on any error the caller can `session.rollback()` and nothing persists.

    `budget_cents` and `run_id` are recorded for the lifecycle service to enforce later.
    Raises InvalidOrderError as described in `build_po_lines`.
    """
    new_lines = build_po_lines(session, supplier_id, lines)

    # The ONLY place a subtotal is computed: the sum of the line totals just built above.
    po = PurchaseOrder(
        supplier_id=supplier_id,
        status=POStatus.DRAFT,
        subtotal_cents=sum(line.line_total_cents for line in new_lines),
        currency="USD",
        rationale=rationale,
        budget_cents=budget_cents,
        run_id=run_id,
        promotion_id=promotion_id,
        lines=new_lines,
    )
    session.add(po)
    session.flush()  # INSERT now so database constraints are checked inside this call
    return po
