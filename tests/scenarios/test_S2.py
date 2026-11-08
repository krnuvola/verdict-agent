"""S2 — a known pre-cutoff filing makes statutory treatment inapplicable."""

import pytest

from gba_agent.clients.graph_client import GraphClient
from gba_agent.guards.temporal_guard import PredicateEvaluator, TemporalGuard
from tests.mocks.mock_graph import applicability
from tests.mocks.temporal import HOLDING, PROVISION_B, fact_payload, known, matter
from tests.mocks.temporal import evaluator as evaluator
from tests.mocks.temporal import graph as graph


@pytest.mark.asyncio
async def test_s2_earlier_filing_preserves_holding(
    graph: GraphClient, evaluator: PredicateEvaluator
) -> None:
    fixture = matter(signing_date=known("2023-05-01"), litigation_commenced_on=known("2023-11-01"))
    result = await TemporalGuard(graph, evaluate=evaluator).check(HOLDING, fixture)
    assert (result.verdict, result.reason_code) == ("pass", "TEMPORAL_OK")
    facts = fact_payload(fixture)
    frame = (await graph.scope_frame(PROVISION_B, fixture.as_of))["frame"]
    scope, _, failed = applicability(frame, facts, fixture.as_of)
    assert (scope, failed) == (False, "non_application")
    standard = (await graph.standard_of_review(fixture.as_of, facts))["standard"]
    assert standard["value"] == "unknown"
    reading = next(item for item in standard["readings"] if item["regime"] == "Holding:mfw-2014")
    assert "mom_approval_obtained" in reading["requires"]


@pytest.mark.asyncio
async def test_s2_final_report_and_exact_non_application(cases) -> None:
    case = await cases("S2")
    report = await case.pipeline.verify(case.claim, case.matter)
    assert report.final == "block"
    result = next(r for r in report.results if r.guard == "scope")
    assert (result.verdict, result.reason_code) == ("block", "SCOPE_NON_APPLICATION")
    assert result.evidence["failed_clause"] == "non_application"


@pytest.mark.asyncio
async def test_s2_holding_scope_passes_with_doc00_facts(cases) -> None:
    from gba_agent.guards.scope_guard import ScopeGuard
    from tests.scenarios.support import scope

    case = await cases("S2")
    claim = case.claim.model_copy(update={"authority_node_id": HOLDING})
    result = await ScopeGuard(case.graph, evaluate=scope).check_claim(claim, case.matter)
    assert result is not None
    # This is the literal Doc 00 expectation. Do not add unstated facts to green it.
    assert (result.verdict, result.reason_code) == ("pass", "SCOPE_OK")
