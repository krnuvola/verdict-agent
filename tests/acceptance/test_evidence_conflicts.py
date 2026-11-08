"""Conflicting admitted sources remain disputed; unadmitted evidence is unreadable."""

import pytest

from gba_agent.access.fetcher import Fetcher
from gba_agent.adapters.base import AdapterRegistry
from gba_agent.orchestrator.matter_store import MatterStore
from gba_agent.orchestrator.tools import PlannerTools
from tests.mocks.temporal import matter
from tests.scenarios.conftest import cases as cases
from tests.sitesim.fetcher import RUN_ID
from tests.sitesim.fetcher import site as site


@pytest.mark.asyncio
async def test_conflicting_admitted_sources_remain_disputed(cases) -> None:
    first, second = await cases("FACT_TRUE"), await cases("FACT_FALSE")
    assert first.claim.source_doc_id != second.claim.source_doc_id
    context = matter()
    store = MatterStore(first.site.database)
    fetcher = Fetcher(
        first.site.settings,
        first.site.database,
        first.site.blobs,
        registry=first.simulator.registry(access="api"),
        transport=first.simulator.transport(),
    )
    tools = PlannerTools(
        RUN_ID,
        context,
        store,
        first.site.blobs,
        first.graph,
        fetcher,
        AdapterRegistry(),
        first.gate,
        lambda *args: None,
    )
    for case, value in ((first, True), (second, False)):
        response = await tools.call(
            "set_fact",
            {
                "key": "special_committee_formed",
                "value": value,
                "span": case.claim.span.model_dump(mode="json"),
            },
        )
        assert "error" not in response
    fact = context.facts["special_committee_formed"]
    assert fact.status == "conflicting" and fact.value is None
    assert len(fact.spans) == 2 and {span.doc_id for span in fact.spans} == {
        first.claim.source_doc_id,
        second.claim.source_doc_id,
    }
    unreadable = await tools.call(
        "read_doc", {"doc_id": "sha256:" + "f" * 64, "start": 0, "end": 1}
    )
    assert unreadable == {"error": "SG_SOURCE_NOT_ADMITTED"}
