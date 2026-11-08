"""Explicit Doc 00 v1 local trajectory adapter until Track C is installed.

Single writer process per service (the v1 deployment uses one API worker).
Never repairs or truncates an interrupted/corrupt file automatically.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from gba_agent.api.trace import new_ulid

COMPONENT = Literal[
    "planner",
    "orchestrator",
    "operator",
    "access",
    "action_guard",
    "source_gate",
    "scope",
    "temporal",
    "claim_verifier",
    "gnn",
    "abstention",
    "graph",
    "workbench",
    "eval",
]
EVENT = Literal[
    "llm_call",
    "search",
    "fetch",
    "block",
    "session",
    "action_proposed",
    "action_decided",
    "action_executed",
    "download",
    "guard_result",
    "graph_query",
    "handoff_requested",
    "handoff_resolved",
    "edit_proposed",
    "edit_decided",
    "abstain",
    "error",
    "state_change",
    "override",
]
REQUIRED = {
    "fetch": {"url", "registry_entry_id", "status", "source_id", "bytes", "from_cache"},
    "search": {"query", "endpoint", "result_count"},
    "block": {"url", "kind", "attempt", "action_taken"},
    "session": {"session_id", "site_id", "event"},
    "action_proposed": {"action"},
    "action_decided": {"decision"},
    "action_executed": {"action", "result", "detail"},
    "download": {"url", "doc_id", "bytes", "complete"},
    "guard_result": {"claim_id", "result"},
    "graph_query": {"endpoint", "request_hash", "status", "ms"},
    "llm_call": {"role", "model", "input_tokens", "output_tokens", "request_hash"},
    "handoff_requested": {"handoff_id", "reason", "site_id"},
    "handoff_resolved": {"handoff_id", "reason", "site_id", "outcome"},
    "edit_proposed": {"edit_id", "document_id"},
    "edit_decided": {"edit_id", "document_id", "decision"},
    "abstain": {"item", "reason_code", "reason_text"},
    "override": {"guard", "claim_id", "by", "reason"},
    "state_change": {"from", "to"},
    "error": {"where", "message"},
}
_secrets: set[str] = set()
_lock = RLock()


def register_secret(value: str) -> None:
    if value:
        with _lock:
            _secrets.add(value)
        try:
            from gba_contracts.trajectory import register_secret as shared_register  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:
            if exc.name not in {"gba_contracts", "gba_contracts.trajectory"}:
                raise
        else:
            shared_register(value)


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _contains_secret(value: object) -> bool:
    if isinstance(value, str):
        return any(secret in value for secret in _secrets)
    if isinstance(value, dict):
        return any(_contains_secret(key) or _contains_secret(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item) for item in value)
    return False


class TrajectoryEvent(BaseModel):
    """Temporary verbatim field layout of Doc 00 section 6."""

    model_config = ConfigDict(extra="forbid")
    event_id: str
    run_id: str
    matter_id: str | None = None
    ts: datetime
    seq: int
    component: COMPONENT
    type: EVENT
    payload: dict[str, Any]
    parent_event_id: str | None = None
    session_id: str | None = None
    prev_hash: str
    event_hash: str


def _read(path: Path) -> list[dict[str, Any]]:
    events = []
    previous = "0" * 64
    last_seq: int | None = None
    for line_number, line in enumerate(path.read_bytes().splitlines(keepends=True), 1):
        try:
            if not line.endswith(b"\n"):
                raise ValueError("Interrupted append")
            data = json.loads(line)
            TrajectoryEvent.model_validate(data)
            digest = data["event_hash"]
            if (
                data["prev_hash"] != previous
                or type(data["seq"]) is not int
                or (last_seq is not None and data["seq"] <= last_seq)
                or digest
                != sha256(
                    canonical({k: v for k, v in data.items() if k != "event_hash"})
                ).hexdigest()
            ):
                raise ValueError("Invalid hash chain")
            if not REQUIRED[data["type"]].issubset(data["payload"]):
                raise ValueError("Incomplete event")
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"Invalid trajectory at line {line_number}") from exc
        previous = digest
        last_seq = data["seq"]
        events.append(data)
    return events


def verify_chain(path: Path) -> int | None:
    try:
        _read(path)
    except ValueError as exc:
        return int(str(exc).rsplit(" ", 1)[-1])
    return None


class Writer(Protocol):
    def emit(
        self,
        *,
        component: str,
        type: str,
        payload: dict[str, Any],
        matter_id: str | None = None,
        parent_event_id: str | None = None,
        session_id: str | None = None,
    ) -> object: ...


class LocalTrajectoryWriter:
    """Fsync every event; fail closed on corruption or registered secrets."""

    backend = "local-doc00-v1"

    def __init__(self, service: str, run_id: str, root: Path) -> None:
        if service not in {"agent-api", "graph-api", "workbench-api"} or not re.fullmatch(
            r"run_[0-7][0-9A-HJKMNP-TV-Z]{25}|adhoc", run_id
        ):
            raise ValueError("Invalid trajectory identity")
        self.service, self.run_id = service, run_id
        self.path = root / run_id / f"{service}.jsonl"

    def emit(
        self,
        *,
        component: str,
        type: str,
        payload: dict[str, Any],
        matter_id: str | None = None,
        parent_event_id: str | None = None,
        session_id: str | None = None,
    ) -> object:
        with _lock:
            payload = {**payload, "trajectory_backend": self.backend}
            if session_id is None and isinstance(payload.get("session_id"), str):
                session_id = payload["session_id"]
            if not REQUIRED.get(type, set()).issubset(payload):
                raise ValueError("Incomplete trajectory payload")
            canonical(payload)
            if _contains_secret(payload):
                raise ValueError("Trajectory contains a registered secret")
            events = _read(self.path) if self.path.exists() else []
            if any(event["run_id"] != self.run_id for event in events):
                raise ValueError("Trajectory run mismatch")
            parent = parent_event_id
            if parent is None:
                # The current phase is the parent of work actually performed in it.
                parent = next(
                    (
                        e["event_id"]
                        for e in reversed(events)
                        if e["type"] == "state_change" and "operation" not in e["payload"]
                    ),
                    None,
                )
            event = TrajectoryEvent.model_validate(
                {
                    "event_id": f"evt_{new_ulid()}",
                    "run_id": self.run_id,
                    "matter_id": matter_id,
                    "ts": datetime.now(UTC)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"),
                    "seq": events[-1]["seq"] + 1 if events else 1,
                    "component": component,
                    "type": type,
                    "payload": payload,
                    "parent_event_id": parent,
                    "session_id": session_id,
                    "prev_hash": events[-1]["event_hash"] if events else "0" * 64,
                    "event_hash": "",
                }
            ).model_dump(mode="json")
            event["ts"] = (
                datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            )
            event["event_hash"] = sha256(
                canonical({k: v for k, v in event.items() if k != "event_hash"})
            ).hexdigest()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("ab") as output:
                output.write(canonical(event) + b"\n")
                output.flush()
                os.fsync(output.fileno())
            return event


def merge(root: Path, run_id: str) -> Iterable[dict[str, Any]]:
    if not re.fullmatch(r"run_[0-7][0-9A-HJKMNP-TV-Z]{25}|adhoc", run_id):
        raise ValueError("Invalid trajectory identity")
    events = []
    for service in ("agent-api", "graph-api", "workbench-api"):
        path = root / run_id / f"{service}.jsonl"
        if path.exists():
            for event in _read(path):
                if event["run_id"] != run_id:
                    raise ValueError("Trajectory run mismatch")
                events.append((event["ts"], service, event["seq"], event))
    return [event for _, _, _, event in sorted(events, key=lambda item: item[:3])]


def factory(root: Path) -> Callable[[str, str], Writer]:
    """Prefer C's writer; identify the compatibility fallback explicitly."""
    try:
        from gba_contracts.trajectory import TrajectoryWriter
    except ModuleNotFoundError as exc:
        if exc.name not in {"gba_contracts", "gba_contracts.trajectory"}:
            raise
        return lambda service, run_id: LocalTrajectoryWriter(service, run_id, root)
    shared: Callable[[str, str], Writer] = TrajectoryWriter
    return shared
