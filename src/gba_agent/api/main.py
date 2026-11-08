"""Track A FastAPI application."""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse

from gba_agent import CONTRACT_VERSION, __version__
from gba_agent.api.auth import authenticate
from gba_agent.api.errors import install_error_handlers, make_error
from gba_agent.api.routes_guards import router as guards_router
from gba_agent.api.routes_orch import make_runner
from gba_agent.api.routes_orch import router as orchestrator_router
from gba_agent.api.trace import TraceContext, new_ulid
from gba_agent.config import get_settings
from gba_agent.db.session import create_database_engine, create_session_factory, verify_database

CONTRACT_VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Validate configuration and database readiness before serving."""
    settings = get_settings()
    engine = create_database_engine(settings)
    try:
        verify_database(engine)
        application.state.database_engine = engine
        runner = make_runner(application.state, settings, create_session_factory(engine))
        application.state.orchestrator = runner
        application.state.handoffs = runner.handoffs
        runner.recover()
        try:
            yield
        finally:
            await runner.shutdown()
    finally:
        engine.dispose()


app = FastAPI(
    title="Guarded Browsing Agent",
    version=__version__,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
install_error_handlers(app)
app.include_router(orchestrator_router)
app.include_router(guards_router)


@app.middleware("http")
async def contract_and_auth_middleware(request: Request, call_next: Any) -> Any:
    """Enforce the peer contract version and bearer auth, and attach trace headers."""
    request_id = request.headers.get("X-Request-Id") or new_ulid()
    if not re.fullmatch(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$", request_id):
        response = JSONResponse(
            status_code=422,
            content=make_error(
                code="VALIDATION_ERROR",
                message="X-Request-Id must be a ULID",
                request_id=request_id,
                details={"header": "X-Request-Id"},
            ),
        )
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Contract-Version"] = CONTRACT_VERSION
        return response
    run_id = request.headers.get("X-Run-Id") or f"run_{new_ulid()}"
    if run_id is not None and not re.fullmatch(r"^run_[0-7][0-9A-HJKMNP-TV-Z]{25}$", run_id):
        response = JSONResponse(
            status_code=422,
            content=make_error(
                code="VALIDATION_ERROR",
                message="X-Run-Id must use the run_ ULID format",
                request_id=request_id,
                details={"header": "X-Run-Id"},
            ),
        )
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Contract-Version"] = CONTRACT_VERSION
        return response
    request.state.trace = TraceContext(
        request_id=request_id,
        run_id=run_id,
        matter_id=request.headers.get("X-Matter-Id"),
        contract_version=CONTRACT_VERSION,
    )

    if request.url.path != "/health":
        supplied_version = request.headers.get("X-Contract-Version")
        if supplied_version is None:
            response = JSONResponse(
                status_code=422,
                content=make_error(
                    code="VALIDATION_ERROR",
                    message="X-Contract-Version header is required",
                    request_id=request_id,
                    details={"header": "X-Contract-Version"},
                ),
            )
        else:
            supplied_match = CONTRACT_VERSION_PATTERN.fullmatch(supplied_version)
            current_match = CONTRACT_VERSION_PATTERN.fullmatch(CONTRACT_VERSION)
            if supplied_match is None:
                response = JSONResponse(
                    status_code=422,
                    content=make_error(
                        code="VALIDATION_ERROR",
                        message="X-Contract-Version must be a semantic version",
                        request_id=request_id,
                        details={"header": "X-Contract-Version"},
                    ),
                )
            elif current_match is not None and supplied_match.group(1) != current_match.group(1):
                response = JSONResponse(
                    status_code=400,
                    content=make_error(
                        code="CONTRACT_VERSION_MISMATCH",
                        message="Peer contract major version is not supported",
                        request_id=request_id,
                        details={"expected": CONTRACT_VERSION, "received": supplied_version},
                    ),
                )
            else:
                auth_error = authenticate(
                    request.headers.get("Authorization"),
                    get_settings(),
                    request_id=request_id,
                )
                if auth_error is not None:
                    response = JSONResponse(
                        status_code=401,
                        content=auth_error,
                    )
                else:
                    response = await call_next(request)
    else:
        response = await call_next(request)

    response.headers["X-Request-Id"] = request_id
    response.headers["X-Contract-Version"] = CONTRACT_VERSION
    trace = request.state.trace
    if trace.run_id:
        response.headers["X-Run-Id"] = trace.run_id
    if trace.matter_id:
        response.headers["X-Matter-Id"] = trace.matter_id
    return response


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    """Return the shared unauthenticated service health shape."""
    return {
        "status": "ok",
        "service": "agent-api",
        "version": __version__,
        "contract_version": CONTRACT_VERSION,
    }


@app.get("/openapi.json", include_in_schema=False)
def openapi_json() -> dict[str, Any]:
    """Serve OpenAPI only to authenticated, contract-compatible peers."""
    return app.openapi()


@app.get("/docs", include_in_schema=False)
def swagger_docs() -> Any:
    """Serve Swagger UI; the OpenAPI document remains authenticated."""
    return get_swagger_ui_html(openapi_url="/openapi.json", title=app.title + " - Swagger UI")
