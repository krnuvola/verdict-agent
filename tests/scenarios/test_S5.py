"""S5 — temporal validity leaves missing applicability facts unresolved."""

import pytest

from gba_agent.clients.graph_client import GraphClient
from gba_agent.guards.temporal_guard import TemporalGuard
from tests.mocks.mock_graph import applicability
from tests.mocks.temporal import PROVISION_B, fact_payload, known, matter
from tests.mocks.temporal import graph as graph


@pytest.mark.asyncio
async def test_s5_missing_transaction_type_is_a_scope_question(graph: GraphClient) -> None:
    fixture = matter(
        transaction_type={"status": "unknown"},
        controller_present=known(True),
        litigation_commenced_on=known(None),
    )
    result = await TemporalGuard(graph).check(PROVISION_B, fixture)
    assert (result.verdict, result.reason_code) == ("pass", "TEMPORAL_OK")
    facts = fact_payload(fixture)
    frame = (await graph.scope_frame(PROVISION_B, fixture.as_of))["frame"]
    scope, missing, failed = applicability(frame, facts, fixture.as_of)
    assert scope is None and failed is None
    assert missing[0] == "transaction_type"
    standard = (await graph.standard_of_review(fixture.as_of, facts))["standard"]
    assert standard["value"] == standard["safe_harbor"] == "unknown"
    assert len(standard["readings"]) >= 2


@pytest.mark.asyncio
async def test_s5_final_report_is_unknown(cases) -> None:
    case = await cases("S5")
    report = await case.pipeline.verify(case.claim, case.matter)
    assert report.final == "unknown"
    result = next(r for r in report.results if r.guard == "scope")
    assert (result.verdict, result.reason_code) == ("unknown", "SCOPE_FACTS_MISSING")
    assert result.evidence["missing_facts"][0] == "transaction_type"
    assert next(r for r in report.results if r.guard == "claim_verifier").verdict == "pass"
