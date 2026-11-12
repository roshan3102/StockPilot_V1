"""The purchase-order lifecycle: create a draft, edit it, approve or cancel it.

Design rules (docs/MVP_PLAN.md sections 3 and 6):

* Every number that matters (prices, pack sizes, stock, supplier minimum, totals) is read from
  the database inside this module. Callers pass only SKUs and quantities, plus, for approval,
  the total the merchant SAW so we can prove they approved what we are about to commit.
* The same validator (`domain.po_validation.validate_order`) runs when a draft is created,
  when it is edited, and again at approval, so a draft that was fine yesterday is re-judged
  against today's prices, stock and budget.
* Status changes go through `domain.po_state.assert_transition`; this module is the only
  code that writes `PurchaseOrder.status`.
* At approval an immutable snapshot (canonical JSON + SHA-256) of the exact order is stored.
  Payment code must take the amount from it and call `verify_approved_snapshot` first.
* Every change appends a `po_events` row in the same transaction.
* Functions flush but never commit: the caller owns the transaction. Each function validates
  BEFORE it mutates anything, so when it raises, nothing was changed.

Concurrency note: checks are read-then-write inside one transaction. SQLite allows a single
writer at a time, which makes that safe for this MVP; a multi-writer database such as
PostgreSQL would need `SELECT ... FOR UPDATE` or a compare-and-swap UPDATE on `version`/`status`.
"""

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

# InvalidTransitionError is re-exported: the API layer may import services but not the domain.
from app.domain.po_state import InvalidTransitionError, POStatus, assert_transition
from app.domain.po_validation import (
    OrderCheck,
    OrderRules,
    RequestedLine,
    TrustedOffer,
    Violation,
    validate_order,
)
from app.domain.restock import RestockRecommendation
from app.models import POEvent, Product, PurchaseOrder, Supplier, SupplierProduct
from app.services.inventory import get_stock_levels
from app.services.promotions import (
    promotion_claim_for_po,
    promotion_eligible_skus,
    promotion_violations,
)
from app.services.purchase_orders import (
    InvalidOrderError,
    LineRequest,
    build_po_lines,
    create_draft_purchase_order,
)

SNAPSHOT_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------------------------
# Errors. The API layer (later) maps each to an HTTP status and error code.
# ---------------------------------------------------------------------------------------------


class PurchaseOrderNotFoundError(LookupError):
    """No purchase order has this id."""


class OrderRejectedError(ValueError):
    """The order breaks one or more purchasing rules. `violations` lists every one."""

    def __init__(self, violations: Sequence[Violation]):
        self.violations = tuple(violations)
        super().__init__("; ".join(v.message for v in self.violations))


class NotEditableError(ValueError):
    """Only a DRAFT purchase order can be edited."""


class StaleDraftError(ValueError):
    """The caller's copy of the draft is out of date: it was edited since they loaded it."""

    def __init__(self, expected_version: int, actual_version: int):
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"The draft is at version {actual_version}, but version {expected_version} was "
            f"submitted. Reload the draft and review it again."
        )


class TotalMismatchError(ValueError):
    """The total the merchant approved differs from the total the server computed."""

    def __init__(self, approved_total_cents: int, server_total_cents: int):
        self.approved_total_cents = approved_total_cents
        self.server_total_cents = server_total_cents
        super().__init__(
            f"Approved total {approved_total_cents} cents does not match the server's "
            f"total of {server_total_cents} cents"
        )


class PricesChangedError(ValueError):
    """Supplier prices or case sizes changed since the draft was priced.

    Nothing is approved: the merchant never saw the new numbers. Saving the draft again
    (`edit_draft_purchase_order` with the same lines) re-prices it and bumps the version.
    """

    def __init__(self, changes: list[dict[str, Any]], draft_total_cents: int, current_total_cents: int):
        self.changes = changes
        self.draft_total_cents = draft_total_cents
        self.current_total_cents = current_total_cents
        super().__init__(
            f"Supplier prices changed since this draft was priced: {len(changes)} line(s) "
            f"differ (draft total {draft_total_cents} cents, current total "
            f"{current_total_cents} cents). Re-price the draft and review it again."
        )


class OrderIntegrityError(RuntimeError):
    """Stored data contradicts itself (e.g. subtotal != sum of lines, or snapshot tampered).
    Not a user mistake: refuse to proceed and investigate."""


# ---------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------


def get_purchase_order(session: Session, po_id: int) -> PurchaseOrder:
    """The PO with its lines, payments and events reachable as attributes."""
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise PurchaseOrderNotFoundError(po_id)
    return po


def list_purchase_orders(session: Session, *, limit: int) -> list[PurchaseOrder]:
    """The most recent purchase orders, newest first, with suppliers, lines and payments
    loaded up front (one query each) instead of one query per row."""
    stmt = (
        select(PurchaseOrder)
        .options(
            selectinload(PurchaseOrder.supplier),
            selectinload(PurchaseOrder.lines),
            selectinload(PurchaseOrder.payments),
        )
        .order_by(PurchaseOrder.id.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


# ---------------------------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------------------------


def _now() -> datetime:
    """UTC, timezone-naive: matches how SQLite stores the DateTime columns."""
    return datetime.now(UTC).replace(tzinfo=None)


def _record(session: Session, po: PurchaseOrder, event: str, detail: dict[str, Any]) -> None:
    session.add(POEvent(po_id=po.id, event=event, detail_json=json.dumps(detail, sort_keys=True)))
    session.flush()


def record_event(session: Session, po: PurchaseOrder, event: str, detail: dict[str, Any]) -> None:
    """Append an audit event that is NOT a status change (e.g. "payment outcome uncertain").
    Status changes record their own event inside `transition_purchase_order`."""
    _record(session, po, event, detail)


def _load_offers(
    session: Session, supplier_id: int, skus: Sequence[str]
) -> dict[str, TrustedOffer]:
    """Current offer + stock facts for each SKU this supplier sells. SKUs it does not sell
    are simply absent, which the validator reports as OFFER_UNAVAILABLE."""
    rows = session.execute(
        select(SupplierProduct, Product.sku)
        .join(Product, Product.id == SupplierProduct.product_id)
        .where(SupplierProduct.supplier_id == supplier_id, Product.sku.in_(list(skus)))
    ).all()
    stock = get_stock_levels(session, list(skus))
    return {
        sku: TrustedOffer(
            unit_price_cents=offer.unit_price_cents,
            pack_size=offer.pack_size,
            on_hand=stock[sku].on_hand,
            max_stock=stock[sku].max_stock,
            incoming=stock[sku].on_order,
        )
        for offer, sku in rows
    }


def _other_committed_cents(session: Session, run_id: int | None, exclude_po_id: int | None) -> int:
    """Spend already claimed by the other live POs of the same run (they share one budget).
    Cancelled POs release their share; every other status, including DRAFT, counts."""
    if run_id is None:
        return 0
    query = select(func.coalesce(func.sum(PurchaseOrder.subtotal_cents), 0)).where(
        PurchaseOrder.run_id == run_id, PurchaseOrder.status != POStatus.CANCELLED
    )
    if exclude_po_id is not None:
        query = query.where(PurchaseOrder.id != exclude_po_id)
    return session.scalar(query)


def _check_order(
    session: Session,
    supplier_id: int,
    lines: Sequence[RequestedLine],
    *,
    budget_cents: int | None,
    run_id: int | None,
    exclude_po_id: int | None,
    promotion_id: int | None = None,
    as_of: date | None = None,
    enforce_promotion: bool = True,
) -> OrderCheck:
    """Run the pure validator against trusted data read right now.

    When the order claims a promotion and `enforce_promotion` is True (new drafts), promotion
    rule failures reject the order. On edit and approve, pass False: the merchant may keep an
    order that no longer meets the rebate, and the claim status is reported separately.
    """
    supplier = session.get(Supplier, supplier_id)
    if supplier is None:
        raise InvalidOrderError(f"Unknown supplier id {supplier_id}")
    rules = OrderRules(
        min_order_cents=supplier.min_order_cents,
        budget_cents=budget_cents,
        other_committed_cents=_other_committed_cents(session, run_id, exclude_po_id),
    )
    offers = _load_offers(session, supplier_id, [line.sku for line in lines])
    if promotion_id is None:
        return validate_order(lines, offers, rules)
    check = validate_order(lines, offers, rules,
                           exempt_from_max_stock=promotion_eligible_skus(session, promotion_id))
    if not enforce_promotion:
        return check
    extra = promotion_violations(
        session, promotion_id, supplier_id, lines, as_of or _now().date()
    )
    return OrderCheck(check.total_cents, check.violations + extra)


def _requested(lines: Sequence[LineRequest]) -> list[RequestedLine]:
    return [RequestedLine(line.sku, line.quantity) for line in lines]


def _line_summary(po: PurchaseOrder) -> list[dict[str, Any]]:
    return [
        {"sku": line.product.sku, "quantity": line.quantity, "unit_price_cents": line.unit_price_cents}
        for line in sorted(po.lines, key=lambda line: line.product.sku)
    ]


# ---------------------------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------------------------


def _snapshot_dict(po: PurchaseOrder, approved_at: datetime) -> dict[str, Any]:
    """Everything that defines what was approved, with deterministic ordering."""
    snapshot = _base_snapshot(po, approved_at)
    if po.promotion_id is not None:  # only present when claimed, so older snapshots still verify
        claim = promotion_claim_for_po(po)
        snapshot["promotion"] = {"id": po.promotion_id, "rebate_cents": claim.rebate_cents,
                                 "meets_terms": claim.meets_terms}
    return snapshot


def _base_snapshot(po: PurchaseOrder, approved_at: datetime) -> dict[str, Any]:
    return {
        "schema": SNAPSHOT_SCHEMA_VERSION,
        "po_id": po.id,
        "version": po.version,
        "run_id": po.run_id,
        "supplier": {"id": po.supplier_id, "name": po.supplier.name},
        "currency": po.currency,
        "budget_cents": po.budget_cents,
        "approved_at": approved_at.isoformat(),
        "total_cents": sum(line.line_total_cents for line in po.lines),
        "lines": [
            {
                "sku": line.product.sku,
                "product_id": line.product_id,
                "name": line.product.name,
                "quantity": line.quantity,
                "unit_price_cents": line.unit_price_cents,
                "pack_size": line.pack_size,
                "line_total_cents": line.line_total_cents,
            }
            for line in sorted(po.lines, key=lambda line: line.product.sku)
        ],
    }


def _canonical_json(data: dict[str, Any]) -> str:
    """Same data always gives the same text (sorted keys, no whitespace), so the hash is stable."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get_approved_snapshot(po: PurchaseOrder) -> dict[str, Any]:
    """The frozen record of what was approved (payment amounts must come from here).
    Does not check integrity by itself: call `verify_approved_snapshot` first."""
    if po.approved_snapshot is None:
        raise OrderIntegrityError(f"Purchase order {po.id} has no approved snapshot")
    return json.loads(po.approved_snapshot)


def verify_approved_snapshot(po: PurchaseOrder) -> None:
    """Prove the stored snapshot is intact AND still describes the live order.

    Checks: (1) its SHA-256 matches the stored hash; (2) rebuilding the snapshot from the
    current lines, supplier and totals gives exactly the stored one; (3) the approved total
    equals the snapshot total. Raises OrderIntegrityError on any difference. The database
    triggers should make a failure impossible; this check proves they were not bypassed.
    """
    if po.approved_snapshot is None or po.approved_at is None:
        raise OrderIntegrityError(f"Purchase order {po.id} has not been approved")
    if _sha256(po.approved_snapshot) != po.approved_snapshot_sha256:
        raise OrderIntegrityError(f"Purchase order {po.id}: snapshot hash does not match")
    stored = json.loads(po.approved_snapshot)
    if stored != _snapshot_dict(po, po.approved_at):
        raise OrderIntegrityError(
            f"Purchase order {po.id}: the live order no longer matches its approved snapshot"
        )
    if stored["total_cents"] != po.approved_total_cents:
        raise OrderIntegrityError(f"Purchase order {po.id}: approved total does not match snapshot")


# ---------------------------------------------------------------------------------------------
# Creating drafts
# ---------------------------------------------------------------------------------------------


def create_validated_draft(
    session: Session,
    supplier_id: int,
    lines: Sequence[LineRequest],
    *,
    budget_cents: int,
    run_id: int | None = None,
    rationale: str | None = None,
    promotion_id: int | None = None,
    as_of: date | None = None,
) -> PurchaseOrder:
    """Create a DRAFT PO only if it satisfies every purchasing rule right now, and, when it
    claims `promotion_id`, every rule of that promotion on `as_of` (default: today).

    Raises OrderRejectedError (with all violations) or InvalidOrderError (unknown supplier).
    On error nothing has been written.
    """
    if budget_cents < 0:
        raise ValueError("budget_cents cannot be negative")
    check = _check_order(
        session, supplier_id, _requested(lines),
        budget_cents=budget_cents, run_id=run_id, exclude_po_id=None,
        promotion_id=promotion_id, as_of=as_of,
    )
    if not check.ok:
        raise OrderRejectedError(check.violations)

    po = create_draft_purchase_order(
        session, supplier_id, lines, rationale, budget_cents=budget_cents, run_id=run_id,
        promotion_id=promotion_id,
    )
    detail: dict[str, Any] = {
        "run_id": run_id, "budget_cents": budget_cents, "total_cents": po.subtotal_cents,
        "version": po.version, "lines": _line_summary(po),
    }
    if promotion_id is not None:
        detail["promotion_id"] = promotion_id
    _record(session, po, "CREATED", detail)
    return po


def create_drafts_from_recommendation(
    session: Session, recommendation: RestockRecommendation, *, run_id: int
) -> list[PurchaseOrder]:
    """Turn each supplier order of a recommendation into a saved DRAFT PO.

    The recommendation is trusted for WHAT to order, not for whether it is allowed: every
    draft is re-validated, and the POs share `run_id` so together they stay within the
    recommendation's budget. If any draft is rejected the exception propagates; the caller
    rolls back, so either all drafts exist or none do.
    """
    return [
        create_validated_draft(
            session,
            order.supplier_id,
            [LineRequest(line.quote.sku, line.quote.quantity, line.reason) for line in order.lines],
            budget_cents=recommendation.budget_cents,
            run_id=run_id,
        )
        for order in recommendation.orders
    ]


# ---------------------------------------------------------------------------------------------
# Editing a draft
# ---------------------------------------------------------------------------------------------


def edit_draft_purchase_order(
    session: Session,
    po_id: int,
    *,
    expected_version: int,
    lines: Sequence[LineRequest],
    supplier_id: int | None = None,
) -> PurchaseOrder:
    """Replace a draft's lines (and optionally its supplier), re-priced from the database.

    `lines` is the complete new list; the caller cannot set prices or totals. The edit is
    validated like a new draft (so a merchant cannot save an over-budget or below-minimum
    order), the version goes up by one, and an EDITED event is recorded. Saving the same
    lines again is the way to re-price a draft after a supplier price change.

    Raises NotEditableError (not a DRAFT), StaleDraftError (version mismatch),
    OrderRejectedError, InvalidOrderError.
    """
    po = get_purchase_order(session, po_id)
    if po.status != POStatus.DRAFT:
        raise NotEditableError(f"Purchase order {po_id} is {po.status}; only a DRAFT can be edited")
    if expected_version != po.version:
        raise StaleDraftError(expected_version, po.version)

    new_supplier_id = po.supplier_id if supplier_id is None else supplier_id
    check = _check_order(
        session, new_supplier_id, _requested(lines),
        budget_cents=po.budget_cents, run_id=po.run_id, exclude_po_id=po.id,
        promotion_id=po.promotion_id, enforce_promotion=False,
    )
    if not check.ok:
        raise OrderRejectedError(check.violations)
    new_lines = build_po_lines(session, new_supplier_id, lines)  # prices read from the DB

    before = {
        "version": po.version, "supplier_id": po.supplier_id,
        "total_cents": po.subtotal_cents, "lines": _line_summary(po),
    }
    # Delete the old lines first: the composite foreign key ties each line to the PO's
    # supplier, so the supplier can only change while no line points at the old one.
    for old in list(po.lines):
        session.delete(old)
    session.flush()
    session.expire(po, ["lines"])

    po.supplier_id = new_supplier_id
    po.lines = new_lines
    po.subtotal_cents = sum(line.line_total_cents for line in new_lines)
    po.version += 1
    session.flush()

    _record(session, po, "EDITED", {
        "before": before,
        "after": {
            "version": po.version, "supplier_id": po.supplier_id,
            "total_cents": po.subtotal_cents, "lines": _line_summary(po),
        },
    })
    return po


# ---------------------------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------------------------


def approve_purchase_order(
    session: Session,
    po_id: int,
    *,
    approved_total_cents: int,
    expected_version: int,
    now: datetime | None = None,
) -> PurchaseOrder:
    """DRAFT -> APPROVED, only if the exact order the merchant reviewed is still valid.

    In order, this refuses with:
      InvalidTransitionError  the PO is not a DRAFT (also covers double-click on Approve)
      StaleDraftError         the draft was edited after the merchant loaded it
      OrderIntegrityError     stored subtotal differs from the sum of its lines
      TotalMismatchError      the total the merchant saw is not the stored total
      PricesChangedError      a supplier price or case size changed since drafting
      OrderRejectedError      any purchasing rule fails against today's data (stock, minimum,
                              budget, offers...)
    On success it freezes a snapshot, sets the approval fields, and records an APPROVED event.
    `now` exists so tests can fix the clock.
    """
    po = get_purchase_order(session, po_id)
    assert_transition(po.status, POStatus.APPROVED)
    if expected_version != po.version:
        raise StaleDraftError(expected_version, po.version)

    lines_total = sum(line.line_total_cents for line in po.lines)
    if lines_total != po.subtotal_cents:
        raise OrderIntegrityError(
            f"Purchase order {po.id}: subtotal {po.subtotal_cents} != sum of lines {lines_total}"
        )
    if approved_total_cents != po.subtotal_cents:
        raise TotalMismatchError(approved_total_cents, po.subtotal_cents)

    # Recompute from today's trusted data.
    requested = [RequestedLine(line.product.sku, line.quantity) for line in po.lines]
    offers = _load_offers(session, po.supplier_id, [r.sku for r in requested])

    changes = []
    for line in po.lines:
        current = offers.get(line.product.sku)
        if current is not None and (
            current.unit_price_cents != line.unit_price_cents or current.pack_size != line.pack_size
        ):
            changes.append({
                "sku": line.product.sku,
                "draft_unit_price_cents": line.unit_price_cents,
                "current_unit_price_cents": current.unit_price_cents,
                "draft_pack_size": line.pack_size,
                "current_pack_size": current.pack_size,
            })
    if changes:
        current_total = sum(
            line.quantity * offers[line.product.sku].unit_price_cents
            for line in po.lines if line.product.sku in offers
        )
        raise PricesChangedError(changes, po.subtotal_cents, current_total)

    approved_at = now or _now()
    check = _check_order(
        session, po.supplier_id, requested,
        budget_cents=po.budget_cents, run_id=po.run_id, exclude_po_id=po.id,
        promotion_id=po.promotion_id, as_of=approved_at.date(),
        enforce_promotion=False,
    )
    if not check.ok:
        raise OrderRejectedError(check.violations)
    if check.total_cents != approved_total_cents:  # defence in depth; unreachable if the above hold
        raise TotalMismatchError(approved_total_cents, check.total_cents)

    snapshot_text = _canonical_json(_snapshot_dict(po, approved_at))
    digest = _sha256(snapshot_text)

    old_status = po.status
    po.status = POStatus.APPROVED
    po.approved_total_cents = check.total_cents
    po.approved_at = approved_at
    po.approved_snapshot = snapshot_text
    po.approved_snapshot_sha256 = digest
    session.flush()
    _record(session, po, "APPROVED", {
        "from": old_status, "approved_total_cents": check.total_cents,
        "version": po.version, "snapshot_sha256": digest,
    })
    return po


# ---------------------------------------------------------------------------------------------
# Cancelling and other status changes
# ---------------------------------------------------------------------------------------------


def cancel_purchase_order(
    session: Session, po_id: int, *, reason: str | None = None
) -> PurchaseOrder:
    """DRAFT or APPROVED -> CANCELLED (a draft the merchant rejects is cancelled).
    The lines and any approval snapshot are kept for the record; cancelling frees the
    PO's share of its run's budget."""
    return transition_purchase_order(
        session, po_id, POStatus.CANCELLED, detail={"reason": reason}
    )


def transition_purchase_order(
    session: Session,
    po_id: int,
    to_status: POStatus | str,
    *,
    detail: dict[str, Any] | None = None,
) -> PurchaseOrder:
    """Move a PO along an allowed edge of the state machine and record the event.

    This is the single door for every status change except approval. Payment code
    (services/payments.py) uses it for PAYMENT_PENDING, PAID and PAYMENT_FAILED. It refuses:
      * edges the state machine does not allow (InvalidTransitionError)
      * APPROVED, because approval needs validation and a snapshot: use approve_purchase_order
      * PAYMENT_PENDING unless the approved snapshot still verifies (OrderIntegrityError)
    """
    target = POStatus(to_status)
    if target == POStatus.APPROVED:
        raise ValueError("Use approve_purchase_order to approve a draft")
    po = get_purchase_order(session, po_id)
    assert_transition(po.status, target)
    if target == POStatus.PAYMENT_PENDING:
        verify_approved_snapshot(po)

    old_status = po.status
    po.status = target
    session.flush()
    _record(session, po, target.value, {"from": old_status, **(detail or {})})
    return po
