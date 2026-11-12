"""Pydantic models: the JSON shapes of the API (docs/MVP_PLAN.md section 5).

Request models validate what clients send. Response models define (and document, via the
OpenAPI page at /docs) what the API returns. The `from_*` helpers only RESHAPE data that the
services already computed; they never calculate a price, quantity or total.

Money is integer cents everywhere, as in the database.
"""

import json
from datetime import date, datetime
from fractions import Fraction
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.domain.constraints import RejectionCode
from app.domain.po_state import POStatus
from app.domain.promotions import AllocationStatus, net_after_rebate
from app.domain.restock import (
    RecommendedLine,
    RestockRecommendation,
    RestockStatus,
    SupplierOrder,
    UnfulfilledCode,
)
from app.services.assistant import MAX_QUESTION_CHARS, AssistantAnswer
from app.services.inventory import SalesHistory
from app.services.procurement import InventoryOverviewRow
from app.services.promotions import (
    MAX_PROMOTION_TEXT_CHARS,
    CatalogOffer,
    PromotionDraft,
    PromotionEvaluation,
    promotion_claim_for_po,
    unavailable_reason,
    variant_of,
)
from app.services.restock_runs import MAX_MESSAGE_CHARS, RunView

if TYPE_CHECKING:  # only for type hints; this module reshapes, it does not touch the database
    from app.models import Payment, Promotion, PurchaseOrder

# $1,000,000. An arbitrary sanity ceiling that rejects typos like an extra few zeros.
MAX_BUDGET_CENTS = 100_000_000


def _cover(value: Fraction | None) -> float | None:
    """Days of cover for display: an exact Fraction inside the engine, 1 decimal on the wire."""
    return None if value is None else round(float(value), 1)


# --- errors -------------------------------------------------------------------------------


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody


# --- GET /inventory -----------------------------------------------------------------------


class InventoryItemOut(BaseModel):
    sku: str
    name: str
    category: str
    on_hand: int
    max_stock: int
    on_order: int
    avg_daily_sales: float
    days_of_cover: float | None = Field(description="null when the product has no recent sales")
    forecast_units: int = Field(description="Expected sales over the cover target")
    forecast_method: Literal["SEASONAL", "RECENT"] = Field(
        description="SEASONAL: last year's same weeks x this year's trend; RECENT: recent rate")
    last_year_units: int | None = Field(description="SEASONAL: sold in the same weeks last year")


class InventoryResponse(BaseModel):
    as_of: date
    items: list[InventoryItemOut]

    @classmethod
    def from_rows(cls, rows: list[InventoryOverviewRow], as_of: date) -> "InventoryResponse":
        return cls(
            as_of=as_of,
            items=[
                InventoryItemOut(
                    sku=r.sku, name=r.name, category=r.category, on_hand=r.on_hand,
                    max_stock=r.max_stock, on_order=r.on_order,
                    avg_daily_sales=round(r.avg_daily_sales, 2),
                    days_of_cover=_cover(r.days_of_cover),
                    forecast_units=r.forecast.demand_units,
                    forecast_method=r.forecast.method,
                    last_year_units=r.forecast.last_year_horizon_units,
                )
                for r in rows
            ],
        )


# --- GET /inventory/{sku}/sales -----------------------------------------------------------


class SalesDayOut(BaseModel):
    date: date
    quantity: int


class SalesHistoryResponse(BaseModel):
    sku: str
    name: str
    days: int
    start_date: date = Field(description="First day of the window (inclusive)")
    end_date: date = Field(description="Last day of the window (inclusive); today is excluded")
    total_units: int
    daily: list[SalesDayOut] = Field(description="Only days with sales, oldest first")

    @classmethod
    def from_history(cls, h: SalesHistory) -> "SalesHistoryResponse":
        return cls(
            sku=h.sku, name=h.name, days=h.days, start_date=h.start_date, end_date=h.end_date,
            total_units=h.total_units,
            daily=[SalesDayOut(date=d.sold_on, quantity=d.quantity) for d in h.daily],
        )


# --- POST /restock-recommendations --------------------------------------------------------


class RestockRecommendationRequest(BaseModel):
    # Unknown fields are rejected, so a client cannot smuggle in e.g. a price or a total.
    model_config = ConfigDict(extra="forbid")

    budget_cents: int | None = Field(
        default=None,
        strict=True,  # 400 only: rejects "400", 400.5 and true instead of silently converting
        ge=0,
        le=MAX_BUDGET_CENTS,
        description="Budget for this run in cents. Omit to use the merchant's default budget.",
        examples=[40000],
    )


class SupplierRef(BaseModel):
    id: int
    name: str


class EvidenceOut(BaseModel):
    """The numbers behind a line's quantity, straight from the engine's forecast."""

    on_hand: int
    incoming: int
    avg_daily_sales: float
    days_of_cover: float | None
    demand_units: int
    shortfall_units: int


class ProposedLineOut(BaseModel):
    sku: str
    quantity: int
    pack_size: int
    unit_price_cents: int
    line_total_cents: int
    reason: str
    evidence: EvidenceOut

    @classmethod
    def from_line(cls, line: RecommendedLine) -> "ProposedLineOut":
        q, f = line.quote, line.forecast
        return cls(
            sku=q.sku, quantity=q.quantity, pack_size=q.pack_size,
            unit_price_cents=q.unit_price_cents, line_total_cents=q.line_total_cents,
            reason=line.reason,
            evidence=EvidenceOut(
                on_hand=f.on_hand, incoming=f.incoming,
                avg_daily_sales=round(f.avg_daily_sales, 2),
                days_of_cover=_cover(f.days_of_cover),
                demand_units=f.demand_units, shortfall_units=f.shortfall_units,
            ),
        )


class ProposedOrderOut(BaseModel):
    """One proposed purchase order. Not saved anywhere: it has no id and no status."""

    supplier: SupplierRef
    subtotal_cents: int
    min_order_cents: int
    currency: Literal["USD"] = "USD"
    lines: list[ProposedLineOut]

    @classmethod
    def from_order(cls, o: SupplierOrder) -> "ProposedOrderOut":
        return cls(
            supplier=SupplierRef(id=o.supplier_id, name=o.supplier_name),
            subtotal_cents=o.subtotal_cents,
            min_order_cents=o.min_order_cents,
            lines=[ProposedLineOut.from_line(line) for line in o.lines],
        )


class UnfulfilledOut(BaseModel):
    sku: str
    shortfall_units: int
    code: UnfulfilledCode
    message: str


class RejectedOptionOut(BaseModel):
    sku: str
    supplier_id: int | None
    code: RejectionCode
    message: str


class RestockRecommendationResponse(BaseModel):
    as_of: date
    status: RestockStatus
    budget_cents: int
    total_cents: int
    remaining_budget_cents: int
    currency: Literal["USD"] = "USD"
    proposed_orders: list[ProposedOrderOut]
    unfulfilled: list[UnfulfilledOut]
    rejected_options: list[RejectedOptionOut]
    assumptions: list[str]

    @classmethod
    def from_result(
        cls, rec: RestockRecommendation, as_of: date
    ) -> "RestockRecommendationResponse":
        return cls(
            as_of=as_of,
            status=rec.status,
            budget_cents=rec.budget_cents,
            total_cents=rec.total_cents,
            remaining_budget_cents=rec.remaining_budget_cents,
            proposed_orders=[ProposedOrderOut.from_order(o) for o in rec.orders],
            unfulfilled=[
                UnfulfilledOut(
                    sku=u.sku, shortfall_units=u.shortfall_units, code=u.code, message=u.message
                )
                for u in rec.unfulfilled
            ],
            rejected_options=[
                RejectedOptionOut(
                    sku=r.sku, supplier_id=r.supplier_id, code=r.code, message=r.message
                )
                for r in rec.rejected_options
            ],
            assumptions=list(rec.assumptions),
        )


# --- /purchase-orders ---------------------------------------------------------------------

MAX_LINE_QUANTITY = 1_000_000  # sanity ceiling against typos; the real limit is room under max_stock
MAX_LINES_PER_ORDER = 100


class LineIn(BaseModel):
    """One requested line: a SKU and a quantity. There is deliberately no price field."""

    model_config = ConfigDict(extra="forbid")

    sku: str = Field(min_length=1, max_length=32)
    quantity: int = Field(strict=True, le=MAX_LINE_QUANTITY)
    reason: str | None = Field(default=None, max_length=500)


class EditPurchaseOrderRequest(BaseModel):
    """Replace a draft's lines. Prices and totals are always read from the database."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(
        strict=True, ge=1, description="The `version` of the draft the merchant was looking at"
    )
    supplier_id: int | None = Field(
        default=None, strict=True, ge=1, description="Omit to keep the current supplier"
    )
    lines: list[LineIn] = Field(max_length=MAX_LINES_PER_ORDER, description="The complete new list")


class ApprovePurchaseOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved_total_cents: int = Field(
        strict=True, ge=0, le=MAX_BUDGET_CENTS,
        description="The total the merchant saw. Must equal the server's total or approval is refused.",
    )
    expected_version: int = Field(
        strict=True, ge=1, description="The `version` of the draft the merchant reviewed"
    )


class CancelPurchaseOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


class PurchaseOrderLineOut(BaseModel):
    sku: str
    name: str
    quantity: int
    pack_size: int
    unit_price_cents: int
    line_total_cents: int
    reason: str | None


class PurchaseOrderEventOut(BaseModel):
    id: int
    event: str
    detail: dict
    at: datetime


class PaymentOut(BaseModel):
    """One PayPal payment attempt. Only fields we chose to keep: no payer name, email or address."""

    id: int
    paypal_order_id: str
    status: str = Field(description="CREATED | PENDING | CAPTURED | FAILED")
    amount_cents: int
    currency: Literal["USD"] = "USD"
    paypal_capture_id: str | None
    verified_at: datetime | None = Field(
        description="Set only when the payment was verified against PayPal's own records"
    )
    error: str | None = Field(description="Why a payment failed, or what needs human review")
    created_at: datetime

    @classmethod
    def from_payment(cls, p: "Payment") -> "PaymentOut":
        return cls(
            id=p.id, paypal_order_id=p.paypal_order_id, status=p.status,
            amount_cents=p.amount_cents, paypal_capture_id=p.paypal_capture_id,
            verified_at=p.verified_at, error=p.error, created_at=p.created_at,
        )


class POPromotionEligibleOut(BaseModel):
    sku: str
    name: str
    variant: str
    size_ml: int | None


class POPromotionOut(BaseModel):
    """The promotion an order claims. The rebate is paid back by the supplier later: the
    order (and PayPal) is charged `subtotal_cents`; `expected_net_cents` is for display.
    `meets_terms` is advisory: a merchant may still edit/approve an order that no longer
    qualifies; then `rebate_cents` is 0."""

    id: int
    title: str
    rebate_cents: int = Field(description="Earned rebate for the current lines; 0 if terms not met")
    rebate_per_bundle_cents: int
    expected_net_cents: int
    meets_terms: bool
    terms_message: str
    # Enough for the draft editor to re-check the claim as the merchant edits, without a
    # round trip for every keystroke.
    required_cases: int
    units_per_case: int
    min_distinct_variants: int
    max_bundles: int | None
    eligible: list[POPromotionEligibleOut]


class PurchaseOrderOut(BaseModel):
    id: int
    status: POStatus
    version: int
    supplier: SupplierRef
    currency: Literal["USD"] = "USD"
    subtotal_cents: int
    budget_cents: int | None
    run_id: int | None
    rationale: str | None
    promotion: POPromotionOut | None
    created_at: datetime
    approved_total_cents: int | None
    approved_at: datetime | None
    approved_snapshot_sha256: str | None = Field(
        description="SHA-256 of the frozen record of the approved order; null until approved"
    )
    lines: list[PurchaseOrderLineOut]
    payments: list[PaymentOut]
    events: list[PurchaseOrderEventOut]

    @classmethod
    def from_po(cls, po: "PurchaseOrder") -> "PurchaseOrderOut":
        """Reshape a saved PO. Every number is copied from the stored row."""
        return cls(
            id=po.id, status=po.status, version=po.version,
            supplier=SupplierRef(id=po.supplier_id, name=po.supplier.name),
            subtotal_cents=po.subtotal_cents, budget_cents=po.budget_cents, run_id=po.run_id,
            rationale=po.rationale, promotion=_po_promotion(po), created_at=po.created_at,
            approved_total_cents=po.approved_total_cents, approved_at=po.approved_at,
            approved_snapshot_sha256=po.approved_snapshot_sha256,
            lines=[
                PurchaseOrderLineOut(
                    sku=line.product.sku, name=line.product.name, quantity=line.quantity,
                    pack_size=line.pack_size, unit_price_cents=line.unit_price_cents,
                    line_total_cents=line.line_total_cents, reason=line.reason,
                )
                for line in po.lines
            ],
            payments=[PaymentOut.from_payment(p) for p in po.payments],
            events=[
                PurchaseOrderEventOut(
                    id=e.id, event=e.event, detail=json.loads(e.detail_json), at=e.at
                )
                for e in po.events
            ],
        )


class PurchaseOrderSummaryOut(BaseModel):
    """One row of the order history. Fetch GET /purchase-orders/{id} for lines and events."""

    id: int
    status: POStatus
    supplier: SupplierRef
    currency: Literal["USD"] = "USD"
    subtotal_cents: int
    line_count: int
    run_id: int | None
    created_at: datetime
    approved_at: datetime | None
    latest_payment: PaymentOut | None = Field(description="The most recent payment attempt, if any")

    @classmethod
    def from_po(cls, po: "PurchaseOrder") -> "PurchaseOrderSummaryOut":
        latest = po.payments[-1] if po.payments else None
        return cls(
            id=po.id, status=po.status,
            supplier=SupplierRef(id=po.supplier_id, name=po.supplier.name),
            subtotal_cents=po.subtotal_cents, line_count=len(po.lines), run_id=po.run_id,
            created_at=po.created_at, approved_at=po.approved_at,
            latest_payment=PaymentOut.from_payment(latest) if latest else None,
        )


class PurchaseOrderListResponse(BaseModel):
    items: list[PurchaseOrderSummaryOut] = Field(description="Newest first")


# --- /purchase-orders/{id}/paypal ---------------------------------------------------------


class CreatePayPalOrderResponse(BaseModel):
    """What the browser needs to open PayPal. The amount is shown for display; it was derived
    from the approved order on the server and is not something the client can change."""

    paypal_order_id: str
    amount_cents: int
    currency: Literal["USD"] = "USD"
    payment_status: str
    approve_url: str | None = Field(
        description="PayPal's approval link, for testing without the JS SDK"
    )
    resumed: bool = Field(description="True if an order that was already open is being returned")


class CapturePayPalOrderRequest(BaseModel):
    """Only the PayPal order id. Extra fields (such as an amount) are rejected, not ignored."""

    model_config = ConfigDict(extra="forbid")

    paypal_order_id: str = Field(pattern=r"^[A-Z0-9]{1,36}$")


class CapturePayPalOrderResponse(BaseModel):
    outcome: Literal["PAID", "PENDING"]
    po_status: POStatus
    payment: PaymentOut


# --- /restock-runs ------------------------------------------------------------------------


class CreateRestockRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_CHARS)
    ] = Field(description="What the merchant asked for, in their own words",
              examples=["Restock snacks and drinks"])
    budget_cents: int | None = Field(
        default=None, strict=True, ge=0, le=MAX_BUDGET_CENTS,
        description="Spending cap for this run in cents (authoritative; never read from "
                    "`message`). Omit to use the merchant's default budget.",
        examples=[40000],
    )


class RunEvidenceOut(BaseModel):
    """Today's numbers for the line's product, from the database, not from the AI's text."""

    on_hand: int
    avg_daily_sales: float
    days_of_cover: float | None


class RunLineOut(BaseModel):
    sku: str
    name: str
    quantity: int
    pack_size: int
    unit_price_cents: int
    line_total_cents: int
    reason: str | None = Field(description="The AI's short explanation for this line")
    evidence: RunEvidenceOut


class RunPurchaseOrderOut(BaseModel):
    id: int
    supplier: SupplierRef
    status: POStatus = Field(description="Current status (a DRAFT may since have been approved)")
    version: int
    subtotal_cents: int
    currency: Literal["USD"] = "USD"
    rationale: str | None
    promotion: POPromotionOut | None
    lines: list[RunLineOut]


class RunErrorOut(BaseModel):
    code: str
    message: str


class AgentStepOut(BaseModel):
    seq: int
    kind: Literal["assistant", "tool_call", "tool_result"]
    name: str | None
    payload: dict


class ProductStockOut(BaseModel):
    """Stock numbers from the database (the lookup tool's results), not from the AI's prose."""

    sku: str
    name: str
    size_ml: int | None
    on_hand: int
    on_order: int
    avg_daily_sales: float
    days_of_cover: float | None

    @classmethod
    def from_match(cls, p: dict) -> "ProductStockOut":
        return cls(
            sku=p["sku"], name=p["name"], size_ml=p.get("size_ml"), on_hand=p["on_hand"],
            on_order=p["on_order"], avg_daily_sales=p["avg_daily_sales"],
            days_of_cover=p.get("days_of_cover"),
        )


class RestockRunOut(BaseModel):
    id: int
    status: Literal["running", "completed", "failed"]
    message: str
    budget_cents: int
    currency: Literal["USD"] = "USD"
    model: str
    created_at: datetime
    finished_at: datetime | None
    as_of: date = Field(description="The date the evidence numbers were computed for")
    summary: str | None = Field(description="The AI's explanation, or ours if it could not finish")
    error: RunErrorOut | None = Field(description="Why the run failed or stopped early")
    warnings: list[str]
    step_count: int
    purchase_orders: list[RunPurchaseOrderOut]
    products: list[ProductStockOut] = Field(
        description="Products the assistant looked up (stock as the lookup tool returned it)"
    )

    @classmethod
    def _fields(cls, view: RunView) -> dict:
        run = view.run
        error = (
            RunErrorOut(code=run.error_code, message=run.error_message or "")
            if run.error_code else None
        )
        # A completed run with an error code stopped early (a step limit); its drafts are valid.
        warnings = [run.error_message] if run.status == "completed" and run.error_message else []
        return dict(
            id=run.id, status=run.status, message=run.message, budget_cents=run.budget_cents,
            model=run.model, created_at=run.created_at, finished_at=run.finished_at,
            as_of=view.as_of, summary=run.summary, error=error, warnings=warnings,
            step_count=len(run.steps),
            products=[ProductStockOut.from_match(p) for p in view.products],
            purchase_orders=[
                RunPurchaseOrderOut(
                    id=po.id, supplier=SupplierRef(id=po.supplier_id, name=po.supplier.name),
                    status=po.status, version=po.version, subtotal_cents=po.subtotal_cents,
                    rationale=po.rationale, promotion=_po_promotion(po),
                    lines=[
                        RunLineOut(
                            sku=line.product.sku, name=line.product.name, quantity=line.quantity,
                            pack_size=line.pack_size, unit_price_cents=line.unit_price_cents,
                            line_total_cents=line.line_total_cents, reason=line.reason,
                            evidence=_run_evidence(view.evidence[line.product.sku]),
                        )
                        for line in po.lines
                    ],
                )
                for po in view.purchase_orders
            ],
        )

    @classmethod
    def from_view(cls, view: RunView) -> "RestockRunOut":
        return cls(**cls._fields(view))


class RestockRunDetailOut(RestockRunOut):
    steps: list[AgentStepOut] = Field(description="The full tool trace, in order")

    @classmethod
    def from_view(cls, view: RunView) -> "RestockRunDetailOut":
        steps = [
            AgentStepOut(seq=s.seq, kind=s.kind, name=s.name, payload=json.loads(s.payload_json))
            for s in view.run.steps
        ]
        return cls(**cls._fields(view), steps=steps)


def _run_evidence(row: InventoryOverviewRow) -> RunEvidenceOut:
    return RunEvidenceOut(
        on_hand=row.on_hand, avg_daily_sales=round(row.avg_daily_sales, 2),
        days_of_cover=_cover(row.days_of_cover),
    )


def _po_promotion(po: "PurchaseOrder") -> "POPromotionOut | None":
    p = po.promotion
    if p is None:
        return None
    claim = promotion_claim_for_po(po)
    return POPromotionOut(
        id=p.id, title=p.title, rebate_cents=claim.rebate_cents,
        rebate_per_bundle_cents=p.rebate_cents,
        expected_net_cents=net_after_rebate(po.subtotal_cents, claim.rebate_cents),
        meets_terms=claim.meets_terms, terms_message=claim.message,
        required_cases=p.required_cases, units_per_case=p.units_per_case,
        min_distinct_variants=p.min_distinct_variants, max_bundles=p.max_bundles,
        eligible=[
            POPromotionEligibleOut(sku=pp.product.sku, name=pp.product.name,
                                   variant=variant_of(pp.product), size_ml=pp.product.size_ml)
            for pp in p.products
        ],
    )


# --- /suppliers ---------------------------------------------------------------------------


class SupplierOut(BaseModel):
    id: int
    name: str
    min_order_cents: int


class SupplierListResponse(BaseModel):
    items: list[SupplierOut]


class CatalogOfferOut(BaseModel):
    sku: str
    name: str
    brand: str | None
    variant: str | None
    size_ml: int | None
    unit_price_cents: int
    pack_size: int


class SupplierCatalogResponse(BaseModel):
    supplier: SupplierRef
    items: list[CatalogOfferOut]

    @classmethod
    def from_offers(
        cls, supplier: SupplierRef, offers: list[CatalogOffer]
    ) -> "SupplierCatalogResponse":
        return cls(supplier=supplier, items=[
            CatalogOfferOut(
                sku=o.sku, name=o.name, brand=o.brand, variant=o.variant, size_ml=o.size_ml,
                unit_price_cents=o.unit_price_cents, pack_size=o.pack_size,
            )
            for o in offers
        ])


# --- /promotions --------------------------------------------------------------------------


class ParsePromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1,
                               max_length=MAX_PROMOTION_TEXT_CHARS)
    ] = Field(description="The promotion as the supplier wrote it",
              examples=["Harbor: rebate of $500 on 10 cases of Svedka, mix and match any 3 "
                        "flavors in 750ml and 1.5L"])


class PromotionDraftOut(BaseModel):
    """The AI's reading of the text, resolved against the catalog. Not saved: the merchant
    checks it and then POSTs /promotions. Nulls are things the text did not say."""

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
    sizes_ml: list[int]
    eligible_skus: list[str]
    valid_from: date | None
    valid_until: date | None
    ambiguities: list[str] = Field(description="What the AI says the merchant should confirm")
    warnings: list[str] = Field(description="What the system could not match or had to assume")
    source_text: str

    @classmethod
    def from_draft(cls, d: PromotionDraft) -> "PromotionDraftOut":
        return cls(
            title=d.title, kind=d.kind, supplier_id=d.supplier_id,
            supplier_name_as_written=d.supplier_name_as_written, brand=d.brand,
            required_cases=d.required_cases, units_per_case=d.units_per_case,
            rebate_cents=d.rebate_cents, min_distinct_variants=d.min_distinct_variants,
            max_bundles=d.max_bundles, sizes_ml=list(d.sizes_ml), eligible_skus=list(d.eligible_skus),
            valid_from=d.valid_from, valid_until=d.valid_until,
            ambiguities=list(d.ambiguities), warnings=list(d.warnings), source_text=d.source_text,
        )


class CreatePromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supplier_id: int = Field(strict=True, ge=1)
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    required_cases: int = Field(strict=True, ge=1, le=1000)
    units_per_case: int = Field(strict=True, ge=1, le=100)
    rebate_cents: int = Field(strict=True, ge=1, le=MAX_BUDGET_CENTS)
    min_distinct_variants: int = Field(strict=True, ge=1, le=50)
    eligible_skus: list[Annotated[str, StringConstraints(min_length=1, max_length=40)]] = Field(
        min_length=1, max_length=100
    )
    valid_from: date | None = None
    valid_until: date | None = None
    source_text: str | None = Field(default=None, max_length=MAX_PROMOTION_TEXT_CHARS)
    max_bundles: int | None = Field(default=None, strict=True, ge=1, le=100,
                                    description="Most rebates one order can earn; null: no limit")
    buy_through: date | None = Field(
        default=None, description="Stock up for demand until this date (e.g. through the holidays)")


class PromotionProductOut(BaseModel):
    sku: str
    name: str
    variant: str
    size_ml: int | None


class PromotionOut(BaseModel):
    id: int
    supplier: SupplierRef
    title: str
    kind: str
    status: str
    required_cases: int
    units_per_case: int
    rebate_cents: int = Field(description="Per bundle")
    min_distinct_variants: int
    max_bundles: int | None
    valid_from: date | None
    valid_until: date | None
    buy_through: date | None
    source_text: str | None
    created_at: datetime
    unavailable_reason: str | None = Field(description="Why it cannot be used today, if so")
    products: list[PromotionProductOut]

    @classmethod
    def from_promotion(cls, p: "Promotion", as_of: date) -> "PromotionOut":
        return cls(
            id=p.id, supplier=SupplierRef(id=p.supplier_id, name=p.supplier.name),
            title=p.title, kind=p.kind, status=p.status, required_cases=p.required_cases,
            units_per_case=p.units_per_case, rebate_cents=p.rebate_cents,
            min_distinct_variants=p.min_distinct_variants, max_bundles=p.max_bundles,
            valid_from=p.valid_from, valid_until=p.valid_until, buy_through=p.buy_through,
            source_text=p.source_text, created_at=p.created_at,
            unavailable_reason=unavailable_reason(p, as_of),
            products=[
                PromotionProductOut(sku=pp.product.sku, name=pp.product.name,
                                    variant=variant_of(pp.product), size_ml=pp.product.size_ml)
                for pp in p.products
            ],
        )


class PromotionListResponse(BaseModel):
    items: list[PromotionOut]


class AllocationRowOut(BaseModel):
    sku: str
    name: str
    variant: str
    size_ml: int | None
    cases: int
    units: int
    unit_price_cents: int
    line_total_cents: int
    avg_daily_sales: float = Field(description="Recent rate")
    forecast_units: int = Field(description="Expected sales until the stock-up date")
    forecast_method: Literal["SEASONAL", "RECENT"]
    last_year_units: int | None = Field(description="SEASONAL: sold in the same weeks last year")
    trend: float | None = Field(description="SEASONAL: this year's sales / last year's")
    shortfall_units: int = Field(description="Units needed until the stock-up date, before this order")
    excess_units: int = Field(description="Units of this order beyond that need")
    over_max_stock_units: int = Field(description="How far stock goes over max stock after the order")
    days_of_cover_before: float | None = Field(description="At the expected rate")
    days_of_cover_after: float | None


class PromotionEvaluationOut(BaseModel):
    """The system's split of the promotion's cases. Every number comes from the engine."""

    promotion: PromotionOut
    usable: bool
    status: Literal["OK", "INFEASIBLE"]
    rows: list[AllocationRowOut]
    currency: Literal["USD"] = "USD"
    list_total_cents: int = Field(description="What the order costs now (and what PayPal charges)")
    bundles: int = Field(description="Rebates the recommended order earns")
    rebate_per_bundle_cents: int
    rebate_cents: int = Field(description="bundles x rebate_per_bundle_cents")
    net_cents: int = Field(description="List total minus the rebate the supplier pays back later")
    total_units: int
    distinct_variants: int
    forecast_units: int = Field(description="Expected sales of the eligible products until the stock-up date")
    need_units: int
    excess_units: int
    horizon_days: int
    stock_up_through: date
    seasonal: bool = Field(description="True when last year's sales shaped the forecast")
    verdict: Literal["ALL_NEEDED", "EXTRA_STOCK"] | None
    message: str

    @classmethod
    def from_evaluation(cls, e: PromotionEvaluation, as_of: date) -> "PromotionEvaluationOut":
        a = e.allocation
        return cls(
            promotion=PromotionOut.from_promotion(e.promotion, as_of),
            usable=e.unavailable_reason is None and a.status == AllocationStatus.OK,
            status=a.status,
            rows=[
                AllocationRowOut(
                    sku=r.sku, name=r.name, variant=r.variant, size_ml=r.size_ml,
                    cases=r.cases, units=r.units, unit_price_cents=r.unit_price_cents,
                    line_total_cents=r.line_total_cents,
                    avg_daily_sales=round(r.avg_daily_sales, 2),
                    forecast_units=r.demand_units, forecast_method=r.forecast_method,
                    last_year_units=r.last_year_units,
                    trend=None if r.trend is None else round(float(r.trend), 2),
                    shortfall_units=r.shortfall_units, excess_units=r.excess_units,
                    over_max_stock_units=r.over_max_stock_units,
                    days_of_cover_before=_cover(r.cover_before),
                    days_of_cover_after=_cover(r.cover_after),
                )
                for r in a.rows
            ],
            list_total_cents=a.list_total_cents, bundles=a.bundles,
            rebate_per_bundle_cents=a.rebate_per_bundle_cents, rebate_cents=a.rebate_cents,
            net_cents=a.net_cents, total_units=a.total_units,
            distinct_variants=a.distinct_variants, forecast_units=a.demand_units,
            need_units=a.need_units, excess_units=a.excess_units, horizon_days=a.horizon_days,
            stock_up_through=e.stock_up_through, seasonal=a.seasonal, verdict=a.verdict,
            message=a.message,
        )


# --- /assistant ---------------------------------------------------------------------------


class AskQuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_CHARS)
    ] = Field(examples=["How much Grey Goose 750ml do we have?"])


class AskQuestionResponse(BaseModel):
    question: str
    answered: bool
    answer: str
    error_code: str | None
    products: list[ProductStockOut]
    steps: list[AgentStepOut]

    @classmethod
    def from_answer(cls, a: AssistantAnswer) -> "AskQuestionResponse":
        return cls(
            question=a.question, answered=a.answered, answer=a.answer, error_code=a.error_code,
            products=[ProductStockOut.from_match(p) for p in a.products],
            steps=[
                AgentStepOut(seq=s.seq, kind=s.kind, name=s.name, payload=s.payload)  # type: ignore[arg-type]
                for s in a.steps
            ],
        )
