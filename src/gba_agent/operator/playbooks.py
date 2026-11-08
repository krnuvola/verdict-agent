"""Versioned, validated YAML playbooks and guarded scripted execution."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Mapping
from pathlib import Path
from string import Formatter
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Literal

import yaml  # type: ignore[import-untyped]
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from gba_agent.access.block_detector import BlockDetector, EventSink
from gba_agent.access.fetcher import BrowserCapture
from gba_agent.api.errors import APIError
from gba_agent.operator.actions import Executor
from gba_agent.operator.perception import DESCRIBE, Perception
from gba_agent.operator.som import marked_screenshot
from gba_agent.operator.vlm import InvalidProposal, Proposal, decide

if TYPE_CHECKING:
    from gba_agent.operator.browser import Workspace
    from gba_agent.operator.repair import RepairQueue
    from gba_agent.operator.vlm import LLMClient

_SLUG = re.compile(r"[a-zA-Z0-9_-]{1,80}")
_TARGETED = {"click", "type", "select", "press_key", "upload"}


class LocatorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str | None = None
    label: str | None = None
    text: str | None = None
    css: str | None = None

    @model_validator(mode="after")
    def validate_one(self) -> LocatorSpec:
        values = [
            value for value in (self.role, self.label, self.text, self.css) if value is not None
        ]
        if len(values) != 1 or not values[0].strip() or len(values[0]) > 2000:
            raise ValueError("Specify exactly one nonempty locator")
        return self


class Expectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url_pattern: str | None = None
    text: str | None = None
    element: str | None = None

    @model_validator(mode="after")
    def validate_expectation(self) -> Expectation:
        values = [
            value for value in (self.url_pattern, self.text, self.element) if value is not None
        ]
        if len(values) != 1 or not values[0].strip() or len(values[0]) > 2000:
            raise ValueError("Specify exactly one nonempty expectation")
        if self.url_pattern is not None:
            try:
                re.compile(self.url_pattern)
            except re.error as error:
                raise ValueError("Invalid expectation URL pattern") from error
        return self


class PlaybookStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    action: Literal[
        "navigate",
        "click",
        "type",
        "select",
        "press_key",
        "scroll",
        "download",
        "upload",
        "wait",
        "back",
        "forward",
        "new_tab",
        "close_tab",
    ]
    locator: LocatorSpec | None = None
    value_template: str | None = Field(default=None, max_length=2000)
    expect: Expectation
    timeout_s: float = Field(gt=0, le=30)

    @model_validator(mode="after")
    def validate_step(self) -> PlaybookStep:
        if self.action in _TARGETED and self.locator is None:
            raise ValueError("This action requires a locator")
        if self.action == "upload":
            raise ValueError("Uploads are unavailable in v1 playbooks")
        if self.value_template is not None:
            for _, field, format_spec, conversion in Formatter().parse(self.value_template):
                if field is not None and (not _SLUG.fullmatch(field) or format_spec or conversion):
                    raise ValueError("Only plain named value placeholders are allowed")
        return self

    def render(self, values: Mapping[str, str]) -> str | None:
        if self.value_template is None:
            return None
        try:
            return self.value_template.format_map(dict(values))
        except KeyError:
            raise APIError(422, "VALIDATION_ERROR", "Playbook parameter is missing") from None


class Playbook(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    site_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    task: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    version: int = Field(gt=0)
    steps: tuple[PlaybookStep, ...] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def validate_ids(self) -> Playbook:
        if len({step.id for step in self.steps}) != len(self.steps):
            raise ValueError("Playbook step IDs must be unique")
        return self


class PlaybookStore:
    """A checked-in or human-released YAML is an immutable approved version."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _directory(self, site_id: str, task: str) -> Path:
        if not _SLUG.fullmatch(site_id) or not _SLUG.fullmatch(task):
            raise APIError(422, "VALIDATION_ERROR", "Invalid playbook identifier")
        directory = self.root / site_id
        if directory.is_symlink():
            raise APIError(422, "VALIDATION_ERROR", "Playbook directory cannot be a symlink")
        return directory

    def load(self, site_id: str, task: str, version: int) -> Playbook:
        path = self._directory(site_id, task) / f"{task}@{version}.yaml"
        if path.is_symlink():
            raise APIError(422, "VALIDATION_ERROR", "Playbook file cannot be a symlink")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            playbook = Playbook.model_validate(data)
        except FileNotFoundError:
            raise APIError(404, "PLAYBOOK_NOT_FOUND", "Approved playbook was not found") from None
        except (OSError, UnicodeError, yaml.YAMLError, ValidationError):
            raise APIError(
                422, "VALIDATION_ERROR", "Approved playbook could not be validated"
            ) from None
        if (playbook.site_id, playbook.task, playbook.version) != (site_id, task, version):
            raise APIError(422, "VALIDATION_ERROR", "Playbook filename and identity disagree")
        return playbook

    def latest(self, site_id: str, task: str) -> Playbook:
        directory = self._directory(site_id, task)
        pattern = re.compile(rf"{re.escape(task)}@([1-9][0-9]*)\.yaml")
        versions = [
            int(match[1])
            for path in directory.glob(f"{task}@*.yaml")
            if (match := pattern.fullmatch(path.name))
        ]
        if not versions:
            raise APIError(404, "PLAYBOOK_NOT_FOUND", "Approved playbook was not found")
        return self.load(site_id, task, max(versions))

    def release(self, playbook: Playbook) -> None:
        directory = self._directory(playbook.site_id, playbook.task)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{playbook.task}@{playbook.version}.yaml"
        payload = yaml.safe_dump(
            playbook.model_dump(mode="json", exclude_none=True), sort_keys=False
        )
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as output:
            temporary = Path(output.name)
            try:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
                output.close()
                os.link(temporary, path)
            except FileExistsError:
                if self.load(playbook.site_id, playbook.task, playbook.version) != playbook:
                    raise APIError(
                        422, "VALIDATION_ERROR", "Playbook versions are immutable"
                    ) from None
            finally:
                output.close()
                temporary.unlink(missing_ok=True)


async def _locate(workspace: Workspace, locator: LocatorSpec, view: Perception) -> int | None:
    if locator.css is not None:
        matching = await workspace.page.locator(locator.css).evaluate_all(
            f"nodes => nodes.slice(0, 201).map(node => ({DESCRIBE})(node).selector)"
        )
        indices = [
            element.index for element in view.elements if element.data["selector"] in matching
        ]
    else:
        values = {"role": locator.role, "label": locator.label, "text": locator.text}
        key, expected = next((key, value) for key, value in values.items() if value is not None)
        indices = [
            element.index
            for element in view.elements
            if expected == element.data.get(key)
            or (key == "label" and expected == element.data.get("name"))
        ]
    return indices[0] if len(indices) == 1 else None


async def _expected(workspace: Workspace, expect: Expectation, view: Perception) -> bool:
    if expect.url_pattern is not None:
        return re.search(expect.url_pattern, workspace.page.url) is not None
    if expect.text is not None:
        return expect.text in view.observation.text_excerpt
    assert expect.element is not None
    locator = workspace.page.locator(expect.element)
    return await locator.count() > 0 and await locator.first.is_visible()


class _Drift(Exception):
    """A locator or expectation drifted, with no observed block or guard denial."""


def _failed() -> APIError:
    return APIError(503, "SOURCE_UNRETRIEVABLE", "Guarded playbook step did not complete")


async def run_playbook(
    workspace: Workspace,
    executor: Executor,
    playbook: Playbook,
    *,
    start_url: str,
    values: Mapping[str, str],
    login_patterns: list[str],
    client: LLMClient | None,
    repairs: RepairQueue | None,
    emit: EventSink | None,
) -> BrowserCapture:
    workspace.documents.clear()
    workspace.attachments.clear()
    workspace.last_download = None
    had_session = workspace.session.last_used_at > workspace.session.created_at
    detector = BlockDetector()

    async def inspect() -> tuple[Perception, BrowserCapture, bool]:
        view = await workspace.perception()
        capture = workspace.capture(start_url, view.dom, had_session)
        return (
            view,
            capture,
            detector.detect(capture.response, login_url_patterns=login_patterns) is not None,
        )

    initial = await executor.step(
        Proposal(action_type="navigate", observation_id=0, value=start_url), None
    )
    if not initial.executed:
        raise _failed()
    view, capture, blocked = await inspect()
    if blocked:
        return capture
    for step in playbook.steps:
        value = step.render(values)
        try:
            async with asyncio.timeout(step.timeout_s):
                index = await _locate(workspace, step.locator, view) if step.locator else None
                if step.locator and index is None:
                    raise _Drift
                outcome = await executor.step(
                    Proposal(
                        action_type=step.action,
                        observation_id=view.observation.observation_id,
                        target_index=index,
                        value=value,
                    ),
                    view,
                )
                if not outcome.executed:
                    # Guard denial and dry run are terminal. Only DOM drift can be repaired.
                    if outcome.reason not in {"BROWSER_ACTION_FAILED", "BROWSER_TIMEOUT"}:
                        raise _failed()
                    raise _Drift
                view, capture, blocked = await inspect()
                if blocked:
                    return capture
                if not await _expected(workspace, step.expect, view):
                    raise _Drift
                continue
        except (_Drift, PlaywrightError, TimeoutError):
            view, capture, blocked = await inspect()
            if blocked:
                return capture
            if client is None or repairs is None or step.locator is None:
                raise _failed() from None
        if emit:
            emit(
                workspace.session.run_id,
                "state_change",
                {
                    "from": "drifted",
                    "to": "repairing",
                    "operation": "playbook_repair",
                    "phase": "started",
                    "step_id": step.id,
                },
            )
        try:
            async with asyncio.timeout(step.timeout_s):
                decision = await decide(
                    client,
                    view.observation,
                    goal=f"Repair locator for playbook step {step.id}.",
                    success_criteria="Use the same action and value on the intended control.",
                    feedback="PLAYBOOK_LOCATOR_OR_EXPECTATION_FAILED",
                    run_id=workspace.session.run_id,
                    emit=emit,
                )
                action = decision.action
                if (
                    action is None
                    or action.action_type != step.action
                    or action.value != value
                    or action.observation_id != view.observation.observation_id
                    or action.target_index is None
                    or not 1 <= action.target_index <= len(view.elements)
                ):
                    raise _failed()
                selector = view.elements[action.target_index - 1].data["selector"]
                if not isinstance(selector, str):
                    raise _failed()
                outcome = await executor.step(action, view)
                if not outcome.executed:
                    raise _failed()
                view, capture, blocked = await inspect()
                if blocked:
                    return capture
                if not await _expected(workspace, step.expect, view):
                    raise _failed()
                screenshot = await marked_screenshot(workspace.page, view.elements)
                repairs.propose(playbook, step.id, LocatorSpec(css=selector), screenshot)
                if emit:
                    emit(
                        workspace.session.run_id,
                        "state_change",
                        {
                            "from": "repairing",
                            "to": "repaired",
                            "operation": "playbook_repair",
                            "phase": "finished",
                            "step_id": step.id,
                        },
                    )
        except (InvalidProposal, PlaywrightError, TimeoutError, APIError):
            if emit:
                emit(
                    workspace.session.run_id,
                    "error",
                    {
                        "where": "playbook_repair",
                        "message": "Playbook repair failed.",
                        "step_id": step.id,
                    },
                )
            raise _failed() from None
    return capture
