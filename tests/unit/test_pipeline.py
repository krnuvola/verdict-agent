from collections.abc import Mapping

import pytest

from gba_agent.api.contracts_compat import (
    _CompatClaim,
    _CompatGuardReport,
    _CompatGuardResult,
    _CompatMatter,
)
from gba_agent.guards.pipeline import ClaimCheck, GuardPipeline
from tests.mocks.temporal import matter
from tests.unit.test_abstention import result
from tests.unit.test_claim_verifier import claim

ORDER = ["source_gate", "claim_verifier", "temporal", "scope", "gnn_alert"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdicts, enabled, expected, order",
    [
        ({}, None, "pass", ORDER),
        ({"source_gate": "block"}, None, "block", ORDER[:1]),
        ({"claim_verifier": "block", "scope": "unknown"}, None, "block", ORDER),
        ({"source_gate": "unknown", "gnn_alert": "flag"}, None, "unknown", ORDER),
        ({"gnn_alert": "flag"}, None, "pass", ORDER),
        ({"gnn_alert": "unknown"}, None, "pass", ORDER),
        ({}, {"temporal", "gnn_alert"}, "pass", ["temporal", "gnn_alert"]),
        ({}, {"access"}, "pass", []),
    ],
)
async def test_pipeline_order_and_evidence(
    verdicts: Mapping[str, str], enabled: set[str] | None, expected: str, order: list[str]
) -> None:
    calls = []
    reports = []

    def check(name: str) -> ClaimCheck:
        async def execute(item: _CompatClaim, context: _CompatMatter) -> _CompatGuardResult:
            calls.append(name)
            assert item.matter_id == context.matter_id
            return result(name, verdicts.get(name, "pass"), proof=name)

        return execute

    async def record(item: _CompatClaim, report: _CompatGuardReport) -> None:
        assert item.claim_id == report.claim_id
        reports.append(report)

    pipeline = GuardPipeline(
        source_gate=check("source_gate"),
        claim_verifier=check("claim_verifier"),
        temporal=check("temporal"),
        scope=check("scope"),
        gnn_alert=check("gnn_alert"),
        record=record,
    )
    context = matter()
    item = claim(matter_id=context.matter_id)
    report = await (
        pipeline.verify(item, context)
        if enabled is None
        else pipeline.verify(item, context, enabled)
    )
    assert report.final == expected
    assert calls == order and [item.evidence["proof"] for item in report.results] == order
    assert reports == [report]
