"""Advisory plausibility for a claim's authority-to-element edge."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from gba_agent.clients.contract_validation import UpstreamContractError
from gba_agent.clients.graph_client import GraphClient
from gba_agent.clients.http import UpstreamServiceError
from gba_agent.guards import reason_codes as rc

if TYPE_CHECKING:
    from gba_agent.api.contracts_compat import _CompatClaim as Claim
    from gba_agent.api.contracts_compat import _CompatGuardResult as GuardResult
else:
    from gba_agent.api.contracts_compat import GuardResult

guard_version = "1.0.0"


class _Score(BaseModel):
    src: str
    dst: str
    edge_type: Literal["addresses"]
    score_raw: float = Field(allow_inf_nan=False, strict=True)
    score_calibrated: float = Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    flag: bool = Field(strict=True)


class _Reply(BaseModel):
    model_version: str = Field(min_length=1)
    scores: list[_Score] = Field(min_length=1, max_length=1)


class GNNAlert:
    def __init__(self, graph: GraphClient) -> None:
        self._graph = graph

    async def check(self, claim: Claim, as_of: date) -> GuardResult | None:
        src, dst = claim.authority_node_id, claim.element_node_id
        if not src or not dst:
            return None
        try:
            reply = _Reply.model_validate(
                await self._graph.score_edges(
                    as_of, [{"src": src, "dst": dst, "edge_type": "addresses"}]
                )
            )
            score = reply.scores[0]
            if (score.src, score.dst) != (src, dst):
                raise UpstreamContractError("Graph returned a score for a different edge")
        except (UpstreamServiceError, UpstreamContractError, ValidationError, httpx.RequestError):
            return GuardResult(
                guard="gnn_alert",
                verdict="unknown",
                reason_code=rc.GNN_UNAVAILABLE,
                reason_text="Graph plausibility was unavailable for the cited edge.",
                deterministic=True,
                guard_version=guard_version,
            )
        return GuardResult(
            guard="gnn_alert",
            verdict="flag" if score.flag else "pass",
            reason_code=rc.GNN_LOW_PLAUSIBILITY if score.flag else rc.GNN_OK,
            reason_text=(
                "Graph flagged this edge for reverification."
                if score.flag
                else "Graph returned no plausibility alert for this edge."
            ),
            evidence={
                "score_raw": score.score_raw,
                "score_calibrated": score.score_calibrated,
                "model_version": reply.model_version,
            },
            deterministic=True,
            guard_version=guard_version,
        )
