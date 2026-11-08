"""Authenticated Doc 00 orchestrator routes and per-run service composition."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from threading import RLock
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request, Response
from sqlalchemy.orm import Session, sessionmaker
from starlette.datastructures import State

from gba_agent.access._registry import load_registry
from gba_agent.access.fetcher import Fetcher
from gba_agent.access.handoff import HandoffManager, HandoffStatus
from gba_agent.adapters.base import AdapterContext, AdapterRegistry
from gba_agent.adapters.courtlistener import CourtListenerAdapter
from gba_agent.api.contracts_compat import (
    CreateRunReq,
    CreateRunResp,
    Dossier,
    HandoffRequest,
    ResolveHandoffReq,
    RunStatus,
    _CompatClaim,
    _CompatGuardResult,
)
from gba_agent.api.errors import APIError
from gba_agent.api.routes_guards import _blobs, _trajectory_factory
from gba_agent.api.trace import TraceContext, trace_from_request
from gba_agent.clients.courtlistener_client import CourtListenerClient
from gba_agent.clients.graph_client import GraphClient
from gba_agent.clients.llm_contract import LLMClient
from gba_agent.clients.peer_modes import peer_options, peer_url
from gba_agent.clients.replay import replay_only
from gba_agent.clients.workbench_client import WorkbenchClient
from gba_agent.config import Settings
from gba_agent.guards.claim_verifier import ClaimVerifier
from gba_agent.guards.gnn_alert import GNNAlert
from gba_agent.guards.pipeline import GuardPipeline
from gba_agent.guards.scope_guard import ScopeGuard
from gba_agent.guards.source_gate import SourceGate
from gba_agent.guards.temporal_guard import TemporalGuard
from gba_agent.operator.browser import BrowserRuntime
from gba_agent.orchestrator.matter_store import MatterStore
from gba_agent.orchestrator.runner import (
    EventReader,
    Execution,
    RunRunner,
    RunServices,
    ServiceFactory,
)

if TYPE_CHECKING:
    from gba_agent.api.contracts_compat import _CompatDossier as DossierModel
    from gba_agent.api.contracts_compat import _CompatHandoffRequest as HandoffModel
else:
    DossierModel = Dossier
    HandoffModel = HandoffRequest

router = APIRouter(prefix="/orch/v1", tags=["orchestrator"])


def _read_shared_events(run_id: str) -> Iterable[dict[str, Any] | str]:
    try:
        from gba_contracts.trajectory import merge, verify_chain  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        if exc.name not in {"gba_contracts", "gba_contracts.trajectory"}:
            raise
        from gba_agent.config import get_settings
        from gba_agent.trajectory import merge as local_merge

        yield from local_merge(get_settings().traj_dir, run_id)
        return
    from gba_agent.config import get_settings

    for service in ("agent-api", "graph-api", "workbench-api"):
        path = get_settings().traj_dir / run_id / f"{service}.jsonl"
        if path.exists() and verify_chain(path) is not None:
            raise APIError(503, "UPSTREAM_UNAVAILABLE", "Trajectory integrity check failed")
    merged = merge(run_id)
    if isinstance(merged, str):
        yield from merged.splitlines()
        return
    for event in merged:
        yield event if isinstance(event, (str, dict)) else event.model_dump(mode="json")


def make_runner(state: State, settings: Settings, database: sessionmaker[Session]) -> RunRunner:
    writer_lock = RLock()
    parents: dict[str, str] = {}

    def emit(
        run_id: str, matter_id: str, component: str, event: str, payload: dict[str, object]
    ) -> None:
        with writer_lock:
            writer = _trajectory_factory(state)("agent-api", run_id)
            session_id = payload.get("session_id")
            recorded = writer.emit(
                component=component,
                type=event,
                payload={
                    **payload,
                    "trajectory_backend": getattr(writer, "backend", "gba_contracts"),
                },
                matter_id=matter_id,
                parent_event_id=parents.get(run_id),
                session_id=session_id if isinstance(session_id, str) else None,
            )
            if event == "state_change" and "operation" not in payload:
                event_id = (
                    recorded.get("event_id")
                    if isinstance(recorded, dict)
                    else getattr(recorded, "event_id", None)
                )
                if isinstance(event_id, str):
                    parents[run_id] = event_id

    @asynccontextmanager
    async def service_factory(ctx: Execution) -> AsyncIterator[RunServices]:
        llm: LLMClient | None = getattr(state, "orch_llm", getattr(state, "guard_llm", None))
        llm = replay_only(llm, settings)
        if llm is None:
            raise APIError(503, "UPSTREAM_UNAVAILABLE", "Shared planner LLM is unavailable")
        shared_llm = ctx.llm(llm)
        store = _blobs(state, settings)
        registry = load_registry(settings.registry_path)
        config = ctx.request.config
        trace = TraceContext(ctx.request_id, ctx.run_id, ctx.request.matter.matter_id, "1.0.0")
        token = settings.service_token.get_secret_value()
        async with AsyncExitStack() as stack:
            graph = await stack.enter_async_context(
                GraphClient(
                    peer_url(settings, "graph"),
                    token,
                    trace,
                    **peer_options(settings, "graph", config.fault_proxy_url),
                )
            )
            wb = (
                await stack.enter_async_context(
                    WorkbenchClient(
                        peer_url(settings, "workbench"),
                        token,
                        trace,
                        **peer_options(settings, "workbench", config.fault_proxy_url),
                    )
                )
                if config.use_workbench
                else None
            )
            browser = (
                await stack.enter_async_context(
                    BrowserRuntime.launch(
                        settings,
                        database,
                        registry,
                        llm=shared_llm,
                        handoffs=ctx.runner.handoffs,
                        run_config=config,
                        emit=ctx.access_event,
                        managed_wall_time=True,
                        executable_path=getattr(state, "orch_browser_executable", None),
                    )
                )
                if settings.session_enc_key is not None and not config.dry_run
                else None
            )
            fetcher = Fetcher(
                settings,
                database,
                store,
                registry=registry,
                browser=browser,
                sessions=browser.sessions if browser else None,
                handoffs=ctx.runner.handoffs,
                run_config=config,
                emit=ctx.access_event,
                login_sites=getattr(state, "orch_login_sites", None),
            )
            adapters = AdapterRegistry.defaults(CourtListenerAdapter(database))
            crosscheck = CourtListenerClient(
                AdapterContext(fetcher, ctx.run_id, ctx.request.matter.matter_id),
                database,
            )
            gate = SourceGate(
                registry,
                store,
                graph,
                database,
                environment=settings.gba_env,
                crosscheck=None if config.dry_run else crosscheck,
            )
            verifier = ClaimVerifier.configured(
                store,
                settings,
                client=shared_llm,
                nli_predictor=getattr(state, "guard_nli", None),
            )

            async def verify(claim: _CompatClaim, _: object) -> _CompatGuardResult:
                return await verifier.verify(claim)

            scope_evaluator = getattr(state, "guard_scope", None)
            scope = (
                ScopeGuard(graph)
                if scope_evaluator is None
                else ScopeGuard(graph, evaluate=scope_evaluator)
            )
            gnn = GNNAlert(graph)
            from gba_agent.guards.abstention import Calibrator

            guards = GuardPipeline(
                source_gate=gate.check_claim,
                claim_verifier=verify,
                temporal=TemporalGuard(
                    graph, evaluate=getattr(state, "guard_predicates", None)
                ).check_claim,
                scope=scope.check_claim,
                gnn_alert=lambda claim, matter: gnn.check(claim, matter.as_of),
                calibrator=Calibrator.load(settings.abstain_calibrator_path),
                min_confidence=settings.abstain_min_confidence,
            )
            yield RunServices(graph, store, fetcher, adapters, gate, guards, shared_llm, wb)

    services: ServiceFactory = getattr(state, "orch_services", service_factory)
    writer = getattr(state, "orch_emit", emit)
    reader: EventReader = getattr(state, "orch_read_events", _read_shared_events)
    return RunRunner(settings, MatterStore(database), services, writer, reader)


def _runner(request: Request) -> RunRunner:
    runner: RunRunner | None = getattr(request.app.state, "orchestrator", None)
    if runner is None:
        raise APIError(503, "UPSTREAM_UNAVAILABLE", "Orchestrator is unavailable")
    return runner


@router.post("/runs", status_code=202, response_model=CreateRunResp)
async def create_run(body: CreateRunReq, request: Request) -> CreateRunResp:
    trace = trace_from_request(request)
    if trace.matter_id is not None and trace.matter_id != body.matter.matter_id:
        raise APIError(422, "VALIDATION_ERROR", "Matter tracing header does not match the request")
    run_id = await _runner(request).create(
        body, request.headers.get("Idempotency-Key"), trace.request_id
    )
    request.state.trace = TraceContext(
        trace.request_id, run_id, body.matter.matter_id, trace.contract_version
    )
    return CreateRunResp(run_id=run_id)


@router.get("/runs/{run_id}", response_model=RunStatus)
async def get_run(run_id: str, request: Request) -> RunStatus:
    return _runner(request).status(run_id)


@router.post("/runs/{run_id}/cancel", response_model=RunStatus)
async def cancel_run(run_id: str, request: Request) -> RunStatus:
    return await _runner(request).cancel(run_id)


@router.get("/runs/{run_id}/dossier", response_model=DossierModel)
async def get_dossier(run_id: str, request: Request) -> dict[str, Any]:
    return _runner(request).dossier(run_id)


@router.get(
    "/runs/{run_id}/trajectory",
    response_class=Response,
    responses={200: {"content": {"application/x-ndjson": {"schema": {"type": "string"}}}}},
)
async def get_trajectory(run_id: str, request: Request) -> Response:
    return Response(_runner(request).trajectory(run_id), media_type="application/x-ndjson")


@router.get("/handoffs")
async def list_handoffs(
    request: Request,
    status: HandoffStatus = "open",
) -> dict[str, list[HandoffModel]]:
    manager: HandoffManager = request.app.state.handoffs
    return {"items": manager.list(status)}


@router.post("/handoffs/{handoff_id}/resolve", response_model=HandoffRequest)
async def resolve_handoff(
    handoff_id: str,
    body: ResolveHandoffReq,
    request: Request,
) -> dict[str, Any]:
    manager: HandoffManager = request.app.state.handoffs
    return manager.resolve(handoff_id, body.resolved_by, body.outcome).model_dump(mode="json")
