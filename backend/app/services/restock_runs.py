"""Restock runs: one merchant request, one agent run, one durable record with its trace.

Unlike the other services, `start_restock_run` COMMITS, twice, because a run's record must
outlive the agent's failure:

  1. the `running` row is committed before the agent starts. If the process dies mid-run, that
     row is what `fail_stale_runs` (called at startup) finds and marks `failed`;
  2. the drafts, the trace and the final status are committed together, so a run never ends
     up with half of its purchase orders, and a `failed` run never has any.

The agent loop flushes drafts but never commits; on FAILED it has already rolled them back.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

# Re-exported so the API layer reaches the agent only through this service.
from app.agent.llm_client import LLMClient, LLMError, get_llm_client
from app.agent.loop import MAX_MESSAGE_CHARS, AgentResult, AgentStatus, run_restock_agent
from app.models import AgentStepRecord, PurchaseOrder, RestockRun, RunStatus
from app.services.assistant import looked_up_products
from app.services.inventory import get_settings_row
from app.services.procurement import InventoryOverviewRow, get_inventory_overview

__all__ = [
    "MAX_MESSAGE_CHARS", "LLMClient", "LLMError", "RestockRunNotFoundError", "RunStatus", "RunView",
    "fail_stale_runs", "get_llm_client", "get_restock_run", "start_restock_run",
]

logger = logging.getLogger("stockpilot")

INTERNAL_ERROR_MESSAGE = (
    "Something went wrong on the server while handling this request. "
    "No purchase orders were created. Please try again."
)
STALE_RUN_MESSAGE = (
    "The server restarted before this request finished. "
    "No purchase orders were created. Please try again."
)


class RestockRunNotFoundError(LookupError):
    """No restock run has this id."""


@dataclass(frozen=True)
class RunView:
    """A run with what the API shows next to it. `evidence` holds today's inventory numbers
    for every SKU on the run's purchase orders (deterministic, from the database)."""

    run: RestockRun
    purchase_orders: list[PurchaseOrder]
    evidence: dict[str, InventoryOverviewRow]
    as_of: date
    products: tuple[dict[str, Any], ...] = ()  # what lookup_products returned during the run


def _now() -> datetime:
    """UTC, timezone-naive: matches how SQLite stores the DateTime columns."""
    return datetime.now(UTC).replace(tzinfo=None)


def _save_steps(session: Session, run_id: int, result: AgentResult) -> None:
    session.add_all(
        AgentStepRecord(
            run_id=run_id, seq=step.seq, kind=step.kind, name=step.name,
            payload_json=json.dumps(step.payload),
        )
        for step in result.steps
    )


def start_restock_run(
    session: Session,
    llm: LLMClient,
    *,
    message: str,
    budget_cents: int | None,
    as_of: date,
    model: str,
) -> RunView:
    """Record the request, run the agent, record the outcome. Returns the finished run.

    Returns normally for `completed` AND `failed` runs (an unreachable model is an expected
    outcome, recorded like any other). Raises only for our own bugs; the run is then marked
    `failed` with INTERNAL_ERROR before the exception propagates.
    """
    settings = get_settings_row(session)  # raises DataNotSeededError before anything is written
    budget = settings.default_budget_cents if budget_cents is None else budget_cents

    run = RestockRun(
        message=message, budget_cents=budget, status=RunStatus.RUNNING.value, model=model
    )
    session.add(run)
    session.commit()
    run_id = run.id

    try:
        result = run_restock_agent(
            session, llm, message=message, budget_cents=budget, run_id=run_id, as_of=as_of
        )
    except Exception:
        session.rollback()  # discards any drafts the loop had flushed
        run = session.get_one(RestockRun, run_id)
        run.status, run.finished_at = RunStatus.FAILED.value, _now()
        run.error_code, run.error_message = "INTERNAL_ERROR", INTERNAL_ERROR_MESSAGE
        session.commit()
        raise

    run = session.get_one(RestockRun, run_id)
    _save_steps(session, run_id, result)
    run.summary, run.finished_at = result.summary, _now()
    run.status = (
        RunStatus.FAILED.value if result.status == AgentStatus.FAILED else RunStatus.COMPLETED.value
    )
    if result.error is not None:
        run.error_code, run.error_message = result.error.code, result.error.message
        if result.error.detail:
            # Developer-facing and already redacted; kept out of the database and the API.
            logger.warning("Restock run %s: %s: %s", run_id, result.error.code, result.error.detail)
    session.commit()
    return get_restock_run(session, run_id, as_of)


def get_restock_run(session: Session, run_id: int, as_of: date) -> RunView:
    run = session.get(RestockRun, run_id)
    if run is None:
        raise RestockRunNotFoundError(run_id)
    pos = list(session.scalars(
        select(PurchaseOrder).where(PurchaseOrder.run_id == run_id).order_by(PurchaseOrder.id)
    ))
    skus = {line.product.sku for po in pos for line in po.lines}
    evidence = (
        {row.sku: row for row in get_inventory_overview(session, as_of) if row.sku in skus}
        if skus else {}
    )
    products = looked_up_products(
        (s.kind, s.name, json.loads(s.payload_json)) for s in run.steps
    )
    return RunView(run=run, purchase_orders=pos, evidence=evidence, as_of=as_of, products=products)


def fail_stale_runs(session: Session) -> int:
    """Mark every `running` run as `failed`. Call once at startup, before serving requests.

    Runs are synchronous inside a single server process, so at startup nothing can still be
    working on them. Their drafts were never committed, so there is nothing else to undo.
    Flushes; the caller commits. Returns how many runs were marked.
    """
    stale = session.scalars(
        select(RestockRun).where(RestockRun.status == RunStatus.RUNNING.value)
    ).all()
    for run in stale:
        run.status, run.finished_at = RunStatus.FAILED.value, _now()
        run.error_code, run.error_message = "SERVER_RESTARTED", STALE_RUN_MESSAGE
    session.flush()
    return len(stale)
