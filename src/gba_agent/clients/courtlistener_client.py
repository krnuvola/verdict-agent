"""CourtListener v4 operations, routed exclusively through the adapter's Fetcher."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session, sessionmaker

from gba_agent.adapters.base import (
    AcquisitionDenied,
    AdapterContext,
    AdapterUnavailable,
    FetchedDocument,
    invalid,
)
from gba_agent.db.repositories import CrosscheckCacheRepository
from gba_agent.db.session import session_scope

_ROOT = "https://www.courtlistener.com"
_API = _ROOT + "/api/rest/v4/"
_TTL = timedelta(days=30)


class CrosscheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    found: bool
    matched_name: str | None = None
    matched_date: date | None = None
    url: str | None = None


def _normalize(value: str) -> str:
    plain = "".join(
        char for char in value.casefold() if not unicodedata.category(char).startswith("P")
    )
    tokens = re.findall(r"[^\W_]+", plain)
    return " ".join("v" if token in {"v", "vs"} else token for token in tokens)


def _citation_key(citation: str) -> str:
    return "".join(re.findall(r"[^\W_]+", citation.casefold(), flags=re.UNICODE))


def _public_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        url = urljoin(_ROOT, value)
        parsed = urlsplit(url)
    except ValueError:
        return None
    if (
        parsed.scheme == "https"
        and parsed.netloc == "www.courtlistener.com"
        and re.fullmatch(r"/opinion/\d+/(?:[a-z0-9-]+/)?", parsed.path)
        and not parsed.query
        and not parsed.fragment
    ):
        return url
    return None


class CourtListenerClient:
    def __init__(
        self,
        ctx: AdapterContext,
        database: sessionmaker[Session],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._ctx, self._database, self._now = ctx, database, now

    async def search_opinions(
        self,
        query: str,
        *,
        maximum: int,
        court: str | None = None,
    ) -> list[FetchedDocument]:
        params = {"q": query, "type": "o"}
        if court:
            params["court"] = court
        target = _API + "search/?" + urlencode(params)
        documents = []
        seen_pages: set[str] = set()
        seen_opinions: set[str] = set()
        while target and target not in seen_pages and len(seen_pages) < 10:
            seen_pages.add(target)
            payload = (await self._ctx.fetch("courtlistener", target)).json_value()
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list):
                raise AdapterUnavailable("CourtListener search returned no result list")
            for row in results:
                if not isinstance(row, dict):
                    raise AdapterUnavailable("CourtListener search result is malformed")
                url = _public_url(row.get("absolute_url"))
                if url is None or url in seen_opinions:
                    continue
                seen_opinions.add(url)
                document = await self.fetch_opinion(url=url)
                documents.append(
                    document.model_copy(
                        update={
                            "identity": {
                                "case_name": row.get("caseName"),
                                "decided_on": row.get("dateFiled"),
                                "citation": row.get("citation"),
                                "cluster_id": row.get("cluster_id"),
                            },
                        }
                    )
                )
                if len(documents) >= maximum:
                    return documents
            next_page = payload.get("next")
            if not isinstance(next_page, str):
                break
            parsed = urlsplit(next_page)
            if (
                parsed.scheme != "https"
                or parsed.netloc != "www.courtlistener.com"
                or parsed.path != "/api/rest/v4/search/"
            ):
                raise AcquisitionDenied("SG_URL_NOT_IN_REGISTRY")
            target = next_page
        return documents

    async def fetch_opinion(
        self,
        *,
        url: str | None = None,
        opinion_id: int | None = None,
    ) -> FetchedDocument:
        if opinion_id is not None and url is None and type(opinion_id) is int and opinion_id > 0:
            return await self._ctx.fetch(
                "courtlistener",
                f"{_API}opinions/{opinion_id}/",
                expected_doc_type="opinion",
            )
        public = _public_url(url)
        if public is None or opinion_id is not None:
            raise invalid("Expected a CourtListener public opinion URL or positive opinion ID")
        return await self._ctx.fetch("courtlistener", public, expected_doc_type="opinion")

    async def crosscheck_citation(
        self,
        citation_string: str,
        case_name: str,
        decided_on: date,
    ) -> CrosscheckResult:
        key, name = self._keys(citation_string, case_name)
        with session_scope(self._database) as db:
            cached = CrosscheckCacheRepository(db).get(key)
            if (
                cached
                and cached.checked_at
                and self._now() - cached.checked_at.replace(tzinfo=UTC) < _TTL
            ):
                entry = cached.result
                if (
                    entry.get("case_name") == name
                    and entry.get("decided_on") == decided_on.isoformat()
                ):
                    return CrosscheckResult.model_validate(entry["result"])
        _, result = await self.citation_document(citation_string, case_name, decided_on)
        return result

    async def citation_document(
        self,
        citation: str,
        case_name: str,
        decided_on: date,
    ) -> tuple[FetchedDocument, CrosscheckResult]:
        key, name = self._keys(citation, case_name)
        document = await self._ctx.citation_lookup(citation)
        result = self._match(document.json_value(), key, name, decided_on)
        with session_scope(self._database) as db:
            CrosscheckCacheRepository(db).store(
                key,
                {
                    "case_name": name,
                    "decided_on": decided_on.isoformat(),
                    "result": result.model_dump(mode="json"),
                },
                self._now(),
            )
        return document, result

    @staticmethod
    def _keys(citation: str, case_name: str) -> tuple[str, str]:
        key, name = _citation_key(citation), _normalize(case_name)
        if not key or len(citation) > 300 or not name:
            raise invalid("Citation and case name are required")
        return key, name

    @staticmethod
    def _match(payload: Any, key: str, name: str, decided_on: date) -> CrosscheckResult:
        if not isinstance(payload, list):
            raise AdapterUnavailable("CourtListener citation lookup did not return a list")
        matches: dict[str, str] = {}
        for item in payload:
            if not isinstance(item, dict):
                raise AdapterUnavailable("CourtListener citation lookup result is malformed")
            citation = item.get("citation")
            if not isinstance(citation, str) or _citation_key(citation) != key:
                continue
            status = item.get("status")
            if status == 429:
                raise AcquisitionDenied("SOURCE_UNRETRIEVABLE")
            if status in {400, 404}:
                continue
            clusters = item.get("clusters")
            if status not in {200, 300} or not isinstance(clusters, list):
                raise AdapterUnavailable(
                    "CourtListener citation lookup has incomplete cluster data"
                )
            for cluster in clusters:
                if not isinstance(cluster, dict):
                    raise AdapterUnavailable("CourtListener citation cluster is malformed")
                case = cluster.get("case_name")
                url = _public_url(cluster.get("absolute_url"))
                if (
                    isinstance(case, str)
                    and _normalize(case) == name
                    and cluster.get("date_filed") == decided_on.isoformat()
                    and url is not None
                ):
                    matches[url] = case
        if len(matches) != 1:
            return CrosscheckResult(found=False)
        url, case = next(iter(matches.items()))
        return CrosscheckResult(found=True, matched_name=case, matched_date=decided_on, url=url)
