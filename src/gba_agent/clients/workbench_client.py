"""Async HTTP client for the documented Document Workbench API."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict

from gba_agent.api.contracts_compat import _CompatProposedEdit
from gba_agent.api.trace import TraceContext
from gba_agent.clients.contract_validation import validate_receipt, validate_shared_model
from gba_agent.clients.http import AsyncServiceClient, UpstreamServiceError


class ReviewStatus(BaseModel):
    """Doc 00 section 9.3 fallback when the shared package is absent."""

    model_config = ConfigDict(extra="forbid")
    review_id: str
    state: Literal["queued", "running", "ready", "failed"]
    edits: list[_CompatProposedEdit]
    consistency_report: dict[str, Any]
    error: str | None = None


class WorkbenchClient(AsyncServiceClient):
    """Track A client for Workbench API v1 endpoints."""

    def __init__(
        self,
        base_url: str,
        service_token: str,
        trace: TraceContext | None = None,
        *,
        timeout: float = 30,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        strict_headers: bool = False,
        ca_bundle: Path | None = None,
        proxy: str | None = None,
    ) -> None:
        super().__init__(
            service="workbench-api",
            base_url=base_url,
            prefix="/wb/v1",
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

    async def health(self) -> dict[str, Any]:
        return await super().health()

    async def ingest_document(self, doc_id: str) -> dict[str, Any]:
        response = await self.request_json("POST", "/documents", json_body={"doc_id": doc_id})
        return validate_shared_model("DocRecord", response)

    async def get_document(self, doc_id: str) -> dict[str, Any]:
        response = await self.request_json("GET", f"/documents/{quote(doc_id, safe='')}")
        return validate_shared_model("DocRecord", response)

    async def put_playbook(
        self,
        playbook_id: str,
        yaml_body: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/yaml"}
        response = await self._request(
            "PUT",
            f"/playbooks/{quote(playbook_id, safe='')}",
            content=yaml_body.encode("utf-8"),
            headers=headers,
            idempotency_key=idempotency_key,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamServiceError(
                self.service,
                502,
                "UPSTREAM_UNAVAILABLE",
                "Peer returned non-JSON playbook metadata",
            ) from exc
        if not isinstance(payload, dict):
            raise UpstreamServiceError(
                self.service,
                502,
                "UPSTREAM_UNAVAILABLE",
                "Peer returned non-object playbook metadata",
            )
        return validate_receipt(payload, {"playbook_id": str, "version": int})

    async def get_playbook(self, playbook_id: str) -> str:
        return await self.request_text("GET", f"/playbooks/{quote(playbook_id, safe='')}")

    async def create_review(
        self,
        request_body: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        response = await self.request_json(
            "POST",
            "/reviews",
            json_body=dict(request_body),
            idempotency_key=idempotency_key,
        )
        return validate_receipt(response, {"review_id": str})

    async def get_review(self, review_id: str) -> dict[str, Any]:
        response = await self.request_json("GET", f"/reviews/{quote(review_id, safe='')}")
        return ReviewStatus.model_validate(
            validate_shared_model("ReviewStatus", response)
        ).model_dump(mode="json")

    async def decide_edit(
        self,
        review_id: str,
        edit_id: str,
        decision: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        response = await self.request_json(
            "POST",
            f"/reviews/{quote(review_id, safe='')}/edits/{quote(edit_id, safe='')}/decision",
            json_body=dict(decision),
            idempotency_key=idempotency_key,
        )
        return validate_shared_model("ProposedEdit", response)

    async def redline(self, review_id: str) -> tuple[bytes, str]:
        return await self.request_bytes("GET", f"/reviews/{quote(review_id, safe='')}/redline.docx")

    async def accepted_copy(self, review_id: str) -> tuple[bytes, str]:
        return await self.request_bytes(
            "GET", f"/reviews/{quote(review_id, safe='')}/accepted.docx"
        )

    async def summary(self, review_id: str) -> str:
        return await self.request_text("GET", f"/reviews/{quote(review_id, safe='')}/summary.md")
