"""S8: scripted fault-proxy 403 cannot turn into evidence or an off-registry request."""

import asyncio
from pathlib import Path

import pytest

from gba_agent.access._registry import Registry
from gba_agent.access.fetcher import Fetcher, FetchRequest
from gba_agent.api.contracts_compat import RunConfig
from gba_agent.trajectory import LocalTrajectoryWriter, verify_chain
from tests.sitesim.fetcher import RUN_ID, FetchFixture, entry
from tests.sitesim.fetcher import site as site

EDGAR = "http://edgar.test"
FILING = EDGAR + "/documents/filing.pdf"


@pytest.mark.asyncio
async def test_s8_fault_proxy_403_one_retry_zero_substitutions(
    site: FetchFixture, tmp_path: Path
) -> None:
    requested: list[str] = []
    user_agents: list[str] = []
    trajectory = LocalTrajectoryWriter("agent-api", RUN_ID, tmp_path)

    def emit(run_id, event, payload):
        site.events.append((run_id, event, payload))
        trajectory.emit(component="access", type=event, payload=payload)

    async def proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
            line, *headers = request.splitlines()
            method, url, _ = line.split()
            requested.append(url)
            user_agents.extend(
                value.strip()
                for header in headers
                for name, _, value in [header.partition(":")]
                if name.lower() == "user-agent"
            )
            assert method == "GET"
            if url == EDGAR + "/robots.txt":
                status, body = "200 OK", b"User-agent: *\nDisallow:\n"
            else:
                assert url == FILING
                status, body = "403 Forbidden", b"Undeclared automated tool"
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    site.site(
        "sec_edgar",
        """
alternates:
  proxy_statement:
    - entry_id: sec_edgar
      task:
        kind: fetch_url
        params: {url: 'http://unregistered.test/documents/substitute.pdf'}
""",
    )
    server = await asyncio.start_server(proxy, "127.0.0.1", 0)
    async with server:
        address, port = server.sockets[0].getsockname()
        fetcher = Fetcher(
            site.settings,
            site.database,
            site.blobs,
            registry=Registry(version=1, entries=[entry(EDGAR, "sec_edgar")]),
            run_config=RunConfig(fault_proxy_url=f"http://{address}:{port}"),
            emit=emit,
            sleep=site.sleep,
            clock=lambda: site.time,
            now=site.now,
            jitter=lambda _a, _b: 1,
        )
        result = await fetcher.fetch(
            FetchRequest(
                url=FILING,
                run_id=RUN_ID,
                expected_doc_type="proxy_statement",
            )
        )

    assert result.status == "unretrievable" and result.reason_code == "SOURCE_UNRETRIEVABLE"
    assert result.source_record is None
    assert [block["kind"] for block in result.blocks] == ["http_403", "http_403"]
    assert site.sleeps == [5]
    assert requested == [EDGAR + "/robots.txt", FILING, FILING]
    assert user_agents == [site.settings.agent_user_agent] * 3
    assert site.sources() == [] and site.blobs.data == {}
    assert [p["action_taken"] for _, kind, p in site.events if kind == "block"] == [
        "retry",
        "switch_source",
    ]
    assert [p["reason_code"] for _, kind, p in site.events if kind == "abstain"] == [
        "SOURCE_UNRETRIEVABLE",
    ]
    assert "Undeclared automated tool" not in repr(site.events)
    assert verify_chain(trajectory.path) is None
    started = [
        p
        for _, event, p in site.events
        if event == "state_change"
        and p.get("operation") == "network_request"
        and p.get("phase") == "started"
    ]
    assert len(started) == len(requested)
