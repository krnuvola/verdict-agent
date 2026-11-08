"""Persistent single-step repair proposals with explicit human release."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from gba_agent.api.errors import APIError
from gba_agent.db.models import PlaybookProposal
from gba_agent.db.repositories import PlaybookProposalsRepository
from gba_agent.db.session import session_scope
from gba_agent.operator.playbooks import LocatorSpec, Playbook, PlaybookStore


class StepDiff(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    before: LocatorSpec
    after: LocatorSpec
    evidence_screenshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class RepairQueue:
    def __init__(self, database: sessionmaker[Session], store: PlaybookStore) -> None:
        self._database, self._store = database, store

    def propose(
        self,
        playbook: Playbook,
        step_id: str,
        new_locator: LocatorSpec,
        screenshot: bytes,
    ) -> int | None:
        step = next((step for step in playbook.steps if step.id == step_id), None)
        if step is None or step.locator is None or not screenshot.startswith(b"\x89PNG\r\n\x1a\n"):
            raise APIError(422, "VALIDATION_ERROR", "Repair lacks a verified step or screenshot")
        if step.locator == new_locator:
            return None
        diff = StepDiff(
            step_id=step_id,
            before=step.locator,
            after=new_locator,
            evidence_screenshot_hash="sha256:" + sha256(screenshot).hexdigest(),
        )
        with session_scope(self._database) as db:
            row = PlaybookProposalsRepository(db).propose(
                PlaybookProposal(
                    site_id=playbook.site_id,
                    task=playbook.task,
                    base_version=playbook.version,
                    diff=diff.model_dump(mode="json", exclude_none=True),
                    status="pending",
                )
            )
            return row.id

    def approve(self, proposal_id: int, reviewer: str) -> Playbook:
        if not re.fullmatch(r"[^\x00-\x1f\x7f]{1,160}", reviewer) or not reviewer.strip():
            raise APIError(422, "VALIDATION_ERROR", "A human reviewer is required")
        with session_scope(self._database) as db:
            row = PlaybookProposalsRepository(db).lock(proposal_id)
            if row is None:
                raise APIError(404, "PLAYBOOK_NOT_FOUND", "Playbook proposal was not found")
            if row.status not in {"pending", "approved"}:
                raise APIError(422, "VALIDATION_ERROR", "Proposal is not pending")
            diff = StepDiff.model_validate(row.diff)
            base = self._store.load(row.site_id, row.task, row.base_version)
            original = next((step for step in base.steps if step.id == diff.step_id), None)
            if original is None or original.locator != diff.before or diff.before == diff.after:
                raise APIError(
                    422, "VALIDATION_ERROR", "Proposal no longer matches its approved base"
                )
            proposed = base.model_copy(
                update={
                    "version": base.version + 1,
                    "steps": tuple(
                        step.model_copy(update={"locator": diff.after})
                        if step.id == diff.step_id
                        else step
                        for step in base.steps
                    ),
                }
            )
            if row.status == "approved":
                if self._store.load(row.site_id, row.task, proposed.version) != proposed:
                    raise APIError(
                        422, "VALIDATION_ERROR", "Approved proposal version does not match"
                    )
                return proposed
            latest = self._store.latest(row.site_id, row.task)
            if latest != base and latest != proposed:
                raise APIError(
                    422, "VALIDATION_ERROR", "Proposal is stale against the current playbook"
                )
            self._store.release(proposed)
            row.status = "approved"
            row.decided_by = reviewer
            row.decided_at = datetime.now(UTC)
            return proposed
