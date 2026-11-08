"""S7 — an admitted source cannot substantiate a fabricated quotation."""

import pytest

from gba_agent.guards.claim_verifier import ClaimVerifier
from tests.sitesim.fetcher import FetchFixture
from tests.sitesim.fetcher import site as site
from tests.unit.test_claim_verifier import claim
from tests.unit.test_source_gate import Canon, GraphDouble, gate, record, row


@pytest.mark.asyncio
async def test_s7_fabricated_quotation_is_blocked(site: FetchFixture) -> None:
    raw = b"<p>This synthetic registry fixture does not state the proposed quotation.</p>"
    source = record(raw)
    canonical = Canon(text="This synthetic registry fixture does not state the proposed quotation.")
    assert (await gate(site, GraphDouble(content=canonical)).admit_document(source, raw)).admitted
    assert row(site, source.source_id).admitted
    result = await ClaimVerifier(site.blobs).verify(
        claim(
            source_id=source.source_id,
            source_doc_id=source.doc_id,
            quote="The court held that the fabricated exception applied.",
        )
    )
    assert result.guard == "claim_verifier"
    assert result.verdict == "block"
    assert result.reason_code == "CV_QUOTE_NOT_FOUND"
    assert result.evidence["quote_exact"] is False


@pytest.mark.asyncio
async def test_s7_final_report_is_block(cases) -> None:
    case = await cases("S7")
    report = await case.pipeline.verify(case.claim, case.matter)
    assert report.final == "block"
    assert next(r for r in report.results if r.guard == "source_gate").verdict == "pass"
    result = next(r for r in report.results if r.guard == "claim_verifier")
    assert (result.verdict, result.reason_code) == ("block", "CV_QUOTE_NOT_FOUND")
