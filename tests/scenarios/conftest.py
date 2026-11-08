"""Isolated, explicitly selected mock peers for scenario and acceptance tests."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from gba_agent.clients.graph_client import GraphClient
from tests.mocks import mock_graph, mock_workbench
from tests.scenarios.support import make_case
from tests.sitesim.app import Simulator
from tests.sitesim.fetcher import FetchFixture
from tests.sitesim.fetcher import site as site

TOKEN = "scenario-peer-token"


@pytest_asyncio.fixture
async def cases(site: FetchFixture, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    monkeypatch.setenv("SERVICE_TOKEN", TOKEN)
    monkeypatch.setattr(mock_graph, "DOCUMENTS", {})
    monkeypatch.setattr(mock_graph, "JOBS", {})
    monkeypatch.setattr(mock_workbench, "REVIEWS", {})
    monkeypatch.setattr(mock_workbench, "RECEIPTS", {})
    simulator = Simulator()
    async with GraphClient(
        "http://graph.test", TOKEN, max_retries=0, transport=httpx.ASGITransport(app=mock_graph.app)
    ) as graph:

        async def create(name: str) -> Any:
            return await make_case(name, site, graph, simulator)

        yield create
