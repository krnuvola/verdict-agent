"""Client URL, header, response, and retry contract tests."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from gba_agent.api.trace import TraceContext
from gba_agent.clients.graph_client import GraphClient
from gba_agent.clients.http import ServiceUnavailableError, UpstreamServiceError
from gba_agent.clients.workbench_client import WorkbenchClient

TRACE = TraceContext(
    request_id="01JDS7K3PZJ4C6S9W2H3G5T8VA",
    run_id="run_01JDS7K3PZJ4C6S9W2H3G5T8VA",
    matter_id="mtr_01JDS7K3PZJ4C6S9W2H3G5T8VA",
    contract_version="1.0.0",
)

MATTER = {
    "matter_id": TRACE.matter_id,
    "title": "Fixture",
    "task": "review",
    "question": "Review fixture",
    "as_of": "2026-11-07",
    "created_at": "2026-11-07T00:00:00Z",
}


async def no_sleep(_: float) -> None:
    return None


def response_handler(
    response: Callable[[httpx.Request], httpx.Response],
) -> httpx.MockTransport:
    return httpx.MockTransport(response)


@pytest.mark.asyncio
async def test_graph_client_propagates_auth_trace_and_routes_to_v1() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "node_id": "Provision:fixture",
                "node_type": "Provision",
                "label": "Fixture",
                "known_from": "2026-11-07",
                "origin": "seed",
            },
        )

    async with GraphClient(
        "http://graph.test",
        "test-token",
        TRACE,
        transport=response_handler(handler),
    ) as client:
        result = await client.get_node("Provision:fixture", "2026-11-07")

    assert result["node_id"] == "Provision:fixture"
    assert (
        str(seen[0].url) == "http://graph.test/graph/v1/nodes/Provision%3Afixture?as_of=2026-11-07"
    )
    assert seen[0].headers["Authorization"] == "Bearer test-token"
    assert seen[0].headers["X-Request-Id"] == TRACE.request_id
    assert seen[0].headers["X-Run-Id"] == TRACE.run_id
    assert seen[0].headers["X-Matter-Id"] == TRACE.matter_id
    assert seen[0].headers["X-Contract-Version"] == "1.0.0"


@pytest.mark.asyncio
async def test_graph_client_generates_missing_trace_ids() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "service": "graph-api",
                "version": "test",
                "contract_version": "1.0.0",
            },
        )

    async with GraphClient(
        "http://graph.test",
        "test-token",
        transport=response_handler(handler),
    ) as client:
        await client.health()

    assert len(seen[0].headers["X-Request-Id"]) == 26
    assert seen[0].headers["X-Run-Id"].startswith("run_")
    assert len(seen[0].headers["X-Run-Id"]) == 30
    assert seen[0].headers["X-Contract-Version"] == "1.0.0"


@pytest.mark.asyncio
async def test_graph_client_uses_120_second_gnn_timeout() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"model_version": "fixture", "scores": []})

    async with GraphClient(
        "http://graph.test",
        "token",
        transport=response_handler(handler),
        gnn_timeout=120,
    ) as client:
        await client.score_edges("2026-11-07", [])

    timeout_extension = seen[0].extensions["timeout"]
    assert timeout_extension["read"] == 120


@pytest.mark.asyncio
async def test_graph_client_retries_safe_get_and_maps_peer_error() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": {"code": "UPSTREAM_UNAVAILABLE"}})
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "service": "graph-api",
                "version": "test",
                "contract_version": "1.0.0",
            },
        )

    async with GraphClient(
        "http://graph.test",
        "token",
        transport=response_handler(handler),
        sleep=no_sleep,
    ) as client:
        assert (await client.health())["status"] == "ok"
    assert calls == 2

    def error_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "error": {
                    "code": "NOT_KNOWN_AS_OF",
                    "message": "not known",
                    "details": {},
                    "request_id": "request",
                }
            },
        )

    async with GraphClient(
        "http://graph.test", "token", transport=response_handler(error_handler)
    ) as client:
        with pytest.raises(UpstreamServiceError) as caught:
            await client.get_node("Provision:later", "2020-01-01")
    assert caught.value.code == "NOT_KNOWN_AS_OF"
    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_graph_post_is_not_retried_without_idempotency() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": {"code": "UPSTREAM_UNAVAILABLE"}})

    async with GraphClient(
        "http://graph.test",
        "token",
        transport=response_handler(handler),
        sleep=no_sleep,
    ) as client:
        with pytest.raises(UpstreamServiceError):
            await client.extract_facts(["sha256:" + "0" * 64], "2026-11-07")
    assert calls == 1


@pytest.mark.asyncio
async def test_graph_query_post_retries_when_explicitly_safe() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"node_id": "Provision:test", "frame": None})

    async with GraphClient(
        "http://graph.test",
        "token",
        transport=response_handler(handler),
        sleep=no_sleep,
    ) as client:
        await client.scope_frame("Provision:test", "2026-11-07")
    assert calls == 2


@pytest.mark.asyncio
async def test_exhausted_safe_retries_raise_upstream_unavailable() -> None:
    async def fail(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    async with GraphClient(
        "http://graph.test",
        "token",
        max_retries=1,
        transport=httpx.MockTransport(fail),
        sleep=no_sleep,
    ) as client:
        with pytest.raises(ServiceUnavailableError) as caught:
            await client.health()
    assert caught.value.code == "UPSTREAM_UNAVAILABLE"


@pytest.mark.asyncio
async def test_workbench_client_review_contract_and_yaml_content_type() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.method == "PUT":
            return httpx.Response(200, json={"playbook_id": "test", "version": 1})
        if request.method == "POST":
            return httpx.Response(202, json={"review_id": "rev_test"})
        return httpx.Response(
            200,
            json={
                "review_id": "rev_test",
                "state": "ready",
                "edits": [],
                "consistency_report": {},
                "error": None,
            },
        )

    async with WorkbenchClient(
        "http://workbench.test",
        "token",
        TRACE,
        transport=response_handler(handler),
    ) as client:
        assert await client.put_playbook("test", "playbook_id: test\n") == {
            "playbook_id": "test",
            "version": 1,
        }
        assert await client.create_review(
            {"doc_id": "sha256:" + "0" * 64, "playbook_id": "test", "matter": MATTER},
            idempotency_key="review-key",
        ) == {"review_id": "rev_test"}
        review = await client.get_review("rev_test")

    assert review["state"] == "ready"
    assert captured[0].headers["Content-Type"] == "application/yaml"
    assert captured[0].content == b"playbook_id: test\n"
    assert captured[1].headers["Idempotency-Key"] == "review-key"
    assert captured[2].headers["X-Run-Id"] == TRACE.run_id


@pytest.mark.asyncio
async def test_workbench_non_idempotent_post_is_not_retried() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    async with WorkbenchClient(
        "http://workbench.test",
        "token",
        transport=response_handler(handler),
        sleep=no_sleep,
    ) as client:
        with pytest.raises(UpstreamServiceError):
            await client.create_review(
                {"doc_id": "sha256:" + "0" * 64, "playbook_id": "test", "matter": MATTER}
            )
    assert calls == 1
