"""Shared scenario evidence setup; actual guards, explicit offline peer services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from html import escape
from typing import Any

from gba_agent.api.contracts_compat import _CompatClaim, _CompatMatter, _CompatMatterFact
from gba_agent.api.trace import new_ulid
from gba_agent.clients.graph_client import GraphClient
from gba_agent.guards.claim_verifier import ClaimVerifier
from gba_agent.guards.gnn_alert import GNNAlert
from gba_agent.guards.pipeline import GuardPipeline
from gba_agent.guards.scope_guard import ScopeGuard
from gba_agent.guards.source_gate import SourceDetails, SourceGate
from gba_agent.guards.temporal_guard import TemporalGuard
from tests.mocks import mock_graph
from tests.mocks.temporal import COMMITTEE, HOLDING, PROVISION_B, known, matter
from tests.sitesim.app import ORIGIN, Simulator
from tests.sitesim.fetcher import FetchFixture
from tests.unit.test_claim_verifier import Replay, llm, reply
from tests.unit.test_source_gate import Canon, record


def scope(
    frame: dict[str, Any], facts: dict[str, _CompatMatterFact], as_of: date
) -> tuple[bool | None, list[str], str | None]:
    return mock_graph.applicability(
        frame, {k: v.model_dump(mode="json") for k, v in facts.items()}, as_of
    )


def predicate(
    pred: dict[str, Any] | None, facts: dict[str, _CompatMatterFact], missing: set[str]
) -> bool | None:
    return mock_graph.evaluate(
        pred, {k: v.model_dump(mode="json") for k, v in facts.items()}, missing
    )


def scenario_matter(name: str) -> _CompatMatter:
    if name == "S2":
        return matter(signing_date=known("2023-05-01"), litigation_commenced_on=known("2023-11-01"))
    if name == "S5":
        return matter(
            transaction_type={"status": "unknown"},
            controller_present=known(True),
            litigation_commenced_on=known(None),
        )
    return matter(
        **COMMITTEE,
        transaction_type=known("going_private" if name == "S4" else "other_controller_deal"),
        controller_present=known(True),
        signing_date=known("2026-06-10"),
        litigation_commenced_on=known(None),
        mom_approval_obtained={"status": "unknown"},
    )


@dataclass
class ScenarioCase:
    name: str
    matter: _CompatMatter
    claim: _CompatClaim
    pipeline: GuardPipeline
    gate: SourceGate
    graph: GraphClient
    site: FetchFixture
    simulator: Simulator
    raw: bytes
    passage: str
    judge_replay: Replay


async def make_case(
    name: str, site: FetchFixture, graph: GraphClient, simulator: Simulator
) -> ScenarioCase:
    propositions = {
        "S1": "Match Group governs this deal.",
        "S2": "Section 144(b) governs this transaction.",
        "S4": "Committee approval alone gives the safe harbor.",
        "S5": "Section 144(b) applies to the transaction.",
        "FACT_TRUE": "The special committee was formed.",
        "FACT_FALSE": "No special committee was formed.",
    }
    proposition = propositions.get(name, "The illustrative document records a committee.")
    passage = proposition  # Synthetic textual support; applicability is independently guarded.
    raw = f"<p>{escape(passage)}</p>".encode()
    source = record(
        raw,
        url=f"{ORIGIN}/documents/0000000123-26-000001/{name}.htm",
        registry_entry_id="sitesim",
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    node_id = HOLDING if name == "S1" else PROVISION_B
    simulator.documents[f"/documents/0000000123-26-000001/{name}.htm"] = passage
    mock_graph.DOCUMENTS[source.doc_id] = {
        "text": passage,
        "meta": {
            "doc_id": source.doc_id,
            "source": source.model_dump(mode="json"),
            "title": "Illustrative test evidence; not legal authority",
            "doc_type": "8k",
            "doc_date": "2026-01-01",
            "canon_version": "1",
            "char_length": len(passage),
            "node_ids": [node_id],
            "page_markers": [],
        },
    }

    def canonicalize(data: bytes, media: str) -> Canon:
        assert data == raw and media == "text/html", "Unexpected evidence fixture"
        return Canon(text=passage, markers=[])

    gate = SourceGate(
        simulator.registry(access="api"),
        site.blobs,
        graph,
        site.database,
        environment="test",
        source_details=lambda _: SourceDetails("8k"),
        canonicalize=canonicalize,
    )
    admission = await gate.admit_document(source, raw)
    assert admission.admitted, admission.reason_code
    context = scenario_matter(name)
    claim = _CompatClaim(
        claim_id=f"clm_{new_ulid()}",
        matter_id=context.matter_id,
        kind="legal_proposition",
        text=proposition,
        authority_node_id=node_id,
        source_id=source.source_id,
        source_doc_id=source.doc_id,
        quote=passage,
        proposed_by="planner",
        as_of=context.as_of,
        span={"doc_id": source.doc_id, "start": 0, "end": len(passage), "quote": passage},
    )
    if name == "S6":
        claim = claim.model_copy(update={"authority_node_id": "Case:smith-v-jones-2021"})
    if name == "S7":
        claim = claim.model_copy(update={"quote": "A fabricated quotation absent from the source."})
    replay = Replay(
        {
            f"<proposition>{escape(proposition, quote=False)}</proposition> "
            f"<passage>{escape(passage, quote=False)}</passage>": reply("supports", 0.99)
        }
    )
    verifier = ClaimVerifier(site.blobs, judge=llm(replay))
    gnn = GNNAlert(graph)

    async def verify(item: _CompatClaim, _: _CompatMatter) -> Any:
        return await verifier.verify(item)

    pipeline = GuardPipeline(
        source_gate=gate.check_claim,
        claim_verifier=verify,
        temporal=TemporalGuard(graph, evaluate=predicate).check_claim,
        scope=ScopeGuard(graph, evaluate=scope).check_claim,
        gnn_alert=lambda item, ctx: gnn.check(item, ctx.as_of),
    )
    return ScenarioCase(
        name, context, claim, pipeline, gate, graph, site, simulator, raw, passage, replay
    )
