"""Offline Graph and predicate fixtures for Temporal Guard tests."""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio

from gba_agent.api.contracts_compat import MatterModel, _CompatMatter, _CompatMatterFact
from gba_agent.clients.graph_client import GraphClient
from gba_agent.guards.temporal_guard import PredicateEvaluator
from tests.mocks.mock_graph import app, evaluate

PROVISION_B = "Provision:de.dgcl.144b@2025-03-25"
PROVISION_C = "Provision:de.dgcl.144c@2025-03-25"
HOLDING = "Holding:match-group-2024"
COMMITTEE = {
    key: {"status": "known", "value": True}
    for key in (
        "special_committee_formed",
        "committee_all_disinterested",
        "committee_power_to_negotiate",
        "committee_power_to_say_no",
        "committee_approval_obtained",
    )
}


def known(value: object) -> dict[str, Any]:
    return {"status": "known", "value": value}


def matter(as_of: str = "2026-11-07", /, **facts: dict[str, Any]) -> _CompatMatter:
    return MatterModel.model_validate(
        {
            "matter_id": "mtr_01JDS7K3PZJ4C6S9W2H3G5T8VA",
            "title": "Illustrative temporal fixture",
            "task": "research",
            "question": "Which authorities are in force?",
            "as_of": date.fromisoformat(as_of),
            "facts": {key: {"key": key, **fact} for key, fact in facts.items()},
            "created_at": datetime(2026, 11, 7, tzinfo=UTC),
        }
    )


def fact_payload(matter: _CompatMatter) -> dict[str, Any]:
    return {key: fact.model_dump(mode="json") for key, fact in matter.facts.items()}


def _fixture_evaluate(
    pred: dict[str, Any] | None, facts: dict[str, _CompatMatterFact], missing: set[str]
) -> bool | None:
    return evaluate(
        pred, {key: fact.model_dump(mode="json") for key, fact in facts.items()}, missing
    )


@pytest.fixture
def evaluator() -> PredicateEvaluator:
    try:
        from gba_contracts.predicates import evaluate as shared_evaluate  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        if exc.name != "gba_contracts":
            raise
        return _fixture_evaluate
    shared: PredicateEvaluator = shared_evaluate
    return shared


@pytest_asyncio.fixture
async def graph(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[GraphClient]:
    token = "temporal-fixture-token"
    monkeypatch.setenv("SERVICE_TOKEN", token)
    async with GraphClient(
        "http://graph.test", token, transport=httpx.ASGITransport(app=app), max_retries=0
    ) as client:
        yield client
