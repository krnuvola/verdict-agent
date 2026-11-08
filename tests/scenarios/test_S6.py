"""S6 — a plausible but fabricated authority is blocked by Source Gate."""

from pathlib import Path

import httpx
import pytest

from gba_agent.access._registry import load_registry
from gba_agent.clients.graph_client import GraphClient
from gba_agent.guards.source_gate import SourceGate
from tests.mocks.mock_graph import app as graph_app
from tests.sitesim.fetcher import FetchFixture
from tests.sitesim.fetcher import site as site
from tests.unit.test_source_gate import claim, matter


@pytest.mark.asyncio
async def test_s6_fabricated_authority_is_blocked(
    site: FetchFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SERVICE_TOKEN", "fixture-service-token")
    registry = load_registry(Path(__file__).resolve().parents[2] / "config" / "registry.yaml")
    async with GraphClient(
        "http://graph.test",
        "fixture-service-token",
        transport=httpx.ASGITransport(app=graph_app),
    ) as graph:
        guard = SourceGate(registry, site.blobs, graph, site.database, environment="test")
        result = await guard.check_claim(
            claim(authority_node_id="Case:smith-v-jones-2021"),
            matter(),
        )
    assert result.guard == "source_gate"
    assert result.verdict == "block"
    assert result.reason_code == "SG_NODE_NOT_IN_GRAPH"
    assert not site.sources()


@pytest.mark.asyncio
async def test_s6_final_report_is_block(cases) -> None:
    case = await cases("S6")
    report = await case.pipeline.verify(case.claim, case.matter)
    assert report.final == "block"
    assert [(r.guard, r.verdict, r.reason_code) for r in report.results] == [
        ("source_gate", "block", "SG_NODE_NOT_IN_GRAPH")
    ]
