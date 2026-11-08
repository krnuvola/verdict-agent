"""Async HTTP client for the documented Graph API."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from gba_agent.api.trace import TraceContext
from gba_agent.clients.contract_validation import (
    validate_receipt,
    validate_shared_items,
    validate_shared_model,
)
from gba_agent.clients.http import AsyncServiceClient


class GraphClient(AsyncServiceClient):
    """Track A client for Graph API v1 endpoints."""

    def __init__(
        self,
        base_url: str,
        service_token: str,
        trace: TraceContext | None = None,
        *,
        timeout: float = 30,
        gnn_timeout: float = 120,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        strict_headers: bool = False,
        ca_bundle: Path | None = None,
        proxy: str | None = None,
    ) -> None:
        super().__init__(
            service="graph-api",
            base_url=base_url,
            prefix="/graph/v1",
            service_token=service_token,
            trace=trace,
            timeout=timeout,
            max_retries=max_retries,
            transport=transport,
            sleep=sleep,
            strict_headers=strict_headers,
            ca_bundle=ca_bundle,
            proxy=proxy,
        )
        self.gnn_timeout = gnn_timeout

    async def health(self) -> dict[str, Any]:
        return await super().health()

    async def get_node(self, node_id: str, as_of: date | str | None = None) -> dict[str, Any]:
        response = await self.request_json(
            "GET", f"/nodes/{quote(node_id, safe='')}", params={"as_of": as_of} if as_of else None
        )
        return validate_shared_model("Node", response)

    async def get_edges(
        self,
        node_id: str,
        *,
        as_of: date | str | None = None,
        edge_type: str | None = None,
        direction: str = "both",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"direction": direction}
        if as_of is not None:
            params["as_of"] = as_of
        if edge_type is not None:
            params["edge_type"] = edge_type
        response = await self.request_json(
            "GET", f"/nodes/{quote(node_id, safe='')}/edges", params=params
        )
        return validate_shared_items("Edge", response)

    async def scope_frame(self, node_id: str, as_of: date | str) -> dict[str, Any]:
        response = await self.request_json(
            "POST",
            "/query/scope_frame",
            json_body={"node_id": node_id, "as_of": str(as_of)},
            retry_safe=True,
        )
        return validate_shared_model("ScopeFrameResp", response)

    async def validity(self, node_id: str, as_of: date | str) -> dict[str, Any]:
        response = await self.request_json(
            "POST",
            "/query/validity",
            json_body={"node_id": node_id, "as_of": str(as_of)},
            retry_safe=True,
        )
        return validate_shared_model("ValidityResult", response)

    async def rule_candidates(
        self,
        query: str,
        as_of: date | str,
        *,
        top_k: int = 10,
        node_types: list[str] | None = None,
        facts: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"query": query, "as_of": str(as_of), "top_k": top_k}
        if node_types is not None:
            body["node_types"] = node_types
        if facts is not None:
            body["facts"] = dict(facts)
        response = await self.request_json(
            "POST", "/query/rule_candidates", json_body=body, retry_safe=True
        )
        result = validate_shared_model("RuleCandidatesResp", response)
        if self.observe:
            self.observe(
                "search",
                {
                    "query": query,
                    "endpoint": "/graph/v1/query/rule_candidates",
                    "result_count": len(result["items"]),
                },
            )
        return result

    async def standard_of_review(
        self, as_of: date | str, facts: Mapping[str, Any]
    ) -> dict[str, Any]:
        response = await self.request_json(
            "POST",
            "/query/standard_of_review",
            json_body={"as_of": str(as_of), "facts": dict(facts)},
            retry_safe=True,
        )
        return validate_shared_model("StandardResp", response)

    async def search_documents(
        self,
        query: str,
        as_of: date | str,
        *,
        top_k: int = 10,
        registry_entry_ids: list[str] | None = None,
        doc_types: list[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"query": query, "as_of": str(as_of), "top_k": top_k}
        if registry_entry_ids is not None:
            body["registry_entry_ids"] = registry_entry_ids
        if doc_types is not None:
            body["doc_types"] = doc_types
        response = await self.request_json(
            "POST", "/search/documents", json_body=body, retry_safe=True
        )
        result = validate_shared_model("DocSearchResp", response)
        if self.observe:
            self.observe(
                "search",
                {
                    "query": query,
                    "endpoint": "/graph/v1/search/documents",
                    "result_count": len(result["items"]),
                },
            )
        return result

    async def document_meta(self, doc_id: str) -> dict[str, Any]:
        response = await self.request_json("GET", f"/documents/{quote(doc_id, safe='')}/meta")
        return validate_shared_model("DocMeta", response)

    async def document_text(self, doc_id: str) -> str:
        return await self.request_text("GET", f"/documents/{quote(doc_id, safe='')}/text")

    async def register_document(
        self, source_record: Mapping[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        response = await self.request_json(
            "POST",
            "/documents/register",
            json_body={"source": dict(source_record)},
            idempotency_key=idempotency_key,
        )
        return validate_shared_model("DocMeta", response)

    async def extract_facts(
        self,
        doc_ids: list[str],
        as_of: date | str,
        *,
        fact_keys: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"doc_ids": doc_ids, "as_of": str(as_of)}
        if fact_keys is not None:
            body["fact_keys"] = fact_keys
        response = await self.request_json(
            "POST",
            "/extract/facts",
            json_body=body,
            idempotency_key=idempotency_key,
        )
        return validate_receipt(response, {"job_id": str})

    async def job_status(self, job_id: str) -> dict[str, Any]:
        response = await self.request_json("GET", f"/jobs/{quote(job_id, safe='')}")
        return validate_shared_model("JobStatus", response)

    async def score_edges(
        self, as_of: date | str, edges: list[Mapping[str, Any]]
    ) -> dict[str, Any]:
        response = await self.request_json(
            "POST",
            "/gnn/score_edges",
            json_body={"as_of": str(as_of), "edges": [dict(edge) for edge in edges]},
            timeout=self.gnn_timeout,
            retry_safe=True,
        )
        return validate_shared_model("ScoreEdgesResp", response)

    async def predict_missing(
        self,
        as_of: date | str,
        anchor_node_ids: list[str],
        *,
        exclude_node_ids: list[str] | None = None,
        top_k: int = 10,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "as_of": str(as_of),
            "anchor_node_ids": anchor_node_ids,
            "top_k": top_k,
        }
        if exclude_node_ids is not None:
            body["exclude_node_ids"] = exclude_node_ids
        response = await self.request_json(
            "POST",
            "/gnn/predict_missing",
            json_body=body,
            timeout=self.gnn_timeout,
            retry_safe=True,
        )
        return validate_shared_model("PredictMissingResp", response)

    async def registry(self) -> dict[str, Any]:
        return await self.request_json("GET", "/registry")
