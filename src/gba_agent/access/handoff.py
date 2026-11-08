"""Persistent noVNC handoffs and run pause/resume."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

from sqlalchemy.orm import Session, sessionmaker

from gba_agent.access.block_detector import EventSink
from gba_agent.api.contracts_compat import HandoffRequest, _CompatHandoffRequest
from gba_agent.api.errors import APIError
from gba_agent.api.trace import new_ulid
from gba_agent.config import Settings
from gba_agent.db.models import Handoff
from gba_agent.db.repositories import HandoffsRepository, RunsRepository
from gba_agent.db.session import session_scope

HandoffReason = Literal["login", "mfa", "captcha", "consent", "payment", "commit_step", "other"]
HandoffStatus = Literal["open", "resolved", "abandoned", "expired"]
HandoffOutcome = Literal["resolved", "abandoned"]


class HandoffManager:
    def __init__(
        self,
        settings: Settings,
        database: sessionmaker[Session],
        *,
        emit: EventSink | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        viewer = urlsplit(settings.novnc_base_url)
        if viewer.username is not None or viewer.query or viewer.fragment:
            raise ValueError("NOVNC_BASE_URL must omit user info, queries and fragments")
        self._viewer_url = f"{settings.novnc_base_url}/vnc.html?autoconnect=1&resize=scale"
        self._timeout = timedelta(seconds=settings.handoff_timeout_seconds)
        self._database, self._emit, self._now = database, emit, now
        self._waiters: dict[str, asyncio.Event] = {}

    def request(
        self, run_id: str, site_id: str, reason: HandoffReason, instructions: str
    ) -> _CompatHandoffRequest:
        request = HandoffRequest(
            handoff_id=f"hof_{new_ulid()}",
            run_id=run_id,
            site_id=site_id,
            reason=reason,
            instructions=instructions,
            viewer_url=self._viewer_url,
            created_at=self._now(),
        )
        with session_scope(self._database) as db:
            run = RunsRepository(db).lock(run_id)
            if run is None:
                raise APIError(404, "RUN_NOT_FOUND", "Run was not found")
            if run.state != "running":
                raise APIError(422, "VALIDATION_ERROR", "A handoff requires a running run")
            run.state = "waiting_handoff"
            run.updated_at = request.created_at
            db.flush()
            HandoffsRepository(db).add(
                Handoff(
                    handoff_id=request.handoff_id,
                    run_id=run_id,
                    status="open",
                    request=request.model_dump(mode="json"),
                    created_at=request.created_at,
                )
            )
        self._event(request, "handoff_requested")
        return request

    def _event(self, request: _CompatHandoffRequest, event: str) -> None:
        if self._emit is not None:
            payload: dict[str, object] = {
                "handoff_id": request.handoff_id,
                "reason": request.reason,
                "site_id": request.site_id,
            }
            if event == "handoff_resolved":
                payload["outcome"] = request.status
            self._emit(request.run_id, event, payload)

    def _transition(
        self,
        handoff_id: str,
        outcome: HandoffOutcome | None = None,
        resolved_by: str | None = None,
    ) -> _CompatHandoffRequest:
        changed = False
        with session_scope(self._database) as db:
            row = HandoffsRepository(db).lock(handoff_id)
            if row is None:
                raise APIError(404, "HANDOFF_NOT_FOUND", "Handoff was not found")
            request = HandoffRequest.model_validate(row.request)
            now = self._now()
            if request.status == "open":
                status: HandoffStatus | None = outcome
                if request.created_at + self._timeout <= now:
                    status = "expired"
                    resolved_by = None
                if status is not None:
                    request.status = status
                    request.resolved_at = now
                    request.resolved_by = resolved_by
                    row.status, row.resolved_at = status, now
                    row.request = request.model_dump(mode="json")
                    run = RunsRepository(db).lock(row.run_id)
                    if run is not None and run.state == "waiting_handoff":
                        run.state, run.updated_at = "running", now
                    changed = True
        if changed:
            waiter = self._waiters.get(handoff_id)
            if waiter is not None:
                waiter.set()
            self._event(request, "handoff_resolved")
            if request.status == "expired" and self._emit is not None:
                self._emit(
                    request.run_id,
                    "abstain",
                    {
                        "item": handoff_id,
                        "reason_code": "HANDOFF_EXPIRED",
                        "reason_text": "The human handoff expired.",
                    },
                )
        return request

    def resolve(
        self, handoff_id: str, resolved_by: str, outcome: HandoffOutcome
    ) -> _CompatHandoffRequest:
        if outcome not in {"resolved", "abandoned"} or not resolved_by.strip():
            raise APIError(422, "VALIDATION_ERROR", "Invalid handoff resolution")
        return self._transition(handoff_id, outcome, resolved_by)

    def list(self, status: HandoffStatus = "open") -> list[_CompatHandoffRequest]:
        with session_scope(self._database) as db:
            repo = HandoffsRepository(db)
            open_ids = [row.handoff_id for row in repo.list_by_status("open")]
        for handoff_id in open_ids:
            self._transition(handoff_id)
        with session_scope(self._database) as db:
            return [
                HandoffRequest.model_validate(row.request)
                for row in HandoffsRepository(db).list_by_status(status)
            ]

    async def wait(self, handoff_id: str) -> _CompatHandoffRequest:
        event = self._waiters.setdefault(handoff_id, asyncio.Event())
        try:
            while True:
                request = self._transition(handoff_id)
                if request.status != "open":
                    return request
                remaining = (request.created_at + self._timeout - self._now()).total_seconds()
                try:
                    # Poll persisted state so a separate API/CLI process can also resolve it.
                    await asyncio.wait_for(event.wait(), timeout=min(1, max(0, remaining)))
                except TimeoutError:
                    continue
        finally:
            self._waiters.pop(handoff_id, None)
