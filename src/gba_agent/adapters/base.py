"""Site adapter contracts and ordered, block-aware resolution."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from html.parser import HTMLParser
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from gba_agent.access.fetcher import (
    AdapterFetchRequest,
    CitationLookupRequest,
    Fetcher,
)
from gba_agent.access.fetcher import (
    AdapterTask as AdapterTask,
)
from gba_agent.api.errors import APIError


class NotSupported(Exception):
    """This method does not implement the requested capability."""


class AdapterUnavailable(APIError):
    """The source responded but its public data did not match the adapter contract."""

    def __init__(self, message: str) -> None:
        super().__init__(503, "UPSTREAM_UNAVAILABLE", message)


class AcquisitionDenied(APIError):
    """Fetcher exhausted its policy. A lower acquisition method cannot take over."""

    def __init__(self, reason: str) -> None:
        super().__init__(503, reason, "The approved acquisition path could not retrieve the source")


class FetchedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    url: str = Field(repr=False)
    raw: bytes = Field(repr=False)
    media_type: str
    http_status: int
    expected_doc_type: str | None = None
    identity: dict[str, Any] | None = None
    _source_id: str | None = PrivateAttr(default=None)

    @property
    def source_id(self) -> str | None:
        return self._source_id

    def json_value(self) -> Any:
        if self.media_type != "application/json":
            raise AdapterUnavailable("The endpoint did not return JSON")
        try:
            return json.loads(self.raw)
        except (UnicodeError, ValueError):
            raise AdapterUnavailable("The endpoint returned malformed JSON") from None


@dataclass(frozen=True)
class AdapterContext:
    fetcher: Fetcher
    run_id: str
    matter_id: str | None = None

    async def fetch(
        self,
        site_id: str,
        url: str,
        *,
        expected_doc_type: str | None = None,
        version_label: str | None = None,
        playbook_task: str | None = None,
        playbook_values: dict[str, str] | None = None,
    ) -> FetchedDocument:
        return await self._capture(
            AdapterFetchRequest(
                url=url,
                site_id=site_id,
                run_id=self.run_id,
                matter_id=self.matter_id,
                expected_doc_type=expected_doc_type,
                version_label=version_label,
                playbook_task=playbook_task,
                playbook_values=playbook_values or {},
            )
        )

    async def citation_lookup(self, citation: str) -> FetchedDocument:
        return await self._capture(
            CitationLookupRequest(
                run_id=self.run_id,
                matter_id=self.matter_id,
                citation=citation,
                expected_doc_type="citation_lookup",
            )
        )

    async def _capture(self, request: AdapterFetchRequest) -> FetchedDocument:
        outcome = await self.fetcher.fetch(request)
        record = outcome.source_record
        if record is None or record.registry_entry_id != request.site_id:
            raise AcquisitionDenied(outcome.reason_code or "SOURCE_UNRETRIEVABLE")
        if record.http_status is None:
            raise AdapterUnavailable("Capture has no verified response status")
        document = FetchedDocument(
            url=record.url,
            raw=self.fetcher.fetched_bytes(record),
            media_type=record.media_type,
            http_status=record.http_status,
            expected_doc_type=record.expected_doc_type,
        )
        document._source_id = record.source_id
        return document

    def report(self, documents: list[FetchedDocument]) -> None:
        for document in documents:
            if document._source_id is not None and document.expected_doc_type is not None:
                identity = document.model_dump(mode="json", include={"identity"})["identity"]
                self.fetcher.report_adapter_document(
                    document._source_id,
                    "sha256:" + sha256(document.raw).hexdigest(),
                    document.expected_doc_type,
                    identity or {},
                )


class SiteAdapter(Protocol):
    site_id: str
    version: str

    async def supports(self, task: AdapterTask) -> bool: ...
    async def run(self, task: AdapterTask, ctx: AdapterContext) -> list[FetchedDocument]: ...


class AdapterRegistry:
    def __init__(
        self,
        *,
        official: Iterable[SiteAdapter] = (),
        scripted: Iterable[SiteAdapter] = (),
        operator: Iterable[SiteAdapter] = (),
    ) -> None:
        self._levels = tuple(tuple(level) for level in (official, scripted, operator))

    async def resolve(self, site_id: str, task: AdapterTask) -> SiteAdapter:
        for level in self._levels:
            for adapter in level:
                if adapter.site_id == site_id and await adapter.supports(task):
                    return adapter
        raise NotSupported(task.kind)

    async def run(
        self,
        site_id: str,
        task: AdapterTask,
        ctx: AdapterContext,
    ) -> list[FetchedDocument]:
        failure: AdapterUnavailable | None = None
        for level in self._levels:
            for adapter in level:
                if adapter.site_id != site_id or not await adapter.supports(task):
                    continue
                try:
                    documents = await adapter.run(task, ctx)
                    ctx.report(documents)
                    return documents
                except NotSupported:
                    continue
                except AdapterUnavailable as error:
                    failure = error
        if failure is not None:
            raise failure
        raise NotSupported(task.kind)

    @classmethod
    def defaults(cls, courtlistener: SiteAdapter) -> AdapterRegistry:
        from gba_agent.adapters.de_code import DelawareCodeAdapter
        from gba_agent.adapters.de_courts import DelawareCourtsAdapter
        from gba_agent.adapters.de_legis import DelawareLegislatureAdapter
        from gba_agent.adapters.edgar import EdgarAdapter

        return cls(
            official=[EdgarAdapter(), courtlistener],
            scripted=[DelawareCodeAdapter(), DelawareLegislatureAdapter(), DelawareCourtsAdapter()],
        )


class Links(HTMLParser):
    """Extract published hrefs and text from untrusted HTML without executing it."""

    def __init__(self, raw: bytes) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.text: list[str] = []
        self._href: str | None = None
        self._label: list[str] = []
        self.feed(raw.decode("utf-8", "replace"))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._label = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.text.append(data.strip())
            if self._href:
                self._label.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join(self._label)))
            self._href = None


def invalid(message: str) -> APIError:
    return APIError(422, "VALIDATION_ERROR", message)


def iso_date(value: object) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise invalid("Expected an ISO calendar date")


def string_parameter(task: AdapterTask, key: str) -> str:
    value = task.params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise invalid(f"{key} must be non-empty text")
    return value.strip()


def limit(task: AdapterTask, default: int = 5) -> int:
    value = task.params.get("limit", default)
    if type(value) is not int or not 1 <= value <= 50:
        raise invalid("limit must be between 1 and 50")
    return value
