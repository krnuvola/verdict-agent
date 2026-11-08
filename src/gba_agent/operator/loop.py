"""Bounded observe, decide, guard, act and verify loop."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, ConfigDict, Field

from gba_agent.access._registry import RegistryEntry
from gba_agent.access.block_detector import BlockDetector, EventSink
from gba_agent.access.fetcher import BrowserCapture
from gba_agent.access.handoff import HandoffManager, HandoffReason
from gba_agent.api.contracts_compat import RunConfig
from gba_agent.operator.actions import Executor
from gba_agent.operator.browser import Workspace
from gba_agent.operator.perception import public_url
from gba_agent.operator.som import marked_screenshot
from gba_agent.operator.vlm import InvalidProposal, Proposal, decide

if TYPE_CHECKING:
    from gba_agent.operator.vlm import LLMClient


class Subtask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str
    site_id: str
    start_url: str
    success_criteria: str
    max_steps: int = Field(default=25, gt=0)


@dataclass
class RunContext:
    workspace: Workspace = field(repr=False)
    entry: RegistryEntry
    site_config: dict[str, object] | object
    config: RunConfig
    llm: LLMClient | None = field(repr=False)
    handoffs: HandoffManager | None
    emit: EventSink | None
    managed_wall_time: bool = False


@dataclass(frozen=True)
class OperateResult:
    capture: BrowserCapture | None = field(default=None, repr=False)
    reason_code: str | None = None
    notes: tuple[str, ...] = field(default=(), repr=False)


def _stuck(subtask: Subtask, run: RunContext, reason: str) -> OperateResult:
    if run.emit:
        run.emit(
            run.workspace.session.run_id,
            "abstain",
            {
                "item": public_url(subtask.start_url),
                "reason_code": reason,
                "reason_text": "The guarded browser task could not be completed.",
            },
        )
    return OperateResult(reason_code=reason)


async def _handoff(run: RunContext, action_class: str | None) -> str:
    manager, session = run.handoffs, run.workspace.session
    if manager is None:
        return "SOURCE_UNRETRIEVABLE"
    reason: HandoffReason = (
        "commit_step"
        if action_class == "COMMIT"
        else "payment"
        if action_class == "SPEND"
        else "other"
    )
    request = manager.request(
        session.run_id, run.entry.entry_id, reason, "Review the blocked browser step in the viewer."
    )
    session.paused = True
    try:
        outcome = await manager.wait(request.handoff_id)
    except asyncio.CancelledError:
        await manager.wait(request.handoff_id)
        raise
    finally:
        session.paused = False
    if outcome.status == "expired":
        return "HANDOFF_EXPIRED"
    return "SOURCE_UNRETRIEVABLE"


async def operate(subtask: Subtask, run: RunContext) -> OperateResult:
    """Execute only actions approved against a live DOM target."""
    if subtask.site_id != run.entry.entry_id:
        return _stuck(subtask, run, "SOURCE_UNRETRIEVABLE")
    try:
        async with asyncio.timeout(None if run.managed_wall_time else run.config.max_wall_seconds):
            return await _operate(subtask, run)
    except TimeoutError:
        return _stuck(subtask, run, "BUDGET_EXCEEDED")
    except PlaywrightError:
        return _stuck(subtask, run, "OPERATOR_STUCK")


async def _operate(subtask: Subtask, run: RunContext) -> OperateResult:
    workspace = run.workspace
    executor = Executor(workspace, run.entry, run.site_config, run.config, run.emit)
    detector = BlockDetector()
    had_session = (
        workspace.page.url != "about:blank"
        or workspace.session.last_used_at > workspace.session.created_at
    )
    workspace.last_download = None
    workspace.documents.clear()
    workspace.attachments.clear()
    initial = await executor.step(
        Proposal(action_type="navigate", observation_id=0, value=subtask.start_url), None
    )
    if not initial.executed:
        if initial.reason == "BROWSER_TIMEOUT":
            raise httpx.ReadTimeout("Browser navigation timed out")
        return _stuck(subtask, run, initial.reason)
    visits: Counter[tuple[str, str]] = Counter()
    feedback = ""
    unchanged = 0
    previous: tuple[str, str] | None = None
    notes: list[str] = []
    configured = (
        run.site_config.get("login_url_patterns") if isinstance(run.site_config, Mapping) else None
    )
    patterns = (
        [item for item in configured if isinstance(item, str)]
        if isinstance(configured, list)
        else []
    )
    for step in range(min(subtask.max_steps, run.config.max_steps) + 1):
        view = await workspace.perception()
        capture = workspace.capture(subtask.start_url, view.dom, had_session)
        if detector.detect(capture.response, login_url_patterns=patterns) is not None:
            return OperateResult(capture=capture, notes=tuple(notes))
        if (
            workspace.last_download is not None
            or run.llm is None
            or capture.response.media_type == "application/pdf"
        ):
            return OperateResult(capture=capture, notes=tuple(notes))
        if step == min(subtask.max_steps, run.config.max_steps):
            break
        observation = view.observation
        marker = (workspace.page.url, observation.ax_digest)
        visits[marker] += 1
        unchanged = unchanged + 1 if marker == previous else 0
        previous = marker
        if visits[marker] > 3:
            break
        if (
            not view.elements or unchanged >= 2 or view.embedded_viewer
        ) and not view.dom.password_input:
            observation.screenshot_png = await marked_screenshot(workspace.page, view.elements)
        try:
            decision = await decide(
                run.llm,
                observation,
                goal=subtask.goal,
                success_criteria=subtask.success_criteria,
                feedback=feedback,
                run_id=workspace.session.run_id,
                emit=run.emit,
            )
        except InvalidProposal:
            feedback = "INVALID_TOOL_CALL"
            if visits[marker] >= 3:
                break
            continue
        if decision.name == "done":
            return OperateResult(capture=capture, notes=tuple([*notes, decision.text]))
        if decision.name == "give_up":
            return _stuck(subtask, run, "OPERATOR_STUCK")
        if visits[marker] >= 3:
            break
        if decision.name == "extract":
            notes.append(decision.text)
            feedback = "EXTRACTED"
            continue
        assert decision.action is not None
        result = await executor.step(decision.action, view)
        feedback = result.reason
        if result.requires_human:
            return _stuck(subtask, run, await _handoff(run, result.action_class))
    return _stuck(subtask, run, "OPERATOR_STUCK")
