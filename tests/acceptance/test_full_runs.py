"""Actual orchestration/guards plus explicitly selected existing B/C mock services."""

import asyncio
import json
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from gba_agent.access.fetcher import Fetcher
from gba_agent.adapters.base import AdapterRegistry
from gba_agent.api.contracts_compat import CreateRunReq, Dossier
from gba_agent.api.trace import TraceContext
from gba_agent.clients.workbench_client import WorkbenchClient
from gba_agent.guards.claim_verifier import ClaimVerifier
from gba_agent.guards.gnn_alert import GNNAlert
from gba_agent.guards.pipeline import GuardPipeline
from gba_agent.guards.scope_guard import ScopeGuard
from gba_agent.guards.temporal_guard import TemporalGuard
from gba_agent.orchestrator.matter_store import MatterStore
from gba_agent.orchestrator.runner import RunRunner, RunServices
from gba_agent.trajectory import LocalTrajectoryWriter, merge, verify_chain
from tests.mocks import mock_workbench
from tests.scenarios.conftest import TOKEN
from tests.scenarios.conftest import cases as cases
from tests.scenarios.support import predicate, scope
from tests.sitesim.app import ORIGIN
from tests.sitesim.fetcher import site as site
from tests.unit.test_claim_verifier import llm
from tests.unit.test_orchestrator import Call, Reply


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,final,reason",
    [
        ("S1", "block", "TEMPORAL_SUPERSEDED"),
        ("S2", "block", "SCOPE_NON_APPLICATION"),
        ("S4", "block", "SCOPE_TRANSACTION_MISMATCH"),
        ("S5", "unknown", "SCOPE_FACTS_MISSING"),
    ],
)
async def test_full_scenario_run_with_existing_peers(
    cases, tmp_path: Path, name: str, final: str, reason: str
) -> None:
    case = await cases(name)
    site = case.site
    claim_args = case.claim.model_dump(mode="json", exclude={"claim_id", "matter_id"})
    script = deque(
        [
            Reply(
                text='{"tasks":["Read admitted illustrative evidence and verify applicability"]}'
            ),
            Reply(
                tool_calls=[
                    Call(
                        "acquire",
                        "fetch_url",
                        {"url": f"{ORIGIN}/documents/0000000123-26-000001/{name}.htm"},
                    ),
                    Call("claim", "propose_claim", claim_args),
                    Call("end", "finish", {}),
                ]
            ),
            Reply(tool_calls=[Call("end2", "finish", {})]),
            Reply(tool_calls=[Call("end3", "finish", {})]),
        ]
    )

    class PlannerReplay:
        replay_safe = True

        def complete(self, **request):
            assert request["role"] == "planner" and request["temperature"] == 0
            assert "untrusted_page_content" in request["messages"][0]["content"]
            assert script, "Unexpected LLM request is a replay miss"
            return script.popleft()

    def emit(run_id, matter_id, component, event, payload):
        LocalTrajectoryWriter("agent-api", run_id, tmp_path).emit(
            component=component, type=event, payload=payload, matter_id=matter_id
        )

    @asynccontextmanager
    async def services(ctx):
        trace = TraceContext(ctx.request_id, ctx.run_id, ctx.request.matter.matter_id, "1.0.0")
        # The fixture admits evidence before execution; rebind its HTTP tracing for this run.
        case.graph._client.headers.update(trace.as_headers())
        planner = ctx.llm(PlannerReplay())
        verifier = ClaimVerifier(site.blobs, llm(ctx.llm(case.judge_replay)))
        gnn = GNNAlert(case.graph)

        async def verify(item, matter):
            return await verifier.verify(item)

        pipeline = GuardPipeline(
            source_gate=case.gate.check_claim,
            claim_verifier=verify,
            temporal=TemporalGuard(case.graph, evaluate=predicate).check_claim,
            scope=ScopeGuard(case.graph, evaluate=scope).check_claim,
            gnn_alert=lambda item, context: gnn.check(item, context.as_of),
        )
        fetcher = Fetcher(
            site.settings,
            site.database,
            site.blobs,
            registry=case.simulator.registry(access="api"),
            transport=case.simulator.transport(),
            emit=ctx.access_event,
            clock=lambda: site.time,
            sleep=site.sleep,
            now=site.now,
        )
        async with WorkbenchClient(
            "http://wb.test",
            TOKEN,
            trace,
            max_retries=0,
            transport=httpx.ASGITransport(app=mock_workbench.app),
        ) as wb:
            yield RunServices(
                case.graph, site.blobs, fetcher, AdapterRegistry(), case.gate, pipeline, planner, wb
            )

    settings = site.settings.model_copy(
        update={
            "llm_model_planner": "fixture-planner",
            "llm_model_judge": "fixture-judge",
            "traj_dir": tmp_path,
        }
    )
    runner = RunRunner(
        settings,
        MatterStore(site.database),
        services,
        emit,
        lambda run_id: merge(tmp_path, run_id),
        sleep=site.sleep,
    )
    request = CreateRunReq(
        matter=case.matter,
        config={"use_workbench": True},
        draft_doc_id="sha256:" + "b" * 64,
        playbook_id="synthetic-review",
    )
    run_id = await runner.create(request, f"acceptance-{name}", "01JDS7K3PZJ4C6S9W2H3G5T8VA")
    try:
        async with asyncio.timeout(10):
            while runner.status(run_id).state not in {"done", "failed", "cancelled"}:
                await asyncio.sleep(0.01)
        assert runner.status(run_id).state == "done", runner.status(run_id)
        claims = runner.store.claims(run_id)
        assert len(claims) == 1 and claims[0][1].final == final
        assert any(r.reason_code == reason for r in claims[0][1].results)
        data = runner.dossier(run_id)
        Dossier.model_validate(data)
        assert data["authorities"] == []
        assert any(item.get("review_state") == "ready" for item in data["abstentions"])
        assert len(data["edits"]) == 1 and data["edits"][0]["status"] == "proposed"
        if name == "S5":
            assert len(data["standard_of_review"]["readings"]) >= 2
        assert verify_chain(tmp_path / run_id / "agent-api.jsonl") is None
        events = [json.loads(line) for line in runner.trajectory(run_id).splitlines()]
        starts = [
            e
            for e in events
            if e["payload"].get("operation") == "network_request"
            and e["payload"].get("phase") == "started"
        ]
        assert len(starts) == len(case.simulator.requests) > 0
        assert all(
            url.startswith(ORIGIN + "/") and method == "GET"
            for method, url in case.simulator.requests
        )
        assert case.simulator.effects == {}
        assert any(e["type"] == "guard_result" for e in events)
        assert any(e["payload"].get("operation") == "workbench" for e in events)
    finally:
        await runner.shutdown()
