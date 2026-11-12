import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api import (
    assistant,
    inventory,
    payments,
    promotions,
    purchase_orders,
    restock,
    restock_runs,
)
from app.api.errors import install_error_handlers
from app.config import get_settings
from app.db import get_engine
from app.services.restock_runs import fail_stale_runs

settings = get_settings()
logger = logging.getLogger("stockpilot")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Runs once at startup (before `yield`) and once at shutdown (after it)."""
    try:
        with Session(get_engine()) as session:
            stale = fail_stale_runs(session)
            session.commit()
    except OperationalError:
        # No restock_runs table yet (fresh clone, or a database from before M6). The server
        # still starts, so /health works and other endpoints can explain what is missing.
        logger.warning("Skipped stale-run cleanup: schema missing. Run `uv run python -m app.seed`.")
    else:
        if stale:
            logger.warning("Marked %d interrupted restock run(s) as failed", stale)
    yield


app = FastAPI(title="StockPilot API", lifespan=lifespan)

install_error_handlers(app)
app.include_router(inventory.router)
app.include_router(restock.router)
app.include_router(restock_runs.router)
app.include_router(purchase_orders.router)
app.include_router(payments.router)
app.include_router(promotions.router)
app.include_router(assistant.router)

# Browsers block a page on one origin (localhost:3000) from calling an API on another
# (localhost:8000) unless the API opts in via CORS headers. We allow only the frontend origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check used by the frontend and by humans to confirm the API is up."""
    return {"status": "ok", "env": settings.app_env}
