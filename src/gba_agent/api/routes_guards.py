"""Synchronous claim verification and deterministic browser action classification."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Protocol

import yaml  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, Request
from pydantic import SecretStr, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import State

from gba_agent.access._registry import load_registry
from gba_agent.access.fetcher import BlobStore
from gba_agent.api.contracts_compat import (
    ActionDecision,
    ClassifyActionReq,
    VerifyClaimsReq,
    VerifyClaimsResp,
    _CompatClaim,
    _CompatGuardReport,
    _CompatGuardResult,
)
from gba_agent.api.deps import graph_client_dependency
from gba_agent.api.errors import APIError
from gba_agent.api.trace import trace_from_request
from gba_agent.clients.graph_client import GraphClient
from gba_agent.clients.llm_contract import LLMClient
from gba_agent.clients.replay import replay_only
from gba_agent.config import Settings, get_settings
from gba_agent.db.models import Claim as ClaimRow
from gba_agent.db.repositories import ClaimsRepository, GuardReportsRepository, RepositoryConflict
from gba_agent.db.session import create_session_factory, session_scope
from gba_agent.guards.abstention import Calibrator
from gba_agent.guards.action_guard import classify
from gba_agent.guards.claim_verifier import ClaimVerifier
from gba_agent.guards.gnn_alert import GNNAlert
from gba_agent.guards.pipeline import DEFAULT_GUARDS, LADDER_GUARDS, GuardPipeline
from gba_agent.guards.scope_guard import ScopeEvaluator, ScopeGuard
from gba_agent.guards.source_gate import SourceGate
from gba_agent.guards.support_judge import NLIPredictor
from gba_agent.guards.temporal_guard import PredicateEvaluator, TemporalGuard
from gba_agent.trajectory import factory as trajectory_factory
from gba_agent.trajectory import register_secret

router = APIRouter(prefix="/guards/v1", tags=["guards"])


class _Trajectory(Protocol):
    def emit(
        self,
        *,
        component: str,
        type: str,
        payload: dict[str, Any],
        matter_id: str,
        parent_event_id: str | None = None,
        session_id: str | None = None,
    ) -> object: ...


type _TrajectoryFactory = Callable[[str, str], _Trajectory]


def _trajectory_factory(state: State) -> _TrajectoryFactory:
    factory: _TrajectoryFactory | None = getattr(state, "guard_trajectory", None)
    if factory is not None:
        return factory
    cached: _TrajectoryFactory | None = getattr(state, "trajectory_factory", None)
    if cached is None:
        settings = get_settings()
        for field in type(settings).model_fields:
            value = getattr(settings, field)
            if isinstance(value, SecretStr):
                register_secret(value.get_secret_value())
        create = trajectory_factory(settings.traj_dir)
        writers: dict[tuple[str, str], _Trajectory] = {}

        def cached_factory(service: str, run_id: str) -> _Trajectory:
            key = service, run_id
            if key not in writers:
                writers[key] = create(service, run_id)
            return writers[key]

        cached = cached_factory
        state.trajectory_factory = cached
    return cached


def _blobs(state: State, settings: Settings) -> BlobStore:
    store: BlobStore | None = getattr(state, "guard_blobs", None)
    if store is not None:
        return store
    if settings.blob_url.startswith("file://"):
        try:
            from gba_contracts.blobstore import FileBlobStore  # type: ignore[import-not-found]
        except ImportError:
            pass
        else:
            local: BlobStore = FileBlobStore(Path(settings.blob_url.removeprefix("file://")))
            return local
    raise APIError(503, "UPSTREAM_UNAVAILABLE", "Shared blob store is unavailable")


def _public_result(result: _CompatGuardResult) -> _CompatGuardResult:
    if "human_only" not in result.evidence:
        return result
    return result.model_copy(
        update={
            "evidence": {
                key: value for key, value in result.evidence.items() if key != "human_only"
            }
        }
    )


def _persist(
    database: sessionmaker[Session],
    claims: list[_CompatClaim],
    reports: list[_CompatGuardReport],
) -> None:
    ordered = sorted(zip(claims, reports, strict=True), key=lambda pair: pair[0].claim_id)
    with session_scope(database) as session:
        claim_rows, report_rows = ClaimsRepository(session), GuardReportsRepository(session)
        for claim, report in ordered:
            data = claim.model_dump(mode="json")
            stored = session.get(ClaimRow, claim.claim_id, with_for_update=True)
            if stored is None:
                claim_rows.add(ClaimRow(claim_id=claim.claim_id, claim=data))
                stored = session.get(ClaimRow, claim.claim_id, with_for_update=True)
            if stored is None or stored.claim != data:
                raise RepositoryConflict("Claim ID already identifies a different claim")
            report_rows.save_latest(claim.claim_id, report.model_dump(mode="json"))
            session.flush()


def _write_events(writer: _Trajectory, matter_id: str, reports: list[_CompatGuardReport]) -> None:
    for report in reports:
        for result in report.results:
            writer.emit(
                component="gnn" if result.guard == "gnn_alert" else result.guard,
                type="guard_result",
                matter_id=matter_id,
                payload={"claim_id": report.claim_id, "result": result.model_dump(mode="json")},
            )
        if report.abstain_reason is not None:
            writer.emit(
                component="abstention",
                type="abstain",
                matter_id=matter_id,
                payload={
                    "item": report.claim_id,
                    "reason_code": report.abstain_reason,
                    "reason_text": next(
                        (
                            result.reason_text
                            for result in report.results
                            if result.reason_code == report.abstain_reason
                        ),
                        "Calibrated confidence was below the configured threshold.",
                    ),
                },
            )


def _pipeline(
    request: Request, graph: GraphClient, database: sessionmaker[Session], settings: Settings
) -> GuardPipeline:
    try:
        calibrator = Calibrator.load(settings.abstain_calibrator_path)
        registry = load_registry(settings.registry_path)
    except (OSError, ValidationError, yaml.YAMLError):
        raise APIError(500, "INTERNAL_ERROR", "Guard configuration is invalid") from None
    store = _blobs(request.app.state, settings)
    llm: LLMClient | None = getattr(request.app.state, "guard_llm", None)
    if llm is not None:
        llm = replay_only(llm, settings)
    nli: NLIPredictor | None = getattr(request.app.state, "guard_nli", None)
    verifier = (
        ClaimVerifier(store, support_min_score=settings.support_min_score)
        if llm is None and settings.support_judge in {"llm", "both"}
        else ClaimVerifier.configured(store, settings, client=llm, nli_predictor=nli)
    )
    predicates: PredicateEvaluator | None = getattr(request.app.state, "guard_predicates", None)
    scope_eval: ScopeEvaluator | None = getattr(request.app.state, "guard_scope", None)
    scope = ScopeGuard(graph) if scope_eval is None else ScopeGuard(graph, evaluate=scope_eval)
    gnn = GNNAlert(graph)

    async def verify_claim(claim: _CompatClaim, _: object) -> _CompatGuardResult:
        return await verifier.verify(claim)

    return GuardPipeline(
        source_gate=SourceGate(
            registry, store, graph, database, environment=settings.gba_env
        ).check_claim,
        claim_verifier=verify_claim,
        temporal=TemporalGuard(graph, evaluate=predicates).check_claim,
        scope=scope.check_claim,
        gnn_alert=lambda claim, matter: gnn.check(claim, matter.as_of),
        calibrator=calibrator,
        min_confidence=settings.abstain_min_confidence,
    )


@router.post("/verify/claims", response_model=VerifyClaimsResp)
async def verify_claims(
    body: VerifyClaimsReq,
    request: Request,
    graph: Annotated[GraphClient, Depends(graph_client_dependency)],
) -> VerifyClaimsResp:
    """Verify and persist a batch without creating an orchestrator run."""
    settings = get_settings()
    enabled = DEFAULT_GUARDS if body.guards_enabled is None else frozenset(body.guards_enabled)
    if unknown := enabled - LADDER_GUARDS:
        raise APIError(
            422, "VALIDATION_ERROR", "Unknown guard selection", {"guards": sorted(unknown)}
        )
    if not enabled and not settings.allow_unregistered_fetch_for_baseline:
        raise APIError(422, "VALIDATION_ERROR", "Raw baseline mode is disabled")
    matter = body.matter
    if request.headers.get("X-Matter-Id", matter.matter_id) != matter.matter_id:
        raise APIError(422, "VALIDATION_ERROR", "Matter tracing header does not match the request")
    for claim in body.claims:
        if claim.matter_id != matter.matter_id or claim.as_of > matter.as_of:
            raise APIError(
                422, "VALIDATION_ERROR", "Claim does not match the matter or its as-of date"
            )
    if not body.claims:
        return VerifyClaimsResp(reports=[])
    engine = getattr(request.app.state, "database_engine", None)
    if engine is None:
        raise APIError(503, "UPSTREAM_UNAVAILABLE", "Guard report database is unavailable")
    database = create_session_factory(engine)
    writer_factory = _trajectory_factory(request.app.state)
    pipeline = _pipeline(request, graph, database, settings)
    reports = [await pipeline.verify(claim, matter, enabled) for claim in body.claims]
    public = [
        report.model_copy(update={"results": [_public_result(result) for result in report.results]})
        for report in reports
    ]
    try:
        await run_in_threadpool(_persist, database, body.claims, reports)
    except RepositoryConflict:
        raise APIError(
            422, "VALIDATION_ERROR", "Claim ID already identifies a different claim"
        ) from None
    except SQLAlchemyError:
        raise APIError(
            503, "UPSTREAM_UNAVAILABLE", "Guard reports could not be persisted"
        ) from None
    try:
        writer = writer_factory("agent-api", request.headers.get("X-Run-Id", "adhoc"))
        _write_events(writer, matter.matter_id, public)
    except (OSError, ValueError):
        raise APIError(
            503, "UPSTREAM_UNAVAILABLE", "Guard trajectory could not be persisted"
        ) from None
    return VerifyClaimsResp(reports=public)


def _read_yaml(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise APIError(
            status_code=500,
            code="INTERNAL_ERROR",
            message="Site policy configuration could not be read",
        ) from exc
    except yaml.YAMLError as exc:
        raise APIError(
            status_code=500,
            code="INTERNAL_ERROR",
            message="Site policy configuration is invalid",
        ) from exc


def _site_context(site_id: str) -> tuple[object | None, object]:
    registry_path = get_settings().registry_path
    if not registry_path.is_file():
        return None, {}
    if not site_id or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for character in site_id
    ):
        return None, {}
    registry = _read_yaml(registry_path)
    if not isinstance(registry, dict) or not isinstance(registry.get("entries"), list):
        raise APIError(
            status_code=500,
            code="INTERNAL_ERROR",
            message="Site registry has an invalid structure",
        )
    matching_entries = [
        entry
        for entry in registry["entries"]
        if isinstance(entry, dict) and entry.get("entry_id") == site_id
    ]
    if len(matching_entries) != 1:
        return None, {}
    site_entry = matching_entries[0]

    site_config_path = registry_path.parent / "sites" / f"{site_id}.yaml"
    if not site_config_path.is_file():
        return site_entry, {}
    site_config = _read_yaml(site_config_path)
    if site_config is None:
        return site_entry, {}
    if not isinstance(site_config, dict):
        raise APIError(
            status_code=500,
            code="INTERNAL_ERROR",
            message="Site policy configuration has an invalid structure",
        )
    return site_entry, site_config


@router.post("/action/classify", response_model=ActionDecision)
async def classify_action(body: ClassifyActionReq, request: Request) -> ActionDecision:
    """Return a deterministic Action Guard decision for one browser action."""
    site_entry, site_config = _site_context(body.action.site_id)
    decision = classify(body.action, site_entry, site_config, None)
    trace = trace_from_request(request)
    from gba_agent.operator.perception import public_url

    writer = _trajectory_factory(request.app.state)("agent-api", trace.run_id or "adhoc")
    writer.emit(
        component="action_guard",
        type="action_proposed",
        matter_id=trace.matter_id or "",
        payload={
            "action": {
                **body.action.model_dump(mode="json"),
                "value": None,
                "target": None,
                "page_url": public_url(body.action.page_url),
            }
        },
    )
    writer.emit(
        component="action_guard",
        type="action_decided",
        matter_id=trace.matter_id or "",
        payload={"decision": decision.model_dump(mode="json")},
    )
    return decision
