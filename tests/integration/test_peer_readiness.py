"""Offline dependency boundary checks; these do not certify real B/C services."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from gba_agent.clients.contract_validation import UpstreamContractError
from gba_agent.clients.graph_client import GraphClient
from gba_agent.clients.http import ServiceUnavailableError, UpstreamServiceError
from gba_agent.clients.peer_modes import PeerReplayTransport, peer_options, peer_url, replay_key
from gba_agent.clients.workbench_client import WorkbenchClient
from tests.mocks.mock_graph import app as graph_app
from tests.mocks.mock_workbench import app as wb_app
from tests.unit.test_config import valid_settings


@pytest.mark.asyncio
@pytest.mark.parametrize("client_type,app", [(GraphClient, graph_app), (WorkbenchClient, wb_app)])
async def test_root_health_and_openapi_against_existing_mocks(client_type, app, monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN", "test-token")
    async with client_type(
        "http://peer.test",
        "test-token",
        strict_headers=True,
        transport=httpx.ASGITransport(app=app),
    ) as client:
        assert (await client.health())["contract_version"] == "1.0.0"
        schema = await client.request_json("GET", "/openapi.json")
        assert "paths" in schema


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "version,code", [("2.0.0", "CONTRACT_VERSION_MISMATCH"), (None, "UPSTREAM_UNAVAILABLE")]
)
async def test_invalid_peer_version_is_visible(version, code):
    def handler(request):
        headers = {k: v for k, v in request.headers.items() if k.startswith("x-")}
        headers.pop("x-contract-version", None)
        if version:
            headers["X-Contract-Version"] = version
        return httpx.Response(200, json={}, headers=headers)

    async with GraphClient(
        "http://peer.test", "token", strict_headers=True, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(UpstreamServiceError) as error:
            await client.get_node("Provision:fixture")
        assert error.value.code == code


@pytest.mark.asyncio
async def test_malformed_nested_response_and_correlation_rejected():
    def handler(request):
        return httpx.Response(
            200,
            json={"node_id": "Provision:fixture"},
            headers={k: v for k, v in request.headers.items() if k.startswith("x-")},
        )

    async with GraphClient(
        "http://peer.test", "token", strict_headers=True, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(UpstreamContractError):
            await client.get_node("Provision:fixture")
    async with GraphClient(
        "http://peer.test",
        "token",
        strict_headers=True,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={}, headers={"X-Contract-Version": "1.0.0"})
        ),
    ) as client:
        with pytest.raises(ServiceUnavailableError, match="correlation"):
            await client.get_node("Provision:fixture")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout])
async def test_unavailable_dependency_has_no_fallback(failure):
    calls = []

    def handler(request):
        calls.append(request)
        raise failure("offline", request=request)

    async def no_sleep(_):
        pass

    async with GraphClient(
        "http://missing.test",
        "token",
        max_retries=2,
        transport=httpx.MockTransport(handler),
        sleep=no_sleep,
    ) as client:
        with pytest.raises(ServiceUnavailableError):
            await client.get_node("Provision:fixture")
    assert len(calls) == 3
    assert {r.url.host for r in calls} == {"missing.test"}


def test_explicit_modes_and_production_rejection(tmp_path):
    live = valid_settings(peer_mode="live")
    mock = valid_settings(peer_mode="mock")
    replay = valid_settings(peer_mode="replay", peer_replay_dir=tmp_path)
    assert peer_url(live, "graph") == "http://localhost:8101"
    assert peer_url(mock, "graph") == "http://localhost:18101"
    assert peer_options(live, "graph")["strict_headers"] is True
    assert "transport" not in peer_options(live, "graph")
    assert isinstance(peer_options(replay, "graph")["transport"], PeerReplayTransport)
    assert (
        peer_options(live, "graph", "http://proxy.test:8199")["proxy"] == "http://proxy.test:8199"
    )
    assert "proxy" not in peer_options(replay, "graph", "http://proxy.test:8199")
    with pytest.raises(ValidationError):
        valid_settings(peer_mode="mock", gba_env="prod")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"review_id": "rev_fixture", "state": "complete", "edits": [], "consistency_report": {}},
        {
            "review_id": "rev_fixture",
            "state": "ready",
            "edits": [{"edit_id": "lost-provenance"}],
            "consistency_report": {},
        },
    ],
)
async def test_workbench_malformed_review_never_completes(payload):
    async with WorkbenchClient(
        "http://peer.test",
        "token",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    ) as client:
        with pytest.raises(UpstreamContractError):
            await client.get_review("rev_fixture")


@pytest.mark.asyncio
async def test_invalid_request_is_rejected_before_external_effect():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(202, json={"review_id": "rev_fixture"})

    async with WorkbenchClient(
        "http://peer.test", "token", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ValueError, match="CreateReviewReq"):
            await client.create_review({"doc_id": "fixture"})
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,payload",
    [
        (
            404,
            {
                "error": {
                    "code": "MADE_UP",
                    "message": "bad",
                    "details": {},
                    "request_id": "fixture",
                }
            },
        ),
        (
            404,
            {
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "bad",
                    "details": {},
                    "request_id": "fixture",
                }
            },
        ),
        (200, []),
    ],
)
async def test_invalid_response_errors_are_dependency_failures(status, payload):
    async with GraphClient(
        "http://peer.test",
        "token",
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=payload)),
    ) as client:
        with pytest.raises(UpstreamServiceError) as error:
            await client.get_node("Provision:fixture")
        assert error.value.code == "UPSTREAM_UNAVAILABLE"


@pytest.mark.asyncio
async def test_peer_replay_hit_miss_and_invalid_cassette(tmp_path: Path):
    request = httpx.Request("GET", "http://never-connect.test/health")
    key = replay_key(request)
    payload = {
        "status": "ok",
        "service": "graph-api",
        "version": "fixture",
        "contract_version": "1.0.0",
    }
    path = tmp_path / f"{key}.json"
    path.write_text(
        json.dumps(
            {
                "request_sha256": key,
                "status_code": 200,
                "headers": {"X-Contract-Version": "1.0.0", "Set-Cookie": "must-not-leak"},
                "body_base64": base64.b64encode(json.dumps(payload).encode()).decode(),
            }
        )
    )
    async with GraphClient(
        "http://never-connect.test",
        "token",
        strict_headers=True,
        transport=PeerReplayTransport(tmp_path, "graph-api"),
    ) as client:
        assert (await client.health()) == payload
        with pytest.raises(ServiceUnavailableError, match="cassette"):
            await client.get_node("Provision:absent")
        path.write_text("{}")
        with pytest.raises(ServiceUnavailableError, match="cassette"):
            await client.health()
