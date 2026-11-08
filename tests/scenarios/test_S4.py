"""S4 — in-force provisions have separate transaction-specific scope."""

import pytest

from gba_agent.clients.graph_client import GraphClient
from gba_agent.guards.temporal_guard import TemporalGuard
from tests.mocks.mock_graph import applicability
from tests.mocks.temporal import COMMITTEE, PROVISION_B, PROVISION_C, fact_payload, known, matter
from tests.mocks.temporal import graph as graph


@pytest.mark.asyncio
async def test_s4_in_force_does_not_determine_transaction_scope(graph: GraphClient) -> None:
    fixture = matter(
        **COMMITTEE,
        transaction_type=known("going_private"),
        controller_present=known(True),
        litigation_commenced_on=known(None),
        mom_approval_obtained={"status": "unknown"},
    )
    facts = fact_payload(fixture)
    for node_id, expected_scope in ((PROVISION_B, False), (PROVISION_C, True)):
        result = await TemporalGuard(graph).check(node_id, fixture)
        assert (result.verdict, result.reason_code) == ("pass", "TEMPORAL_OK")
        frame = (await graph.scope_frame(node_id, fixture.as_of))["frame"]
        scope, _, failed = applicability(frame, facts, fixture.as_of)
        assert scope is expected_scope
        assert failed == (None if expected_scope else "transaction_type")

    standard = (await graph.standard_of_review(fixture.as_of, facts))["standard"]
    assert standard["safe_harbor"] == "unknown"
    assert any("mom_approval_obtained" in reading["requires"] for reading in standard["readings"])


@pytest.mark.asyncio
async def test_s4_final_report_is_block(cases) -> None:
    case = await cases("S4")
    report = await case.pipeline.verify(case.claim, case.matter)
    assert report.final == "block"
    result = next(r for r in report.results if r.guard == "scope")
    assert (result.verdict, result.reason_code) == ("block", "SCOPE_TRANSACTION_MISMATCH")
    assert result.evidence["failed_clause"] == "transaction_type"
