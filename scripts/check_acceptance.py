"""Track A test/coverage gate; raw observations only, not C's evaluation harness."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


def summarize(junit: Path, coverage_json: Path) -> dict[str, Any]:
    root = ET.parse(junit).getroot()
    cases = list(root.iter("testcase"))
    failures = [
        {"test": f"{case.get('classname')}.{case.get('name')}", "message": child.get("message", "")}
        for case in cases
        for child in case
        if child.tag in {"failure", "error"}
    ]
    skips = [
        {"test": f"{case.get('classname')}.{case.get('name')}", "reason": child.get("message", "")}
        for case in cases
        for child in case
        if child.tag == "skipped"
    ]
    data = json.loads(coverage_json.read_text(encoding="utf-8"))
    critical = {}
    for filename, value in data["files"].items():
        if Path(filename).name in {"action_guard.py", "source_gate.py"}:
            summary = value["summary"]
            if summary["num_branches"] == 0:
                raise ValueError("Critical guard has no measured branches")
            critical[Path(filename).name] = {
                "covered": summary["covered_branches"],
                "total": summary["num_branches"],
                "percent": 100 * summary["covered_branches"] / summary["num_branches"],
            }
    if set(critical) != {"action_guard.py", "source_gate.py"}:
        raise ValueError("Critical guard coverage is missing")
    totals = data["totals"]
    line_percent = 100 * totals["covered_lines"] / totals["num_statements"]
    return {
        "tests": {
            "passed": len(cases) - len(failures) - len(skips),
            "failed_or_error": len(failures),
            "skipped": len(skips),
        },
        "failures": failures,
        "skips": skips,
        "coverage": {
            "overall_lines": line_percent,
            "overall_branch_aware": totals["percent_covered"],
            "critical_branches": critical,
        },
        "coverage_gate": line_percent >= 80
        and totals["percent_covered"] >= 80
        and all(item["covered"] == item["total"] for item in critical.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-peers",
        action="store_true",
        help="Explicit integration mode: invoke C's installed check-service CLI",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    artifacts = root / ".pytest_cache" / "acceptance"
    artifacts.mkdir(parents=True, exist_ok=True)
    junit, coverage_json = artifacts / "tests.xml", artifacts / "coverage.json"
    commands = [
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "mypy", "src/gba_agent", "scripts/check_acceptance.py"],
        [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--branch",
            "--source=gba_agent",
            "-m",
            "pytest",
            "-q",
            f"--junitxml={junit}",
        ],
        [sys.executable, "-m", "coverage", "json", "-o", str(coverage_json)],
    ]
    # All checks run even if one fails; failures cannot be hidden by the last exit code.
    receipts = [
        {"command": command, "exit_code": subprocess.run(command, cwd=root, check=False).returncode}
        for command in commands
    ]
    shared = importlib.util.find_spec("gba_contracts") is not None
    peer_checks = []
    executable = shutil.which("gba-contracts")
    if args.check_peers and shared and executable:
        for service, variable, default in (
            ("agent", "AGENT_API_URL", "http://localhost:8100"),
            ("graph", "GRAPH_API_URL", "http://localhost:8101"),
            ("workbench", "WORKBENCH_API_URL", "http://localhost:8102"),
        ):
            command = [executable, "check-service", service, os.environ.get(variable, default)]
            peer_checks.append(
                {
                    "command": command,
                    "exit_code": subprocess.run(command, cwd=root, check=False).returncode,
                }
            )
    try:
        report = summarize(junit, coverage_json)
    except (OSError, ValueError, KeyError, ET.ParseError) as exc:
        report = {"coverage_gate": False, "artifact_error": type(exc).__name__}
    local_go = all(item["exit_code"] == 0 for item in receipts) and report["coverage_gate"]
    integration_go = (
        local_go
        and shared
        and len(peer_checks) == 3
        and all(item["exit_code"] == 0 for item in peer_checks)
    )
    report.update(
        {
            "local_verdict": "GO" if local_go else "NO-GO",
            "integration_verdict": "GO" if integration_go else "NO-GO",
            "commands": receipts,
            "shared_package_installed": shared,
            "peer_contract_checks": peer_checks,
            "mode": "explicit_peer_checks" if args.check_peers else "offline",
            "dependencies": [
                "Published pinned gba_contracts v1.0.0 and shared contract suite",
                "Doc 04 real B/C scenario and shared-writer interoperability checks",
                "Lawyer confirmation before treating illustrative content as ground truth",
            ],
        }
    )
    (artifacts / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if integration_go else 1


if __name__ == "__main__":
    raise SystemExit(main())
