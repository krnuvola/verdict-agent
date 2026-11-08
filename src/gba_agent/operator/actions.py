"""Resolve, revalidate, classify and execute one Chromium action."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Any
from urllib.parse import urldefrag, urljoin

from playwright.async_api import ElementHandle, Page
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from gba_agent.access._registry import RegistryEntry, matches_prefix, safe_url
from gba_agent.access.block_detector import EventSink
from gba_agent.api.contracts_compat import ActionDecision, BrowserAction, RunConfig
from gba_agent.guards import reason_codes as rc
from gba_agent.guards.action_guard import classify
from gba_agent.operator.browser import Workspace
from gba_agent.operator.perception import Element, Perception, describe_target, public_url
from gba_agent.operator.vlm import Proposal

_TIMEOUT = 4000
_TARGET_ACTIONS = {"click", "type", "select", "press_key", "upload"}
_NAVIGATION = {"navigate", "new_tab", "download"}
_SAFE_KEYS = {
    "Enter",
    "Tab",
    "Escape",
    "ArrowDown",
    "ArrowUp",
    "ArrowLeft",
    "ArrowRight",
    "PageDown",
    "PageUp",
    "Home",
    "End",
}
_VERIFY_FIELDS = (
    "tag",
    "input_type",
    "href",
    "form_action",
    "form_method",
    "text",
    "autocomplete",
    "label",
    "opens_tab",
)


@dataclass(frozen=True)
class StepResult:
    reason: str
    executed: bool = False
    requires_human: bool = False
    action_class: str | None = None


class Executor:
    def __init__(
        self,
        workspace: Workspace,
        entry: RegistryEntry,
        site_config: object,
        run_config: RunConfig,
        emit: EventSink | None,
    ) -> None:
        self.workspace, self.entry = workspace, entry
        self.site_config, self.run_config, self.emit = site_config, run_config, emit

    def _trace(self, kind: str, payload: dict[str, object]) -> None:
        if self.emit is not None:
            self.emit(self.workspace.session.run_id, kind, payload)

    def _scope_allowed(self, url: str) -> bool:
        dest = urldefrag(url)[0]
        return safe_url(dest) is not None and any(
            matches_prefix(dest, prefix) for prefix in self.entry.url_prefixes
        )

    @staticmethod
    def _deny(code: str, reason: str) -> Any:
        return ActionDecision(
            action_class="COMMIT",
            allowed=False,
            requires_human=False,
            reason_code=code,
            reason_text=reason,
        )

    async def _target(
        self,
        proposal: Proposal,
        perception: Perception | None,
    ) -> tuple[ElementHandle | None, dict[str, Any] | None]:
        if proposal.target_index is None:
            return None, None
        if perception is None or not 1 <= proposal.target_index <= len(perception.elements):
            raise ValueError("Unknown element index")
        observed = perception.elements[proposal.target_index - 1]
        handle = await self.workspace.page.query_selector(observed.data["selector"])
        if handle is None:
            raise ValueError("Observed element is gone")
        enter = proposal.action_type == "press_key" and proposal.value == "Enter"
        try:
            live = await describe_target(self.workspace.page, observed.data["selector"], enter)
        except PlaywrightError:
            await handle.dispose()
            raise ValueError("Observed document changed") from None
        if live is None:
            await handle.dispose()
            raise ValueError("Observed element is gone")
        current, effective = live
        if (
            not current["visible"]
            or not current["enabled"]
            or any(current[key] != observed.data[key] for key in _VERIFY_FIELDS)
            or (current["name"] and current["name"] != observed.data["name"])
        ):
            await handle.dispose()
            raise ValueError("Observed element has changed")
        effective["index"] = proposal.target_index
        return handle, effective

    async def step(self, proposal: Proposal, perception: Perception | None) -> StepResult:
        page = self.workspace.page
        action_type = proposal.action_type
        value = proposal.value
        base = (
            page.url
            if page.url != "about:blank"
            else (value if action_type in _NAVIGATION and value else "")
        )
        event_action: dict[str, object] = {
            "action_type": action_type,
            "target": {"index": proposal.target_index}
            if proposal.target_index is not None
            else None,
            "value": None,
            "page_url": public_url(base),
            "site_id": self.entry.entry_id,
        }
        self._trace("action_proposed", {"action": event_action})
        handle = None
        try:
            if (
                perception is not None
                and proposal.observation_id != perception.observation.observation_id
            ):
                raise ValueError("The observation is stale")
            if self.workspace.session.paused:
                raise ValueError("The browser is paused for a human handoff")
            handle, target = await self._target(proposal, perception)
            if action_type in _TARGET_ACTIONS and handle is None:
                raise ValueError("An element index is required")
            if action_type in _NAVIGATION and value:
                value = urljoin(base, value)
            target_fields = (
                Element(proposal.target_index or 0, target).model_fields() if target else None
            )
            canonical = BrowserAction.model_validate(
                {
                    "action_type": action_type,
                    "target": target_fields,
                    "value": value,
                    "page_url": base,
                    "site_id": self.entry.entry_id,
                }
            )
            guarded = canonical.model_dump()
            if target is not None:
                guarded["target"].update(
                    label=target.get("label"), autocomplete=target.get("autocomplete")
                )
            decision = classify(guarded, self.entry, self.site_config, self.run_config)
            if decision.allowed:
                decision = self._check_safe_dispatch(action_type, value, target, decision)
            self._trace("action_decided", {"decision": decision.model_dump()})
            if not decision.allowed:
                return StepResult(
                    decision.reason_code,
                    requires_human=decision.requires_human,
                    action_class=decision.action_class,
                )
            if self.run_config.dry_run:
                self._trace(
                    "action_executed",
                    {"action": event_action, "result": "error", "detail": "DRY_RUN"},
                )
                return StepResult("DRY_RUN")
            try:
                await self._perform(page, action_type, value, target, handle)
            except PlaywrightError as error:
                self._trace(
                    "action_executed",
                    {"action": event_action, "result": "error", "detail": "BROWSER_ACTION_FAILED"},
                )
                if self.workspace.route_error is not None:
                    raise self.workspace.route_error from None
                if isinstance(error, PlaywrightTimeout):
                    return StepResult("BROWSER_TIMEOUT")
                return StepResult("BROWSER_ACTION_FAILED")
            self._trace("action_executed", {"action": event_action, "result": "ok", "detail": None})
            return StepResult(
                decision.reason_code, executed=True, action_class=decision.action_class
            )
        except ValueError:
            decision = self._deny(rc.AG_UNCLASSIFIED_DENY, "Stale or invalid browser action")
            self._trace("action_decided", {"decision": decision.model_dump()})
            return StepResult(decision.reason_code)
        finally:
            if handle is not None:
                await handle.dispose()

    def _check_safe_dispatch(
        self,
        kind: str,
        value: str | None,
        target: dict[str, Any] | None,
        decision: Any,
    ) -> Any:
        if (
            target
            and target.get("ambiguous_label")
            and (kind == "click" or (kind == "press_key" and value == "Enter"))
        ):
            return self._deny(rc.AG_UNCLASSIFIED_DENY, "The target label is ambiguous")
        for destination in (
            value if kind in _NAVIGATION else None,
            target.get("href") if target else None,
            target.get("form_action") if target else None,
        ):
            if destination and not self._scope_allowed(destination):
                return self._deny(
                    rc.AG_SITE_NOT_ALLOWED, "Destination is outside registry prefixes"
                )
        if (
            kind in {"click", "press_key"}
            and target
            and target.get("form_method") == "post"
            and (target["tag"] != "a" or not target.get("href"))
        ):
            return ActionDecision(
                action_class="COMMIT",
                allowed=False,
                requires_human=True,
                reason_code=rc.AG_COMMIT_FORBIDDEN,
                reason_text="Agent form submission must be read-only",
            )
        if kind in {"navigate", "new_tab"} and not value:
            return self._deny(rc.AG_UNCLASSIFIED_DENY, "A navigation URL is required")
        if (
            kind == "download"
            and not value
            and not (target and target["tag"] == "a" and target["href"])
        ):
            return self._deny(rc.AG_UNCLASSIFIED_DENY, "A download requires a URL or link")
        if kind == "press_key" and value not in _SAFE_KEYS:
            return self._deny(rc.AG_UNCLASSIFIED_DENY, "Unsupported keyboard shortcut")
        if kind == "upload" or (kind in {"type", "select"} and value is None):
            return self._deny(rc.AG_UNCLASSIFIED_DENY, "Unsupported browser action")
        if kind == "close_tab" and len(self.workspace.session.context.pages) <= 1:
            return self._deny(rc.AG_UNCLASSIFIED_DENY, "The only tab cannot be closed")
        if kind in {"wait", "scroll"}:
            try:
                number = float(value or ("0" if kind == "wait" else "600"))
            except ValueError:
                number = math.nan
            if not math.isfinite(number):
                return self._deny(rc.AG_UNCLASSIFIED_DENY, "Invalid numeric action")
        return decision

    async def _perform(
        self,
        page: Page,
        kind: str,
        value: str | None,
        target: dict[str, Any] | None,
        handle: ElementHandle | None,
    ) -> None:
        before = set(self.workspace.session.context.pages)
        urls = [page.url]
        if kind in _NAVIGATION and value:
            urls.append(value)
        if target:
            urls.extend(url for key in ("href", "form_action") if (url := target.get(key)))
        if kind in {"back", "forward"}:
            urls.extend(self.workspace.documents)
        download_url = (value or (target or {}).get("href")) if kind == "download" else None
        async with self.workspace.action_scope(urls, download=download_url):
            if kind == "navigate" and value:
                await self._goto(page, value)
            elif kind == "new_tab" and value:
                self.workspace.page = await page.context.new_page()
                await self._goto(self.workspace.page, value)
            elif kind == "close_tab":
                await page.close()
            elif kind == "back":
                await page.go_back(wait_until="domcontentloaded", timeout=_TIMEOUT)
            elif kind == "forward":
                await page.go_forward(wait_until="domcontentloaded", timeout=_TIMEOUT)
            elif kind == "scroll":
                await page.mouse.wheel(0, min(2000, max(-2000, float(value or "600"))))
            elif kind == "wait":
                await asyncio.sleep(min(10, max(0, float(value or "0"))))
            elif kind == "download":
                async with page.expect_download(timeout=_TIMEOUT) as expected:
                    if value:
                        await self._goto(page, value)
                    elif handle is not None:
                        await handle.click(timeout=_TIMEOUT)
                await self.workspace.complete_download(await expected.value)
            elif handle is not None:
                if kind == "click":
                    if target and target.get("opens_tab"):
                        async with page.expect_popup(timeout=_TIMEOUT) as expected_popup:
                            await handle.click(timeout=_TIMEOUT)
                        self.workspace.page = await expected_popup.value
                    else:
                        await handle.click(timeout=_TIMEOUT)
                elif kind == "type" and value is not None:
                    await handle.fill(value, timeout=_TIMEOUT)
                elif kind == "select" and value is not None:
                    await handle.select_option(value=value, timeout=_TIMEOUT)
                elif kind == "press_key" and value is not None:
                    await handle.press(value, timeout=_TIMEOUT)
            await self.workspace.settled(before)

    @staticmethod
    async def _goto(page: Page, url: str) -> None:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=_TIMEOUT)
        except PlaywrightError as error:
            if "Download is starting" not in str(error):
                raise
