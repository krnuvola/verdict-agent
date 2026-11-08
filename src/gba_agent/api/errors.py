"""Contract-conforming API error responses and exception handlers."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from gba_agent.api.contracts_compat import ErrorBody, ErrorEnvelope

logger = logging.getLogger(__name__)


class APIError(Exception):
    """Explicit application error with an HTTP status and shared envelope."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = dict(details or {})


def make_error(
    *,
    code: str,
    message: str,
    request_id: str | None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct the exact shared ``ErrorEnvelope`` JSON representation."""
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=message,
            details=dict(details or {}),
            request_id=request_id,
        )
    )
    return envelope.model_dump(mode="json")


def request_id_for(request: Request) -> str | None:
    """Read request ID from middleware state when available."""
    trace = getattr(request.state, "trace", None)
    return trace.request_id if trace is not None else None


def install_error_handlers(app: FastAPI) -> None:
    """Register HTTP, validation, and explicit API error handlers."""

    @app.exception_handler(APIError)
    async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=make_error(
                code=exc.code,
                message=exc.message,
                details=exc.details,
                request_id=request_id_for(request),
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        for error in exc.errors():
            if error["type"] == "too_many_claims":
                return JSONResponse(
                    status_code=422,
                    content=make_error(
                        code="TOO_MANY_CLAIMS",
                        message=error["msg"],
                        details=error.get("ctx"),
                        request_id=request_id_for(request),
                    ),
                )
        details = [
            {
                "loc": [str(part) for part in error["loc"]],
                "msg": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=make_error(
                code="VALIDATION_ERROR",
                message="Request validation failed",
                details={"errors": details},
                request_id=request_id_for(request),
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        catalogue: dict[int, tuple[str, str]] = {
            401: ("UNAUTHORIZED", "Missing or invalid bearer token"),
            404: ("NOT_FOUND", "Route not found"),
            405: ("VALIDATION_ERROR", "Method not allowed"),
        }
        code, message = catalogue.get(exc.status_code, ("INTERNAL_ERROR", "Request failed"))
        return JSONResponse(
            status_code=exc.status_code,
            content=make_error(
                code=code,
                message=message,
                details={"detail": exc.detail} if exc.detail is not None else {},
                request_id=request_id_for(request),
            ),
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled API exception", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content=make_error(
                code="INTERNAL_ERROR",
                message="Unexpected internal error",
                request_id=request_id_for(request),
            ),
        )
