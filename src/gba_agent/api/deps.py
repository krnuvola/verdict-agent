"""FastAPI dependencies for trace-aware downstream clients."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request

from gba_agent.api.trace import trace_from_request
from gba_agent.clients.graph_client import GraphClient
from gba_agent.clients.peer_modes import peer_options, peer_url
from gba_agent.clients.workbench_client import WorkbenchClient
from gba_agent.config import get_settings


async def graph_client_dependency(request: Request) -> AsyncIterator[GraphClient]:
    """Yield a Graph API client with request tracing propagated."""
    settings = get_settings()
    async with GraphClient(
        peer_url(settings, "graph"),
        settings.service_token.get_secret_value(),
        trace_from_request(request),
        **peer_options(settings, "graph"),
    ) as client:
        from gba_agent.api.routes_guards import _trajectory_factory

        trace = trace_from_request(request)
        writer = _trajectory_factory(request.app.state)("agent-api", trace.run_id or "adhoc")

        def observe(event: str, payload: dict[str, object]) -> None:
            writer.emit(
                component="orchestrator",
                type=event,
                payload=payload,
                matter_id=trace.matter_id or "",
            )

        client.observe = observe
        yield client


async def workbench_client_dependency(request: Request) -> AsyncIterator[WorkbenchClient]:
    """Yield a Workbench API client with request tracing propagated."""
    settings = get_settings()
    async with WorkbenchClient(
        peer_url(settings, "workbench"),
        settings.service_token.get_secret_value(),
        trace_from_request(request),
        **peer_options(settings, "workbench"),
    ) as client:
        yield client
