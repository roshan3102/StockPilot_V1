"""The bounded agent loop: ask the model, run the tools it requests, feed results back, repeat.

The control flow here is ordinary code. The model decides WHICH tools to call and WHAT to
write; this loop decides whether a call is allowed to run, how many may run, and when to stop.

Bounds (all in `AgentLimits`): LLM calls, total tool calls, and wall-clock seconds. Hitting a
bound ends the run with status LIMIT_REACHED; the drafts created so far are valid (each passed
the validator) and are kept, with a summary written by us, not the model.

Transactions: tools only `flush`. On COMPLETED or LIMIT_REACHED the drafts are left pending
and the CALLER commits (M6 will, together with the run record). On FAILED this function calls
`session.rollback()`, so a run that ends in failure leaves no purchase orders behind
(docs/MVP_PLAN.md section 3: "run failed, no POs").
"""

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from app.agent.llm_client import LLMClient, LLMError, Message
from app.agent.prompts import build_question_prompt, build_system_prompt, usd
from app.agent.tools import (
    QUESTION_TOOLS,
    TOOLS,
    Tool,
    ToolContext,
    error_result,
    execute_tool,
    tool_schemas,
)

MAX_MESSAGE_CHARS = 2000


@dataclass(frozen=True)
class AgentLimits:
    max_llm_calls: int = 12  # plan: "hard cap (~12 steps)"
    max_tool_calls: int = 24
    max_seconds: float = 90.0  # checked between model calls, not inside one (the client has its own timeout)


class AgentStatus(StrEnum):
    COMPLETED = "COMPLETED"  # the model finished and wrote its explanation
    LIMIT_REACHED = "LIMIT_REACHED"  # stopped by a bound; drafts so far are kept
    FAILED = "FAILED"  # the model could not be used; nothing was kept


@dataclass(frozen=True)
class AgentStep:
    """One line of the trace. Same shape as the planned `agent_steps` table (M6)."""

    seq: int
    kind: str  # "assistant" | "tool_call" | "tool_result"
    name: str | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class AgentError:
    code: str
    message: str  # safe to show to a merchant
    detail: str | None = None  # developer-facing technical cause (redacted); see LLMError.detail


@dataclass(frozen=True)
class AgentResult:
    status: AgentStatus
    summary: str  # for the merchant: the model's explanation, or ours when the model could not
    draft_po_ids: tuple[int, ...]
    total_drafted_cents: int  # summed from the database totals, never from model text
    steps: tuple[AgentStep, ...]
    llm_calls: int
    tool_calls: int
    error: AgentError | None = None


def _drafts_sentence(ctx: ToolContext) -> str:
    if not ctx.drafts:
        return "No draft purchase orders were created."
    listing = ", ".join(f"PO #{d.po_id} ({usd(d.total_cents)})" for d in ctx.drafts)
    return (
        f"{len(ctx.drafts)} draft purchase order(s) were created, {usd(ctx.spent_cents)} in total: "
        f"{listing}. They are drafts: review and approve them before anything is paid."
    )


def run_restock_agent(
    session: Session,
    llm: LLMClient,
    *,
    message: str,
    budget_cents: int,
    run_id: int,
    as_of: date,
    limits: AgentLimits = AgentLimits(),
    clock: Callable[[], float] = time.monotonic,
) -> AgentResult:
    """Run one restock request to completion (or to a bound, or to a provider failure).

    `budget_cents` is the authoritative spending cap (the merchant's structured field); the
    model never chooses it. `run_id` is required: it is what makes all drafts of this run
    share ONE budget in the validator. `clock` is a parameter so tests can fake the time.
    """
    message = message.strip()
    if not message or len(message) > MAX_MESSAGE_CHARS:
        raise ValueError(f"message must be 1-{MAX_MESSAGE_CHARS} characters")
    if budget_cents < 0:
        raise ValueError("budget_cents cannot be negative")

    ctx = ToolContext(session=session, as_of=as_of, budget_cents=budget_cents, run_id=run_id)
    return _run_loop(
        session, llm, ctx, system_prompt=build_system_prompt(budget_cents, as_of), message=message,
        tools=TOOLS, limits=limits, clock=clock,
    )


QUESTION_LIMITS = AgentLimits(max_llm_calls=6, max_tool_calls=12, max_seconds=45.0)


def run_question_agent(
    session: Session,
    llm: LLMClient,
    *,
    question: str,
    as_of: date,
    budget_cents: int,
    limits: AgentLimits = QUESTION_LIMITS,
    clock: Callable[[], float] = time.monotonic,
) -> AgentResult:
    """Answer one question about stock, sales or promotions with read-only tools.

    It cannot create anything: its tool set has no create_draft_po (a test enforces this),
    and the session is rolled back afterwards regardless. `budget_cents` only feeds the
    engine suggestion that get_reorder_candidates includes.
    """
    question = question.strip()
    if not question or len(question) > MAX_MESSAGE_CHARS:
        raise ValueError(f"question must be 1-{MAX_MESSAGE_CHARS} characters")
    ctx = ToolContext(session=session, as_of=as_of, budget_cents=budget_cents, run_id=0)
    try:
        return _run_loop(
            session, llm, ctx, system_prompt=build_question_prompt(as_of), message=question,
            tools=QUESTION_TOOLS, limits=limits, clock=clock,
        )
    finally:
        session.rollback()


def _run_loop(
    session: Session,
    llm: LLMClient,
    ctx: ToolContext,
    *,
    system_prompt: str,
    message: str,
    tools: Sequence[Tool],
    limits: AgentLimits,
    clock: Callable[[], float],
) -> AgentResult:
    messages: list[Message] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": message},
    ]
    schemas = tool_schemas(tools)
    steps: list[AgentStep] = []
    llm_calls = 0
    tool_calls = 0
    deadline = clock() + limits.max_seconds

    def step(kind: str, name: str | None, payload: dict[str, Any]) -> None:
        steps.append(AgentStep(len(steps) + 1, kind, name, payload))

    def finish(status: AgentStatus, summary: str, error: AgentError | None = None) -> AgentResult:
        if status == AgentStatus.FAILED:
            session.rollback()  # discard any drafts: a failed run keeps nothing
            ctx.drafts.clear()
        return AgentResult(
            status=status, summary=summary,
            draft_po_ids=tuple(d.po_id for d in ctx.drafts),
            total_drafted_cents=ctx.spent_cents,
            steps=tuple(steps), llm_calls=llm_calls, tool_calls=tool_calls, error=error,
        )

    def limit_reached(why: str) -> AgentResult:
        return finish(
            AgentStatus.LIMIT_REACHED,
            f"The assistant stopped early ({why}) before writing an explanation. "
            f"{_drafts_sentence(ctx)}",
            AgentError("LIMIT_REACHED", why),
        )

    while True:
        if llm_calls >= limits.max_llm_calls:
            return limit_reached(f"reached the limit of {limits.max_llm_calls} model calls")
        if clock() >= deadline:
            return limit_reached(f"reached the {limits.max_seconds:g}-second time limit")

        llm_calls += 1
        try:
            reply = llm.complete(messages, schemas)
        except LLMError as exc:
            return finish(
                AgentStatus.FAILED,
                f"StockPilot could not complete this request: {exc} No purchase orders were "
                f"created. Please try again.",
                AgentError(exc.code, str(exc), exc.detail),
            )

        step("assistant", None, {
            "content": reply.content, "tool_call_ids": [c.id for c in reply.tool_calls],
        })

        if not reply.tool_calls:  # no tool requested = the model considers itself done
            text = (reply.content or "").strip()
            summary = text or f"The assistant finished without an explanation. {_drafts_sentence(ctx)}"
            return finish(AgentStatus.COMPLETED, summary)

        # The assistant message must be echoed back verbatim, including its tool_calls, and
        # EVERY tool_call id must get a "tool" reply, or the provider rejects the next request.
        messages.append({
            "role": "assistant",
            "content": reply.content,
            "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in reply.tool_calls
            ],
        })
        out_of_tool_calls = False
        for call in reply.tool_calls:
            step("tool_call", call.name, {"id": call.id, "arguments": call.arguments})
            if tool_calls >= limits.max_tool_calls:
                out_of_tool_calls = True
                result_payload = error_result(
                    "TOOL_LIMIT_REACHED", "No more tool calls are allowed in this run."
                )
            else:
                tool_calls += 1
                result_payload = execute_tool(ctx, call.name, call.arguments, tools).payload
            step("tool_result", call.name, {
                "id": call.id, "is_error": "error" in result_payload, "result": result_payload,
            })
            messages.append({
                "role": "tool", "tool_call_id": call.id, "content": json.dumps(result_payload),
            })
        if out_of_tool_calls:
            return limit_reached(f"reached the limit of {limits.max_tool_calls} tool calls")
