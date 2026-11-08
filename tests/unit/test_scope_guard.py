"""Scope mapping used by the A-6 pipeline."""

from datetime import date
from typing import Any

import httpx
import pytest

from gba_agent.api.contracts_compat import _CompatMatterFact
from gba_agent.clients.graph_client import GraphClient
from gba_agent.guards import scope_guard
from gba_agent.guards.scope_guard import ScopeGuard
from tests.mocks.temporal import known, matter
from tests.unit.test_claim_verifier import claim

NODE = "Holding:synthetic"
FRAME = {
    "valid_from": "2025-01-01",
    "valid_to": None,
    "addressee_classes": ["controller", "interested_director"],
    "transaction_types": ["going_private"],
    "applies_when": {"fact": "closing_date", "gte": "2025-01-01"},
}


def graph_client(
    *, reply: dict[str, Any] | None = None, fail_frame: bool = False, fail_label: bool = False
) -> GraphClient:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            if fail_frame:
                return httpx.Response(503)
            return httpx.Response(
                200,
                json=reply if reply is not None else {"node_id": NODE, "frame": FRAME},
            )
        assert request.url.path == f"/graph/v1/nodes/{NODE}"
        if fail_label:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "node_id": NODE,
                "node_type": "Holding",
                "label": "Fixture holding",
                "known_from": "2024-01-01",
                "origin": "seed",
            },
        )

    return GraphClient("http://graph.test", "fixture", transport=httpx.MockTransport(respond))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tri, failed, expected, code",
    [
        ("true", None, "pass", "SCOPE_OK"),
        (False, "in_force", "block", "SCOPE_NOT_IN_FORCE"),
        (False, "addressee", "block", "SCOPE_ADDRESSEE_MISMATCH"),
        (False, "transaction_type", "block", "SCOPE_TRANSACTION_MISMATCH"),
        (False, "applies_when", "block", "SCOPE_CONDITION_UNMET"),
        ("false", "exception:x", "block", "SCOPE_EXCEPTION_APPLIES"),
        (False, "non_application", "block", "SCOPE_NON_APPLICATION"),
        (False, "invalid", "unknown", "SCOPE_FACTS_MISSING"),
        ("unknown", None, "unknown", "SCOPE_FACTS_MISSING"),
    ],
)
async def test_scope_verdict_mapping(
    tri: Any, failed: str | None, expected: str, code: str
) -> None:
    def evaluate(
        frame: dict[str, Any], facts: dict[str, _CompatMatterFact], as_of: date
    ) -> tuple[Any, list[str], str | None]:
        assert frame == FRAME and as_of.isoformat() == "2026-11-07"
        assert facts["closing_date"].value == "2026-01-01"
        return tri, ["controller_present"] if tri == "unknown" else [], failed

    async with graph_client() as graph:
        result = await ScopeGuard(graph, evaluate=evaluate).check_claim(
            claim(authority_node_id=NODE),
            matter(closing_date=known(date(2026, 1, 1)), interested_director_involved=known(False)),
        )
    assert result is not None and (result.verdict, result.reason_code) == (expected, code)
    assert "Fixture holding" in result.reason_text
    assert result.evidence["facts"] == {
        "closing_date": "2026-01-01",
        "interested_director_involved": False,
    }
    assert result.evidence["failed_clause"] == failed


@pytest.mark.asyncio
async def test_scope_skip_for_citation_and_non_scoped_node() -> None:
    async with graph_client() as graph:
        guard = ScopeGuard(graph)
        citation = await guard.check_claim(claim(kind="citation"), matter())
        other = await guard.check_claim(claim(authority_node_id="Case:synthetic"), matter())
    assert citation is not None and citation.reason_code == "SCOPE_SKIPPED_CITATION_ONLY"
    assert other is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["no-node", "no-frame", "wrong-node", "outage", "no-evaluator"])
async def test_scope_unavailable_is_unknown(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scope_guard, "shared_models", None)
    reply = (
        {"node_id": NODE, "frame": None}
        if mode == "no-frame"
        else {"node_id": "Holding:other", "frame": FRAME}
        if mode == "wrong-node"
        else None
    )
    async with graph_client(reply=reply, fail_frame=mode == "outage") as graph:
        result = await ScopeGuard(graph).check_claim(
            claim(authority_node_id=None if mode == "no-node" else NODE), matter()
        )
    assert result is not None and (result.verdict, result.reason_code) == (
        "unknown",
        "SCOPE_NO_FRAME",
    )


@pytest.mark.asyncio
async def test_scope_label_is_advisory() -> None:
    def evaluate(*_: object) -> tuple[bool, list[str], None]:
        return True, [], None

    async with graph_client(fail_label=True) as graph:
        result = await ScopeGuard(graph, evaluate=evaluate).check_claim(
            claim(authority_node_id=NODE), matter()
        )
    assert result is not None and result.verdict == "pass" and NODE in result.reason_text
