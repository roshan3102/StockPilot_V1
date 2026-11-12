"""Answer a merchant's question ("how much Grey Goose 750ml do we have?") with the read-only agent.

Nothing is stored and nothing is changed: the question agent only has read tools and its
session work is rolled back. The products shown beside the answer come from the lookup tool's
results, i.e. from the database, so the UI never has to trust numbers in the model's prose.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.agent.llm_client import LLMClient
from app.agent.loop import MAX_MESSAGE_CHARS, AgentStatus, AgentStep, run_question_agent
from app.services.inventory import get_settings_row

MAX_QUESTION_CHARS = MAX_MESSAGE_CHARS


@dataclass(frozen=True)
class AssistantAnswer:
    question: str
    answered: bool
    answer: str
    products: tuple[dict[str, Any], ...]
    steps: tuple[AgentStep, ...]
    error_code: str | None = None
    llm_failed: bool = False  # the model could not be used at all (vs. stopped by a step limit)


def looked_up_products(steps: Iterable[tuple[str, str | None, dict[str, Any]]]) -> tuple[dict[str, Any], ...]:
    """The products lookup_products returned, first occurrence of each SKU, from
    (kind, name, payload) trace steps."""
    seen: dict[str, dict[str, Any]] = {}
    for kind, name, payload in steps:
        if kind == "tool_result" and name == "lookup_products":
            for match in payload.get("result", {}).get("matches", []):
                seen.setdefault(match["sku"], match)
    return tuple(seen.values())


def _products_from(steps: tuple[AgentStep, ...]) -> tuple[dict[str, Any], ...]:
    return looked_up_products((s.kind, s.name, s.payload) for s in steps)


def answer_question(
    session: Session, llm: LLMClient, *, question: str, as_of: date
) -> AssistantAnswer:
    """Raises DataNotSeededError if the store has no settings, ValueError for a bad question."""
    settings = get_settings_row(session)
    result = run_question_agent(
        session, llm, question=question, as_of=as_of,
        budget_cents=settings.default_budget_cents,
    )
    answered = result.status == AgentStatus.COMPLETED
    if answered:
        answer = result.summary
    elif result.error is not None:
        answer = result.error.message
    else:
        answer = "StockPilot could not finish answering that question. Try asking it more simply."
    return AssistantAnswer(
        question=question.strip(),
        answered=answered,
        answer=answer,
        products=_products_from(result.steps),
        steps=result.steps,
        error_code=None if answered or result.error is None else result.error.code,
        llm_failed=result.status == AgentStatus.FAILED,
    )
