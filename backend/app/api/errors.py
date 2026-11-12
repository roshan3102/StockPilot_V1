"""One error format for the whole API (docs/MVP_PLAN.md section 5):

    {"error": {"code": "...", "message": "...", "details": {...}}}

FastAPI would otherwise answer with several different shapes (`{"detail": ...}` for 404 and
405, a list of error objects for validation failures). The handlers below translate all of
them, so a client only ever has to parse one.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.services.inventory import DataNotSeededError
from app.services.payments import PayPalConfigError
from app.services.restock_runs import LLMError

logger = logging.getLogger("stockpilot")

_HTTP_CODES = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}


class ApiError(Exception):
    """Raise from a route to return a specific status and error code."""

    def __init__(self, status_code: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def error_response(
    status_code: int, code: str, message: str, details: dict | None = None
) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "details": details or {}}}
    return JSONResponse(status_code=status_code, content=body)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Keep only JSON-safe fields: Pydantic's raw errors can carry non-serialisable objects.
        errors = [
            {"field": ".".join(str(part) for part in e["loc"]), "message": e["msg"], "type": e["type"]}
            for e in exc.errors()
        ]
        return error_response(
            422, "VALIDATION_ERROR", "The request is invalid", {"errors": errors}
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, f"HTTP_{exc.status_code}")
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(DataNotSeededError)
    async def _not_seeded(_: Request, exc: DataNotSeededError) -> JSONResponse:
        return error_response(
            503, "NOT_SEEDED",
            "The database has no merchant settings. Run `uv run python -m app.seed` in backend/.",
        )

    @app.exception_handler(PayPalConfigError)
    async def _paypal_not_configured(_: Request, exc: PayPalConfigError) -> JSONResponse:
        # Raised while building the PayPal client (a dependency), before any route code runs.
        # The message names variables, never values, so it is safe to show.
        return error_response(503, "PAYPAL_NOT_CONFIGURED", str(exc))

    @app.exception_handler(LLMError)
    async def _llm_not_available(_: Request, exc: LLMError) -> JSONResponse:
        # Only building the client raises outside the agent loop (no API key: LLM_NOT_CONFIGURED);
        # failures during a run are recorded on the run instead. The message names no secret.
        return error_response(503, exc.code, str(exc))

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        # Log the real cause for the developer; tell the client nothing about internals.
        logger.exception("Unhandled error", exc_info=exc)
        return error_response(500, "INTERNAL_ERROR", "Something went wrong on the server")
