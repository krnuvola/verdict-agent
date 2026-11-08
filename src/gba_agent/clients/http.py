"""Shared async HTTP transport behavior for project service clients."""

from __future__ import annotations

import asyncio
import email.utils
import re
import ssl
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any, Self

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from gba_agent.api.contracts_compat import ErrorEnvelope
from gba_agent.api.trace import TraceContext, new_ulid
from gba_agent.clients.contract_validation import validate_request
from gba_agent.trajectory import canonical

type ClientObserver = Callable[[str, dict[str, Any]], None]


class _Health(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    service: str
    version: str
    contract_version: str


class UpstreamServiceError(RuntimeError):
    """A peer returned an HTTP error or a response outside the expected shape."""

    def __init__(
        self,
        service: str,
        status_code: int,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{service} returned HTTP {status_code}: {message}")
        self.service = service
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = dict(details or {})


class ServiceUnavailableError(UpstreamServiceError):
    """A peer could not be reached after safe retries."""

    def __init__(self, service: str, message: str) -> None:
        super().__init__(
            service=service,
            status_code=503,
            code="UPSTREAM_UNAVAILABLE",
            message=message,
        )


class AsyncServiceClient:
    """HTTPX client with bearer auth, trace propagation, and bounded safe retries."""

    def __init__(
        self,
        *,
        service: str,
        base_url: str,
        prefix: str,
        service_token: str,
        trace: TraceContext | None = None,
        timeout: float = 30,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        strict_headers: bool = False,
        ca_bundle: Path | None = None,
        proxy: str | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self.service = service
        self.strict_headers = strict_headers
        self.observe: ClientObserver | None = None
        self.prefix = prefix.rstrip("/")
        self.max_retries = max_retries
        self.sleep = sleep or asyncio.sleep
        self._timeout = httpx.Timeout(timeout)
        trace = trace or TraceContext(
            request_id=new_ulid(),
            run_id=f"run_{new_ulid()}",
            matter_id=None,
            contract_version="1.0.0",
        )
        if trace.run_id is None:
            trace = TraceContext(
                request_id=trace.request_id,
                run_id=f"run_{new_ulid()}",
                matter_id=trace.matter_id,
                contract_version=trace.contract_version,
            )
        headers = {
            "Authorization": f"Bearer {service_token}",
            "X-Contract-Version": trace.contract_version,
        }
        headers.update(trace.as_headers())
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=self._timeout,
            transport=transport,
            trust_env=False,
            proxy=proxy,
            verify=ssl.create_default_context(cafile=str(ca_bundle)) if ca_bundle else True,
        )

    async def health(self) -> dict[str, Any]:
        payload = await self.request_json("GET", "/health")
        try:
            result = _Health.model_validate(payload).model_dump(mode="json")
            if result["status"] != "ok":
                raise ValueError("Unhealthy peer")
        except (ValueError, ValidationError):
            raise ServiceUnavailableError(
                self.service, "Invalid or unhealthy peer health response"
            ) from None
        self._version(result["contract_version"])
        return result

    def _version(self, version: str | None) -> None:
        if version is None and not self.strict_headers:
            return
        if version is None or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
            raise ServiceUnavailableError(
                self.service, "Peer contract version missing or malformed"
            )
        if version.split(".", 1)[0] != "1":
            raise UpstreamServiceError(
                self.service,
                400,
                "CONTRACT_VERSION_MISMATCH",
                "Peer contract major version differs",
            )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTPX connection pool."""
        await self._client.aclose()

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        idempotency_key: str | None = None,
        retry_safe: bool = False,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Issue a JSON request and decode its response."""
        response = await self._request(
            method,
            path,
            json_body=json_body,
            params=params,
            timeout=timeout,
            idempotency_key=idempotency_key,
            retry_safe=retry_safe,
            content=content,
            headers=headers,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamServiceError(
                self.service,
                502,
                "UPSTREAM_UNAVAILABLE",
                "Peer returned a non-JSON response",
            ) from exc
        if not isinstance(payload, dict):
            raise UpstreamServiceError(
                self.service,
                502,
                "UPSTREAM_UNAVAILABLE",
                "Peer returned a JSON value that was not an object",
            )
        return payload

    async def request_text(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> str:
        """Issue a request whose successful response is plain text."""
        response = await self._request(method, path, params=params, timeout=timeout)
        return response.text

    async def request_bytes(
        self,
        method: str,
        path: str,
        *,
        timeout: float | None = None,
    ) -> tuple[bytes, str]:
        """Issue a request whose successful response is a binary file."""
        response = await self._request(method, path, timeout=timeout)
        return response.content, response.headers.get("content-type", "application/octet-stream")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        idempotency_key: str | None = None,
        retry_safe: bool = False,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        method = method.upper()
        url = self.path(path)
        if json_body is not None:
            model_name = {
                ("graph-api", "/query/scope_frame"): "ScopeFrameReq",
                ("graph-api", "/query/validity"): "ValidityReq",
                ("graph-api", "/query/rule_candidates"): "RuleCandidatesReq",
                ("graph-api", "/query/standard_of_review"): "StandardReq",
                ("graph-api", "/search/documents"): "DocSearchReq",
                ("graph-api", "/documents/register"): "RegisterDocReq",
                ("graph-api", "/extract/facts"): "ExtractReq",
                ("graph-api", "/gnn/score_edges"): "ScoreEdgesReq",
                ("graph-api", "/gnn/predict_missing"): "PredictMissingReq",
                ("workbench-api", "/documents"): "IngestDocReq",
                ("workbench-api", "/reviews"): "CreateReviewReq",
            }.get((self.service, path))
            if self.service == "workbench-api" and path.endswith("/decision"):
                model_name = "EditDecisionReq"
            if model_name is not None:
                json_body = validate_request(model_name, json_body)
        request_headers = dict(headers or {})
        if idempotency_key:
            request_headers["Idempotency-Key"] = idempotency_key
        can_retry = (
            method in {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}
            or retry_safe
            or idempotency_key is not None
        )
        attempts = self.max_retries + 1 if can_retry else 1
        request_timeout = httpx.Timeout(timeout) if timeout is not None else self._timeout
        last_error: Exception | None = None

        for attempt in range(attempts):
            started = perf_counter()

            def record(status: int | str, started: float = started, attempt: int = attempt) -> None:
                if self.observe is not None:
                    self.observe(
                        "graph_query" if self.service == "graph-api" else "state_change",
                        {
                            "from": "requesting",
                            "to": "response_received",
                            "operation": "peer_request",
                            "endpoint": self.path(path),
                            "request_hash": sha256(
                                canonical(
                                    {
                                        "method": method,
                                        "path": path,
                                        "body": json_body,
                                        "params": str(httpx.QueryParams(params))
                                        if params
                                        else None,
                                        "content_hash": sha256(content).hexdigest()
                                        if content
                                        else None,
                                    }
                                )
                            ).hexdigest(),
                            "status": status,
                            "ms": (perf_counter() - started) * 1000,
                            "attempt": attempt + 1,
                            "service": self.service,
                        },
                    )

            try:
                if self.observe is not None:
                    self.observe(
                        "state_change",
                        {
                            "from": "prepared",
                            "to": "requesting",
                            "operation": "peer_request_started",
                            "endpoint": self.path(path),
                            "service": self.service,
                            "attempt": attempt + 1,
                            "request_hash": sha256(
                                canonical(
                                    {
                                        "method": method,
                                        "path": path,
                                        "body": json_body,
                                        "params": str(httpx.QueryParams(params))
                                        if params
                                        else None,
                                        "content_hash": sha256(content).hexdigest()
                                        if content
                                        else None,
                                    }
                                )
                            ).hexdigest(),
                        },
                    )
                response = await self._client.request(
                    method,
                    url,
                    json=json_body,
                    content=content,
                    params=params,
                    headers=request_headers,
                    timeout=request_timeout,
                )
            except asyncio.CancelledError:
                record("cancelled")
                raise
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                record("unavailable")
                last_error = exc
                if attempt + 1 == attempts:
                    break
                await self.sleep(min(0.25 * (2**attempt), 2.0))
                continue

            record(response.status_code)
            self._version(response.headers.get("X-Contract-Version"))
            if self.strict_headers:
                for name in ("X-Request-Id", "X-Run-Id", "X-Matter-Id"):
                    expected = request_headers.get(name, self._client.headers.get(name))
                    if expected is not None and response.headers.get(name) != expected:
                        raise ServiceUnavailableError(
                            self.service, "Peer correlation header mismatch"
                        )
            if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                await self.sleep(self._retry_delay(response, attempt))
                continue
            if response.is_error:
                raise self._error_from_response(response)
            if not response.is_success:
                raise ServiceUnavailableError(self.service, "Peer returned an unexpected redirect")
            return response

        raise ServiceUnavailableError(
            self.service, "Peer timed out or was unreachable after safe retries"
        ) from last_error

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), 2.0)
            except ValueError:
                try:
                    retry_at: datetime = email.utils.parsedate_to_datetime(retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    delay = (retry_at - datetime.now(UTC)).total_seconds()
                    return float(min(max(delay, 0), 2))
                except (TypeError, ValueError, OverflowError):
                    pass
        return float(min(0.25 * (2**attempt), 2.0))

    def _error_from_response(self, response: httpx.Response) -> UpstreamServiceError:
        try:
            body = response.json()
        except ValueError:
            body = {}
        error = body.get("error", {}) if isinstance(body, dict) else {}
        if not isinstance(error, dict):
            error = {}
        try:
            validated = ErrorEnvelope.model_validate(body).model_dump(mode="json")["error"]
        except ValidationError:
            return ServiceUnavailableError(self.service, "Peer returned an invalid ErrorEnvelope")
        code, message, details = validated["code"], validated["message"], validated["details"]
        statuses = {
            "UNAUTHORIZED": {401},
            "VALIDATION_ERROR": {422},
            "CONTRACT_VERSION_MISMATCH": {400},
            "UPSTREAM_UNAVAILABLE": {502, 503},
            "INTERNAL_ERROR": {500},
            "NODE_NOT_FOUND": {404},
            "NOT_KNOWN_AS_OF": {404},
            "DOC_NOT_FOUND": {404},
            "JOB_NOT_FOUND": {404},
            "RUN_NOT_FOUND": {404},
            "DOSSIER_NOT_READY": {404},
            "HANDOFF_NOT_FOUND": {404},
            "REVIEW_NOT_FOUND": {404},
            "EDIT_NOT_FOUND": {404},
            "PLAYBOOK_NOT_FOUND": {404},
            "REVIEW_NOT_READY": {409},
            "UNSUPPORTED_FOR_REDLINE": {422},
            "TOO_MANY_CLAIMS": {422},
        }
        if response.status_code not in statuses.get(code, set()):
            return ServiceUnavailableError(self.service, "Peer error code/status violates contract")
        return UpstreamServiceError(
            service=self.service,
            status_code=response.status_code,
            code=code,
            message=message if isinstance(message, str) else "Peer request failed",
            details=details if isinstance(details, dict) else {},
        )

    def path(self, relative_path: str) -> str:
        """Return a path relative to the peer's service prefix."""
        if relative_path in {"/health", "/openapi.json"}:
            return relative_path
        return f"{self.prefix}/{relative_path.lstrip('/')}"
