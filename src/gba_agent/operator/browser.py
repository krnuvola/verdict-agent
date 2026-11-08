"""Real Chromium runtime. Fetcher supplies the sole external HTTP exchange."""

from __future__ import annotations

import asyncio
import re
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx
from playwright.async_api import (
    Dialog,
    Download,
    Page,
    Route,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from sqlalchemy.orm import Session as DBSession
from sqlalchemy.orm import sessionmaker

from gba_agent.access._registry import Registry, RegistryEntry, load_site_metadata, safe_url
from gba_agent.access.block_detector import DOMSignals, EventSink, ResponseInfo
from gba_agent.access.fetcher import (
    AdapterFetchRequest,
    BrowserCapture,
    BrowserPermit,
    FetchRequest,
    OperatorFetchRequest,
    _RobotsDenied,
)
from gba_agent.access.handoff import HandoffManager
from gba_agent.access.polite_client import _RobotsThrottle
from gba_agent.access.session_manager import Session, SessionManager
from gba_agent.api.contracts_compat import BrowserAction, RunConfig
from gba_agent.api.errors import APIError
from gba_agent.config import Settings
from gba_agent.guards.action_guard import classify
from gba_agent.operator.perception import Perception, observe, public_url

if TYPE_CHECKING:
    from gba_agent.operator.playbooks import PlaybookStore
    from gba_agent.operator.repair import RepairQueue
    from gba_agent.operator.vlm import LLMClient

_REDIRECTS = {301, 302, 303, 307, 308}
_TRANSPORT_HEADERS = {
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "alt-svc",
    "refresh",
    "set-cookie",
}


@dataclass(frozen=True)
class Capture:
    url: str
    status: int
    media: str
    raw: bytes = field(repr=False)
    retry_after: str | None = None

    @classmethod
    def from_response(cls, response: httpx.Response) -> Capture:
        media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        return cls(
            str(response.url),
            response.status_code,
            media,
            response.content,
            response.headers.get("retry-after"),
        )

    def result(
        self, requested: str, dom: DOMSignals, session: Session, had_session: bool
    ) -> BrowserCapture:
        sample = (
            self.raw[:8192].decode("utf-8", "replace") if self.media.startswith("text/") else ""
        )
        return BrowserCapture(
            ResponseInfo(
                self.status,
                {"retry-after": self.retry_after} if self.retry_after else {},
                self.url,
                requested,
                self.media,
                sample,
                dom,
                had_session,
            ),
            self.raw,
            session,
        )


def _destination(url: str) -> tuple[str, str, str]:
    parsed = urlsplit(url)
    return parsed.scheme, parsed.netloc, parsed.path or "/"


class Workspace:
    """One persistent context, with a request route installed before any page is created."""

    def __init__(self, session: Session, entry: RegistryEntry, emit: EventSink | None) -> None:
        self.session, self.emit = session, emit
        self.entry = entry
        self.page: Page
        self.permit: BrowserPermit | None = None
        self.issued: set[tuple[str, str, str]] = set()
        self.network: deque[dict[str, Any]] = deque(maxlen=30)
        self.dialogs: deque[str] = deque(maxlen=10)
        self.downloads: deque[Download] = deque(maxlen=10)
        self.documents: dict[str, Capture] = {}
        self.attachments: dict[str, Capture] = {}
        self.last_download: Capture | None = None
        self._completed: deque[Download] = deque(maxlen=10)
        self._download_name: str | None = None
        self.route_error: Exception | None = None
        self.force_download: str | None = None
        self.sequence = 0
        self.ready = False

    async def start(self) -> None:
        context = self.session.context
        await context.route("**/*", self._route)
        await context.route_web_socket("**/*", lambda socket: socket.close())
        context.on("page", self._listen)
        self.page = await context.new_page()
        self.ready = True

    def _listen(self, page: Page) -> None:
        page.on("dialog", self._dismiss)
        page.on("download", lambda download: self.downloads.append(download))

    async def _dismiss(self, dialog: Dialog) -> None:
        self.dialogs.append(dialog.type)
        if self.session.paused or self.permit is None:
            return
        page_url = (dialog.page or self.page).url
        action = BrowserAction(
            action_type="press_key",
            value="Escape",
            page_url=page_url,
            site_id=self.entry.entry_id,
        )
        decision = classify(action, self.entry, None, None)
        event_action = action.model_dump()
        event_action["page_url"] = public_url(page_url)
        if self.emit:
            self.emit(self.session.run_id, "action_proposed", {"action": event_action})
            self.emit(self.session.run_id, "action_decided", {"decision": decision.model_dump()})
        if decision.allowed:
            await dialog.dismiss()
            if self.emit:
                self.emit(
                    self.session.run_id,
                    "action_executed",
                    {
                        "action": event_action,
                        "result": "ok",
                        "detail": None,
                    },
                )

    @asynccontextmanager
    async def action_scope(
        self, urls: list[str], *, download: str | None = None
    ) -> AsyncIterator[None]:
        self.issued = {_destination(url) for url in urls}
        self.route_error = None
        self.force_download = download
        try:
            yield
        finally:
            self.issued.clear()
            self.force_download = None

    async def _route(self, route: Route) -> None:
        request = route.request
        url = request.url
        permit = self.permit
        navigation = request.is_navigation_request()
        # The context's deny proxy also prevents any non-intercepted egress.
        if permit is None or self.session.paused or request.method not in {"GET", "HEAD"}:
            await route.abort("blockedbyclient")
            return
        if navigation and _destination(url) not in self.issued:
            await route.abort("blockedbyclient")
            return
        try:
            response = await self._exchange(route, permit)
        except (
            APIError,
            httpx.RequestError,
            TimeoutError,
            _RobotsDenied,
            _RobotsThrottle,
        ) as error:
            if navigation:
                self.route_error = error
            await route.abort("blockedbyclient")
            return
        capture = Capture.from_response(response)
        headers = {
            name: value
            for name, value in response.headers.items()
            if name not in _TRANSPORT_HEADERS
        }
        cookies = response.headers.get_list("set-cookie")
        if cookies and _destination(url)[:2] == _destination(capture.url)[:2]:
            headers["set-cookie"] = "\n".join(cookies)
        forced = self.force_download == url and 200 <= capture.status < 300
        attachment = forced or "attachment" in headers.get("content-disposition", "").lower()
        if forced and "attachment" not in headers.get("content-disposition", "").lower():
            headers["content-disposition"] = "attachment"
        if navigation or attachment:
            self.documents[url] = capture
            if attachment:
                self.attachments[url] = capture
        self.network.append(
            {
                "url": public_url(capture.url),
                "status": capture.status,
                "resource_type": request.resource_type,
            }
        )
        await route.fulfill(status=capture.status, headers=headers, body=capture.raw)

    async def _exchange(self, route: Route, permit: BrowserPermit) -> httpx.Response:
        request = route.request
        url = request.url
        headers = await request.all_headers()
        for _ in range(6):
            async with permit(url, request.method, headers=headers) as response:
                if response is None:
                    raise APIError(503, "SOURCE_UNRETRIEVABLE", "Browser exchange unavailable")
            if response.status_code not in _REDIRECTS:
                return response
            location = response.headers.get("location")
            destination = urldefrag(urljoin(url, location or ""))[0]
            if (
                not location
                or safe_url(destination) is None
                or (url.startswith("https:") and not destination.startswith("https:"))
            ):
                break
            if _destination(url)[:2] != _destination(destination)[:2]:
                headers.pop("cookie", None)
            url = destination
        raise APIError(503, "SOURCE_UNRETRIEVABLE", "Browser redirect could not be verified")

    async def settled(self, before: set[Page]) -> None:
        # Page creation is driven by Chromium, including target=_blank links.
        opened = [page for page in self.session.context.pages if page not in before]
        if opened:
            self.page = opened[-1]
        if self.page.is_closed():
            remaining = self.session.context.pages
            if remaining:
                self.page = remaining[-1]
            else:
                return
        try:
            await self.page.wait_for_load_state("networkidle", timeout=1500)
        except PlaywrightError:
            # Long-polling or a closed popup must not stall a step.
            pass
        if self.route_error is not None:
            raise self.route_error
        while self.downloads:
            await self.complete_download(self.downloads.popleft())

    async def complete_download(self, download: Download) -> None:
        if download in self._completed:
            return
        async with asyncio.timeout(5):
            path = await download.path()
            raw = await asyncio.to_thread(path.read_bytes)
        captured = self.attachments.pop(download.url, None) or self.documents.get(download.url)
        if captured is None or raw != captured.raw:
            raise APIError(503, "SOURCE_UNRETRIEVABLE", "Download provenance could not be verified")
        self._completed.append(download)
        self.last_download = captured
        self._download_name = Path(download.suggested_filename).name
        if self.emit:
            self.emit(
                self.session.run_id,
                "download",
                {
                    "url": public_url(captured.url),
                    "doc_id": "sha256:" + sha256(raw).hexdigest(),
                    "bytes": len(raw),
                    "complete": True,
                },
            )

    async def perception(self) -> Perception:
        self.sequence += 1
        downloads = []
        if self.last_download is not None:
            downloads.append(
                {"suggested_name": self._download_name, "path": None, "complete": True}
            )
        return await observe(
            self.page,
            self.sequence,
            network=list(self.network),
            downloads=downloads,
            dialogs=list(self.dialogs),
        )

    def capture(self, requested: str, dom: DOMSignals, had_session: bool) -> BrowserCapture:
        item = self.last_download or self.documents.get(urldefrag(self.page.url)[0])
        if item is None:
            raise APIError(503, "SOURCE_UNRETRIEVABLE", "No verified browser response")
        return item.result(requested, dom, self.session, had_session)


class BrowserRuntime:
    """Process-owned browser and encrypted per-site contexts, used by Fetcher."""

    def __init__(
        self,
        sessions: SessionManager,
        registry: Registry,
        *,
        llm: LLMClient | None,
        handoffs: HandoffManager | None,
        run_config: RunConfig,
        emit: EventSink | None,
        site_config: Mapping[str, Mapping[str, object]] | None,
        playbooks: PlaybookStore | None = None,
        repairs: RepairQueue | None = None,
        managed_wall_time: bool = False,
    ) -> None:
        self.sessions, self.llm, self.handoffs = sessions, llm, handoffs
        self.registry, self.run_config, self.emit = registry, run_config, emit
        self.site_config = dict(site_config or {})
        self.playbooks, self.repairs = playbooks, repairs
        self.managed_wall_time = managed_wall_time
        self._workspaces: dict[str, Workspace] = {}
        self._lock = asyncio.Lock()

    @classmethod
    @asynccontextmanager
    async def launch(
        cls,
        settings: Settings,
        database: sessionmaker[DBSession],
        registry: Registry,
        *,
        llm: LLMClient | None = None,
        handoffs: HandoffManager | None = None,
        run_config: RunConfig | None = None,
        emit: EventSink | None = None,
        executable_path: Path | None = None,
        site_config: Mapping[str, Mapping[str, object]] | None = None,
        managed_wall_time: bool = False,
    ) -> AsyncIterator[BrowserRuntime]:
        from gba_agent.operator.playbooks import PlaybookStore
        from gba_agent.operator.repair import RepairQueue

        settings.require_session_encryption_key()
        metadata: dict[str, Mapping[str, object]] = {
            entry.entry_id: load_site_metadata(settings.registry_path, entry.entry_id)
            for entry in registry.entries
        }
        metadata.update(site_config or {})
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=executable_path,
                headless=settings.browser_mode == "headless",
                proxy={"server": "http://127.0.0.1:1", "bypass": "<-loopback>"},
                args=[
                    "--disable-background-networking",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                ],
            )
            try:
                playbooks = PlaybookStore(settings.playbook_dir)
                yield cls(
                    SessionManager(
                        settings, browser, database, emit=emit, block_service_workers=True
                    ),
                    registry,
                    llm=llm,
                    handoffs=handoffs,
                    run_config=run_config or RunConfig(),
                    emit=emit,
                    site_config=metadata,
                    playbooks=playbooks,
                    repairs=RepairQueue(database, playbooks),
                    managed_wall_time=managed_wall_time,
                )
            finally:
                await browser.close()

    async def _workspace(self, session: Session) -> Workspace:
        workspace = self._workspaces.get(session.session_id)
        if workspace is None:
            entry = next(
                (entry for entry in self.registry.entries if entry.entry_id == session.site_id),
                None,
            )
            if entry is None:
                raise APIError(403, "SG_URL_NOT_IN_REGISTRY", "Browser source is not registered")
            workspace = Workspace(session, entry, self.emit)
            await workspace.start()
            self._workspaces[session.session_id] = workspace
        return workspace

    async def acquire(
        self,
        request: FetchRequest,
        permit: BrowserPermit,
        *,
        proxy_url: str | None,
        ca_bundle: Path | None,
    ) -> BrowserCapture:
        # Upstream proxy and CA policy are already applied by Fetcher's exchange.
        from gba_agent.operator.loop import RunContext, Subtask, operate

        entry = self.registry.match_url(request.url)
        if entry is None:
            raise APIError(403, "SG_URL_NOT_IN_REGISTRY", "Browser source is not registered")
        async with self._lock:
            session = await self.sessions.get_or_create(entry.entry_id, request.run_id)
            workspace = await self._workspace(session)
            workspace.permit = permit
            try:
                if isinstance(request, AdapterFetchRequest) and request.playbook_task is not None:
                    return await self._scripted(workspace, entry, request)
                outcome = await operate(
                    Subtask(
                        goal=(
                            request.goal
                            if isinstance(request, OperatorFetchRequest)
                            else "Retrieve the requested public document."
                        ),
                        site_id=entry.entry_id,
                        start_url=request.url,
                        success_criteria=(
                            request.success_criteria
                            if isinstance(request, OperatorFetchRequest)
                            else "Find the requested document."
                        ),
                        max_steps=min(25, self.run_config.max_steps),
                    ),
                    RunContext(
                        workspace,
                        entry,
                        self.site_config.get(entry.entry_id, {}),
                        self.run_config,
                        self.llm,
                        self.handoffs,
                        self.emit,
                        self.managed_wall_time,
                    ),
                )
                if outcome.capture is None:
                    raise APIError(
                        503, "SOURCE_UNRETRIEVABLE", "Operator did not retrieve a document"
                    )
                return outcome.capture
            finally:
                workspace.permit = None

    async def _scripted(
        self,
        workspace: Workspace,
        entry: RegistryEntry,
        request: AdapterFetchRequest,
    ) -> BrowserCapture:
        from gba_agent.operator.actions import Executor
        from gba_agent.operator.playbooks import run_playbook

        if self.playbooks is None or request.playbook_task is None:
            raise APIError(503, "SOURCE_UNRETRIEVABLE", "Playbook store unavailable")
        site = self.site_config.get(entry.entry_id, {})
        configured = site.get("login_url_patterns")
        patterns = (
            [item for item in configured if isinstance(item, str)]
            if isinstance(configured, list)
            else []
        )
        try:
            playbook = self.playbooks.latest(entry.entry_id, request.playbook_task)
        except APIError as error:
            if error.code != "PLAYBOOK_NOT_FOUND":
                raise
            raise APIError(503, "SOURCE_UNRETRIEVABLE", "Approved playbook unavailable") from None
        async with asyncio.timeout(self.run_config.max_wall_seconds):
            return await run_playbook(
                workspace,
                Executor(workspace, entry, site, self.run_config, self.emit),
                playbook,
                start_url=request.url,
                values=request.playbook_values,
                login_patterns=patterns,
                client=self.llm,
                repairs=self.repairs,
                emit=self.emit,
            )

    def login_browser(
        self,
        permit: BrowserPermit,
        *,
        proxy_url: str | None,
        ca_bundle: Path | None,
    ) -> LoginViewer:
        return LoginViewer(self, permit)


class LoginViewer:
    """Read-only observation and guarded navigation for Access Layer handoff."""

    def __init__(self, runtime: BrowserRuntime, permit: BrowserPermit) -> None:
        self.runtime, self.permit = runtime, permit

    async def prepare(self, session: Session, url: str) -> ResponseInfo:
        from gba_agent.operator.actions import Executor
        from gba_agent.operator.vlm import Proposal

        entry = self.runtime.registry.match_url(url)
        if entry is None or entry.entry_id != session.site_id:
            raise APIError(403, "SG_URL_NOT_IN_REGISTRY", "Login URL is not registered")
        workspace = await self.runtime._workspace(session)
        workspace.last_download = None
        workspace.permit = self.permit
        try:
            result = await Executor(
                workspace, entry, {}, self.runtime.run_config, self.runtime.emit
            ).step(Proposal(action_type="navigate", observation_id=0, value=url), None)
            if not result.executed:
                raise APIError(403, "SOURCE_UNRETRIEVABLE", "Login navigation was blocked")
            perception = await workspace.perception()
            return workspace.capture(url, perception.dom, True).response
        finally:
            workspace.permit = None

    async def has_visible_selector(self, session: Session, selectors: Sequence[str]) -> bool:
        workspace = await self.runtime._workspace(session)
        for selector in selectors:
            if await workspace.page.locator(selector).first.is_visible():
                return True
        return False

    async def check_authenticated(self, session: Session, check: object) -> bool:
        workspace = await self.runtime._workspace(session)
        selector = getattr(check, "selector", None)
        pattern = getattr(check, "url_pattern", None)
        if isinstance(selector, str):
            return await workspace.page.locator(selector).first.is_visible()
        return isinstance(pattern, str) and re.search(pattern, workspace.page.url) is not None
