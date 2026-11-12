"""The tools the LLM may call. This file is the agent's entire power.

Each tool has three parts:
  1. a Pydantic *argument model*: the typed, validated shape of what the model may send. It is
     also where the JSON Schema shown to the model comes from, so the schema and the checks
     can never drift apart;
  2. a *handler*: ordinary Python that calls the existing services and returns plain data;
  3. a one-line registry entry (TOOLS below).

Rules this module enforces (docs/MVP_PLAN.md section 6):
  * The model sends only identifiers and quantities (`supplier_id`, `sku`, `quantity`). It never
    supplies a price, a total, or a budget; those come from the database and the validator.
  * Every failure the MODEL can cause (unknown tool, bad JSON, wrong types, unknown SKU, a
    purchase order that breaks a rule) comes back as a structured `{"error": ...}` result, so it
    can read it and try again. Failures that are OUR bug (database errors, ...) are not
    caught here; they propagate and fail the run loudly.
  * There is no tool to approve, cancel, edit, pay, or change settings. A test enforces this.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError
from sqlalchemy.orm import Session

from app.agent.llm_client import ToolSchema
from app.services.catalog import search_products
from app.services.inventory import UnknownSkuError, get_sales_history, get_settings_row
from app.services.po_lifecycle import OrderRejectedError, create_validated_draft
from app.services.procurement import (
    build_restock_recommendation,
    compare_supplier_offers,
    get_inventory_overview,
)
from app.services.promotions import (
    PromotionNotFoundError,
    active_promotions,
    evaluate_promotion,
    expected_rebate_cents,
    variant_of,
)
from app.services.purchase_orders import InvalidOrderError, LineRequest

# ---------------------------------------------------------------------------------------------
# Per-run state
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DraftRecord:
    """A draft PO this run has created (what the tools need to remember between calls)."""

    po_id: int
    supplier_id: int
    total_cents: int
    skus: tuple[str, ...]


@dataclass
class ToolContext:
    """Everything a handler may use. Built by the loop, never by the model.

    `budget_cents` is the merchant's structured budget field (authoritative); the model only
    ever sees it, it cannot change it. `run_id` ties all drafts of a run to one shared budget
    in the validator, so two drafts to two suppliers cannot each spend the whole budget.
    """

    session: Session
    as_of: date
    budget_cents: int
    run_id: int
    drafts: list[DraftRecord] = field(default_factory=list)

    @property
    def spent_cents(self) -> int:
        return sum(d.total_cents for d in self.drafts)

    @property
    def ordered_skus(self) -> set[str]:
        return {sku for d in self.drafts for sku in d.skus}


# ---------------------------------------------------------------------------------------------
# Argument models. `extra="forbid"`: an argument we do not know about is an error, not ignored.
# ---------------------------------------------------------------------------------------------


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(_Args):
    pass


class ReorderCandidatesArgs(_Args):
    category: str | None = Field(
        default=None, max_length=40,
        description="Only products in this category (case-insensitive). Omit for all categories.",
    )
    cover_days: StrictInt | None = Field(
        default=None, ge=1, le=364,
        description="Days of future sales to cover. Pass 7 for 'next week', 21 for 'next 3 "
                    "weeks', 30 for 'next month'. Omit to use the store's target_cover_days.",
    )


class SalesHistoryArgs(_Args):
    sku: str = Field(min_length=1, max_length=32, description="Exact SKU, e.g. 'DRK-COLA'.")
    days: StrictInt | None = Field(
        default=None, ge=1, le=400,
        description="Window length in days ending yesterday. Omit to use the merchant's lookback.",
    )


class SupplierQuotesArgs(_Args):
    skus: list[str] = Field(
        min_length=1, max_length=20, description="Exact SKUs to compare suppliers for."
    )
    cover_days: StrictInt | None = Field(
        default=None, ge=1, le=364,
        description="Must match the cover_days used in get_reorder_candidates for this request.",
    )


class DraftLine(_Args):
    sku: str = Field(min_length=1, max_length=32)
    # StrictInt: the number 12 is accepted, the text "12" and 12.5 are not. A purchase quantity
    # is not something to guess at.
    quantity: StrictInt = Field(gt=0, le=100_000, description="Units; a multiple of the pack size.")
    reason: str | None = Field(
        default=None, max_length=300, description="One short sentence for the merchant."
    )


class CreateDraftPOArgs(_Args):
    supplier_id: StrictInt = Field(ge=1, description="A supplier_id returned by get_supplier_quotes.")
    lines: list[DraftLine] = Field(min_length=1, max_length=30)
    rationale: str | None = Field(
        default=None, max_length=500, description="Why this supplier, for the merchant."
    )
    promotion_id: StrictInt | None = Field(
        default=None, ge=1,
        description="Only when this order claims a promotion from get_active_promotions. The "
                    "system checks the order against the promotion's terms.",
    )


class LookupProductsArgs(_Args):
    query: str = Field(
        min_length=1, max_length=120,
        description="Words naming the product, with a size if given, e.g. 'grey goose 750ml' "
                    "or 'svedka vanilla'.",
    )


class PromotionIdArgs(_Args):
    promotion_id: StrictInt = Field(ge=1, description="An id from get_active_promotions.")


# ---------------------------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------------------------


def error_result(code: str, message: str, **details: Any) -> dict[str, Any]:
    """The one shape every failure takes, so the model (and the trace UI) can rely on it."""
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {"error": error}


@dataclass(frozen=True)
class ToolResult:
    payload: dict[str, Any]

    @property
    def is_error(self) -> bool:
        return "error" in self.payload


# ---------------------------------------------------------------------------------------------
# Handlers. Each takes the context and VALIDATED arguments and returns JSON-friendly data.
# They format numbers that deterministic code produced; they compute nothing of their own
# except display rounding.
# ---------------------------------------------------------------------------------------------

_RULES = [
    "Each purchase order is for exactly one supplier.",
    "Quantities must be a positive whole number of cases (a multiple of the pack size).",
    "A quantity may not exceed the room left under max_stock (max_stock - on_hand - on_order), "
    "except for the eligible products on an order that claims a promotion (stocking up on a "
    "deal is allowed).",
    "Each purchase order must reach its supplier's min_order_cents.",
    "All draft orders of this run together may not exceed budget_cents.",
    "A SKU may appear on only one draft order per run.",
    "Prices and totals are set by the system from supplier data; you cannot set them.",
    "Orders created here are DRAFTS. Only the merchant can approve and pay for them.",
]


def _get_constraints(ctx: ToolContext, _: NoArgs) -> dict[str, Any]:
    settings = get_settings_row(ctx.session)
    return {
        "currency": "USD",
        "budget_cents": ctx.budget_cents,
        "already_drafted_cents": ctx.spent_cents,
        "remaining_budget_cents": ctx.budget_cents - ctx.spent_cents,
        "target_cover_days": settings.target_cover_days,
        "lookback_days": settings.lookback_days,
        "as_of": ctx.as_of.isoformat(),
        "forecast": "Products with a year of history are forecast from last year's sales for "
                    "the same coming weeks, scaled by this year's trend (forecast_basis "
                    "SEASONAL); others from the recent daily rate (RECENT).",
        "rules": _RULES,
    }


def _forecast_evidence(forecast: Any) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "forecast_units": forecast.demand_units,
        "forecast_days": forecast.horizon_days,
        "forecast_basis": str(forecast.method),
    }
    if forecast.trend is not None:  # SEASONAL
        evidence["last_year_same_weeks_units"] = forecast.last_year_horizon_units
        evidence["trend_vs_last_year"] = round(float(forecast.trend), 2)
    return evidence


def _get_reorder_candidates(ctx: ToolContext, args: ReorderCandidatesArgs) -> dict[str, Any]:
    overview = get_inventory_overview(ctx.session, ctx.as_of)
    categories = sorted({row.category for row in overview})
    wanted = None
    if args.category is not None:
        wanted = next((c for c in categories if c.lower() == args.category.strip().lower()), None)
        if wanted is None:
            return error_result(
                "UNKNOWN_CATEGORY", f"No category named {args.category!r}.", valid_categories=categories
            )

    recommendation = build_restock_recommendation(
        ctx.session, ctx.as_of, ctx.budget_cents, horizon_days=args.cover_days
    )
    by_sku = {row.sku: row for row in overview}
    cover_days = args.cover_days or get_settings_row(ctx.session).target_cover_days

    # Most urgent first (fewest days of cover, sorted on the exact fraction, ties by SKU): the
    # same order the engine uses. A product with a shortfall always has sales, so cover is set.
    short = sorted(
        (f for f in recommendation.forecasts if f.needs_restock),
        key=lambda f: (f.days_of_cover, f.sku),
    )
    candidates = []
    for forecast in short:
        row = by_sku[forecast.sku]
        if wanted is None or row.category == wanted:
            candidates.append({
                "sku": row.sku, "name": row.name, "category": row.category,
                "on_hand": row.on_hand, "on_order": row.on_order, "max_stock": row.max_stock,
                "avg_daily_sales": round(row.avg_daily_sales, 2),
                "days_of_cover": round(float(forecast.days_of_cover), 1),
                **_forecast_evidence(forecast),
                "shortfall_units": forecast.shortfall_units,
            })

    return {
        "cover_days": cover_days,
        "categories": categories,
        "candidates": candidates,
        # The engine's own budget-fitting plan: a good starting point, not an instruction.
        # It is computed over ALL categories; use only the lines that match the request.
        "engine_suggestion": {
            "status": str(recommendation.status),
            "total_cents": recommendation.total_cents,
            "orders": [
                {
                    "supplier_id": order.supplier_id,
                    "supplier_name": order.supplier_name,
                    "min_order_cents": order.min_order_cents,
                    "subtotal_cents": order.subtotal_cents,
                    "lines": [
                        {
                            "sku": line.quote.sku,
                            "category": by_sku[line.quote.sku].category,
                            "quantity": line.quote.quantity,
                            "pack_size": line.quote.pack_size,
                            "unit_price_cents": line.quote.unit_price_cents,
                            "line_total_cents": line.quote.line_total_cents,
                            "capped_by_max_stock": line.quote.capped_by_max_stock,
                            "engine_reason": line.reason,
                        }
                        for line in order.lines
                    ],
                }
                for order in recommendation.orders
            ],
            "not_ordered": [
                {"sku": u.sku, "shortfall_units": u.shortfall_units,
                 "code": str(u.code), "message": u.message}
                for u in recommendation.unfulfilled
            ],
        },
    }


def _get_sales_history(ctx: ToolContext, args: SalesHistoryArgs) -> dict[str, Any]:
    history = get_sales_history(ctx.session, args.sku, ctx.as_of, args.days)
    return {
        "sku": history.sku, "name": history.name,
        "start_date": history.start_date.isoformat(), "end_date": history.end_date.isoformat(),
        "days": history.days, "total_units": history.total_units,
        "avg_daily_units": round(history.total_units / history.days, 2),
        "daily": [{"date": d.sold_on.isoformat(), "quantity": d.quantity} for d in history.daily],
        "note": "Days without sales are omitted.",
    }


def _get_supplier_quotes(ctx: ToolContext, args: SupplierQuotesArgs) -> dict[str, Any]:
    skus = list(dict.fromkeys(args.skus))  # drop duplicates, keep order
    comparisons = compare_supplier_offers(
        ctx.session, ctx.as_of, skus, horizon_days=args.cover_days
    )
    unknown = [sku for sku in skus if sku not in {c.sku for c in comparisons}]
    if unknown:
        return error_result(
            "UNKNOWN_SKU", f"These SKUs do not exist: {unknown}. Take SKUs from get_reorder_candidates.",
            unknown_skus=unknown,
        )
    products = []
    for comparison in comparisons:
        offers = []
        for o in comparison.offers:
            offer: dict[str, Any] = {
                "supplier_id": o.supplier_id, "supplier_name": o.supplier_name,
                "unit_price_cents": o.unit_price_cents, "pack_size": o.pack_size,
                "supplier_min_order_cents": o.min_order_cents, "lead_time_days": o.lead_time_days,
            }
            if o.quote is not None:
                offer["quote_for_shortfall"] = {
                    "quantity": o.quote.quantity,
                    "cases": o.quote.quantity // o.pack_size,
                    "line_total_cents": o.quote.line_total_cents,
                    "capped_by_max_stock": o.quote.capped_by_max_stock,
                }
            elif o.rejection is not None:
                offer["unusable_because"] = o.rejection.message
            offers.append(offer)
        products.append({
            "sku": comparison.sku, "name": comparison.name,
            "shortfall_units": comparison.shortfall_units, "offers": offers,
        })
    return {
        "cover_days": args.cover_days or get_settings_row(ctx.session).target_cover_days,
        "products": products,
    }


def _cover(value: Any) -> float | None:
    return None if value is None else round(float(value), 1)


def _lookup_products(ctx: ToolContext, args: LookupProductsArgs) -> dict[str, Any]:
    result = search_products(ctx.session, ctx.as_of, args.query)
    note = None
    if not result.matches:
        note = "No product matched every word. Try fewer or differently spelled words."
    elif result.size_found is False:
        note = (f"No matching product comes in {result.requested_size_ml}ml. The matches below "
                f"are the sizes that exist; say so rather than guessing which one was meant.")
    return {
        "query": result.query,
        "requested_size_ml": result.requested_size_ml,
        "size_found": result.size_found,
        "matches": [
            {
                "sku": m.sku, "name": m.name, "category": m.category, "brand": m.brand,
                "variant": m.variant, "size_ml": m.size_ml, "on_hand": m.on_hand,
                "on_order": m.on_order, "max_stock": m.max_stock,
                "avg_daily_sales": round(m.avg_daily_sales, 2),
                "days_of_cover": _cover(m.days_of_cover),
            }
            for m in result.matches
        ],
        "note": note,
    }


def _get_active_promotions(ctx: ToolContext, _: NoArgs) -> dict[str, Any]:
    return {
        "promotions": [
            {
                "promotion_id": p.id, "title": p.title, "kind": p.kind,
                "supplier_id": p.supplier_id, "supplier_name": p.supplier.name,
                "required_cases_per_bundle": p.required_cases, "units_per_case": p.units_per_case,
                "rebate_cents_per_bundle": p.rebate_cents,
                "max_bundles": p.max_bundles,
                "min_distinct_flavors": p.min_distinct_variants,
                "valid_until": p.valid_until.isoformat() if p.valid_until else None,
                "stock_up_through": p.buy_through.isoformat() if p.buy_through else None,
                "eligible_products": [
                    {"sku": pp.product.sku, "flavor": variant_of(pp.product),
                     "size_ml": pp.product.size_ml}
                    for pp in p.products
                ],
            }
            for p in active_promotions(ctx.session, ctx.as_of)
        ],
        "note": "A REBATE is paid back by the supplier after the purchase; the order itself is "
                "paid at list price and counts against the budget at list price. Rebates "
                "stack: every whole bundle earns one, up to max_bundles (null: no limit).",
    }


def _evaluate_promotion(ctx: ToolContext, args: PromotionIdArgs) -> dict[str, Any]:
    try:
        evaluation = evaluate_promotion(ctx.session, args.promotion_id, ctx.as_of)
    except PromotionNotFoundError:
        return error_result("UNKNOWN_PROMOTION", f"There is no promotion #{args.promotion_id}.")
    a = evaluation.allocation
    return {
        "promotion_id": evaluation.promotion.id,
        "supplier_id": evaluation.promotion.supplier_id,
        "supplier_name": evaluation.promotion.supplier.name,
        "usable_today": evaluation.unavailable_reason is None,
        "unavailable_reason": evaluation.unavailable_reason,
        "status": str(a.status),
        "stock_up_through": evaluation.stock_up_through.isoformat(),
        "horizon_days": a.horizon_days,
        "forecast_basis": "SEASONAL" if a.seasonal else "RECENT",
        "bundles": a.bundles,
        "recommended_lines": [
            {"sku": r.sku, "quantity": r.units, "cases": r.cases} for r in a.rows if r.cases
        ],
        "evidence": [
            {
                "sku": r.sku, "flavor": r.variant, "size_ml": r.size_ml, "cases": r.cases,
                "recent_avg_daily_sales": round(r.avg_daily_sales, 2),
                "forecast_units": r.demand_units, "forecast_basis": str(r.forecast_method),
                "last_year_same_weeks_units": r.last_year_units,
                "trend_vs_last_year": None if r.trend is None else round(float(r.trend), 2),
                "shortfall_units": r.shortfall_units,
                "over_max_stock_units": r.over_max_stock_units,
                "days_of_cover_before": _cover(r.cover_before),
                "days_of_cover_after": _cover(r.cover_after),
            }
            for r in a.rows
        ],
        "forecast_units": a.demand_units,
        "need_units": a.need_units,
        "list_total_cents": a.list_total_cents,
        "rebate_cents": a.rebate_cents,
        "net_after_rebate_cents": a.net_cents,
        "units_beyond_need": a.excess_units,
        "verdict": str(a.verdict) if a.verdict else None,
        "message": a.message,
    }


def _create_draft_po(ctx: ToolContext, args: CreateDraftPOArgs) -> dict[str, Any]:
    skus = [line.sku for line in args.lines]
    repeated = sorted(set(skus) & ctx.ordered_skus)
    if repeated:
        # The validator checks stock against PAID orders only, so without this a second draft
        # to another supplier could double-order the same SKU past max_stock.
        return error_result(
            "SKU_ALREADY_DRAFTED",
            f"{repeated} already appear on a draft order in this run; each SKU may be ordered once.",
            skus=repeated,
        )

    # The model's numbers stop here: from this call on, prices, totals and every rule check
    # come from the database and the validator. On rejection nothing has been written.
    po = create_validated_draft(
        ctx.session,
        args.supplier_id,
        [LineRequest(line.sku, line.quantity, line.reason) for line in args.lines],
        budget_cents=ctx.budget_cents,
        run_id=ctx.run_id,
        rationale=args.rationale,
        promotion_id=args.promotion_id,
        as_of=ctx.as_of,
    )
    ctx.drafts.append(DraftRecord(po.id, po.supplier_id, po.subtotal_cents, tuple(skus)))
    promotion = (
        {"promotion_id": po.promotion_id, "title": po.promotion.title,
         "expected_rebate_cents": expected_rebate_cents(po.promotion, po),
         "note": "Paid back by the supplier later; this order is paid at list price."}
        if po.promotion is not None else None
    )
    return {
        "po_id": po.id,
        "status": str(po.status),
        "supplier": po.supplier.name,
        "total_cents": po.subtotal_cents,
        "promotion": promotion,
        "lines": [
            {"sku": line.product.sku, "quantity": line.quantity,
             "cases": line.quantity // line.pack_size, "pack_size": line.pack_size,
             "unit_price_cents": line.unit_price_cents, "line_total_cents": line.line_total_cents}
            for line in po.lines
        ],
        "remaining_budget_cents": ctx.budget_cents - ctx.spent_cents,
        "next_step": "Draft saved. It is NOT approved or paid; the merchant must approve it.",
    }


# ---------------------------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[_Args]
    handler: Callable[[ToolContext, Any], dict[str, Any]]

    def schema(self) -> ToolSchema:
        """What the provider is shown: name, description and a JSON Schema for the arguments."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }


TOOLS: tuple[Tool, ...] = (
    Tool(
        "get_constraints",
        "Get the budget for this run, how much is already drafted, the merchant's settings, and "
        "the rules every purchase order must satisfy. Call this first.",
        NoArgs, _get_constraints,
    ),
    Tool(
        "get_reorder_candidates",
        "List products that are short on stock for a cover period, most urgent first, with "
        "stock and sales evidence, plus the system's own suggested orders for the budget. "
        "Pass cover_days when the merchant names a period (7 for a week, 21 for 3 weeks); "
        "omit it to use the store's default target_cover_days.",
        ReorderCandidatesArgs, _get_reorder_candidates,
    ),
    Tool(
        "get_sales_history",
        "Daily sales for one product over a recent window. Use it to check a product's demand "
        "when the request or the data looks unusual.",
        SalesHistoryArgs, _get_sales_history,
    ),
    Tool(
        "get_supplier_quotes",
        "Compare every supplier for the given SKUs: unit price, case size, supplier minimum, "
        "lead time, and the exact quantity and cost that would cover the current shortfall. "
        "Pass the same cover_days used in get_reorder_candidates.",
        SupplierQuotesArgs, _get_supplier_quotes,
    ),
    Tool(
        "lookup_products",
        "Find products by name, brand, flavor or size and get their stock on hand, stock on "
        "order, sales rate and days of cover. Use it to answer 'how much X do we have'.",
        LookupProductsArgs, _lookup_products,
    ),
    Tool(
        "get_active_promotions",
        "List the supplier promotions (rebates) usable today, with their terms and eligible "
        "products.",
        NoArgs, _get_active_promotions,
    ),
    Tool(
        "evaluate_promotion",
        "The system's recommended split of a promotion's cases across its eligible products, "
        "from current stock and sales, with list total, rebate, net cost and evidence per line.",
        PromotionIdArgs, _evaluate_promotion,
    ),
    Tool(
        "create_draft_po",
        "Create a DRAFT purchase order for ONE supplier from {sku, quantity} lines. The system "
        "prices it and checks every rule; if it is rejected you get the violations back and may "
        "fix and retry. A draft is not approved or paid.",
        CreateDraftPOArgs, _create_draft_po,
    ),
)

TOOLS_BY_NAME: dict[str, Tool] = {tool.name: tool for tool in TOOLS}

# Answering questions needs no budget and must change nothing: read-only tools only.
QUESTION_TOOL_NAMES = (
    "lookup_products", "get_sales_history", "get_reorder_candidates", "get_active_promotions",
    "evaluate_promotion",
)
QUESTION_TOOLS: tuple[Tool, ...] = tuple(TOOLS_BY_NAME[name] for name in QUESTION_TOOL_NAMES)


def tool_schemas(tools: Sequence[Tool] = TOOLS) -> list[ToolSchema]:
    return [tool.schema() for tool in tools]


# ---------------------------------------------------------------------------------------------
# The single entry point the loop uses
# ---------------------------------------------------------------------------------------------


def execute_tool(
    ctx: ToolContext, name: str, raw_arguments: str, tools: Sequence[Tool] = TOOLS
) -> ToolResult:
    """Run one model-requested tool call. Never raises for mistakes the model can make.

    `raw_arguments` is the JSON text the model wrote: untrusted. Order of defences:
    known tool (in `tools`, the set this run was given) -> valid JSON object -> Pydantic
    validation -> handler -> service-level rules.
    """
    allowed = {tool.name: tool for tool in tools}
    tool = allowed.get(name)
    if tool is None:
        return ToolResult(error_result(
            "UNKNOWN_TOOL", f"There is no tool named {name!r}.", available_tools=sorted(allowed)
        ))

    try:
        data = json.loads(raw_arguments) if raw_arguments.strip() else {}
    except json.JSONDecodeError:
        return ToolResult(error_result("INVALID_ARGUMENTS", "The arguments were not valid JSON."))
    if not isinstance(data, dict):
        return ToolResult(error_result("INVALID_ARGUMENTS", "The arguments must be a JSON object."))

    try:
        args = tool.args_model.model_validate(data)
    except ValidationError as exc:
        # Report where and what, but never echo the submitted values back.
        problems = [
            {"field": ".".join(str(part) for part in e["loc"]) or "(root)", "problem": e["msg"]}
            for e in exc.errors(include_url=False, include_input=False, include_context=False)
        ]
        return ToolResult(error_result(
            "INVALID_ARGUMENTS", "The arguments do not match the tool's schema.", problems=problems
        ))

    try:
        return ToolResult(tool.handler(ctx, args))
    except UnknownSkuError as exc:
        return ToolResult(error_result(
            "UNKNOWN_SKU", f"No product has SKU {exc.args[0]!r}. Take SKUs from get_reorder_candidates."
        ))
    except OrderRejectedError as exc:
        return ToolResult(error_result(
            "ORDER_REJECTED",
            "The order breaks purchasing rules and was NOT saved. Fix every violation and retry.",
            violations=[
                {"code": str(v.code), "message": v.message, "sku": v.sku} for v in exc.violations
            ],
        ))
    except InvalidOrderError as exc:
        return ToolResult(error_result("INVALID_ORDER", str(exc)))
