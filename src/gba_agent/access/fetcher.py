"""Registry-gated acquisition and the deterministic Doc 01 section 6.7 policy."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.orm import Session as DBSession
from sqlalchemy.orm import sessionmaker

from gba_agent.access._registry import (
    Registry,
    RegistryEntry,
    load_registry,
    load_site_metadata,
    matches_prefix,
    origin,
    safe_url,
)
from gba_agent.access.block_detector import (
    BlockAction,
    BlockDetector,
    BlockEvent,
    DOMSignals,
    EventSink,
    ResponseInfo,
    html_signals,
)
from gba_agent.access.handoff import HandoffManager
from gba_agent.access.login import LoginBrowser, LoginFlow, LoginSite
from gba_agent.access.polite_client import PoliteClient, _RobotsThrottle
from gba_agent.access.rate_limiter import RateLimiter
from gba_agent.access.session_manager import Session, SessionManager
from gba_agent.api.contracts_compat import BrowserAction, RunConfig
from gba_agent.api.errors import APIError
from gba_agent.api.trace import new_ulid
from gba_agent.config import Settings
from gba_agent.db.models import SourceRecord as SourceRow
from gba_agent.db.repositories import SourceRecordsRepository
from gba_agent.db.session import session_scope
from gba_agent.guards.action_guard import classify

if TYPE_CHECKING:
    from gba_agent.api.contracts_compat import _CompatSourceRecord as SourceRecord
else:
    from gba_agent.api.contracts_compat import SourceRecord

_FILING_TYPES = frozenset({"merger_agreement", "proxy_statement", "8k", "exhibit", "filing_index"})
_DOCUMENT_MEDIA = frozenset({"application/pdf", "text/html", "application/xhtml+xml", "text/plain"})
_API_MEDIA = _DOCUMENT_MEDIA | {"application/json", "application/xml", "text/xml"}
_RETRY_SWITCH = {"http_403", "silent_content_change"}
_RETRY_BACKOFF = {"http_429", "timeout"}


class FetchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    url: str = Field(repr=False)
    run_id: str
    matter_id: str | None = None
    expected_doc_type: str | None = None
    version_label: str | None = None
    method_hint: Literal["auto", "api", "adapter", "operator"] = "auto"


class AdapterFetchRequest(FetchRequest):
    """An already-resolved target, scoped to its registered adapter."""

    site_id: str
    playbook_task: str | None = None
    playbook_values: dict[str, str] = Field(default_factory=dict, repr=False)


class OperatorFetchRequest(FetchRequest):
    site_id: str
    goal: str = Field(min_length=1, max_length=1000, repr=False)
    success_criteria: str = Field(min_length=1, max_length=1000, repr=False)
    method_hint: Literal["operator"] = "operator"


class CitationLookupRequest(AdapterFetchRequest):
    url: Literal["https://www.courtlistener.com/api/rest/v4/citation-lookup/"] = (
        "https://www.courtlistener.com/api/rest/v4/citation-lookup/"
    )
    site_id: Literal["courtlistener"] = "courtlistener"
    citation: str = Field(min_length=1, max_length=300, repr=False)


class FetchOutcome(BaseModel):
    status: Literal["ok", "unretrievable"]
    source_record: SourceRecord | None
    blocks: list[dict[str, object]]
    reason_code: str | None


class BlobStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...


@dataclass(frozen=True)
class BrowserCapture:
    """A guarded runtime returns bytes and observations, never credentials or page handles."""

    response: ResponseInfo
    raw: bytes = field(repr=False)
    session: Session | None = field(default=None, repr=False)
    doc_type: str | None = None


class BrowserPermit(Protocol):
    def __call__(
        self,
        url: str,
        method: str = "GET",
        *,
        headers: dict[str, str] | None = None,
    ) -> AbstractAsyncContextManager[httpx.Response | None]: ...


class GuardedBrowser(Protocol):
    """A-3 runtime. Apply the permit before every outgoing request and guard every action."""

    async def acquire(
        self,
        request: FetchRequest,
        permit: BrowserPermit,
        *,
        proxy_url: str | None,
        ca_bundle: Path | None,
    ) -> BrowserCapture: ...

    def login_browser(
        self,
        permit: BrowserPermit,
        *,
        proxy_url: str | None,
        ca_bundle: Path | None,
    ) -> LoginBrowser: ...


class AdapterTask(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    kind: Literal[
        "find_filing",
        "list_filings",
        "fetch_url",
        "fetch_statute",
        "fetch_bill",
        "search_opinions",
        "fetch_opinion",
        "crosscheck_citation",
    ]
    params: dict[str, object]


class _Alternate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    entry_id: str
    task: AdapterTask


class SitePolicy(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    alternates: dict[str, list[_Alternate]] = Field(default_factory=dict)
    login_url_patterns: list[str] = Field(default_factory=list)
    minimum_content_length: int | None = Field(default=None, gt=0)
    immutable_url_prefixes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_patterns(self) -> SitePolicy:
        for pattern in self.login_url_patterns:
            re.compile(pattern)
        for prefix in self.immutable_url_prefixes:
            if safe_url(prefix) is None:
                raise ValueError("Immutable URL prefixes must be HTTP(S) URLs")
        return self


@dataclass
class _AttemptBudget:
    total: int = 0
    transient: int = 0
    switch: int = 0
    login: int = 0
    captcha: int = 0

    def action(self, event: BlockEvent, login: bool, captcha: bool) -> BlockAction:
        if event.kind in _RETRY_SWITCH:
            return "retry" if self.total < 3 and self.switch == 0 else "switch_source"
        if self.total < 3:
            if event.kind in _RETRY_BACKOFF and self.transient < 2:
                return "retry"
            if event.kind in {"login_wall", "session_expired"} and login and self.login == 0:
                return "handoff"
            if event.kind == "captcha" and captcha and self.captcha == 0:
                return "handoff"
        return "mark_unretrievable"


@dataclass(frozen=True)
class _Acquired:
    info: ResponseInfo
    raw: bytes = field(default=b"", repr=False)
    session: Session | None = field(default=None, repr=False)
    doc_type: str | None = None


def _safe_trace_url(url: str) -> str:
    parsed = safe_url(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")) if parsed else ""


def _hash(raw: bytes) -> str:
    return "sha256:" + sha256(raw).hexdigest()


def _direct_alternate(task: AdapterTask) -> str | None:
    url = task.params.get("url")
    return url if task.kind == "fetch_url" and isinstance(url, str) else None


class Fetcher:
    """The public external acquisition entry point."""

    def __init__(
        self,
        settings: Settings,
        database: sessionmaker[DBSession],
        blobs: BlobStore,
        *,
        registry: Registry | None = None,
        browser: GuardedBrowser | None = None,
        sessions: SessionManager | None = None,
        handoffs: HandoffManager | None = None,
        login_sites: Mapping[str, LoginSite] | None = None,
        adapters: Mapping[str, Callable[[FetchRequest], str | None]] | None = None,
        adapter_tasks: Mapping[str, Callable[[AdapterTask], str | None]] | None = None,
        run_config: RunConfig | None = None,
        emit: EventSink | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._registry = registry if registry is not None else load_registry(settings.registry_path)
        self._settings, self._db, self._blobs = settings, database, blobs
        self._browser, self._sessions, self._handoffs = browser, sessions, handoffs
        self._login_sites = dict(login_sites or {})
        self._adapters = dict(adapters or {})
        self._tasks = dict(adapter_tasks or {})
        self._proxy = run_config.fault_proxy_url if run_config else None
        self._ca = settings.gba_ca_bundle if self._proxy else None
        self._emit, self._sleep, self._now = emit, sleep, now
        limits = {entry.entry_id: entry.rate_limit for entry in self._registry.entries}
        options = {} if jitter is None else {"jitter": jitter}
        self._limiter = RateLimiter(limits, clock=clock, utcnow=now, sleep=sleep, **options)
        self._client = PoliteClient(
            settings,
            self._limiter,
            self._authorize,
            fault_proxy_url=self._proxy,
            transport=transport,
            clock=clock,
        )
        self._detector = BlockDetector(emit)

    def _authorize(self, url: str) -> RegistryEntry:
        parsed = safe_url(url)
        entry = self._registry.match_url(url) if parsed else None
        if (
            entry is None
            or parsed is None
            or not any(
                matches_prefix(url, prefix) and origin(parsed) == origin(urlsplit(prefix))
                for prefix in entry.url_prefixes
            )
        ):
            raise APIError(403, "SG_URL_NOT_IN_REGISTRY", "URL is outside the source registry")
        if not self._terms_allowed(entry):
            raise APIError(403, "SG_TERMS_UNVERIFIED", "Source terms lack human verification")
        return entry

    def _terms_allowed(self, entry: RegistryEntry) -> bool:
        return self._settings.gba_env != "prod" or bool(
            entry.terms_verified and entry.terms_url and entry.terms_checked_on
        )

    def approved_sites(self) -> list[dict[str, object]]:
        """Read-only catalogue for planning. Acquisition still checks each URL."""
        return [
            {
                "site_id": entry.entry_id,
                "access": entry.access,
                "doc_types": list(entry.doc_types),
                "url_prefixes": list(entry.url_prefixes),
            }
            for entry in self._registry.entries
            if "READ" in entry.allowed_action_classes and self._terms_allowed(entry)
        ]

    def _policy(self, entry: RegistryEntry) -> SitePolicy:
        try:
            return SitePolicy.model_validate(
                load_site_metadata(self._settings.registry_path, entry.entry_id)
            )
        except (ValidationError, re.error):
            raise APIError(500, "INTERNAL_ERROR", "Site access policy is invalid") from None

    def fetched_bytes(self, record: SourceRecord) -> bytes:
        raw = self._blobs.get("raw/" + record.doc_id.removeprefix("sha256:"))
        if len(raw) != record.byte_length or _hash(raw) != record.doc_id:
            raise APIError(503, "SOURCE_UNRETRIEVABLE", "Stored capture failed verification")
        return raw

    def site_metadata(self, site_id: str) -> dict[str, object]:
        return load_site_metadata(self._settings.registry_path, site_id)

    def report_adapter_document(
        self,
        source_id: str,
        doc_id: str,
        document_type: str,
        identity: dict[str, object],
    ) -> None:
        """Attach independently reported adapter metadata without changing SourceRecord."""
        with session_scope(self._db) as db:
            repo = SourceRecordsRepository(db)
            row = repo.lock(source_id)
            if row is None or row.record.get("doc_id") != doc_id:
                raise APIError(
                    503, "SOURCE_UNRETRIEVABLE", "Adapter report has no verified capture"
                )
            metadata = repo.gate_data(row)
            report = {"document_type": document_type, "identity": identity}
            conflict = "adapter" in metadata and metadata["adapter"] != report
            if conflict:
                metadata["adapter_conflict"] = True
                row.admitted = False
                row.admission_reason = "SG_IDENTITY_MISMATCH"
            else:
                metadata["adapter"] = report
            row.record = row.record | {"_source_gate": metadata}
        if conflict:
            raise APIError(422, "VALIDATION_ERROR", "Adapter reports conflict")

    def _immutable(self, url: str, entry: RegistryEntry, policy: SitePolicy) -> bool:
        if entry.entry_id == "sec_edgar" and matches_prefix(url, "https://www.sec.gov/Archives/"):
            return True
        return any(matches_prefix(url, prefix) for prefix in policy.immutable_url_prefixes)

    def _stored(self, url: str, entry: RegistryEntry) -> list[SourceRecord]:
        with session_scope(self._db) as db:
            return [
                SourceRecord.model_validate(SourceRecordsRepository.contract_data(row))
                for row in SourceRecordsRepository(db).for_url(url, entry.entry_id)
            ]

    def _cached(
        self,
        request: FetchRequest,
        stored: list[SourceRecord],
        entry: RegistryEntry,
    ) -> SourceRecord | None:
        if entry.requires_login:
            return None
        for record in stored:
            if (
                record.expected_doc_type != request.expected_doc_type
                or record.version_label != request.version_label
                or record.fetch_method != entry.access
                or record.session_id is not None
            ):
                continue
            key = "raw/" + record.doc_id.removeprefix("sha256:")
            if self._blobs.exists(key):
                raw = self._blobs.get(key)
                if len(raw) == record.byte_length and _hash(raw) == record.doc_id:
                    return record
        return None

    async def fetch(self, req: FetchRequest) -> FetchOutcome:
        blocks: list[dict[str, object]] = []
        try:
            entry = self._authorize(req.url)
        except APIError as error:
            return self._finish(req, None, blocks, reason=error.code)
        if not self._permits(req, entry):
            return self._finish(req, entry, blocks)
        policy = self._policy(entry)
        record, switch, cached = await self._try_url(req, entry, policy, blocks)
        if record is not None:
            return self._finish(req, entry, blocks, record=record, cached=cached)
        if switch and req.expected_doc_type is not None:
            seen = {req.url}
            for alternate in policy.alternates.get(req.expected_doc_type, []):
                resolve = self._tasks.get(alternate.entry_id, _direct_alternate)
                url = resolve(alternate.task)
                if url is None or url in seen:
                    continue
                try:
                    candidate = self._authorize(url)
                except APIError:
                    continue
                if candidate.entry_id != alternate.entry_id:
                    continue
                alternate_req = req.model_copy(update={"url": url})
                if not self._permits(alternate_req, candidate):
                    continue
                seen.add(url)
                record, _, cached = await self._try_url(
                    alternate_req, candidate, self._policy(candidate), blocks
                )
                if record is not None:
                    return self._finish(
                        alternate_req, candidate, blocks, record=record, cached=cached
                    )
        return self._finish(req, entry, blocks)

    @staticmethod
    def _permits(request: FetchRequest, entry: RegistryEntry) -> bool:
        hint = "api" if entry.access == "bulk" else entry.access
        if isinstance(request, OperatorFetchRequest) and request.site_id != entry.entry_id:
            return False
        if isinstance(request, AdapterFetchRequest) and (
            request.site_id != entry.entry_id
            or (request.playbook_task is not None and entry.access != "adapter")
        ):
            return False
        if isinstance(request, CitationLookupRequest) and (
            request.url != CitationLookupRequest.model_fields["url"].default
            or entry.entry_id != "courtlistener"
            or entry.access != "api"
        ):
            return False
        return (
            "READ" in entry.allowed_action_classes
            and request.method_hint in ("auto", hint)
            and (
                request.expected_doc_type is None
                or request.expected_doc_type == "pdf"
                or request.expected_doc_type in entry.doc_types
            )
            and urlsplit(request.url).path != "/robots.txt"
        )

    async def _try_url(
        self,
        req: FetchRequest,
        entry: RegistryEntry,
        policy: SitePolicy,
        blocks: list[dict[str, object]],
    ) -> tuple[SourceRecord | None, bool, bool]:
        stored = (
            self._stored(req.url, entry)
            if not isinstance(req, CitationLookupRequest)
            and self._immutable(req.url, entry, policy)
            else []
        )
        cached = self._cached(req, stored, entry)
        if cached is not None:
            return cached, False, True
        budget = _AttemptBudget()
        while True:
            budget.total += 1
            try:
                result = await self._acquire(req, entry)
            except APIError as error:
                if error.code not in {
                    "SG_URL_NOT_IN_REGISTRY",
                    "SG_TERMS_UNVERIFIED",
                    "SOURCE_UNRETRIEVABLE",
                }:
                    raise
                return None, False, False

            info = result.info
            baseline = stored
            if info.final_url != req.url and self._immutable(info.final_url, entry, policy):
                baseline = self._stored(info.final_url, entry)
            site = self._login_sites.get(entry.entry_id)
            ready = self._handoffs is not None and (
                result.session is not None
                or (self._browser is not None and self._sessions is not None)
            )
            can_login = bool(
                ready and entry.requires_login and site and self._sessions and self._browser
            )
            can_captcha = bool(ready)

            action = partial(budget.action, login=can_login, captcha=can_captcha)

            event = self._detector.detect(
                info,
                login_url_patterns=policy.login_url_patterns
                or (site.login_url_patterns if site else ()),
                minimum_content_length=policy.minimum_content_length,
                immutable_hash=baseline[-1].doc_id if baseline else None,
                run_id=req.run_id,
                attempt=budget.total,
                action_taken=action,
            )
            if event is None:
                if self._valid_document(req, entry, result):
                    if result.session is not None and self._sessions is not None:
                        await self._sessions.save(result.session)
                    return self._persist(req, entry, result), False, False
                return None, False, False

            blocks.append({"kind": event.kind, "evidence": event.evidence})
            chosen = action(event)
            if event.kind == "http_429":
                budget.transient += 1
                self._limiter.record_response(
                    entry.entry_id,
                    429,
                    retry_after=info.headers.get("retry-after"),
                    attempt=budget.transient,
                )
            elif event.kind == "http_403" or info.status == 403:
                self._limiter.record_response(entry.entry_id, 403)
            if chosen == "switch_source":
                return None, True, False
            if chosen == "mark_unretrievable":
                return None, False, False
            if chosen == "handoff":
                if not await self._recover(req, entry, result, event, site):
                    return None, False, False
                if event.kind == "captcha":
                    budget.captcha += 1
                else:
                    budget.login += 1
            elif event.kind in _RETRY_SWITCH:
                budget.switch += 1
                await self._sleep(5)
            elif event.kind == "timeout":
                budget.transient += 1
                await self._sleep(self._limiter.backoff(budget.transient))
            # The 429 cooldown is enforced by the next acquisition's limiter slot.

    def _permit_browser(
        self, entry: RegistryEntry, granted: set[str] | None = None
    ) -> BrowserPermit:
        @asynccontextmanager
        async def permit(
            url: str,
            method: str = "GET",
            *,
            headers: dict[str, str] | None = None,
        ) -> AsyncIterator[httpx.Response | None]:
            if method not in {"GET", "HEAD"}:
                raise APIError(403, "SOURCE_UNRETRIEVABLE", "Browser request is not read-only")
            self._client._entry_for_hop(url, entry)
            decision = classify(
                BrowserAction(action_type="navigate", page_url=url, site_id=entry.entry_id),
                entry,
                None,
                None,
            )
            if not decision.allowed:
                raise APIError(403, "SOURCE_UNRETRIEVABLE", "Browser navigation denied")
            if not await self._client._allowed_by_robots(url, entry):
                raise _RobotsDenied
            async with self._limiter.slot(entry.entry_id):
                if granted is not None:
                    granted.add(url)
                yield (
                    None
                    if headers is None
                    else await self._client._exchange(
                        url,
                        entry,
                        method=method,
                        request_headers={
                            name: value
                            for name, value in headers.items()
                            if name in {"accept", "accept-language", "cookie"}
                        },
                    )
                )

        return permit

    async def _acquire(self, request: FetchRequest, entry: RegistryEntry) -> _Acquired:
        if self._emit:

            def observe(payload: dict[str, object]) -> None:
                if self._emit:
                    self._emit(
                        request.run_id,
                        "state_change",
                        {
                            **payload,
                            "url": _safe_trace_url(str(payload["url"])),
                            "from": "prepared" if payload["phase"] == "started" else "requesting",
                            "to": payload["phase"],
                            "operation": "network_request",
                            "registry_entry_id": entry.entry_id,
                        },
                    )

            self._client.observe = observe
        url = request.url
        empty = ResponseInfo(None, {}, url, url, None, "", DOMSignals(), False)
        requested_document = urlsplit(url).path.lower().endswith(
            ".pdf"
        ) or request.expected_doc_type in _FILING_TYPES | {"pdf"}
        try:
            if entry.access == "operator" or (
                isinstance(request, AdapterFetchRequest) and request.playbook_task is not None
            ):
                if self._browser is None:
                    raise APIError(
                        503, "SOURCE_UNRETRIEVABLE", "Guarded browser runtime unavailable"
                    )
                granted: set[str] = set()
                capture = await self._browser.acquire(
                    request,
                    self._permit_browser(entry, granted),
                    proxy_url=self._proxy,
                    ca_bundle=self._ca,
                )
                self._client._entry_for_hop(capture.response.final_url, entry)
                if capture.response.final_url not in granted and not (
                    capture.response.timed_out or capture.response.robots_disallowed
                ):
                    raise APIError(
                        403, "SOURCE_UNRETRIEVABLE", "Browser capture lacks a request permit"
                    )
                if capture.session is not None and (
                    capture.session.site_id != entry.entry_id
                    or capture.session.run_id != request.run_id
                    or capture.session.paused
                ):
                    raise APIError(
                        503, "SOURCE_UNRETRIEVABLE", "Browser session does not match the request"
                    )
                info = replace(
                    capture.response,
                    requested_url=url,
                    requested_document=requested_document
                    or urlsplit(capture.response.final_url).path.lower().endswith(".pdf"),
                    body_hash=_hash(capture.raw),
                    content_length=len(capture.raw),
                    headers={key.lower(): value for key, value in capture.response.headers.items()},
                )
                return _Acquired(info, capture.raw, capture.session, capture.doc_type)
            target = url
            if entry.access == "adapter" and not isinstance(request, AdapterFetchRequest):
                adapter = self._adapters.get(entry.entry_id)
                adapted = adapter(request) if adapter else None
                if adapted is None:
                    raise APIError(503, "SOURCE_UNRETRIEVABLE", "Registered adapter unavailable")
                target = adapted
            if isinstance(request, CitationLookupRequest):
                response = await self._client._citation_lookup(target, entry, request.citation)
            else:
                response = await self._client._get(target, entry)
        except (httpx.TimeoutException, TimeoutError):
            return _Acquired(replace(empty, timed_out=True))
        except (httpx.RequestError, _RobotsDenied) as error:
            if isinstance(error, _RobotsDenied):
                return _Acquired(replace(empty, robots_disallowed=True))
            raise APIError(503, "SOURCE_UNRETRIEVABLE", "HTTP acquisition failed") from None
        except _RobotsThrottle as throttle:
            headers = {"retry-after": throttle.retry_after} if throttle.retry_after else {}
            return _Acquired(replace(empty, status=429, headers=headers))
        if response is None:
            return _Acquired(replace(empty, robots_disallowed=True))
        media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        raw = response.content
        sample = raw[:8192].decode("utf-8", "replace") if media != "application/pdf" else ""
        return _Acquired(
            ResponseInfo(
                response.status_code,
                response.headers,
                str(response.url),
                url,
                media,
                sample,
                html_signals(sample, media),
                False,
                requested_document=requested_document or response.url.path.lower().endswith(".pdf"),
                content_length=len(raw),
                body_hash=_hash(raw),
            ),
            raw,
        )

    async def _recover(
        self,
        req: FetchRequest,
        entry: RegistryEntry,
        result: _Acquired,
        event: BlockEvent,
        site: LoginSite | None,
    ) -> bool:
        session, handoffs = result.session, self._handoffs
        assert handoffs is not None
        try:
            if session is None:
                assert self._sessions is not None and self._browser is not None
                session = await self._sessions.get_or_create(entry.entry_id, req.run_id)
                if event.kind == "captcha":
                    # Display the interstitial in the human's browser. The retry still
                    # uses the selected HTTP route and never exports browser cookies.
                    granted: set[str] = set()
                    viewer = self._browser.login_browser(
                        self._permit_browser(entry, granted),
                        proxy_url=self._proxy,
                        ca_bundle=self._ca,
                    )
                    observation = await viewer.prepare(session, result.info.final_url)
                    if observation.final_url not in granted:
                        return False
                    challenge = self._detector.detect(observation)
                    if challenge is None or challenge.kind != "captcha":
                        return False
        except (APIError, _RobotsDenied, _RobotsThrottle, httpx.RequestError, TimeoutError):
            return False
        if event.kind == "captcha":
            handoff = handoffs.request(
                req.run_id, entry.entry_id, "captcha", "Complete the CAPTCHA in the browser viewer."
            )
            session.paused = True
            resolved = False
            try:
                outcome = await handoffs.wait(handoff.handoff_id)
                resolved = outcome.status == "resolved"
            except asyncio.CancelledError:
                await handoffs.wait(handoff.handoff_id)
                raise
            finally:
                session.paused = False
                if not resolved and self._sessions is not None:
                    await self._sessions.mark_expired(session, "SOURCE_UNRETRIEVABLE")
            return resolved
        assert site is not None and self._sessions is not None and self._browser is not None
        try:
            self._client._entry_for_hop(site.form.login_url, entry)
            browser = self._browser.login_browser(
                self._permit_browser(entry), proxy_url=self._proxy, ca_bundle=self._ca
            )
            await LoginFlow(browser, self._detector, handoffs, self._sessions).run(session, site)
        except (APIError, _RobotsDenied, _RobotsThrottle, httpx.RequestError, TimeoutError):
            return False
        return True

    @staticmethod
    def _valid_document(req: FetchRequest, entry: RegistryEntry, result: _Acquired) -> bool:
        info = result.info
        if info.status is None or info.status != 200 or not result.raw:
            return False
        media = (info.media_type or "").split(";", 1)[0].strip().lower()
        if media not in (_API_MEDIA if entry.access in {"api", "bulk"} else _DOCUMENT_MEDIA):
            return False
        pdf = req.expected_doc_type == "pdf" or any(
            urlsplit(url).path.lower().endswith(".pdf") for url in (req.url, info.final_url)
        )
        if pdf or media == "application/pdf":
            if media != "application/pdf" or not result.raw.startswith(b"%PDF-"):
                return False
        if result.doc_type is not None:
            if result.doc_type not in entry.doc_types or req.expected_doc_type not in (
                None,
                result.doc_type,
                "pdf",
            ):
                return False
        return True

    def _persist(self, req: FetchRequest, entry: RegistryEntry, result: _Acquired) -> SourceRecord:
        info = result.info
        digest = info.body_hash
        assert digest is not None and info.media_type is not None
        record = SourceRecord(
            source_id=f"src_{new_ulid()}",
            url=info.final_url,
            registry_entry_id=entry.entry_id,
            retrieved_at=self._now(),
            session_id=result.session.session_id if result.session else None,
            doc_id=digest,
            media_type=info.media_type.split(";", 1)[0].strip().lower(),
            byte_length=len(result.raw),
            http_status=info.status,
            fetch_method=entry.access,
            retrieved_by=f"track_a.{entry.access}.{entry.entry_id}",
            expected_doc_type=req.expected_doc_type,
            version_label=req.version_label,
        )
        self._blobs.put("raw/" + digest.removeprefix("sha256:"), result.raw, record.media_type)
        with session_scope(self._db) as db:
            SourceRecordsRepository(db).add(
                SourceRow(
                    source_id=record.source_id,
                    run_id=req.run_id,
                    record=record.model_dump(mode="json"),
                    admitted=False,
                )
            )
        return record

    def _finish(
        self,
        req: FetchRequest,
        entry: RegistryEntry | None,
        blocks: list[dict[str, object]],
        *,
        record: SourceRecord | None = None,
        cached: bool = False,
        reason: str = "SOURCE_UNRETRIEVABLE",
    ) -> FetchOutcome:
        status: Literal["ok", "unretrievable"] = "ok" if record else "unretrievable"
        if self._emit is not None:
            self._emit(
                req.run_id,
                "fetch",
                {
                    "url": _safe_trace_url(req.url),
                    "registry_entry_id": entry.entry_id if entry else None,
                    "status": status,
                    "source_id": record.source_id if record else None,
                    "doc_id": record.doc_id if record else None,
                    "session_id": record.session_id if record else None,
                    "reason_code": None if record else reason,
                    "bytes": record.byte_length if record else 0,
                    "from_cache": cached,
                },
            )
            if record is None and reason == "SOURCE_UNRETRIEVABLE":
                self._emit(
                    req.run_id,
                    "abstain",
                    {
                        "item": _safe_trace_url(req.url),
                        "reason_code": reason,
                        "reason_text": "The source could not be retrieved under its access policy.",
                    },
                )
        return FetchOutcome(
            status=status,
            source_record=record,
            blocks=blocks,
            reason_code=None if record else reason,
        )


class _RobotsDenied(Exception):
    """Browser navigation stopped by a preflight robots rule."""
