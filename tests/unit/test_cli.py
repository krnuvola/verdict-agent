"""CLI uses existing endpoints and explicit human approval commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr

from gba_agent import cli
from tests.mocks.temporal import matter


@pytest.mark.parametrize(
    "arguments,expected",
    [
        (
            ["serve"],
            [
                "uvicorn",
                "gba_agent.api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8100",
                "--workers",
                "1",
            ],
        ),
        (["migrate"], ["alembic", "upgrade", "head"]),
        (["canary"], ["pytest", "-q", "-m", "adapter_canary"]),
        (["canary", "--live"], ["pytest", "-q", "-m", "adapter_canary or live", "--live"]),
    ],
)
def test_process_commands(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str], expected: list[str]
) -> None:
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["gba-agent", *arguments])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 0
    assert run.call_args.args[0] == [sys.executable, "-m", *expected]


@pytest.mark.parametrize(
    "arguments,method,path",
    [
        (["run", "status", "run_fixture"], "GET", "/orch/v1/runs/run_fixture"),
        (["run", "trace", "run_fixture"], "GET", "/orch/v1/runs/run_fixture/trajectory"),
        (["run", "dossier", "run_fixture"], "GET", "/orch/v1/runs/run_fixture/dossier"),
        (["run", "cancel", "run_fixture"], "POST", "/orch/v1/runs/run_fixture/cancel"),
        (["handoff", "list"], "GET", "/orch/v1/handoffs"),
        (
            ["handoff", "resolve", "hof_fixture", "--resolved-by", "reviewer"],
            "POST",
            "/orch/v1/handoffs/hof_fixture/resolve",
        ),
    ],
)
def test_api_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    method: str,
    path: str,
) -> None:
    requests = []
    original = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"state": "observed"})

    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(
            agent_api_url="http://agent.test", service_token=SecretStr("fixture-token")
        ),
    )
    monkeypatch.setattr(
        cli.httpx,
        "Client",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(sys, "argv", ["gba-agent", *arguments])
    cli.main()
    assert requests[0].method == method and requests[0].url.path == path
    assert requests[0].headers["X-Contract-Version"] == "1.0.0"
    assert requests[0].headers["Authorization"] == "Bearer fixture-token"
    assert json.loads(capsys.readouterr().out) == {"state": "observed"}


def test_create_validates_request_and_applies_ladder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "run.json"
    path.write_text(json.dumps({"matter": matter().model_dump(mode="json")}), encoding="utf-8")
    request_bodies: list[dict[str, Any]] = []
    original = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        request_bodies.append(json.loads(request.content))
        assert request.headers["Idempotency-Key"] == "stable-key"
        return httpx.Response(202, json={"run_id": "run_fixture"})

    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(
            agent_api_url="http://agent.test", service_token=SecretStr("fixture-token")
        ),
    )
    monkeypatch.setattr(
        cli.httpx,
        "Client",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gba-agent",
            "run",
            "create",
            str(path),
            "--ladder",
            "A2",
            "--idempotency-key",
            "stable-key",
        ],
    )
    cli.main()
    assert request_bodies[0]["config"]["guards_enabled"] == ["access", "source_gate"]
    assert request_bodies[0]["config"]["use_workbench"] is False


@pytest.mark.parametrize("mode", ["bad_input", "peer_error", "network_error"])
def test_cli_failures_are_machine_readable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    path = tmp_path / "invalid.json"
    path.write_text("not json", encoding="utf-8")
    original = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        if mode == "network_error":
            raise httpx.ConnectError("untrusted error", request=request)
        return httpx.Response(503, json={"error": {"code": "UPSTREAM_UNAVAILABLE"}})

    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(
            agent_api_url="http://agent.test", service_token=SecretStr("fixture-token")
        ),
    )
    monkeypatch.setattr(
        cli.httpx,
        "Client",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gba-agent",
            "run",
            *(["create", str(path)] if mode == "bad_input" else ["status", "run_fixture"]),
        ],
    )
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 1
    assert "error" in json.loads(capsys.readouterr().err)


def test_playbook_approval_keeps_human_reviewer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine, queue = Mock(), Mock()
    queue.approve.return_value.model_dump_json.return_value = '{"approved":true}'
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(playbook_dir=tmp_path))
    monkeypatch.setattr(cli, "create_database_engine", lambda _: engine)
    monkeypatch.setattr(cli, "create_session_factory", lambda _: Mock())
    monkeypatch.setattr(cli, "RepairQueue", lambda *_: queue)
    monkeypatch.setattr(
        sys, "argv", ["gba-agent", "playbooks", "approve", "12", "--reviewer", "lawyer"]
    )
    cli.main()
    queue.approve.assert_called_once_with(12, "lawyer")
    engine.dispose.assert_called_once()
