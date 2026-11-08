"""S1 — conditional statutory supersession on the fixture facts."""

import pytest

from gba_agent.clients.graph_client import GraphClient
from gba_agent.guards.temporal_guard import PredicateEvaluator, TemporalGuard
from tests.mocks.mock_graph import applicability
from tests.mocks.temporal import COMMITTEE, HOLDING, PROVISION_B, fact_payload, known, matter
from tests.mocks.temporal import evaluator as evaluator
from tests.mocks.temporal import graph as graph


@pytest.mark.asyncio
async def test_s1_statutory_treatment_applies(
    graph: GraphClient, evaluator: PredicateEvaluator
) -> None:
    fixture = matter(
        **COMMITTEE,
        transaction_type=known("other_controller_deal"),
        controller_present=known(True),
        signing_date=known("2026-06-10"),
        litigation_commenced_on=known(None),
        mom_approval_obtained={"status": "unknown"},
    )
    result = await TemporalGuard(graph, evaluate=evaluator).check(HOLDING, fixture)
    assert (result.verdict, result.reason_code) == ("block", "TEMPORAL_SUPERSEDED")
    assert result.evidence["superseded_by"] == [PROVISION_B]

    facts = fact_payload(fixture)
    frame = (await graph.scope_frame(PROVISION_B, fixture.as_of))["frame"]
    assert applicability(frame, facts, fixture.as_of) == (True, [], None)
    standard = (await graph.standard_of_review(fixture.as_of, facts))["standard"]
    assert standard["safe_harbor"] == "true" and standard["basis"] == [PROVISION_B]


@pytest.mark.asyncio
async def test_s1_final_report_is_block(cases) -> None:
    case = await cases("S1")
    report = await case.pipeline.verify(case.claim, case.matter)
    assert report.final == "block"
    assert [(r.verdict, r.reason_code) for r in report.results if r.guard == "temporal"] == [
        ("block", "TEMPORAL_SUPERSEDED")
    ]
    assert next(r for r in report.results if r.guard == "source_gate").verdict == "pass"
    assert next(r for r in report.results if r.guard == "claim_verifier").verdict == "pass"
