"""Local simulator routes exercised through the real Access Layer."""

import socket
from collections import deque
from pathlib import Path

import pytest

from gba_agent.access.fetcher import Fetcher, FetchRequest
from gba_agent.operator.browser import BrowserRuntime
from gba_agent.trajectory import LocalTrajectoryWriter, verify_chain
from tests.integration.test_browser_operator import Replay, _executable, act
from tests.sitesim.app import ORIGIN, TEXT, Simulator
from tests.sitesim.fetcher import RUN_ID, FetchFixture
from tests.sitesim.fetcher import site as site


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,status,block",
    [
        ("/redirect", "ok", None),
        ("/malicious-redirect", "unretrievable", None),
        ("/denied", "unretrievable", "http_403"),
        ("/timeout", "unretrievable", "timeout"),
        ("/expired", "unretrievable", "login_wall"),
        ("/changed.pdf", "unretrievable", "silent_content_change"),
        ("/throttled", "ok", "http_429"),
    ],
)
async def test_simulated_access_failures_and_recovery(
    site: FetchFixture, path: str, status: str, block: str | None, tmp_path: Path
) -> None:
    simulator = Simulator()
    sink = LocalTrajectoryWriter("agent-api", RUN_ID, tmp_path)

    def emit(run_id, event, payload):
        site.events.append((run_id, event, payload))
        sink.emit(component="access", type=event, payload=payload)

    fetcher = Fetcher(
        site.settings,
        site.database,
        site.blobs,
        registry=simulator.registry(access="api"),
        transport=simulator.transport(),
        emit=emit,
        sleep=site.sleep,
        clock=lambda: site.time,
        now=site.now,
        jitter=lambda a, b: 1,
    )
    result = await fetcher.fetch(FetchRequest(url=ORIGIN + path, run_id=RUN_ID))
    assert result.status == status
    if block:
        assert any(item["kind"] == block for item in result.blocks)
    if status == "unretrievable":
        assert result.source_record is None and site.sources() == []
    if path == "/throttled":
        assert simulator.attempts[path] == 2 and site.sleeps
    assert simulator.effects == {}
    assert all(url.startswith(ORIGIN + "/") for _, url in simulator.requests)
    assert verify_chain(sink.path) is None
    starts = [
        p
        for _, e, p in site.events
        if e == "state_change"
        and p.get("operation") == "network_request"
        and p.get("phase") == "started"
    ]
    assert len(starts) == len(simulator.requests)


@pytest.mark.asyncio
async def test_simulated_search_pagination_download_and_selector_drift(site: FetchFixture) -> None:
    simulator = Simulator()
    replay = Replay(
        deque(
            [
                act("type", 1, "committee"),
                act("select", 3, "2025"),
                act("press_key", 1, "Enter"),
                act("click", 2),
                act("download", 2),
            ]
        )
    )
    registry = simulator.registry()
    async with BrowserRuntime.launch(
        site.settings,
        site.database,
        registry,
        llm=replay,
        executable_path=_executable(),
        emit=lambda *args: site.events.append(args),
    ) as runtime:
        fetcher = Fetcher(
            site.settings,
            site.database,
            site.blobs,
            registry=registry,
            transport=simulator.transport(),
            browser=runtime,
            sessions=runtime.sessions,
            emit=lambda *args: site.events.append(args),
            sleep=site.sleep,
            clock=lambda: site.time,
            now=site.now,
        )
        result = await fetcher.fetch(FetchRequest(url=ORIGIN + "/search", run_id=RUN_ID))
    assert result.source_record is not None, (result, simulator.requests)
    assert result.source_record.media_type == "application/pdf"
    raw = fetcher.fetched_bytes(result.source_record)
    assert raw.startswith(b"%PDF-1.4") and raw.rstrip().endswith(b"%%EOF")
    assert TEXT.encode("ascii") in raw
    assert any("year=2025" in url for _, url in simulator.requests)
    assert any("page=2" in url for _, url in simulator.requests)
    assert simulator.effects == {}
    # A second visit changes the selector deterministically, without a live website.
    direct = Fetcher(
        site.settings,
        site.database,
        site.blobs,
        registry=simulator.registry(access="api"),
        transport=simulator.transport(),
    )
    first = await direct.fetch(FetchRequest(url=ORIGIN + "/selectors", run_id=RUN_ID))
    second = await direct.fetch(FetchRequest(url=ORIGIN + "/selectors", run_id=RUN_ID))
    assert first.source_record and second.source_record
    assert b"old-result" in direct.fetched_bytes(first.source_record)
    assert b"changed-result" in direct.fetched_bytes(second.source_record)


def test_non_live_tests_cannot_resolve_real_external_hosts() -> None:
    with pytest.raises(AssertionError, match="external network"):
        socket.getaddrinfo("unapproved.invalid", 443)
