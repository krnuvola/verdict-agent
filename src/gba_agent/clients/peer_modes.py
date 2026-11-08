"""Explicit peer connection selection. No production import of development mocks."""

from __future__ import annotations

import base64
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from gba_agent.clients.http import ServiceUnavailableError
from gba_agent.config import Settings
from gba_agent.trajectory import canonical


def replay_key(request: httpx.Request) -> str:
    """Local HTTP cassette key; auth/correlation headers are never stored."""
    return sha256(
        canonical(
            {
                "method": request.method,
                "target": request.url.raw_path.decode("ascii"),
                "body_sha256": sha256(request.content).hexdigest(),
            }
        )
    ).hexdigest()


class _Cassette(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_sha256: str
    status_code: int
    headers: dict[str, str]
    body_base64: str


class PeerReplayTransport(httpx.AsyncBaseTransport):
    def __init__(self, root: Path, service: str) -> None:
        self.root, self.service = root, service

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key = replay_key(request)
        try:
            cassette = _Cassette.model_validate_json((self.root / f"{key}.json").read_bytes())
            if cassette.request_sha256 != key or not 100 <= cassette.status_code <= 599:
                raise ValueError("Invalid cassette identity/status")
            body = base64.b64decode(cassette.body_base64, validate=True)
        except (OSError, ValueError):
            raise ServiceUnavailableError(
                self.service, "Peer replay cassette missing or invalid"
            ) from None
        headers = {
            k: v
            for k, v in cassette.headers.items()
            if k.lower() not in {"authorization", "set-cookie", "cookie"}
            and k.lower() not in {"x-request-id", "x-run-id", "x-matter-id"}
        }
        # Current correlation IDs identify this replay invocation, not the recorded run.
        headers.update(
            {
                name: request.headers[name]
                for name in ("X-Request-Id", "X-Run-Id", "X-Matter-Id")
                if name in request.headers
            }
        )
        return httpx.Response(cassette.status_code, content=body, headers=headers)


def peer_url(settings: Settings, service: Literal["graph", "workbench"]) -> str:
    field = f"{service}_mock_api_url" if settings.peer_mode == "mock" else f"{service}_api_url"
    value: str = getattr(settings, field)
    return value


def peer_transport(
    settings: Settings, service: Literal["graph", "workbench"]
) -> httpx.AsyncBaseTransport | None:
    if settings.peer_mode == "replay":
        return PeerReplayTransport(settings.peer_replay_dir / service, f"{service}-api")
    return None


def peer_options(
    settings: Settings, service: Literal["graph", "workbench"], proxy: str | None = None
) -> dict[str, Any]:
    options: dict[str, Any] = {"strict_headers": True, "ca_bundle": settings.gba_ca_bundle}
    transport = peer_transport(settings, service)
    if transport is not None:
        options["transport"] = transport
    elif proxy:
        options["proxy"] = proxy
    return options
