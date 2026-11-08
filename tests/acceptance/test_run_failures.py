"""Failure/human-control runs retain real audit chains and abstentions."""

import asyncio
from pathlib import Path

import httpx
import pytest

from gba_agent.trajectory import LocalTrajectoryWriter, merge, verify_chain
from tests.unit.test_orchestrator import DOC_ID, HEADERS, ROOT, URL, World
from tests.unit.test_orchestrator import world as world


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["graph_timeout", "workbench_timeout", "handoff", "interrupt", "budget"]
)
async def test_controlled_failures_have_complete_audit_prefix(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    capture = world.runner.emit

    def emit(run_id, matter_id, component, event, payload):
        capture(run_id, matter_id, component, event, payload)
        LocalTrajectoryWriter("agent-api", run_id, tmp_path).emit(
            component=component, type=event, payload=payload, matter_id=matter_id
        )

    world.runner.emit = emit
    world.runner.read_events = lambda run_id: merge(tmp_path, run_id)
    world.llm.rounds = [[("finish", {})]]
    config = {}
    if fault == "graph_timeout":

        async def unavailable(request):
            raise httpx.ReadTimeout("Offline graph timeout", request=request)

        monkeypatch.setattr(world, "graph", unavailable)
    if fault == "workbench_timeout":
        world.workbench_state = "queued"
        world.runner.draft_timeout = 0.02
        config = {"use_workbench": True}
    if fault == "handoff":
        world.handoff = True
        world.llm.rounds = [[("fetch_url", {"url": URL}), ("finish", {})]]
    if fault == "interrupt":
        world.block_graph = True
    if fault == "budget":
        config = {"max_steps": 1}
    payload = world.payload(config=config)
    if fault == "workbench_timeout":
        payload.update(draft_doc_id=DOC_ID, playbook_id="fixture")
    response = await world.client.post(ROOT, headers=HEADERS, json=payload)
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    if fault == "handoff":
        async with asyncio.timeout(3):
            while not world.runner.handoffs.list():
                await asyncio.sleep(0.01)
        handoff = world.runner.handoffs.list()[0]
        assert world.runner.status(run_id).state == "waiting_handoff"
        world.runner.handoffs.resolve(handoff.handoff_id, "scripted-human", "resolved")
    if fault == "interrupt":
        async with asyncio.timeout(3):
            while not world.paths:
                await asyncio.sleep(0.01)
        await world.runner.cancel(run_id)
        await asyncio.gather(*list(world.runner.tasks.values()), return_exceptions=True)
    status = await world.wait(run_id)
    assert status["state"] == ("cancelled" if fault == "interrupt" else "done")
    events = list(merge(tmp_path, run_id))
    assert verify_chain(tmp_path / run_id / "agent-api.jsonl") is None
    if fault == "handoff":
        assert sum(e["type"] == "handoff_requested" for e in events) == 1
        assert sum(e["type"] == "handoff_resolved" for e in events) == 1
    if fault in {"budget", "workbench_timeout", "graph_timeout"}:
        code = {
            "budget": "BUDGET_EXCEEDED",
            "workbench_timeout": "WORKBENCH_FAILED",
            "graph_timeout": "UPSTREAM_UNAVAILABLE",
        }[fault]
        assert any(e["type"] == "abstain" and e["payload"]["reason_code"] == code for e in events)
    assert all(url.startswith("https://fixture.test/") for url in world.network)
