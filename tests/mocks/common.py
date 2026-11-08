"""Authentication, tracing, and ErrorEnvelope helpers shared by local mocks."""

from __future__ import annotations

import hmac
import os
import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from ulid import ULID

CONTRACT_VERSION = "1.0.0"
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def error(
    request_id: str | None,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": request_id,
        }
    }


def install_contract_middleware(app: FastAPI) -> None:
    """Make a test mock obey the shared auth and tracing conventions."""

    @app.middleware("http")
    async def contract_headers(request: Request, call_next: Any) -> Any:
        request_id = request.headers.get("X-Request-Id") or str(ULID())
        run_id = request.headers.get("X-Run-Id") or "run_" + str(ULID())
        request.state.request_id = request_id
        request.state.run_id = run_id
        response: Any
        if request.url.path != "/health":
            supplied = request.headers.get("X-Contract-Version")
            token = os.environ.get("SERVICE_TOKEN")
            authorization = request.headers.get("Authorization", "")
            scheme, separator, supplied_token = authorization.partition(" ")
            if supplied is None or VERSION_RE.fullmatch(supplied) is None:
                response = JSONResponse(
                    status_code=422,
                    content=error(
                        request_id,
                        "VALIDATION_ERROR",
                        "A valid X-Contract-Version header is required",
                    ),
                )
            elif supplied.split(".", 1)[0] != CONTRACT_VERSION.split(".", 1)[0]:
                response = JSONResponse(
                    status_code=400,
                    content=error(
                        request_id,
                        "CONTRACT_VERSION_MISMATCH",
                        "Peer contract major version is not supported",
                        {"expected": CONTRACT_VERSION, "received": supplied},
                    ),
                )
            elif (
                token is None
                or not separator
                or scheme.lower() != "bearer"
                or not hmac.compare_digest(token, supplied_token)
            ):
                response = JSONResponse(
                    status_code=401,
                    content=error(
                        request_id,
                        "UNAUTHORIZED",
                        "Missing or invalid bearer token",
                    ),
                )
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Contract-Version"] = CONTRACT_VERSION
        response.headers["X-Run-Id"] = run_id
        if "X-Matter-Id" in request.headers:
            response.headers["X-Matter-Id"] = request.headers["X-Matter-Id"]
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error(
                getattr(request.state, "request_id", None),
                "VALIDATION_ERROR",
                "Request validation failed",
                {
                    "errors": [
                        {
                            "loc": [str(part) for part in item["loc"]],
                            "msg": item["msg"],
                            "type": item["type"],
                        }
                        for item in exc.errors()
                    ]
                },
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        known_codes = {
            "REVIEW_NOT_FOUND",
            "NODE_NOT_FOUND",
            "NOT_KNOWN_AS_OF",
            "DOC_NOT_FOUND",
            "JOB_NOT_FOUND",
        }
        code = (
            exc.detail
            if isinstance(exc.detail, str) and exc.detail in known_codes
            else "VALIDATION_ERROR"
        )
        message = {
            "NODE_NOT_FOUND": "Graph node was not found",
            "NOT_KNOWN_AS_OF": "Graph node was not known as of the requested date",
            "DOC_NOT_FOUND": "Document was not found",
            "JOB_NOT_FOUND": "Extraction job was not found",
        }.get(code, "Request failed")
        return JSONResponse(
            status_code=exc.status_code,
            content=error(getattr(request.state, "request_id", None), code, message),
        )


def require_object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    """Validate a small response payload shape in the no-gba_contracts fallback."""
    if not isinstance(value, dict) or not keys.issubset(value):
        raise ValueError(f"{label} is missing required fields: {sorted(keys)}")
    return value
