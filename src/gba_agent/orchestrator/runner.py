"""Asynchronous Track A run lifecycle and bounded research rounds."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from gba_agent.access.fetcher import BlobStore, Fetcher
from gba_agent.access.handoff import HandoffManager
from gba_agent.adapters.base import AdapterRegistry
from gba_agent.api.contracts_compat import (
    CreateRunReq,
    Dossier,
    MatterFactModel,
    RunStatus,
    _CompatCondition,
    _CompatGuardResult,
    _CompatStandard,
)
from gba_agent.api.errors import APIError
from gba_agent.api.trace import new_ulid
from gba_agent.clients.graph_client import GraphClient
from gba_agent.clients.llm_contract import LLMCacheMiss, LLMClient
from gba_agent.clients.workbench_client import WorkbenchClient
from gba_agent.config import Settings
from gba_agent.guards.pipeline import LADDER_GUARDS, GuardPipeline
from gba_agent.guards.source_gate import SourceGate
from gba_agent.orchestrator.dossier import UNKNOWN_STANDARD, build, required_facts
from gba_agent.orchestrator.matter_store import MatterStore, fact_value, merge_fact, validate_matter
from gba_agent.orchestrator.metrics import RunMetrics
from gba_agent.orchestrator.planner import BudgetedLLM, Planner
from gba_agent.orchestrator.state import Budget, BudgetExceeded, Step
from gba_agent.orchestrator.tools import GRAPH_ERRORS, PlannerTools

logger = logging.getLogger(__name__)
type EventWriter = Callable[[str, str, str, str, dict[str, object]], None]
type EventReader = Callable[[str], Iterable[dict[str, Any] | str]]


@dataclass
class RunServices:
    graph: GraphClient
    blobs: BlobStore
    fetcher: Fetcher
    adapters: AdapterRegistry
    source_gate: SourceGate
    guards: GuardPipeline
    llm: LLMClient
    workbench: WorkbenchClient | None = None


@dataclass
class Execution:
    runner: RunRunner
    run_id: str
    request: CreateRunReq
    request_id: str
    budget: Budget
    notices: list[dict[str, str]] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    standard: dict[str, Any] = field(default_factory=lambda: UNKNOWN_STANDARD.copy())
    conditions: list[dict[str, Any]] = field(default_factory=list)
    standard_facts: dict[str, Any] | None = None
    step: Step = Step.INTAKE
    step_id: str = field(default_factory=new_ulid)
    metrics: RunMetrics = field(default_factory=RunMetrics)

    def emit(self, component: str, event: str, payload: dict[str, object]) -> None:
        self.metrics.observe(event, payload)
        self.runner.emit(
            self.run_id,
            self.request.matter.matter_id,
            component,
            event,
            {"step": self.step, "step_id": self.step_id,
             "peer_mode": self.runner.settings.peer_mode, **payload},
        )

    def notice(self, item: str, code: str, reason: str) -> None:
        payload = {"item": item, "reason_code": code, "reason_text": reason}
        if payload not in self.notices:
            self.notices.append(payload)
            self.emit("orchestrator", "abstain", dict(payload))

    def transition(self, step: Step, **extra: object) -> None:
        previous = self.runner.store.transition(self.run_id, step)
        if previous is None:
            raise asyncio.CancelledError
        self.step = step
        self.step_id = new_ulid()
        self.emit("orchestrator", "state_change", {"from": previous, "to": step, **extra})

    def usage(self) -> None:
        self.runner.store.usage(self.run_id, self.budget.steps, self.budget.cost)

    def llm(self, client: LLMClient) -> LLMClient:
        settings = self.runner.settings
        return BudgetedLLM(
            client,
            self.budget,
            self.usage,
            self.emit,
            {
                "planner": settings.llm_model_planner,
                "judge": settings.llm_model_judge,
                "vlm": settings.llm_model_vlm,
            },
        )

    def access_event(self, run_id: str, event: str, payload: dict[str, object]) -> None:
        if run_id != self.run_id:
            raise RuntimeError("Acquisition event was routed to another run")
        component = (
            "action_guard"
            if event == "action_decided"
            else "operator"
            if event.startswith("action_") or event in {"download", "llm_call"}
            else "access"
        )
        self.emit(component, event, payload)
        if event == "llm_call":
            self.budget.check_cost()


type ServiceFactory = Callable[[Execution], AbstractAsyncContextManager[RunServices]]


class RunRunner:
    def __init__(
        self,
        settings: Settings,
        store: MatterStore,
        services: ServiceFactory,
        emit: EventWriter,
        read_events: EventReader,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        facts_timeout: float = 600,
        draft_timeout: float = 1200,
        questions_path: Path = Path("config/facts_questions.yaml"),
    ) -> None:
        self.settings, self.store, self.services = settings, store, services
        self.emit, self.read_events, self.sleep = emit, read_events, sleep
        self.facts_timeout, self.draft_timeout = facts_timeout, draft_timeout
        self.questions_path = questions_path
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.executions: dict[str, Execution] = {}
        self.handoffs = HandoffManager(settings, store.database, emit=self._handoff_event)
        self._creation_lock = asyncio.Lock()

    async def create(self, body: CreateRunReq, key: str | None, request_id: str) -> str:
        validate_matter(body.matter)
        enabled = set(body.config.guards_enabled)
        if enabled - LADDER_GUARDS or (
            not enabled and not self.settings.allow_unregistered_fetch_for_baseline
        ):
            raise APIError(422, "VALIDATION_ERROR", "The requested guard ladder is unavailable")
        if key is not None and (not key.strip() or len(key) > 255):
            raise APIError(422, "VALIDATION_ERROR", "Invalid idempotency key")
        if body.config.fault_proxy_url is not None:
            try:
                proxy = urlsplit(body.config.fault_proxy_url)
                valid = (
                    proxy.scheme in {"http", "https"} and proxy.hostname and proxy.port is not None
                )
            except ValueError:
                valid = False
            if not valid:
                raise APIError(422, "VALIDATION_ERROR", "Invalid fault proxy URL")
        async with self._creation_lock:
            run_id, created = self.store.create(body, key, request_id)
            if created:
                self._schedule(run_id)
            return run_id

    def _schedule(self, run_id: str) -> None:
        task = asyncio.create_task(self._run(run_id), name=run_id)
        self.tasks[run_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(run_id, None))

    def status(self, run_id: str) -> RunStatus:
        row = self.store.row(run_id)
        return RunStatus.model_validate(
            {
                "run_id": run_id,
                "state": row.state,
                "step": row.step or Step.INTAKE,
                "steps_used": row.steps_used,
                "cost_usd": row.cost_usd,
                "open_handoffs": self.store.handoffs(run_id),
                "error": row.error,
            }
        )

    def dossier(self, run_id: str) -> dict[str, Any]:
        row = self.store.row(run_id)
        data = row.config.get("_dossier")
        if not isinstance(data, dict):
            raise APIError(404, "DOSSIER_NOT_READY", "Run dossier is not ready")
        return data

    def trajectory(self, run_id: str) -> str:
        self.store.row(run_id)
        return "".join(
            event.rstrip("\r\n") + "\n"
            if isinstance(event, str)
            else json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            for event in self.read_events(run_id)
        )

    async def cancel(self, run_id: str) -> RunStatus:
        self.store.row(run_id)
        changed = self._terminal(run_id, "cancelled")
        if changed and (task := self.tasks.get(run_id)) is not None:
            task.cancel()
        return self.status(run_id)

    def recover(self) -> None:
        failed, queued = self.store.recover()
        for run_id in failed:
            self._terminal(run_id, "failed", "RESTART")
        for run_id in queued:
            self._schedule(run_id)

    async def shutdown(self) -> None:
        active = list(self.tasks.items())
        for run_id, task in active:
            self._terminal(run_id, "failed", "RESTART")
            task.cancel()
        await asyncio.gather(*(task for _, task in active), return_exceptions=True)

    def _terminal(self, run_id: str, state: str, error: str | None = None) -> bool:
        previous = self.store.terminal(run_id, state, error)
        if previous is None:
            return False
        for handoff_id in self.store.handoffs(run_id):
            try:
                self.handoffs.resolve(handoff_id, "orchestrator", "abandoned")
            except (APIError, OSError, ValueError):
                logger.warning("Could not close a handoff for run %s", run_id)
        row = self.store.row(run_id)
        try:
            if error:
                self.emit(
                    run_id,
                    row.matter_id,
                    "orchestrator",
                    "error",
                    {
                        "where": previous,
                        "message": "Run stopped before completion.",
                        "reason_code": error,
                    },
                )
            execution = self.executions.get(run_id)
            self.emit(
                run_id,
                row.matter_id,
                "orchestrator",
                "state_change",
                {
                    "from": previous,
                    "to": row.step if state != "cancelled" else "cancelled",
                    **({"reason_code": error} if error else {}),
                    "metrics": execution.metrics.snapshot(state)
                    if execution
                    else {
                        "status": state,
                        "usage_status": "unavailable",
                        "elapsed_wall_seconds": None,
                    },
                },
            )
        except (APIError, OSError, ValueError):
            self.store.recording_failed(run_id)
            logger.error("Trajectory recording failed for run %s; run marked failed", run_id)
        return True

    def _handoff_event(self, run_id: str, event: str, payload: dict[str, object]) -> None:
        execution = self.executions.get(run_id)
        if execution is not None:
            if event == "handoff_requested":
                execution.budget.pause()
                execution.emit(
                    "orchestrator",
                    "state_change",
                    {
                        "from": "running",
                        "to": "waiting_handoff",
                    },
                )
            elif event == "handoff_resolved" and self.store.row(run_id).state == "running":
                execution.budget.resume()
                execution.emit(
                    "orchestrator",
                    "state_change",
                    {
                        "from": "waiting_handoff",
                        "to": "running",
                    },
                )
            elif event == "abstain" and payload.get("reason_code") == "HANDOFF_EXPIRED":
                execution.notice(
                    str(payload["item"]), "HANDOFF_EXPIRED", "The human handoff expired."
                )
                return
        self.emit(run_id, self.store.row(run_id).matter_id, "access", event, payload)

    async def _run(self, run_id: str) -> None:
        try:
            row = self.store.row(run_id)
            if row.state != "queued":
                return
            request = self.store.request(row)
            execution = Execution(
                self,
                run_id,
                request,
                str(row.config["_request_id"]),
                Budget(request.config, self.settings.llm_price_table_json),
            )
            self.executions[run_id] = execution
            execution.transition(Step.INTAKE)
            try:
                async with asyncio.timeout(None) as timer:
                    execution.budget.start(timer)
                    async with self.services(execution) as services:
                        services.graph.observe = lambda event, payload: execution.emit(
                            "orchestrator", event, payload
                        )
                        if services.workbench is not None:
                            services.workbench.observe = lambda event, payload: execution.emit(
                                "orchestrator", event, payload
                            )
                        await self._execute(execution, services, row.created_at)
            except (BudgetExceeded, TimeoutError):
                self._budget_dossier(execution, row.created_at)
            finally:
                execution.budget.timer = None
            if self._terminal(run_id, "done"):
                self._save_trajectory(run_id)
        except asyncio.CancelledError:
            self._terminal(run_id, "cancelled")
            raise
        except LLMCacheMiss:
            self._terminal(run_id, "failed", "UPSTREAM_UNAVAILABLE")
            logger.warning("Planner replay cache miss for run %s", run_id)
        except APIError as exc:
            self._terminal(run_id, "failed", exc.code)
        except Exception:
            self._terminal(run_id, "failed", "INTERNAL_ERROR")
            logger.error("Unexpected orchestrator failure for run %s", run_id)
        finally:
            self.executions.pop(run_id, None)

    def _save_trajectory(self, run_id: str) -> None:
        path = self.settings.traj_dir / run_id / "merged.jsonl"
        try:
            data = self.trajectory(run_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(data, encoding="utf-8")
            temporary.replace(path)
        except (APIError, OSError, ValueError):
            self.store.recording_failed(run_id)
            logger.error("Could not materialize merged trajectory for run %s", run_id)

    async def _execute(self, ctx: Execution, services: RunServices, created_at: datetime) -> None:
        request = ctx.request
        tools = PlannerTools(
            ctx.run_id,
            request.matter,
            self.store,
            services.blobs,
            services.graph,
            services.fetcher,
            services.adapters,
            services.source_gate,
            ctx.notice,
            request.config.dry_run,
            emit=ctx.emit,
        )
        planner = Planner(services.llm, request.matter, ctx.budget, ctx.usage, ctx.emit)
        if request.config.dry_run:
            ctx.notice("run", "DRY_RUN", "This run does not perform external acquisition.")
        ctx.transition(Step.FACTS)
        await self._facts(ctx, tools)
        ctx.transition(Step.PLAN)
        await self._standard(ctx, tools)
        candidates = await tools.call(
            "rule_candidates", {"query": request.matter.question[:1000], "top_k": 10}
        )
        inputs = {
            "facts": tools.facts(),
            "required_facts": required_facts(ctx.standard),
            "authorities": candidates.get("items", []),
            "approved_sites": services.fetcher.approved_sites()
            if not request.config.dry_run
            else [],
        }
        tasks = await planner.plan(inputs)
        await self._research(ctx, tools, planner, services.guards, {**inputs, "tasks": tasks})
        ctx.transition(Step.DOSSIER)
        await self._standard(ctx, tools)
        data = self._dossier(ctx, created_at)
        self.store.save_dossier(ctx.run_id, data)
        if request.config.use_workbench:
            ctx.transition(Step.DRAFT)
            data = await self._draft(ctx, services.workbench, data)
        self.store.save_dossier(ctx.run_id, data)

    def _budget_dossier(self, ctx: Execution, created_at: datetime) -> None:
        if ctx.step != Step.DOSSIER:
            ctx.transition(Step.DOSSIER)
        unfinished = [
            claim.claim_id for claim, report in self.store.claims(ctx.run_id) if report is None
        ]
        for item in unfinished or ["research"]:
            ctx.notice(item, "BUDGET_EXCEEDED", "The run budget was exhausted before completion.")
        self.store.save_dossier(ctx.run_id, self._dossier(ctx, created_at))

    async def _facts(self, ctx: Execution, tools: PlannerTools) -> None:
        accepted = []
        for doc_id in dict.fromkeys(tools.matter.source_doc_ids):
            try:
                if await tools.ensure_document(doc_id):
                    accepted.append(doc_id)
                    continue
            except (*GRAPH_ERRORS, APIError, OSError, KeyError, ValueError, TypeError):
                pass
            ctx.notice(
                doc_id,
                "SG_SOURCE_NOT_ADMITTED",
                "An input document was not admitted for extraction.",
            )
        if not accepted:
            return
        try:
            async with asyncio.timeout(self.facts_timeout):
                job = await tools.graph.extract_facts(
                    accepted,
                    tools.matter.as_of,
                    idempotency_key=f"{ctx.run_id}:facts",
                )
                while True:
                    status = await tools.graph.job_status(job["job_id"])
                    if status["status"] == "done":
                        for value in status.get("result") or []:
                            self._merge_extracted(tools, value, set(accepted))
                        self.store.save_matter(ctx.run_id, tools.matter)
                        return
                    if status["status"] not in {"queued", "running"}:
                        break
                    await self.sleep(5)
        except (*GRAPH_ERRORS, KeyError, TypeError, TimeoutError):
            pass
        ctx.notice("facts", "UPSTREAM_UNAVAILABLE", "Graph fact extraction did not complete.")

    @staticmethod
    def _merge_extracted(tools: PlannerTools, value: object, accepted: set[str]) -> None:
        try:
            fact = MatterFactModel.model_validate(value)
            if (
                fact.status != "known"
                or not fact.spans
                or (fact.reported_date is not None and fact.reported_date > tools.matter.as_of)
            ):
                return
            if any(
                span.doc_id not in accepted or not tools.valid_span(span) for span in fact.spans
            ):
                return
            fact.value = fact_value(fact.key, fact.value)
            fact.extracted_by = "llm_extracted"
            merge_fact(tools.matter, fact)
        except (ValueError, TypeError):
            return

    async def _standard(self, ctx: Execution, tools: PlannerTools) -> None:
        facts = tools.facts()
        ctx.standard, ctx.conditions = UNKNOWN_STANDARD.copy(), []
        ctx.standard_facts = facts
        try:
            response = await tools.graph.standard_of_review(tools.matter.as_of, facts)
            standard, conditions = response["standard"], response["conditions"]
            # Validate the normative shapes before using them as prompts or publishing them.
            ctx.standard = _CompatStandard.model_validate(standard).model_dump(mode="json")
            ctx.conditions = [
                _CompatCondition.model_validate(item).model_dump(mode="json") for item in conditions
            ]
        except (*GRAPH_ERRORS, TypeError, KeyError):
            ctx.standard, ctx.conditions = UNKNOWN_STANDARD.copy(), []
            ctx.notice(
                "standard_of_review",
                "UPSTREAM_UNAVAILABLE",
                "Graph could not establish the standard of review.",
            )

    async def _research(
        self,
        ctx: Execution,
        tools: PlannerTools,
        planner: Planner,
        guards: GuardPipeline,
        inputs: dict[str, Any],
    ) -> None:
        def record_guard(claim_id: str, result: _CompatGuardResult) -> None:
            data = result.model_dump(mode="json")
            data["evidence"].pop("human_only", None)
            ctx.emit(
                "gnn" if result.guard == "gnn_alert" else result.guard,
                "guard_result",
                {"claim_id": claim_id, "result": data},
            )

        guards.observe = record_guard
        feedback_count: dict[tuple[str | None, str | None], int] = {}
        feedback_seen: set[str] = set()
        for round_number in range(3):
            ctx.transition(
                Step.ACQUIRE,
                round=round_number,
                **({"tasks": inputs["tasks"]} if round_number == 0 else {}),
            )

            def propose() -> None:
                if ctx.step == Step.ACQUIRE:
                    ctx.transition(Step.PROPOSE)

            await planner.research(tools, inputs, propose)
            propose()
            ctx.transition(Step.VERIFY)
            for claim, report in self.store.claims(ctx.run_id):
                if report is not None:
                    continue
                report = await guards.verify(
                    claim, tools.matter, set(ctx.request.config.guards_enabled)
                )
                self.store.save_report(report)
                if (
                    report.final == "pass"
                    and claim.authority_node_id
                    and claim.authority_node_id not in ctx.labels
                ):
                    try:
                        node = await tools.graph.get_node(claim.authority_node_id, claim.as_of)
                        if node.get("node_id") == claim.authority_node_id and isinstance(
                            node.get("label"), str
                        ):
                            ctx.labels[claim.authority_node_id] = node["label"]
                    except GRAPH_ERRORS:
                        pass
                ctx.budget.check_cost()
            ctx.transition(Step.GAP)
            if round_number == 2:
                break
            claims = self.store.claims(ctx.run_id)
            flags = []
            for claim, report in claims:
                lineage = (claim.authority_node_id, claim.element_node_id)
                if (
                    report
                    and any(result.verdict == "flag" for result in report.results)
                    and claim.claim_id not in feedback_seen
                    and feedback_count.get(lineage, 0) < self.settings.max_gnn_loops
                ):
                    flags.append(
                        {
                            "claim": claim.model_dump(mode="json"),
                            "reason_code": "GNN_LOW_PLAUSIBILITY",
                        }
                    )
                    feedback_seen.add(claim.claim_id)
                    feedback_count[lineage] = feedback_count.get(lineage, 0) + 1
            hints = await self._gap(ctx, tools, claims)
            if not hints and not flags:
                break
            inputs = {
                "facts": tools.facts(),
                "search_hints": hints,
                "reverify": flags,
                "required_facts": required_facts(ctx.standard),
                "approved_sites": inputs["approved_sites"],
            }

    async def _gap(
        self, ctx: Execution, tools: PlannerTools, claims: list[Any]
    ) -> list[dict[str, Any]]:
        if not {"gnn", "gnn_alert"}.intersection(ctx.request.config.guards_enabled):
            return []
        anchors = {claim.element_node_id for claim, _ in claims if claim.element_node_id}
        if ctx.standard["value"] != "unknown":
            anchors.add(f"Standard:{ctx.standard['value']}")
        if not anchors:
            return []
        excludes = {claim.authority_node_id for claim, _ in claims if claim.authority_node_id}
        try:
            result = await tools.graph.predict_missing(
                tools.matter.as_of,
                sorted(anchors),
                exclude_node_ids=sorted(excludes),
                top_k=5,
            )
            return [
                {"node_id": item["node_id"], "score": item["score"]}
                for item in result["items"]
                if item["node_id"] not in excludes
            ][:5]
        except (*GRAPH_ERRORS, KeyError, TypeError):
            ctx.notice("gap", "GNN_UNAVAILABLE", "Graph search hints were unavailable.")
            return []

    def _dossier(self, ctx: Execution, created_at: datetime) -> dict[str, Any]:
        matter = ctx.request.matter
        facts = {key: fact.model_dump(mode="json") for key, fact in matter.facts.items()}
        current = ctx.standard_facts == facts
        return build(
            ctx.run_id,
            matter,
            self.store,
            created_at.replace(tzinfo=created_at.tzinfo or UTC),
            ctx.standard if current else UNKNOWN_STANDARD.copy(),
            ctx.conditions if current else [],
            ctx.notices,
            ctx.labels,
            self.settings.traj_dir / ctx.run_id / "merged.jsonl",
            self.questions_path,
        )

    async def _draft(
        self, ctx: Execution, client: WorkbenchClient | None, dossier: dict[str, Any]
    ) -> dict[str, Any]:
        request = ctx.request
        if client and request.draft_doc_id and request.playbook_id and not request.config.dry_run:
            try:
                async with asyncio.timeout(self.draft_timeout):
                    review = await client.create_review(
                        {
                            "doc_id": request.draft_doc_id,
                            "playbook_id": request.playbook_id,
                            "matter": dossier["matter"],
                            "run_id": ctx.run_id,
                            "dossier": dossier,
                        },
                        idempotency_key=f"{ctx.run_id}:draft",
                    )
                    ctx.emit(
                        "orchestrator",
                        "state_change",
                        {
                            "from": "requesting_review",
                            "to": "review_created",
                            "review_id": review["review_id"],
                            "operation": "workbench",
                        },
                    )
                    while True:
                        result = await client.get_review(review["review_id"])
                        if result["review_id"] != review["review_id"]:
                            raise ValueError("Workbench returned another review")
                        if result["state"] == "ready":
                            ids = [edit["edit_id"] for edit in result["edits"]]
                            if len(ids) != len(set(ids)) or any(
                                edit["document_id"] != request.draft_doc_id
                                or set(edit["authority_claim_ids"])
                                != {report["claim_id"] for report in edit["guard_reports"]}
                                for edit in result["edits"]
                            ):
                                raise ValueError("Workbench edit provenance mismatch")
                            amended: dict[str, Any] = Dossier.model_validate(
                                {
                                    **dossier,
                                    "edits": result["edits"],
                                    "abstentions": [
                                        *dossier["abstentions"],
                                        {
                                            "item": "draft",
                                            "reason_code": "CV_SUPPORT_UNCERTAIN",
                                            "reason_text": "Lawyer review required.",
                                            "review_id": result["review_id"],
                                            "review_state": result["state"],
                                            "consistency_report": result["consistency_report"],
                                        },
                                    ],
                                }
                            ).model_dump(mode="json")
                            ctx.emit(
                                "orchestrator",
                                "state_change",
                                {
                                    "from": "review_created",
                                    "to": "review_ready",
                                    "review_id": review["review_id"],
                                    "operation": "workbench",
                                },
                            )
                            return amended
                        if result["state"] not in {"queued", "running"}:
                            break
                        await self.sleep(5)
            except (*GRAPH_ERRORS, KeyError, TypeError, ValueError, TimeoutError):
                pass
        ctx.notice("draft", "WORKBENCH_FAILED", "The Workbench review was unavailable.")
        return {**dossier, "abstentions": [*dossier["abstentions"], ctx.notices[-1]]}
