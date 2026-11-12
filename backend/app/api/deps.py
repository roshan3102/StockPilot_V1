"""FastAPI dependencies: objects a route needs, supplied by the framework.

A route declares `session: Session = Depends(get_session)` and FastAPI calls the function
for each request. Tests swap them via `app.dependency_overrides[get_session] = ...`, which is
how API tests get a throwaway database and a fixed "today" without touching route code.
"""

from collections.abc import Iterator
from datetime import date
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db import get_engine
from app.services.payments import PayPalGateway, get_paypal_client
from app.services.promotions import StructuredLLMClient
from app.services.restock_runs import LLMClient, get_llm_client


def get_session() -> Iterator[Session]:
    """One database session per request, always closed afterwards.

    The `yield` splits the function: code before it runs before the route, code after it
    runs once the response is done (even if the route raised). The session never commits by
    itself: a route that writes must call `session.commit()` after its service call succeeds.
    If the route raises first, closing the session discards the open transaction (a rollback),
    so a refused request leaves no trace.
    """
    with Session(get_engine()) as session:
        yield session


def get_llm() -> LLMClient:
    """The real LLM client. A wrapper because FastAPI would read `get_llm_client`'s optional
    `settings` parameter as a request body field."""
    return get_llm_client()


def get_structured_llm() -> StructuredLLMClient:
    """The same client, used for one-shot JSON answers (reading a promotion's text)."""
    return get_llm_client()


def get_as_of() -> date:
    """"Today", as the date the sales window ends on. The only place the clock is read; the
    services take it as a parameter so they stay reproducible."""
    return date.today()


# `Annotated[..., Depends(...)]` lets routes write `session: SessionDep` instead of repeating
# the Depends call in every signature.
SessionDep = Annotated[Session, Depends(get_session)]
AsOfDep = Annotated[date, Depends(get_as_of)]
# The PayPal gateway. Tests replace it with an in-memory fake via
# `app.dependency_overrides[get_paypal_client]`, so no test ever contacts PayPal.
PayPalDep = Annotated[PayPalGateway, Depends(get_paypal_client)]
# The LLM client. Raises LLMError (-> 503) when no API key is set. Tests replace it with a
# scripted fake via `app.dependency_overrides[get_llm]`, so no test calls a real model.
LLMDep = Annotated[LLMClient, Depends(get_llm)]
# Overridden in tests via `app.dependency_overrides[get_structured_llm]`.
StructuredLLMDep = Annotated[StructuredLLMClient, Depends(get_structured_llm)]
