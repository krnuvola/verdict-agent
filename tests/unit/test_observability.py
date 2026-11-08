"""Offline integrity, honest measurements, replay and canonical ladder probes."""

from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from gba_agent.api.trace import new_ulid
from gba_agent.clients.graph_client import GraphClient
from gba_agent.clients.llm_contract import LLMCacheMiss
from gba_agent.clients.replay import CassetteReplay, replay_only
from gba_agent.orchestrator.ladder import ladder_config
from gba_agent.orchestrator.metrics import RunMetrics
from gba_agent.orchestrator.planner import BudgetedLLM, call_metadata
from gba_agent.orchestrator.state import Budget
from gba_agent.trajectory import (
    LocalTrajectoryWriter,
    canonical,
    factory,
    merge,
    register_secret,
    verify_chain,
)


def writer(
    root: Path, service: str = "agent-api", run_id: str | None = None
) -> LocalTrajectoryWriter:
    return LocalTrajectoryWriter(service, run_id or f"run_{new_ulid()}", root)


def test_chain_restart_parent_order_and_tamper(tmp_path: Path) -> None:
    sink = writer(tmp_path)
    first = sink.emit(
        component="orchestrator", type="state_change", payload={"from": "queued", "to": "INTAKE"}
    )
    assert isinstance(first, dict)
    restarted = writer(tmp_path, run_id=sink.run_id)
    second = restarted.emit(
        component="orchestrator", type="error", payload={"where": "fetch", "message": "Interrupted"}
    )
    assert isinstance(second, dict)
    assert second["parent_event_id"] == first["event_id"]
    assert second["seq"] == 2 and second["prev_hash"] == first["event_hash"]
    assert verify_chain(sink.path) is None
    assert list(merge(tmp_path, sink.run_id)) == [first, second]
    sink.path.write_bytes(sink.path.read_bytes().replace(b"Interrupted", b"Tampered"))
    assert verify_chain(sink.path) == 2
    with pytest.raises(ValueError, match="line 2"):
        restarted.emit(
            component="orchestrator",
            type="error",
            payload={"where": "restart", "message": "No repair"},
        )


@pytest.mark.parametrize("content", [b"{", b"{}\n", b'{"bad":true}\n'])
def test_interrupted_or_malformed_file_never_silently_repaired(
    tmp_path: Path, content: bytes
) -> None:
    sink = writer(tmp_path)
    sink.path.parent.mkdir(parents=True)
    sink.path.write_bytes(content)
    assert verify_chain(sink.path) == 1
    with pytest.raises(ValueError):
        list(merge(tmp_path, sink.run_id))
    assert sink.path.read_bytes() == content


def test_required_fields_secrets_invalid_identity_and_merge_peers(tmp_path: Path) -> None:
    sink = factory(tmp_path)("agent-api", f"run_{new_ulid()}")
    assert isinstance(sink, LocalTrajectoryWriter)
    with pytest.raises(ValueError):
        sink.emit(component="access", type="fetch", payload={})
    register_secret("fixture-secret-that-must-never-be-logged")
    with pytest.raises(ValueError, match="secret"):
        sink.emit(
            component="orchestrator",
            type="error",
            payload={"where": "test", "message": "fixture-secret-that-must-never-be-logged"},
        )
    for service, run_id in [("invalid", sink.run_id), ("agent-api", "../escape")]:
        with pytest.raises(ValueError):
            LocalTrajectoryWriter(service, run_id, tmp_path)
    with pytest.raises(ValueError):
        list(merge(tmp_path, "../escape"))
    for service in ("agent-api", "graph-api", "workbench-api"):
        peer = writer(tmp_path, service, sink.run_id)
        peer.emit(
            component="orchestrator"
            if service == "agent-api"
            else "graph"
            if service == "graph-api"
            else "workbench",
            type="error",
            payload={"where": "test", "message": service},
        )
    result = list(merge(tmp_path, sink.run_id))
    assert len(result) == 3
    assert all(event["ts"].endswith("Z") for event in result)


def test_metrics_missing_usage_is_unknown_and_observed_counts_are_exact() -> None:
    metrics = RunMetrics()
    metrics.observe("llm_call", {"model": None, "input_tokens": None, "output_tokens": 8})
    metrics.observe("llm_call", {"model": "recorded-model", "input_tokens": 4, "output_tokens": 3})
    metrics.observe("action_executed", {"result": "ok"})
    metrics.observe("action_executed", {"detail": "DRY_RUN"})
    metrics.observe(
        "state_change",
        {"operation": "network_request", "phase": "started", "url": "https://fixture.test/page"},
    )
    metrics.observe(
        "fetch", {"url": "https://fixture.test/page", "status": "ok", "source_id": "src_1"}
    )
    metrics.observe("graph_query", {"operation": "peer_request", "attempt": 2})
    metrics.observe("block", {"action_taken": "retry"})
    metrics.observe("state_change", {"operation": "playbook_repair", "phase": "started"})
    metrics.observe(
        "state_change", {"operation": "source_admission", "admitted": True, "source_id": "src_1"}
    )
    metrics.observe("guard_result", {"result": {"guard": "scope", "verdict": "unknown"}})
    metrics.observe("abstain", {"reason_code": "BUDGET_EXCEEDED"})
    result = metrics.snapshot("done")
    assert result["input_tokens"] is result["output_tokens"] is None
    assert result["observed_input_tokens"] == 4 and result["observed_output_tokens"] == 11
    assert result["model_status"] == "unavailable" and result["models"] == ["recorded-model"]
    assert result["retry_count"] == 2 and result["recovery_attempts"] == 1
    assert result["browser_action_count"] == result["domain_count"] == 1
    assert result["evidence_admitted"] == ["src_1"] and result["budget_stopped"]
    assert result["elapsed_wall_seconds"] >= 0


def test_missing_llm_usage_never_becomes_zero_or_crashes_accounting() -> None:
    raw = SimpleNamespace(text="", tool_calls=[], request_hash="a" * 64)
    client = Mock()
    client.complete.return_value = raw
    events = []
    budget = Budget(ladder_config("A5"), {})
    wrapped = BudgetedLLM(
        client,
        budget,
        lambda: None,
        lambda component, event, payload: events.append((event, payload)),
        {},
    )
    wrapped.complete(role="planner", system="private", messages=[])
    assert events[0][1]["input_tokens"] is None
    assert events[0][1]["model"] is None
    assert "private" not in repr(events)
    client.complete.side_effect = OSError("secret upstream message")
    with pytest.raises(OSError):
        wrapped.complete(role="planner", system="private", messages=[])
    assert events[-1][0] == "error" and "secret upstream message" not in repr(events)
    assert call_metadata("judge", raw)["output_tokens"] is None


def test_replay_hash_hit_and_miss_never_calls_a_provider(tmp_path: Path) -> None:
    request = {
        "role": "planner",
        "model": "cassette-model",
        "system": "system",
        "messages": [],
        "tools": None,
        "response_schema": None,
        "temperature": 0.0,
        "max_tokens": 4096,
        "image_sha256s": [],
    }
    digest = sha256(canonical(request)).hexdigest()
    path = tmp_path / f"{digest}.json"
    payload = {
        "model": "cassette-model",
        "text": "recorded",
        "tool_calls": [],
        "input_tokens": 7,
        "output_tokens": 2,
        "request_hash": digest,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    replay = CassetteReplay(tmp_path, {"planner": "cassette-model"})
    assert replay.complete(role="planner", system="system", messages=[]).input_tokens == 7
    with pytest.raises(LLMCacheMiss):
        replay.complete(role="planner", system="different", messages=[])
    path.write_text(json.dumps(payload | {"model": "wrong"}), encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        replay.complete(role="planner", system="system", messages=[])


@pytest.mark.asyncio
async def test_http_attempts_are_recorded_including_failed_retry() -> None:
    events: list[dict[str, Any]] = []
    attempts = 0

    def transport(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503 if attempts == 1 else 200, json={})

    async def sleep(_: float) -> None:
        return None

    async with GraphClient(
        "http://fixture",
        "token",
        max_retries=1,
        sleep=sleep,
        transport=httpx.MockTransport(transport),
    ) as client:
        client.observe = lambda event, payload: events.append({"event_type": event, **payload})
        await client.request_json("GET", "/fixture")
    starts = [event for event in events if event["event_type"] == "state_change"]
    events = [event for event in events if event["event_type"] == "graph_query"]
    assert [event["attempt"] for event in starts] == [1, 2]
    assert [event["request_hash"] for event in starts] == [
        event["request_hash"] for event in events
    ]
    assert [event["status"] for event in events] == [503, 200]
    assert [event["attempt"] for event in events] == [1, 2]
    assert all(event["ms"] >= 0 and len(event["request_hash"]) == 64 for event in events)
    assert "token" not in repr(events)


@pytest.mark.parametrize("index,count", [(0, 0), (1, 1), (2, 2), (3, 3), (4, 5), (5, 6), (6, 6)])
def test_all_ladder_configs_match_frozen_mapping(index: int, count: int) -> None:
    config = ladder_config(f"A{index}")
    assert len(config.guards_enabled) == count
    assert config.use_workbench is (index == 6)
    assert "action_guard" not in config.guards_enabled
    if index == 4:
        assert config.guards_enabled == [
            "access",
            "source_gate",
            "claim_verifier",
            "scope",
            "temporal",
        ]


def test_invalid_ladder_rejected() -> None:
    with pytest.raises(ValueError):
        ladder_config("A7")


def test_json_escaping_cannot_bypass_secret_rejection(tmp_path: Path) -> None:
    secret = 'fixture-password-"quote"-with\nnewline'
    register_secret(secret)
    sink = writer(tmp_path)
    with pytest.raises(ValueError, match="secret"):
        sink.emit(
            component="orchestrator",
            type="error",
            payload={
                "where": "test",
                "message": "fixture",
                "nested": [{"value": "prefix-" + secret}],
            },
        )
    assert not sink.path.exists()


def test_peer_sequences_need_only_be_monotonic_per_frozen_contract(tmp_path: Path) -> None:
    sink = writer(tmp_path)
    events = []
    previous = "0" * 64
    for seq in (0, 5):
        raw = sink.emit(
            component="orchestrator",
            type="error",
            payload={"where": "fixture", "message": "Peer numbering"},
        )
        assert isinstance(raw, dict)
        event = {**raw, "seq": seq, "prev_hash": previous}
        event["event_hash"] = sha256(
            canonical({k: v for k, v in event.items() if k != "event_hash"})
        ).hexdigest()
        events.append(event)
        previous = event["event_hash"]
        sink.path.write_bytes(b"".join(canonical(item) + b"\n" for item in events))
    assert verify_chain(sink.path) is None
    appended = sink.emit(
        component="orchestrator", type="error", payload={"where": "fixture", "message": "Restart"}
    )
    assert isinstance(appended, dict) and appended["seq"] == 6


def test_failed_llm_call_has_unavailable_usage_instead_of_zero() -> None:
    metrics = RunMetrics()
    metrics.observe("error", {"where": "llm", "message": "Call failed."})
    result = metrics.snapshot("failed")
    assert result["llm_calls"] == result["failed_llm_calls"] == 1
    assert result["input_tokens"] is result["output_tokens"] is None
    assert result["usage_status"] == result["model_status"] == "unavailable"


def test_shared_writer_is_preferred_when_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = ModuleType("gba_contracts.trajectory")
    shared = Mock()
    module.TrajectoryWriter = shared  # type: ignore[attr-defined]
    module.register_secret = Mock()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gba_contracts.trajectory", module)
    assert factory(tmp_path) is shared
    register_secret("shared-registration-test-secret")
    module.register_secret.assert_called_once()  # type: ignore[attr-defined]


def test_replay_mode_never_invokes_unmarked_provider(tmp_path: Path) -> None:
    provider = Mock()
    settings = SimpleNamespace(
        llm_mode="replay",
        llm_cache_dir=tmp_path,
        **{
            f"llm_model_{role}": "fixture-model"
            for role in ("planner", "judge", "extractor", "vlm", "drafter")
        },
    )
    client = replay_only(provider, settings)  # type: ignore[arg-type]
    assert isinstance(client, CassetteReplay)
    with pytest.raises(LLMCacheMiss):
        client.complete(role="planner", system="private", messages=[])
    provider.complete.assert_not_called()


@pytest.mark.asyncio
async def test_network_failure_is_a_recorded_http_attempt() -> None:
    events = []

    def transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private network error", request=request)

    async with GraphClient(
        "http://fixture", "token", max_retries=0, transport=httpx.MockTransport(transport)
    ) as client:
        client.observe = lambda event, payload: events.append({"event_type": event, **payload})
        with pytest.raises(RuntimeError):
            await client.request_json("GET", "/fixture")
    assert len(events) == 2 and events[0]["operation"] == "peer_request_started"
    assert events[1]["status"] == "unavailable"
    assert "private network error" not in repr(events)


def test_shared_reader_verifies_before_returning_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gba_agent.api.errors import APIError
    from gba_agent.api.routes_orch import _read_shared_events

    sink = writer(tmp_path)
    event = sink.emit(
        component="orchestrator", type="error", payload={"where": "test", "message": "fixture"}
    )
    module = ModuleType("gba_contracts.trajectory")
    module.verify_chain = Mock(return_value=None)  # type: ignore[attr-defined]
    module.merge = lambda _: "fixture-ndjson\n"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gba_contracts.trajectory", module)
    monkeypatch.setattr("gba_agent.config.get_settings", lambda: SimpleNamespace(traj_dir=tmp_path))
    assert list(_read_shared_events(sink.run_id)) == ["fixture-ndjson"]
    module.merge = lambda _: [event, SimpleNamespace(model_dump=lambda **_: event)]  # type: ignore[attr-defined]
    assert list(_read_shared_events(sink.run_id)) == [event, event]
    module.verify_chain.return_value = 1  # type: ignore[attr-defined]
    with pytest.raises(APIError, match="integrity"):
        list(_read_shared_events(sink.run_id))
