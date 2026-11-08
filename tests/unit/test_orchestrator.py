"""Full offline runs using the real HTTP service clients, Fetcher, and guards."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import create_engine

from gba_agent.access._registry import Registry
from gba_agent.access.fetcher import Fetcher
from gba_agent.adapters.base import AdapterContext, AdapterRegistry, AdapterTask, FetchedDocument
from gba_agent.api import routes_orch
from gba_agent.api.contracts_compat import CreateRunReq, _CompatClaim, _CompatGuardResult
from gba_agent.api.main import app
from gba_agent.clients.graph_client import GraphClient
from gba_agent.clients.workbench_client import WorkbenchClient
from gba_agent.config import get_settings
from gba_agent.db.models import Base, Run, SourceRecord
from gba_agent.db.session import create_session_factory, session_scope
from gba_agent.guards.claim_verifier import ClaimVerifier
from gba_agent.guards.gnn_alert import GNNAlert
from gba_agent.guards.pipeline import GuardPipeline
from gba_agent.guards.scope_guard import ScopeGuard
from gba_agent.guards.source_gate import SourceGate
from gba_agent.guards.temporal_guard import TemporalGuard
from gba_agent.orchestrator.matter_store import MatterStore
from gba_agent.orchestrator.runner import Execution, RunRunner, RunServices
from gba_agent.orchestrator.tools import TOOL_SCHEMAS
from tests.integration.test_browser_operator import _executable
from tests.mocks.temporal import known, matter
from tests.sitesim.fetcher import MemoryBlobs, entry
from tests.unit.test_guards_api import scope_eval
from tests.unit.test_source_gate import Canon, record

TOKEN = "orchestrator-test"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Contract-Version": "1.0.0"}
ROOT = "/orch/v1/runs"
AUTHORITY = "Provision:synthetic@2020-01-01"
ELEMENT = "Element:synthetic"
URL = "https://fixture.test/Archives/edgar/data/123/000000012326000001/example.htm"
QUOTE = "The committee could reject the transaction."
TEXT = QUOTE + "\n\nA separate paragraph."
RAW = f"<p>{QUOTE}</p><p>A separate paragraph.</p>".encode()
DOC_ID = f"sha256:{sha256(RAW).hexdigest()}"
INPUT_TEXT = "The filing records that the special committee was formed."
INPUT_RAW = f"<p>{INPUT_TEXT}</p>".encode()
INPUT_DOC = f"sha256:{sha256(INPUT_RAW).hexdigest()}"


@dataclass
class Call:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Reply:
    model: str = "fixture-planner"
    text: str = ""
    tool_calls: list[Call] = field(default_factory=list)
    input_tokens: int = 1
    output_tokens: int = 1
    request_hash: str = "a" * 64


class Replay:
    replay_safe = True

    def __init__(self) -> None:
        self.rounds: list[list[tuple[str, dict[str, Any]]]] = []
        self.requests: list[dict[str, Any]] = []
        self.offline = False

    def complete(self, **request: Any) -> Reply:
        self.requests.append(request)
        if self.offline:
            raise OSError("fixture outage")
        if request["role"] == "judge":
            assert QUOTE in request["messages"][0]["content"]
            return Reply(
                model="fixture-judge", text='{"label":"supports","score":0.9,"reason":"Fixture"}'
            )
        if request["tools"] and request["tools"] != TOOL_SCHEMAS:
            assert request["role"] in {"planner", "vlm"}
            assert any(tool["name"] == "done" for tool in request["tools"])
            return Reply(
                tool_calls=[Call("browser_done", "done", {"summary": "Retrieved the page."})]
            )
        assert request["role"] == "planner" and "You do not give legal advice." in request["system"]
        if request["response_schema"]:
            return Reply(text='{"tasks":["Check the public filing"]}')
        assert request["tools"] == TOOL_SCHEMAS
        assert self.rounds, "An unexpected planner call would be a replay miss"
        return Reply(
            tool_calls=[
                Call(f"call_{index}", name, args)
                for index, (name, args) in enumerate(self.rounds.pop(0))
            ]
        )


@dataclass
class World:
    runner: RunRunner
    client: httpx.AsyncClient
    llm: Replay
    events: dict[str, list[dict[str, Any]]]
    network: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    graph_offline: bool = False
    handoff: bool = False
    block_graph: bool = False
    workbench_offline: bool = False
    workbench_state: str = "ready"
    docs: dict[str, Any] = field(default_factory=dict)

    def payload(
        self, *, config: dict[str, Any] | None = None, sources: bool = False
    ) -> dict[str, Any]:
        item = matter(transaction_type=known("other_controller_deal")).model_dump(mode="json")
        if sources:
            item["source_doc_ids"] = [INPUT_DOC]
            item["facts"]["controller_present"] = {
                "key": "controller_present",
                "value": True,
                "status": "known",
                "extracted_by": "human",
            }
        return {"matter": item, "config": config or {}}

    async def start(self, **kwargs: Any) -> str:
        response = await self.client.post(ROOT, json=self.payload(**kwargs), headers=HEADERS)
        assert response.status_code == 202, response.text
        run_id = str(response.json()["run_id"])
        assert response.headers["X-Run-Id"] == run_id
        return run_id

    async def wait(self, run_id: str) -> dict[str, Any]:
        async with asyncio.timeout(6):
            while (status := self.runner.status(run_id)).state not in {
                "done",
                "failed",
                "cancelled",
            }:
                await asyncio.sleep(0.01)
        return status.model_dump()

    async def graph(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/graph/v1")
        self.paths.append(path)
        if self.block_graph:
            await asyncio.sleep(60)
        if self.graph_offline:
            return httpx.Response(503, json={"error": {"code": "UPSTREAM_UNAVAILABLE"}})
        body = json.loads(request.content) if request.method == "POST" else {}
        if path.endswith("/meta"):
            meta = self.docs.get(path.split("/")[2])
            return (
                httpx.Response(200, json=meta)
                if meta
                else httpx.Response(
                    404,
                    json={
                        "error": {
                            "code": "DOC_NOT_FOUND",
                            "message": "Fixture absent",
                            "details": {},
                            "request_id": request.headers["X-Request-Id"],
                        }
                    },
                )
            )
        if path == "/documents/register":
            source = body["source"]
            doc = source["doc_id"]
            self.docs[doc] = {
                "doc_id": doc,
                "source": source,
                "doc_type": "8k",
                "doc_date": "2025-01-01",
                "canon_version": "1",
                "char_length": len(TEXT if doc == DOC_ID else INPUT_TEXT),
                "page_markers": [],
                "title": "Synthetic filing",
                "node_ids": [],
            }
            return httpx.Response(201, json=self.docs[doc])
        if path == "/extract/facts":
            assert body["doc_ids"] == [INPUT_DOC]
            return httpx.Response(202, json={"job_id": "job_fixture"})
        if path == "/jobs/job_fixture":
            evidence = {
                "doc_id": INPUT_DOC,
                "start": 0,
                "end": len(INPUT_TEXT),
                "quote": INPUT_TEXT,
            }
            return httpx.Response(
                200,
                json={
                    "job_id": "job_fixture",
                    "status": "done",
                    "result": [
                        {
                            "key": "special_committee_formed",
                            "value": value,
                            "status": "known",
                            "spans": [evidence],
                        }
                        for value in (True, False)
                    ]
                    + [
                        {
                            "key": "controller_present",
                            "value": False,
                            "status": "known",
                            "spans": [evidence],
                        },
                        {
                            "key": "committee_approval_obtained",
                            "value": True,
                            "status": "known",
                            "spans": [{**evidence, "quote": "fabricated"}],
                        },
                    ],
                },
            )
        if path == "/query/standard_of_review":
            return httpx.Response(
                200,
                json={
                    "standard": {
                        "value": "unknown",
                        "safe_harbor": "unknown",
                        "basis": [AUTHORITY],
                        "readings": [
                            {
                                "regime": AUTHORITY,
                                "applies_if": [],
                                "requires": ["special_committee_formed"],
                                "would_yield": "depends",
                            }
                        ],
                    },
                    "conditions": [],
                },
            )
        if path == "/query/rule_candidates":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "node": {
                                "node_id": AUTHORITY,
                                "node_type": "Provision",
                                "label": "Synthetic authority",
                                "known_from": "2020-01-01",
                                "origin": "seed",
                                "scope_frame": {
                                    "jurisdiction": "US-DE",
                                    "valid_from": "2020-01-01",
                                },
                            },
                            "score": 1,
                            "spans": [],
                        }
                    ]
                },
            )
        if path == "/search/documents":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "doc_id": DOC_ID,
                            "source_id": "src_fixture",
                            "title": "Synthetic filing",
                            "doc_type": "8k",
                            "doc_date": "2025-01-01",
                            "score": 1,
                            "spans": [
                                {
                                    "doc_id": DOC_ID,
                                    "start": 0,
                                    "end": len(QUOTE),
                                    "quote": "NEVER FORWARD",
                                }
                            ],
                        }
                    ]
                },
            )
        if path == f"/nodes/{AUTHORITY}":
            return httpx.Response(
                200,
                json={
                    "node_id": AUTHORITY,
                    "node_type": "Provision",
                    "label": "Synthetic authority",
                    "known_from": "2020-01-01",
                    "origin": "seed",
                },
            )
        if path == "/query/validity":
            return httpx.Response(
                200,
                json={
                    "node_id": AUTHORITY,
                    "as_of": body["as_of"],
                    "in_force": "true",
                    "good_law_unconditional": "good",
                    "treatments": [],
                    "reasons": [],
                },
            )
        if path == "/query/scope_frame":
            return httpx.Response(
                200,
                json={
                    "node_id": AUTHORITY,
                    "frame": {
                        "jurisdiction": "US-DE",
                        "valid_from": "2020-01-01",
                        "transaction_types": ["other_controller_deal"],
                    },
                },
            )
        if path == "/gnn/score_edges":
            return httpx.Response(
                200,
                json={
                    "model_version": "fixture",
                    "scores": [
                        {
                            **body["edges"][0],
                            "score_raw": 0.1,
                            "score_calibrated": 0.1,
                            "flag": True,
                        }
                    ],
                },
            )
        assert path == "/gnn/predict_missing"
        assert body["top_k"] == 5 and body["exclude_node_ids"] == [AUTHORITY]
        return httpx.Response(200, json={"model_version": "fixture", "items": []})


@pytest_asyncio.fixture
async def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    for name, value in {
        "SERVICE_TOKEN": TOKEN,
        "GBA_ENV": "test",
        "TRAJ_DIR": str(tmp_path / "trajectory"),
        "DATABASE_URL": "postgresql+psycopg://user:pass@localhost/agentdb",
        "AGENT_USER_AGENT": "OrchTest/1.0 (contact: test@example.test)",
        "LLM_PROVIDER": "openai_compatible",
        "LLM_MODE": "replay",
        "LLM_MODEL_PLANNER": "fixture-planner",
        "LLM_MODEL_JUDGE": "fixture-judge",
        "SUPPORT_JUDGE": "llm",
        "HANDOFF_TIMEOUT_SECONDS": "3",
        "MAX_GNN_LOOPS": "1",
        "LLM_PRICE_TABLE_JSON": '{"fixture-planner":{"in_per_mtok":1000000,"out_per_mtok":0}}',
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    settings = get_settings()
    engine = create_engine(f"sqlite:///{tmp_path / 'orch.db'}")
    Base.metadata.create_all(engine)
    database = create_session_factory(engine)
    store, blobs, llm = MatterStore(database), MemoryBlobs(), Replay()
    events: dict[str, list[dict[str, Any]]] = {}
    source = record(
        INPUT_RAW, url=URL.replace("example.htm", "input.htm"), registry_entry_id="fixture"
    )
    blobs.put(f"raw/{INPUT_DOC[7:]}", INPUT_RAW, "text/html")
    with session_scope(database) as session:
        session.add(
            SourceRecord(
                source_id=source.source_id, record=source.model_dump(mode="json"), admitted=False
            )
        )
    registry = Registry(
        version=1,
        entries=[
            entry("https://fixture.test", "fixture").model_copy(
                update={
                    "url_prefixes": [
                        "https://fixture.test/Archives/",
                        "https://fixture.test/robots.txt",
                    ],
                    "doc_types": ["8k"],
                }
            )
        ],
    )

    def emit(
        run_id: str, matter_id: str, component: str, kind: str, payload: dict[str, object]
    ) -> None:
        events.setdefault(run_id, []).append(
            {
                "run_id": run_id,
                "matter_id": matter_id,
                "component": component,
                "type": kind,
                "payload": payload,
            }
        )

    def read(run_id: str) -> Iterable[dict[str, Any] | str]:
        return events.get(run_id, [])

    @asynccontextmanager
    async def services(ctx: Execution) -> AsyncIterator[RunServices]:
        async def external(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            fixture.network.append(url)
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow:\n")
            assert url == URL
            if fixture.handoff:
                handoff = ctx.runner.handoffs.request(
                    ctx.run_id, "fixture", "captcha", "Test challenge"
                )
                outcome = await ctx.runner.handoffs.wait(handoff.handoff_id)
                assert outcome.status == "resolved"
            return httpx.Response(200, content=RAW, headers={"Content-Type": "text/html"})

        def wb_response(request: httpx.Request) -> httpx.Response:
            if fixture.workbench_offline:
                return httpx.Response(503, json={"error": {"code": "UPSTREAM_UNAVAILABLE"}})
            if request.method == "POST":
                assert json.loads(request.content)["dossier"]["run_id"] == ctx.run_id
                return httpx.Response(202, json={"review_id": "review_fixture"})
            return httpx.Response(
                200,
                json={
                    "review_id": "review_fixture",
                    "state": fixture.workbench_state,
                    "edits": [],
                    "consistency_report": {},
                },
            )

        async with (
            GraphClient(
                "http://graph.test",
                TOKEN,
                transport=httpx.MockTransport(fixture.graph),
                max_retries=0,
            ) as graph,
            WorkbenchClient(
                "http://wb.test", TOKEN, transport=httpx.MockTransport(wb_response), max_retries=0
            ) as wb,
        ):
            fetcher = Fetcher(
                settings,
                database,
                blobs,
                registry=registry,
                transport=httpx.MockTransport(external),
                emit=ctx.access_event,
            )
            gate = SourceGate(
                registry,
                blobs,
                graph,
                database,
                environment="test",
                canonicalize=lambda raw, _: Canon(text=TEXT if raw == RAW else INPUT_TEXT),
            )
            client = ctx.llm(llm)
            verifier = ClaimVerifier.configured(blobs, settings, client=client)

            async def verify(claim: _CompatClaim, _: object) -> _CompatGuardResult:
                return await verifier.verify(claim)

            gnn = GNNAlert(graph)
            pipeline = GuardPipeline(
                source_gate=gate.check_claim,
                claim_verifier=verify,
                temporal=TemporalGuard(graph).check_claim,
                scope=ScopeGuard(graph, evaluate=scope_eval).check_claim,
                gnn_alert=lambda claim, matter: gnn.check(claim, matter.as_of),
            )
            yield RunServices(
                graph,
                blobs,
                fetcher,
                AdapterRegistry(official=[FixtureAdapter()]),
                gate,
                pipeline,
                client,
                wb,
            )

    runner = RunRunner(settings, store, services, emit, read)
    monkeypatch.setattr(app.state, "orchestrator", runner, raising=False)
    monkeypatch.setattr(app.state, "handoffs", runner.handoffs, raising=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
    ) as client:
        fixture = World(runner, client, llm, events)
        yield fixture
        await runner.shutdown()
    engine.dispose()
    get_settings.cache_clear()


class FixtureAdapter:
    site_id = "fixture"
    version = "1"

    async def supports(self, task: AdapterTask) -> bool:
        return task.kind == "fetch_url"

    async def run(self, task: AdapterTask, context: AdapterContext) -> list[FetchedDocument]:
        return [await context.fetch(self.site_id, str(task.params["url"]))]


@pytest.mark.asyncio
async def test_complete_run_acquires_verifies_and_persists_dossier(world: World) -> None:
    evidence = {"doc_id": DOC_ID, "start": 0, "end": len(QUOTE), "quote": QUOTE}
    world.llm.rounds = [
        [
            ("shell", {"command": "echo forbidden"}),
            ("fetch_url", {"url": "https://unregistered.test/forbidden"}),
            ("fetch_url", {"url": URL}),
            ("read_doc", {"doc_id": DOC_ID, "start": 0, "end": len(QUOTE)}),
            ("find_in_doc", {"doc_id": DOC_ID, "query": "committee reject"}),
            ("set_fact", {"key": "committee_power_to_say_no", "value": True, "span": evidence}),
            (
                "propose_claim",
                {
                    "kind": "legal_proposition",
                    "text": "The committee could reject the transaction.",
                    "authority_node_id": AUTHORITY,
                    "element_node_id": ELEMENT,
                    "span": evidence,
                },
            ),
        ],
        [("finish", {})],
        [("finish", {})],
    ]
    run_id = await world.start(sources=True)
    status = await world.wait(run_id)
    assert status["state"] == "done", (status, world.events[run_id])
    assert status["steps_used"] == 14
    assert status["cost_usd"] == 4
    assert world.network == ["https://fixture.test/robots.txt", URL]
    assert "/extract/facts" in world.paths and world.paths.count("/documents/register") == 2
    assert world.paths.count("/gnn/predict_missing") == 2
    response = await world.client.get(f"{ROOT}/{run_id}/dossier", headers=HEADERS)
    assert response.status_code == 200, response.text
    dossier = response.json()
    assert dossier["authorities"][0]["node_id"] == AUTHORITY
    assert dossier["authorities"][0]["guard_report"]["final"] == "pass"
    assert dossier["authorities"][0]["guard_report"]["results"][-1]["verdict"] == "flag"
    assert dossier["matter"]["facts"]["special_committee_formed"]["status"] == "conflicting"
    assert dossier["matter"]["facts"]["controller_present"]["value"] is True
    assert "committee_approval_obtained" not in dossier["matter"]["facts"]
    assert dossier["matter"]["facts"]["committee_power_to_say_no"]["spans"] == [
        evidence | {"pincite": None}
    ]
    assert "Was a special committee formed?" in dossier["open_questions"]
    assert any(item["reason_code"] == "SOURCE_UNRETRIEVABLE" for item in dossier["abstentions"])
    assert (await world.client.get(f"{ROOT}/{run_id}/dossier", headers=HEADERS)).json() == dossier
    trajectory = await world.client.get(f"{ROOT}/{run_id}/trajectory", headers=HEADERS)
    assert trajectory.headers["content-type"] == "application/x-ndjson"
    transitions = [
        item["payload"]["to"]
        for item in world.events[run_id]
        if item["type"] == "state_change" and "operation" not in item["payload"]
    ]
    assert transitions == [
        "INTAKE",
        "FACTS",
        "PLAN",
        "ACQUIRE",
        "PROPOSE",
        "VERIFY",
        "GAP",
        "ACQUIRE",
        "PROPOSE",
        "VERIFY",
        "GAP",
        "DOSSIER",
        "DONE",
    ]
    assert QUOTE not in trajectory.text
    assert any("UNKNOWN_TOOL" in repr(request["messages"]) for request in world.llm.requests)
    assert (
        '"approved_sites":[{"site_id":"fixture"' in world.llm.requests[0]["messages"][0]["content"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config, steps",
    [({"max_steps": 2}, 2), ({"max_cost_usd": 0.5}, 1), ({"max_wall_seconds": 1}, 0)],
)
async def test_run_budgets_finish_with_abstention(
    world: World,
    config: dict[str, Any],
    steps: int,
) -> None:
    world.block_graph = "max_wall_seconds" in config
    world.llm.rounds = [[("fetch_url", {"url": URL})]]
    run_id = await world.start(config=config)
    status = await world.wait(run_id)
    assert status["state"] == "done" and status["steps_used"] == steps
    assert world.network == []
    assert any(
        item["reason_code"] == "BUDGET_EXCEEDED"
        for item in world.runner.dossier(run_id)["abstentions"]
    )


@pytest.mark.asyncio
async def test_idempotency_cancellation_and_no_early_dossier(world: World) -> None:
    world.block_graph = True
    headers = HEADERS | {"Idempotency-Key": "test-run-key"}
    payload = world.payload()
    initial = await world.client.post(ROOT, json=payload, headers=headers)
    duplicate = await world.client.post(ROOT, json=payload, headers=headers)
    assert (
        initial.status_code == duplicate.status_code == 202 and initial.json() == duplicate.json()
    )
    run_id = initial.json()["run_id"]
    changed = await world.client.post(
        ROOT, json=world.payload(config={"max_steps": 3}), headers=headers
    )
    assert changed.status_code == 422
    early = await world.client.get(f"{ROOT}/{run_id}/dossier", headers=HEADERS)
    assert early.status_code == 404 and early.json()["error"]["code"] == "DOSSIER_NOT_READY"
    cancelled = await world.client.post(f"{ROOT}/{run_id}/cancel", headers=HEADERS)
    assert cancelled.json()["state"] == "cancelled"
    assert (await world.wait(run_id))["state"] == "cancelled"
    assert (await world.client.get(f"{ROOT}/{run_id}", headers=HEADERS)).json() == cancelled.json()


@pytest.mark.asyncio
async def test_handoff_wait_excludes_wall_time_and_can_be_resolved(world: World) -> None:
    world.handoff = True
    world.llm.rounds = [[("fetch_url", {"url": URL}), ("finish", {})]]
    run_id = await world.start(config={"max_wall_seconds": 1})
    async with asyncio.timeout(2):
        while world.runner.status(run_id).state != "waiting_handoff":
            await asyncio.sleep(0.01)
    await asyncio.sleep(1.1)
    handoffs = (await world.client.get("/orch/v1/handoffs", headers=HEADERS)).json()["items"]
    assert len(handoffs) == 1 and handoffs[0]["viewer_url"]
    resolved = await world.client.post(
        f"/orch/v1/handoffs/{handoffs[0]['handoff_id']}/resolve",
        headers=HEADERS,
        json={"resolved_by": "fixture", "outcome": "resolved"},
    )
    assert resolved.status_code == 200 and resolved.json()["status"] == "resolved"
    assert (await world.wait(run_id))["state"] == "done"
    assert not any(
        item["reason_code"] == "BUDGET_EXCEEDED"
        for item in world.runner.dossier(run_id)["abstentions"]
    )


@pytest.mark.asyncio
async def test_restart_marks_inflight_runs_failed_and_closes_handoffs(world: World) -> None:
    body = CreateRunReq.model_validate(world.payload())
    for state in ("running", "waiting_handoff"):
        run_id, _ = world.runner.store.create(body, None, "fixture")
        with session_scope(world.runner.store.database) as db:
            row = db.get(Run, run_id)
            assert row is not None
            row.state = "running"
        if state == "waiting_handoff":
            handoff = world.runner.handoffs.request(run_id, "fixture", "mfa", "Test MFA")
        world.runner.recover()
        status = world.runner.status(run_id)
        assert (status.state, status.step, status.error) == ("failed", "FAILED", "RESTART")
        assert status.open_handoffs == []
    assert world.runner.handoffs.list("abandoned")[0].handoff_id == handoff.handoff_id


@pytest.mark.asyncio
@pytest.mark.parametrize("offline", [False, True])
async def test_optional_workbench_failure_is_an_abstention(world: World, offline: bool) -> None:
    world.workbench_offline = offline
    world.llm.rounds = [[("finish", {})]]
    payload = world.payload(config={"use_workbench": True})
    payload.update(draft_doc_id=DOC_ID, playbook_id="fixture")
    response = await world.client.post(ROOT, json=payload, headers=HEADERS)
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    assert (await world.wait(run_id))["state"] == "done"
    assert ("WORKBENCH_FAILED" in repr(world.runner.dossier(run_id)["abstentions"])) is offline


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "failed", "wrong_review", "malformed"])
async def test_workbench_incomplete_review_preserves_dossier(
    world: World, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    async def status(_: WorkbenchClient, review_id: str) -> dict[str, Any]:
        if mode == "malformed":
            raise ValueError("Malformed peer response")
        return {
            "review_id": "another_review" if mode == "wrong_review" else review_id,
            "state": "queued" if mode == "timeout" else "failed",
        }

    monkeypatch.setattr(WorkbenchClient, "get_review", status)
    world.runner.draft_timeout = 0.02
    world.llm.rounds = [[("finish", {})]]
    payload = world.payload(config={"use_workbench": True})
    payload.update(draft_doc_id=DOC_ID, playbook_id="fixture")
    response = await world.client.post(ROOT, json=payload, headers=HEADERS)
    run_id = response.json()["run_id"]
    assert (await world.wait(run_id))["state"] == "done"
    dossier = world.runner.dossier(run_id)
    assert dossier["matter"] == payload["matter"]
    assert dossier["edits"] == []
    assert any(item["reason_code"] == "WORKBENCH_FAILED" for item in dossier["abstentions"])


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [f"A{i}" for i in range(7)])
async def test_ladder_runs_are_observable_without_mock_substitution(
    world: World, level: str
) -> None:
    from gba_agent.orchestrator.ladder import ladder_config

    world.runner.settings = world.runner.settings.model_copy(
        update={"allow_unregistered_fetch_for_baseline": True}
    )
    world.llm.rounds = [[("finish", {})]]
    run_id = await world.start(config=ladder_config(level).model_dump(mode="json"))
    assert (await world.wait(run_id))["state"] == "done"
    events = world.events[run_id]
    completion = next(
        event
        for event in reversed(events)
        if event["type"] == "state_change" and event["payload"]["to"] == "DONE"
    )
    metrics = completion["payload"]["metrics"]
    assert metrics["llm_calls"] == 2
    assert metrics["input_tokens"] == 2 and metrics["output_tokens"] == 2
    assert metrics["models"] == ["fixture-planner"]
    assert world.network == []
    assert sum(event["type"] == "llm_call" for event in events) == 2


@pytest.mark.asyncio
async def test_run_uses_valid_local_chain_and_records_budget_stop(
    world: World, tmp_path: Path
) -> None:
    from gba_agent.trajectory import LocalTrajectoryWriter, merge, verify_chain

    capture = world.runner.emit

    def emit(
        run_id: str, matter_id: str, component: str, event: str, payload: dict[str, object]
    ) -> None:
        capture(run_id, matter_id, component, event, payload)
        LocalTrajectoryWriter("agent-api", run_id, tmp_path).emit(
            component=component, type=event, payload=payload, matter_id=matter_id
        )

    world.runner.emit = emit
    world.runner.read_events = lambda run_id: merge(tmp_path, run_id)
    run_id = await world.start(config={"max_steps": 1})
    assert (await world.wait(run_id))["state"] == "done"
    assert verify_chain(tmp_path / run_id / "agent-api.jsonl") is None
    events = list(merge(tmp_path, run_id))
    assert events[-1]["payload"]["metrics"]["budget_stopped"]
    assert events[-1]["payload"]["to"] == "DONE"
    assert any(event["parent_event_id"] for event in events[1:])


@pytest.mark.asyncio
async def test_recording_failure_cannot_report_success(world: World) -> None:
    capture = world.runner.emit

    def emit(
        run_id: str, matter_id: str, component: str, event: str, payload: dict[str, object]
    ) -> None:
        if event == "state_change" and payload.get("to") == "DONE":
            raise OSError("fixture disk failure")
        capture(run_id, matter_id, component, event, payload)

    world.runner.emit = emit
    world.llm.rounds = [[("finish", {})]]
    run_id = await world.start()
    status = await world.wait(run_id)
    assert status["state"] == "failed" and status["error"] == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_default_composition_records_and_reads_real_chain(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.datastructures import State

    from gba_agent.trajectory import verify_chain

    state = State()
    state.orch_services = world.runner.services  # Explicit offline peers; use the real recorder.
    runner = routes_orch.make_runner(state, world.runner.settings, world.runner.store.database)
    monkeypatch.setattr(app.state, "orchestrator", runner)
    world.llm.rounds = [[("finish", {})]]
    run_id = await world.start()
    try:
        assert (await world.wait(run_id))["state"] == "done"
        path = world.runner.settings.traj_dir / run_id / "agent-api.jsonl"
        assert verify_chain(path) is None
        events = [json.loads(line) for line in runner.trajectory(run_id).splitlines()]
        assert events[-1]["payload"]["metrics"]["llm_calls"] == 2
        assert events[-1]["payload"]["trajectory_backend"] == "local-doc00-v1"
        assert events[1]["parent_event_id"] == events[0]["event_id"]
    finally:
        await runner.shutdown()


@pytest.mark.asyncio
async def test_graph_and_planner_outages_are_distinguished(world: World) -> None:
    world.graph_offline = True
    world.llm.rounds = [[("finish", {})]]
    run_id = await world.start()
    assert (await world.wait(run_id))["state"] == "done"
    dossier = world.runner.dossier(run_id)
    assert dossier["standard_of_review"]["value"] == "unknown"
    assert "UPSTREAM_UNAVAILABLE" in repr(dossier["abstentions"])
    world.llm.offline = True
    run_id = await world.start()
    status = await world.wait(run_id)
    assert (status["state"], status["error"]) == ("failed", "UPSTREAM_UNAVAILABLE")


@pytest.mark.asyncio
async def test_adapter_and_graph_search_return_only_admitted_content(world: World) -> None:
    world.llm.rounds = [
        [
            ("search_corpus", {"query": "committee"}),
            ("run_adapter", {"site_id": "fixture", "kind": "fetch_url", "params": {"url": URL}}),
            ("search_corpus", {"query": "committee"}),
            (
                "operate_site",
                {
                    "site_id": "fixture",
                    "start_url": URL,
                    "goal": "Read the filing",
                    "success_criteria": "Find the filing",
                },
            ),
        ],
        [("finish", {})],
    ]
    run_id = await world.start()
    assert (await world.wait(run_id))["state"] == "done"
    assert world.network == ["https://fixture.test/robots.txt", URL]
    messages = repr(world.llm.requests[-1]["messages"])
    assert QUOTE in messages and "NEVER FORWARD" not in messages
    assert '"admitted":false' in messages and '"admitted":true' in messages


@pytest.mark.asyncio
async def test_fact_changes_invalidate_previously_passed_claims(world: World) -> None:
    span = {"doc_id": DOC_ID, "start": 0, "end": len(QUOTE), "quote": QUOTE}
    world.llm.rounds = [
        [
            ("fetch_url", {"url": URL}),
            (
                "propose_claim",
                {
                    "kind": "legal_proposition",
                    "text": QUOTE,
                    "authority_node_id": AUTHORITY,
                    "element_node_id": ELEMENT,
                    "span": span,
                },
            ),
            ("finish", {}),
        ],
        [
            ("set_fact", {"key": "transaction_type", "value": "going_private", "span": span}),
            ("finish", {}),
        ],
    ]
    run_id = await world.start()
    assert (await world.wait(run_id))["state"] == "done"
    dossier = world.runner.dossier(run_id)
    assert dossier["authorities"] == []
    assert dossier["matter"]["facts"]["transaction_type"]["status"] == "conflicting"
    assert any(item["reason_code"] == "SCOPE_FACTS_MISSING" for item in dossier["abstentions"])
    verdicts = [
        event["payload"]["result"]["verdict"]
        for event in world.events[run_id]
        if event["component"] == "scope" and event["type"] == "guard_result"
    ]
    assert verdicts == ["pass", "unknown"]


@pytest.mark.asyncio
async def test_production_composition_runs_guarded_chromium(
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Chromium executes the guarded navigation. Its sole HTTP exchange is faked here.
    registry_path = tmp_path / "registry.yaml"
    registry = Registry(
        version=1,
        entries=[
            entry("https://fixture.test", "fixture", access="operator").model_copy(
                update={
                    "url_prefixes": [
                        "https://fixture.test/Archives/",
                        "https://fixture.test/robots.txt",
                    ],
                    "doc_types": ["8k"],
                },
            )
        ],
    )
    registry_path.write_text(registry.model_dump_json(), encoding="utf-8")
    settings = world.runner.settings.model_copy(
        update={
            "registry_path": registry_path,
            "session_dir": tmp_path / "sessions",
            "session_enc_key": SecretStr("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="),
        }
    )
    monkeypatch.setattr(app.state, "orch_llm", world.llm, raising=False)
    monkeypatch.setattr(app.state, "guard_blobs", MemoryBlobs(), raising=False)
    monkeypatch.setattr(app.state, "orch_emit", world.runner.emit, raising=False)
    monkeypatch.setattr(app.state, "orch_read_events", world.runner.read_events, raising=False)
    monkeypatch.setattr(app.state, "orch_browser_executable", _executable(), raising=False)

    async def correlated_graph(request: httpx.Request) -> httpx.Response:
        response = await world.graph(request)
        response.headers.update({k: v for k, v in request.headers.items() if k.startswith("x-")})
        return response

    monkeypatch.setattr(
        routes_orch,
        "GraphClient",
        partial(
            GraphClient,
            transport=httpx.MockTransport(correlated_graph),
            max_retries=0,
        ),
    )

    def network(request: httpx.Request) -> httpx.Response:
        world.network.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        assert str(request.url) == URL
        return httpx.Response(200, content=RAW, headers={"Content-Type": "text/html"})

    monkeypatch.setattr(
        routes_orch,
        "Fetcher",
        partial(
            Fetcher,
            transport=httpx.MockTransport(network),
        ),
    )
    # The published canonicalizer is unavailable in this checkout.
    monkeypatch.setattr(
        routes_orch,
        "SourceGate",
        partial(
            SourceGate,
            canonicalize=lambda raw, _: Canon(text=TEXT),
        ),
    )
    runner = routes_orch.make_runner(app.state, settings, world.runner.store.database)
    monkeypatch.setattr(app.state, "orchestrator", runner)
    world.llm.rounds = [
        [
            (
                "operate_site",
                {
                    "site_id": "fixture",
                    "start_url": URL,
                    "goal": "Read the filing",
                    "success_criteria": "Find the filing",
                },
            ),
            ("finish", {}),
        ]
    ]
    run_id = await world.start()
    try:
        assert (await world.wait(run_id))["state"] == "done", world.events[run_id]
    finally:
        await runner.shutdown()
    assert world.network == ["https://fixture.test/robots.txt", URL]
    assert world.runner.store.admitted(DOC_ID, matter().as_of)
    assert any(event["type"] == "action_executed" for event in world.events[run_id])
