import json
from datetime import date
from typing import Any

import httpx
import pytest

from gba_agent.clients.graph_client import GraphClient
from gba_agent.guards.gnn_alert import GNNAlert
from tests.unit.test_claim_verifier import claim

SRC, DST = "Holding:synthetic", "Element:synthetic"
DAY = date(2024, 12, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, verdict, reason",
    [
        ("flag", "flag", "GNN_LOW_PLAUSIBILITY"),
        ("pass", "pass", "GNN_OK"),
        ("timeout", "unknown", "GNN_UNAVAILABLE"),
        ("outage", "unknown", "GNN_UNAVAILABLE"),
        ("wrong-edge", "unknown", "GNN_UNAVAILABLE"),
        ("invalid", "unknown", "GNN_UNAVAILABLE"),
        ("missing-anchor", None, None),
    ],
)
async def test_gnn_api_contract_and_outages(
    mode: str, verdict: str | None, reason: str | None
) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == "/graph/v1/gnn/score_edges" and request.method == "POST"
        assert request.extensions["timeout"]["read"] == 120
        probe = {"src": SRC, "dst": DST, "edge_type": "addresses"}
        assert json.loads(request.content) == {"as_of": DAY.isoformat(), "edges": [probe]}
        if mode == "timeout":
            raise httpx.ReadTimeout("fixture", request=request)
        if mode == "outage":
            return httpx.Response(503, json={"error": {"code": "UPSTREAM_UNAVAILABLE"}})
        score: dict[str, Any] = {
            **probe,
            "score_raw": -1.2,
            "score_calibrated": 0.2,
            "flag": mode == "flag",
        }
        if mode == "wrong-edge":
            score["dst"] = "Element:other"
        if mode == "invalid":
            score["score_calibrated"] = 1.2
        return httpx.Response(200, json={"model_version": "fixture-1", "scores": [score]})

    async with GraphClient(
        "http://graph.test", "fixture", transport=httpx.MockTransport(respond), max_retries=0
    ) as graph:
        check = claim(
            authority_node_id=SRC, element_node_id=None if mode == "missing-anchor" else DST
        )
        result = await GNNAlert(graph).check(check, DAY)
    if verdict is None:
        assert result is None and calls == []
    else:
        assert result is not None and len(calls) == 1
        assert (result.verdict, result.reason_code) == (verdict, reason)
        if verdict != "unknown":
            assert result.evidence == {
                "score_raw": -1.2,
                "score_calibrated": 0.2,
                "model_version": "fixture-1",
            }
