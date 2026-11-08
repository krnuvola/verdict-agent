"""S10: hostile page text cannot authorize execution or credential disclosure."""

import json
from collections import deque
from pathlib import Path

import pytest

from gba_agent.access.fetcher import Fetcher, FetchRequest
from gba_agent.operator.browser import BrowserRuntime
from gba_agent.trajectory import LocalTrajectoryWriter, merge, register_secret, verify_chain
from tests.integration.test_browser_operator import Replay, Tool, _executable, act
from tests.sitesim.app import INJECTION, ORIGIN, Simulator
from tests.sitesim.fetcher import RUN_ID, FetchFixture
from tests.sitesim.fetcher import site as site


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attack,code",
    [
        ("commit", "AG_COMMIT_FORBIDDEN"),
        ("spend", "AG_SPEND_NEEDS_HUMAN"),
        ("login", "AG_LOGIN_VIA_VAULT_ONLY"),
        ("exfiltrate", None),
        ("pipeline", "AG_COMMIT_FORBIDDEN"),
    ],
)
async def test_s10_guarded_hostile_page(
    site: FetchFixture, tmp_path: Path, attack: str, code: str | None
) -> None:
    simulator = Simulator()
    register_secret(simulator.secret)
    sink = LocalTrajectoryWriter("agent-api", RUN_ID, tmp_path)

    def emit(run_id: str, event: str, payload: dict[str, object]) -> None:
        assert run_id == RUN_ID
        site.events.append((run_id, event, payload))
        sink.emit(
            component="action_guard"
            if event == "action_decided"
            else "operator"
            if event.startswith("action_") or event == "llm_call"
            else "access",
            type=event,
            payload=payload,
        )

    attacks = {
        "commit": act("click", 1),
        "spend": act("click", 2),
        "login": act("click", 3),
        "exfiltrate": act("navigate", value="https://unapproved.invalid/exfiltrate"),
        "pipeline": Tool("disable_guards", {"guards_enabled": []}),
    }
    scripted = [act("navigate", value="/hostile"), attacks[attack]]
    if attack == "pipeline":
        scripted.append(act("click", 1))
    scripted.append(Tool("done", {"summary": "Refused hostile instructions"}))
    replay = Replay(deque(scripted))
    registry = simulator.registry()
    async with BrowserRuntime.launch(
        site.settings, site.database, registry, llm=replay, executable_path=_executable(), emit=emit
    ) as runtime:
        fetcher = Fetcher(
            site.settings,
            site.database,
            site.blobs,
            registry=registry,
            browser=runtime,
            sessions=runtime.sessions,
            transport=simulator.transport(),
            emit=emit,
            clock=lambda: site.time,
            sleep=site.sleep,
            now=site.now,
        )
        await fetcher.fetch(FetchRequest(url=ORIGIN + "/cookie-seed", run_id=RUN_ID))

    decisions = [data["decision"] for _, event, data in site.events if event == "action_decided"]
    if code:
        assert any(
            decision["reason_code"] == code and not decision["allowed"] for decision in decisions
        )
    assert simulator.authenticated_requests > 0  # The browser really held protected session data.
    assert simulator.effects == {}
    assert all(
        method == "GET" and url.startswith(ORIGIN + "/") for method, url in simulator.requests
    )
    assert not any("unapproved.invalid" in url for _, url in simulator.requests)
    executed = [
        data["action"]["action_type"]
        for _, event, data in site.events
        if event == "action_executed" and data["result"] == "ok"
    ]
    assert executed == ["navigate", "navigate"]
    assert not any(
        decision["allowed"]
        for decision in decisions
        if decision["action_class"] in {"COMMIT", "SPEND", "LOGIN"}
    )
    assert any(INJECTION in json.dumps(data) for _, data in replay.calls)
    assert simulator.secret not in repr(replay.calls) + sink.path.read_text(encoding="utf-8")
    assert verify_chain(sink.path) is None
    events = list(merge(tmp_path, RUN_ID))
    starts = [
        e
        for e in events
        if e["payload"].get("operation") == "network_request"
        and e["payload"].get("phase") == "started"
    ]
    assert len(starts) == len(simulator.requests)
