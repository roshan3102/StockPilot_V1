"""Developer smoke test: send ONE restock request to the real model and print what happened.

    uv run python -m app.run_agent "Restock the items running low, keep it under $500" --budget 500

What it is for: checking that your API key, the model name, tool calling and the whole loop
work against the real provider. It is not part of the product (there is no HTTP endpoint yet).

Safety
- It NEVER commits. Draft orders are created, printed, then rolled back, so running it any
  number of times leaves your dev database exactly as it was.
- It never prints the API key. The only credentials-related output is a "not configured" notice.
- Each run costs a few model calls (at most 12; see AgentLimits).

Needs a seeded database: `uv run python -m app.seed`.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.agent.llm_client import LLMClient, LLMError, get_llm_client
from app.agent.loop import AgentResult, AgentStatus, run_restock_agent
from app.agent.prompts import usd
from app.config import get_settings
from app.db import get_engine
from app.services.inventory import DataNotSeededError
from app.services.po_lifecycle import get_purchase_order

# A run id nothing else uses. Drafts of one run share one budget, and the validator finds "the
# rest of this run" by this id, so it must not collide with real runs' drafts.
SMOKE_RUN_ID = 999_999
TRACE_WIDTH = 160
MAX_BUDGET_CENTS = 100_000_000  # $1,000,000: a typo guard for a smoke test, not a business rule


def dollars_to_cents(text: str) -> int:
    """'500' or '49.99' -> cents, using exact decimal arithmetic (never a float)."""
    try:
        cents = Decimal(text) * 100
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not a dollar amount: {text!r}") from None
    if not cents.is_finite() or cents != cents.to_integral_value() or not 0 <= cents <= MAX_BUDGET_CENTS:
        raise argparse.ArgumentTypeError(
            f"budget must be between $0 and {usd(MAX_BUDGET_CENTS)} with at most 2 decimals"
        )
    return int(cents)


def _shorten(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TRACE_WIDTH else text[: TRACE_WIDTH - 1] + "…"


def format_report(result: AgentResult, session: Session, *, model: str, budget_cents: int) -> str:
    """Human-readable account of a run. Money comes from the database, not from model text."""
    lines = [
        f"Model: {model}   Budget: {usd(budget_cents)}",
        f"Status: {result.status}   model calls: {result.llm_calls}   tool calls: {result.tool_calls}",
    ]
    if result.error:
        lines.append(f"Error: {result.error.code}: {result.error.message}")
        if result.error.detail:
            lines.append(f"Detail (developer only): {result.error.detail}")

    lines += ["", "--- Trace ---"]
    for step in result.steps:
        if step.kind == "assistant":
            detail = _shorten(step.payload["content"] or "(tool calls only)")
        elif step.kind == "tool_call":
            detail = _shorten(step.payload["arguments"])
        else:
            outcome = "ERROR" if step.payload["is_error"] else "ok"
            detail = f"{outcome} {_shorten(str(step.payload['result']))}"
        lines.append(f"{step.seq:>3}. {step.kind:<11} {step.name or '':<24} {detail}")

    lines += ["", "--- Draft purchase orders (as recorded in the database; rolled back after) ---"]
    if not result.draft_po_ids:
        lines.append("(none)")
    for po_id in result.draft_po_ids:
        po = get_purchase_order(session, po_id)
        lines.append(f"PO #{po.id}  {po.supplier.name}  {po.status}  total {usd(po.subtotal_cents)}")
        for line in po.lines:
            lines.append(
                f"    {line.product.sku:<16} {line.quantity:>5} x {usd(line.unit_price_cents)}"
                f" = {usd(line.line_total_cents)}"
            )
    lines.append(f"Drafted in total: {usd(result.total_drafted_cents)} of {usd(budget_cents)}")

    lines += ["", "--- Assistant's explanation ---", result.summary]
    return "\n".join(lines)


def run(
    session: Session, llm: LLMClient, *, message: str, budget_cents: int, as_of: date, model: str
) -> tuple[str, AgentResult]:
    """Run the agent, build the report, then roll back so nothing is ever saved."""
    result = run_restock_agent(
        session, llm, message=message, budget_cents=budget_cents, run_id=SMOKE_RUN_ID, as_of=as_of
    )
    try:
        report = format_report(result, session, model=model, budget_cents=budget_cents)
    finally:
        session.rollback()  # the "never commits" guarantee, even if formatting fails
    return report, result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send one restock request to the real LLM.")
    parser.add_argument("message", help="what the merchant would type")
    # The default is the STRING "500": argparse only applies `type=` to string defaults.
    parser.add_argument("--budget", type=dollars_to_cents, default="500",
                        help="spending cap in dollars (default 500); the system enforces it")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None,
                        help="treat this date as 'today' (default: the real today)")
    args = parser.parse_args(argv)
    return _execute(args.message, args.budget, args.as_of or date.today())


def _execute(message: str, budget_cents: int, as_of: date) -> int:
    settings = get_settings()
    try:
        llm = get_llm_client(settings)
    except LLMError as exc:
        print(f"Cannot run: {exc}\nAdd OPENAI_API_KEY to .env (see .env.example).", file=sys.stderr)
        return 2
    try:
        with Session(get_engine()) as session:
            report, result = run(
                session, llm, message=message, budget_cents=budget_cents, as_of=as_of,
                model=settings.llm_model,
            )
    except DataNotSeededError:
        print("The database is empty. Run: uv run python -m app.seed", file=sys.stderr)
        return 2
    print(report)
    return 1 if result.status == AgentStatus.FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
