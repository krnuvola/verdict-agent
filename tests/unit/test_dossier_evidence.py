"""Dossier publication must never upgrade incomplete evidence."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from gba_agent.api.contracts_compat import _CompatClaim, _CompatGuardReport
from gba_agent.orchestrator.dossier import build
from tests.mocks.temporal import matter


@pytest.mark.parametrize("verdict", [None, "pass", "block", "unknown"])
def test_missing_citation_never_establishes_law(verdict: str | None) -> None:
    item = matter()
    claim = _CompatClaim(
        claim_id="clm_fixture",
        matter_id=item.matter_id,
        kind="legal_proposition",
        text="Illustrative assertion",
        authority_node_id="Provision:fixture",
        as_of=item.as_of,
        proposed_by="test",
    )
    report = (
        None
        if verdict is None
        else _CompatGuardReport.model_validate(
            {
                "claim_id": claim.claim_id,
                "results": [],
                "final": verdict,
            }
        )
    )
    store = Mock()
    store.claims.return_value = [(claim, report)]
    standard: dict[str, Any] = {
        "value": "entire_fairness",
        "safe_harbor": "false",
        "basis": ["Provision:fixture"],
        "readings": [],
    }
    result = build(
        "run_fixture",
        item,
        store,
        datetime(2026, 11, 7, tzinfo=UTC),
        standard,
        [],
        [],
        {},
        Path("trajectory"),
        Path("config/facts_questions.yaml"),
    )
    assert result["authorities"] == []
    assert result["standard_of_review"]["value"] == "unknown"
    assert bool(result["dropped_claims"]) == (verdict == "block")
    assert result["abstentions"]
    assert standard["value"] == "entire_fairness"


@pytest.mark.asyncio
async def test_workbench_mock_duplicate_review_has_stable_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from tests.mocks.mock_workbench import app

    monkeypatch.setenv("SERVICE_TOKEN", "test-token")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = {
            "Authorization": "Bearer test-token",
            "Idempotency-Key": "dossier-repeat",
            "X-Contract-Version": "1.0.0",
        }
        body = {"doc_id": "sha256:" + "a" * 64, "playbook_id": "fixture", "matter": {}}
        first = await client.post("/wb/v1/reviews", json=body, headers=headers)
        second = await client.post("/wb/v1/reviews", json=body, headers=headers)
        assert first.status_code == second.status_code == 202
        assert first.json() == second.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"review_id": "rev_test", "state": "invented"}])
async def test_malformed_workbench_status_rejected(payload: dict[str, Any]) -> None:
    import httpx

    from gba_agent.clients.workbench_client import WorkbenchClient

    async with WorkbenchClient(
        "http://test",
        "test-token",
        max_retries=0,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    ) as client:
        with pytest.raises(ValueError):
            await client.get_review("rev_test")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"review_id": 42}, {"review_id": ""}])
async def test_invalid_review_receipt_rejected(payload: dict[str, Any]) -> None:
    import httpx

    from gba_agent.clients.workbench_client import WorkbenchClient

    async with WorkbenchClient(
        "http://test",
        "test-token",
        max_retries=0,
        transport=httpx.MockTransport(lambda _: httpx.Response(202, json=payload)),
    ) as client:
        with pytest.raises(ValueError):
            await client.create_review({"doc_id": "sha256:" + "a" * 64})


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["not json", "[]"])
async def test_invalid_playbook_metadata_rejected(body: str) -> None:
    import httpx

    from gba_agent.clients.http import UpstreamServiceError
    from gba_agent.clients.workbench_client import WorkbenchClient

    async with WorkbenchClient(
        "http://test",
        "test-token",
        max_retries=0,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)),
    ) as client:
        with pytest.raises(UpstreamServiceError):
            await client.put_playbook("fixture", "version: 1")
