"""Delaware Courts opinion search and public opinion downloads."""

from __future__ import annotations

from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

from gba_agent.adapters.base import (
    AdapterContext,
    AdapterTask,
    FetchedDocument,
    NotSupported,
    invalid,
    limit,
    string_parameter,
)

_OPINIONS = "https://courts.delaware.gov/opinions/"


def _opinion_url(url: str) -> bool:
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    return (
        parts.path.casefold() == "/opinions/download.aspx"
        and len(ids := query.get("id", query.get("ID", []))) == 1
        and ids[0].isascii()
        and ids[0].isdecimal()
    )


class _Rows(HTMLParser):
    def __init__(self, raw: bytes) -> None:
        super().__init__()
        self.rows: list[tuple[str, list[str]]] = []
        self.cells: list[str] | None = None
        self.href: str | None = None
        self.cell: list[str] | None = None
        self.feed(raw.decode("utf-8", "replace"))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.cells, self.href = [], None
        if tag == "td" and self.cells is not None:
            self.cell = []
        if tag == "a" and self.cell is not None:
            href = dict(attrs).get("href")
            if href and _opinion_url(urljoin(_OPINIONS, href)):
                self.href = href

    def handle_data(self, data: str) -> None:
        if self.cell is not None and data.strip():
            self.cell.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self.cell is not None and self.cells is not None:
            self.cells.append(" ".join(self.cell))
            self.cell = None
        if tag == "tr":
            if self.href and self.cells and len(self.cells) >= 4:
                self.rows.append((self.href, self.cells))
            self.cells, self.cell, self.href = None, None, None


class DelawareCourtsAdapter:
    site_id = "de_courts"
    version = "1"

    async def supports(self, task: AdapterTask) -> bool:
        return task.kind in {"search_opinions", "fetch_opinion"}

    async def run(self, task: AdapterTask, ctx: AdapterContext) -> list[FetchedDocument]:
        if task.kind == "fetch_opinion":
            return [await self._opinion(ctx, string_parameter(task, "url"))]
        if task.kind != "search_opinions":
            raise NotSupported(task.kind)
        query = string_parameter(task, "query")
        maximum = limit(task)
        page = await ctx.fetch(
            self.site_id,
            _OPINIONS,
            playbook_task="search_opinions",
            playbook_values={"query": query},
        )
        result = []
        for href, cells in _Rows(page.raw).rows:
            url = urljoin(page.url, href)
            case, displayed_date, docket, court = cells[:4]
            try:
                decided = datetime.strptime(displayed_date, "%m/%d/%Y").date().isoformat()
            except ValueError:
                continue
            if query.casefold() not in " ".join(cells).casefold():
                continue
            document = await self._opinion(ctx, url)
            result.append(
                document.model_copy(
                    update={
                        "identity": {
                            "case_name": case,
                            "decided_on": decided,
                            "docket_number": docket,
                            "court": court,
                        },
                    }
                )
            )
            if len(result) >= maximum:
                break
        return result

    async def _opinion(self, ctx: AdapterContext, url: str) -> FetchedDocument:
        if not _opinion_url(url):
            raise invalid("Expected a Delaware Courts public opinion download URL")
        return await ctx.fetch(self.site_id, url, expected_doc_type="opinion")
