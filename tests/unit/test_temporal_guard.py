"""Temporal Guard tests with contract-shaped, deterministic Graph responses."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx
import pytest

from gba_agent.api.contracts_compat import ClaimModel, _CompatMatterFact
from gba_agent.clients.graph_client import GraphClient
from gba_agent.guards import temporal_guard
from gba_agent.guards.temporal_guard import PredicateEvaluator, TemporalGuard
from tests.mocks.temporal import evaluator as evaluator
from tests.mocks.temporal import known, matter

NODE = "Holding:temporal-example"
AS_OF = "2000-06-15"
PAST = "1999-01-01"
FUTURE = "2000-06-16"


def authority(**overrides: Any) -> dict[str, Any]:
    return {
        "node_id": NODE,
        "node_type": "Holding",
        "label": "Synthetic authority",
        "known_from": PAST,
        "origin": "seed",
        **overrides,
    }


def validity(*events: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    return {
        "node_id": NODE,
        "as_of": AS_OF,
        "in_force": "true",
        # This aggregate intentionally cannot establish treatment applicability.
        "good_law_unconditional": "superseded",
        "treatments": list(events),
        "reasons": [],
        **overrides,
    }


def treatment(kind: str, name: str, **overrides: Any) -> dict[str, Any]:
    by = f"Holding:{name}"
    return {
        "edge": {
            "edge_id": name,
            "src": by,
            "dst": NODE,
            "edge_type": kind,
            "known_from": PAST,
            "origin": "seed",
            **overrides,
        },
        "effective_date": overrides.get("valid_from", PAST),
        "by_node_id": by,
    }


def graph_client(
    *,
    valid: dict[str, Any] | None = None,
    node: dict[str, Any] | None = None,
    error: Callable[[httpx.Request], httpx.Response] | None = None,
) -> GraphClient:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path == "/graph/v1/query/validity"
            assert json.loads(request.content) == {"node_id": NODE, "as_of": AS_OF}
            return (
                error(request)
                if error is not None
                else httpx.Response(200, json=validity() if valid is None else valid)
            )
        assert request.method == "GET"
        assert (
            request.url.raw_path
            == f"/graph/v1/nodes/Holding%3Atemporal-example?as_of={AS_OF}".encode()
        )
        return httpx.Response(200, json=authority() if node is None else node)

    return GraphClient(
        "http://graph.test", "fixture", transport=httpx.MockTransport(respond), max_retries=0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "node, in_force, verdict, code",
    [
        (authority(valid_from=FUTURE), "false", "block", "TEMPORAL_NOT_IN_FORCE"),
        (authority(valid_from=AS_OF, known_from=AS_OF), "true", "pass", "TEMPORAL_OK"),
        (authority(valid_to=FUTURE), "true", "pass", "TEMPORAL_OK"),
        (authority(valid_to=AS_OF), "false", "block", "TEMPORAL_EXPIRED"),
        (authority(), "true", "pass", "TEMPORAL_OK"),
        (authority(known_from=FUTURE), "true", "block", "TEMPORAL_NOT_IN_FORCE"),
        (authority(valid_from=FUTURE), "true", "unknown", "TEMPORAL_VALIDITY_UNKNOWN"),
        (authority(node_id="Holding:other"), "true", "unknown", "TEMPORAL_VALIDITY_UNKNOWN"),
        (authority(known_from="invalid"), "true", "unknown", "TEMPORAL_VALIDITY_UNKNOWN"),
    ],
)
async def test_historical_boundaries(
    node: dict[str, Any], in_force: str, verdict: str, code: str
) -> None:
    async with graph_client(node=node, valid=validity(in_force=in_force)) as graph:
        result = await TemporalGuard(graph).check(NODE, matter(AS_OF))
    assert (result.verdict, result.reason_code) == (verdict, code)
    assert result.deterministic and result.guard == "temporal"


@pytest.mark.asyncio
async def test_claim_uses_matter_date_instead_of_claim_date() -> None:
    fixture = matter(AS_OF)
    claim = ClaimModel.model_validate(
        {
            "claim_id": "clm_fixture",
            "matter_id": fixture.matter_id,
            "kind": "citation",
            "text": "Synthetic authority citation.",
            "authority_node_id": NODE,
            "as_of": FUTURE,
            "proposed_by": "fixture",
        }
    )
    async with graph_client() as graph:
        guard = TemporalGuard(graph)
        assert (await guard.check_claim(claim, fixture)).reason_code == "TEMPORAL_OK"
        result = await guard.check_claim(
            claim.model_copy(update={"authority_node_id": None}), fixture
        )
        assert (result.verdict, result.reason_code) == ("unknown", "TEMPORAL_VALIDITY_UNKNOWN")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "valid",
    [
        validity(as_of=FUTURE),
        validity(node_id="Holding:other"),
        validity(in_force="unknown"),
        validity(treatments=None),
        validity(treatment("limits", "unrelated", dst="Holding:other")),
    ],
)
async def test_mismatched_unknown_or_malformed_graph_data(valid: dict[str, Any]) -> None:
    async with graph_client(valid=valid) as graph:
        result = await TemporalGuard(graph).check(NODE, matter(AS_OF))
    assert (result.verdict, result.reason_code) == ("unknown", "TEMPORAL_VALIDITY_UNKNOWN")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, graph_code, verdict, code",
    [
        (404, "NOT_KNOWN_AS_OF", "block", "TEMPORAL_NOT_IN_FORCE"),
        (404, "NODE_NOT_FOUND", "unknown", "TEMPORAL_VALIDITY_UNKNOWN"),
        (503, "UPSTREAM_UNAVAILABLE", "unknown", "TEMPORAL_VALIDITY_UNKNOWN"),
    ],
)
async def test_graph_error_envelopes(status: int, graph_code: str, verdict: str, code: str) -> None:
    def error(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": graph_code, "message": "Fixture"}})

    async with graph_client(error=error) as graph:
        result = await TemporalGuard(graph).check(NODE, matter(AS_OF))
    assert (result.verdict, result.reason_code) == (verdict, code)
    assert result.evidence["graph_code"] == graph_code


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [httpx.ReadTimeout, httpx.RemoteProtocolError])
async def test_transport_outage(exception: type[httpx.RequestError]) -> None:
    def error(request: httpx.Request) -> httpx.Response:
        raise exception("Offline fixture", request=request)

    async with graph_client(error=error) as graph:
        result = await TemporalGuard(graph).check(NODE, matter(AS_OF))
    assert (result.verdict, result.reason_code) == ("unknown", "TEMPORAL_VALIDITY_UNKNOWN")


@pytest.mark.asyncio
async def test_historical_treatments_are_filtered_and_active_limit_warns() -> None:
    future_effect = treatment("overrules", "future-effect")
    future_effect["effective_date"] = FUTURE
    valid = validity(
        future_effect,
        treatment("overrules", "future-edge", valid_from=FUTURE),
        treatment("overrules", "future-publication", known_from=FUTURE),
        treatment("overrules", "expired-edge", valid_to=AS_OF),
        treatment("limits", "active-limit", valid_from=AS_OF, known_from=AS_OF),
    )
    async with graph_client(valid=valid) as graph:
        result = await TemporalGuard(graph).check(NODE, matter(AS_OF))
    assert (result.verdict, result.reason_code) == ("pass", "TEMPORAL_LIMITED_WARN")
    assert result.evidence["limited_by"] == ["Holding:active-limit"]
    assert "future-publication" not in repr(result.evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocking, expected",
    [
        (("overrules", "superseded_by_statute"), "TEMPORAL_OVERRULED"),
        (("superseded_by_statute",), "TEMPORAL_SUPERSEDED"),
        ((), "TEMPORAL_CONDITION_UNKNOWN"),
    ],
)
async def test_treatment_precedence_and_missing_fact_union(
    blocking: tuple[str, ...], expected: str, evaluator: PredicateEvaluator
) -> None:
    valid = validity(
        treatment("limits", "unconditional-limit"),
        treatment(
            "overrules", "conditionally-overrules", condition={"fact": "closing_date", "ne": None}
        ),
        treatment("limits", "conditional-limit", condition={"fact": "closing_date", "eq": None}),
        treatment(
            "superseded_by_statute",
            "conditional-supersession",
            condition={"fact": "controller_present", "eq": True},
        ),
        *(
            treatment(kind, kind, condition={"fact": "litigation_commenced_on", "eq": None})
            for kind in blocking
        ),
    )
    fixture = matter(
        AS_OF,
        controller_present={"status": "conflicting", "value": False},
        litigation_commenced_on=known(None),
    )
    async with graph_client(valid=valid) as graph:
        result = await TemporalGuard(graph, evaluate=evaluator).check(NODE, fixture)
    assert result.reason_code == expected
    assert result.verdict == ("block" if blocking else "unknown")
    if not blocking:
        assert result.evidence["missing_facts"] == ["closing_date", "controller_present"]


@pytest.mark.asyncio
async def test_false_compound_condition_does_not_add_missing_facts(
    evaluator: PredicateEvaluator,
) -> None:
    condition = {"all": [{"fact": "closing_date", "ne": None}, {"const": False}]}
    async with graph_client(
        valid=validity(treatment("overrules", "false", condition=condition))
    ) as graph:
        result = await TemporalGuard(graph, evaluate=evaluator).check(NODE, matter(AS_OF))
    assert (result.verdict, result.reason_code) == ("pass", "TEMPORAL_OK")
    assert "missing_facts" not in result.evidence


@pytest.mark.asyncio
async def test_absent_or_failing_predicate_evaluator_is_uncertain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(temporal_guard, "_shared_evaluate", None)

    def invalid(*_: object) -> bool:
        raise ValueError("Invalid condition")

    valid = validity(treatment("overrules", "conditional", condition={"const": True}))
    async with graph_client(valid=valid) as graph:
        for evaluate in (None, invalid):
            result = await TemporalGuard(graph, evaluate=evaluate).check(NODE, matter(AS_OF))
            assert (result.verdict, result.reason_code) == ("unknown", "TEMPORAL_CONDITION_UNKNOWN")


@pytest.mark.asyncio
async def test_python_date_fact_uses_iso_wire_value(evaluator: PredicateEvaluator) -> None:
    def check_facts(
        pred: dict[str, Any] | None, facts: dict[str, _CompatMatterFact], missing: set[str]
    ) -> bool | None:
        assert facts["closing_date"].value == AS_OF
        return evaluator(pred, facts, missing)

    event = treatment("limits", "date", condition={"fact": "closing_date", "lte": AS_OF})
    fixture = matter(AS_OF, closing_date=known(date.fromisoformat(AS_OF)))
    async with graph_client(valid=validity(event)) as graph:
        result = await TemporalGuard(graph, evaluate=check_facts).check(NODE, fixture)
    assert (result.verdict, result.reason_code) == ("pass", "TEMPORAL_LIMITED_WARN")
    assert fixture.facts["closing_date"].value == date.fromisoformat(AS_OF)
