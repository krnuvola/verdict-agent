"""Track A command-line entry point."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from getpass import getuser
from pathlib import Path
from urllib.parse import quote

import httpx

from gba_agent.api.contracts_compat import CreateRunReq
from gba_agent.api.errors import APIError
from gba_agent.api.trace import new_ulid
from gba_agent.config import get_settings
from gba_agent.db.session import create_database_engine, create_session_factory
from gba_agent.operator.playbooks import PlaybookStore
from gba_agent.operator.repair import RepairQueue
from gba_agent.orchestrator.ladder import ladder_config


def main() -> None:
    parser = argparse.ArgumentParser(prog="gba-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Serve the existing authenticated API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8100)
    commands.add_parser("migrate", help="Apply existing Alembic migrations")
    run = commands.add_parser("run", help="Control existing orchestrator API resources")
    operations = run.add_subparsers(dest="action", required=True)
    create = operations.add_parser("create")
    create.add_argument("request", type=Path, help="Doc 00 CreateRunReq JSON file")
    create.add_argument("--ladder", choices=[f"A{i}" for i in range(7)])
    create.add_argument("--idempotency-key")
    for operation in ("status", "trace", "dossier", "cancel"):
        operations.add_parser(operation).add_argument("run_id")
    handoff = commands.add_parser("handoff", help="List or resolve human handoffs")
    actions = handoff.add_subparsers(dest="action", required=True)
    list_action = actions.add_parser("list")
    list_action.add_argument(
        "--status", choices=("open", "resolved", "abandoned", "expired"), default="open"
    )
    resolve = actions.add_parser("resolve")
    resolve.add_argument("handoff_id")
    resolve.add_argument("--resolved-by")
    resolve.add_argument("--outcome", choices=("resolved", "abandoned"), default="resolved")
    playbooks = commands.add_parser("playbooks", help="Review and release playbook repairs")
    revisions = playbooks.add_subparsers(dest="action", required=True)
    approve = revisions.add_parser("approve")
    approve.add_argument("proposal_id", type=int)
    approve.add_argument("--reviewer", default=None)
    canary = commands.add_parser("canary", help="Check adapters against offline canary fixtures")
    canary.add_argument("--live", action="store_true", help="Also run opt-in read-only live checks")
    args = parser.parse_args()

    if args.command in {"serve", "migrate"}:
        command = (
            [
                "uvicorn",
                "gba_agent.api.main:app",
                "--host",
                args.host,
                "--port",
                str(args.port),
                "--workers",
                "1",
            ]
            if args.command == "serve"
            else ["alembic", "upgrade", "head"]
        )
        raise SystemExit(subprocess.run([sys.executable, "-m", *command], check=False).returncode)

    if args.command == "canary":
        tests = ["-m", "adapter_canary or live" if args.live else "adapter_canary"]
        if args.live:
            tests.append("--live")
        raise SystemExit(
            subprocess.run([sys.executable, "-m", "pytest", "-q", *tests], check=False).returncode
        )

    settings = get_settings()
    if args.command in {"run", "handoff"}:
        headers = {
            "Authorization": f"Bearer {settings.service_token.get_secret_value()}",
            "X-Contract-Version": "1.0.0",
            "X-Request-Id": new_ulid(),
        }
        body = None
        method = "GET"
        path = "/orch/v1/handoffs"
        params = None
        try:
            if args.command == "run":
                if args.action == "create":
                    body = json.loads(args.request.read_text(encoding="utf-8"))
                    if args.ladder:
                        body["config"] = {
                            **body.get("config", {}),
                            **ladder_config(args.ladder).model_dump(
                                include={"guards_enabled", "use_workbench"}
                            ),
                        }
                    body = CreateRunReq.model_validate(body).model_dump(mode="json")
                    if args.idempotency_key:
                        headers["Idempotency-Key"] = args.idempotency_key
                    path, method = "/orch/v1/runs", "POST"
                else:
                    headers["X-Run-Id"] = args.run_id
                    suffix = {
                        "status": "",
                        "trace": "/trajectory",
                        "dossier": "/dossier",
                        "cancel": "/cancel",
                    }[args.action]
                    path = f"/orch/v1/runs/{quote(args.run_id, safe='')}{suffix}"
                    method = "POST" if args.action == "cancel" else "GET"
            elif args.action == "list":
                params = {"status": args.status}
            else:
                path += f"/{quote(args.handoff_id, safe='')}/resolve"
                method = "POST"
                body = {"resolved_by": args.resolved_by or getuser(), "outcome": args.outcome}
            with httpx.Client(
                base_url=settings.agent_api_url, headers=headers, timeout=30
            ) as client:
                response = client.request(method, path, json=body, params=params)
                if response.is_error:
                    print(response.text, file=sys.stderr)
                    raise SystemExit(1)
                print(response.text, end="" if args.action == "trace" else "\n")
                return
        except (OSError, ValueError, httpx.RequestError):
            print(
                json.dumps(
                    {
                        "error": {
                            "code": "UPSTREAM_UNAVAILABLE",
                            "message": "CLI input or API request failed",
                            "details": {},
                            "request_id": headers["X-Request-Id"],
                        }
                    }
                ),
                file=sys.stderr,
            )
            raise SystemExit(1) from None
    engine = create_database_engine(settings)
    try:
        if args.command == "playbooks":
            approved = RepairQueue(
                create_session_factory(engine),
                PlaybookStore(settings.playbook_dir),
            ).approve(args.proposal_id, args.reviewer or getuser())
            print(approved.model_dump_json())
            return
    except APIError as exc:
        parser.exit(1, f"{exc.code}: {exc.message}\n")
    finally:
        engine.dispose()
