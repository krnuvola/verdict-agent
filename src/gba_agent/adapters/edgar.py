"""SEC EDGAR submissions, full-text search and archive adapter."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from gba_agent.adapters.base import (
    AdapterContext,
    AdapterTask,
    AdapterUnavailable,
    FetchedDocument,
    NotSupported,
    invalid,
    iso_date,
    limit,
    string_parameter,
)

_SUBMISSIONS = "https://data.sec.gov/submissions/"
_TICKERS = "https://www.sec.gov/files/company_tickers.json"
_SEARCH = "https://efts.sec.gov/LATEST/search-index"
_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/"
_ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")
_PRIMARY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_FIELDS = ("accessionNumber", "filingDate", "form", "primaryDocument")


def _name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _cik(value: object) -> int:
    if type(value) is int or (isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value)):
        result = int(value)
        if 0 < result <= 9_999_999_999:
            return result
    raise invalid("CIK must be a positive identifier of at most ten digits")


def _rows(payload: object) -> Iterator[tuple[str, str, str, str]]:
    if not isinstance(payload, dict) or any(
        not isinstance(payload.get(key), list) for key in _FIELDS
    ):
        raise AdapterUnavailable("SEC submissions data is missing filing columns")
    columns = [payload[key] for key in _FIELDS]
    if len({len(column) for column in columns}) != 1:
        raise AdapterUnavailable("SEC filing columns have inconsistent lengths")
    for accession, filed, form, primary in zip(*columns, strict=True):
        if not all(isinstance(value, str) for value in (accession, filed, form, primary)):
            raise AdapterUnavailable("SEC filing fields have unexpected types")
        yield accession, filed, form, primary


class EdgarAdapter:
    site_id = "sec_edgar"
    version = "1"

    async def supports(self, task: AdapterTask) -> bool:
        return task.kind in {"find_filing", "list_filings", "fetch_url"}

    async def run(self, task: AdapterTask, ctx: AdapterContext) -> list[FetchedDocument]:
        if not await self.supports(task):
            raise NotSupported(task.kind)
        if task.kind == "fetch_url":
            url = string_parameter(task, "url")
            if not url.startswith(_ARCHIVE):
                raise invalid("EDGAR document must use the public filing archive")
            doc = await ctx.fetch(self.site_id, url)
            path = urlsplit(url).path
            if match := _ACCESSION.search(path):
                doc.identity = {"accession_number": match[0]}
            elif match := re.search(r"/(\d{10})(\d{2})(\d{6})/", path):
                doc.identity = {"accession_number": "-".join(match.groups())}
            return [doc]
        forms = task.params.get("form_types", [])
        if not isinstance(forms, list) or not all(isinstance(form, str) and form for form in forms):
            raise invalid("form_types must be a list of SEC form identifiers")
        start = iso_date(task.params["start_date"]) if "start_date" in task.params else date.min
        end = iso_date(task.params["end_date"]) if "end_date" in task.params else date.max
        if start > end:
            raise invalid("start_date must precede end_date")
        maximum = limit(task, 1 if task.kind == "find_filing" else 5)
        cik = await self._company(task, ctx)
        if "query" in task.params:
            return await self._search(task, ctx, cik, forms, start, end, maximum)
        if cik is None:
            raise invalid("SEC filing lookup requires a CIK or company name")
        return await self._submissions(ctx, cik, forms, start, end, maximum)

    async def _company(self, task: AdapterTask, ctx: AdapterContext) -> int | None:
        if "cik" in task.params:
            return _cik(task.params["cik"])
        if "company_name" not in task.params:
            return None
        company = _name(string_parameter(task, "company_name"))
        payload = (await ctx.fetch(self.site_id, _TICKERS)).json_value()
        if not isinstance(payload, dict):
            raise AdapterUnavailable("SEC company directory has an unexpected format")
        matches = {
            _cik(row.get("cik_str"))
            for row in payload.values()
            if isinstance(row, dict)
            and any(_name(str(row.get(key, ""))) == company for key in ("title", "ticker"))
        }
        if len(matches) != 1:
            raise invalid("Company name did not identify a unique CIK")
        return matches.pop()

    async def _submissions(
        self,
        ctx: AdapterContext,
        cik: int,
        forms: list[str],
        start: date,
        end: date,
        maximum: int,
    ) -> list[FetchedDocument]:
        data = (await ctx.fetch(self.site_id, f"{_SUBMISSIONS}CIK{cik:010}.json")).json_value()
        filings = data.get("filings") if isinstance(data, dict) else None
        if not isinstance(filings, dict) or not isinstance(filings.get("files"), list):
            raise AdapterUnavailable("SEC company response has no filing history")
        result: list[FetchedDocument] = []
        seen: set[str] = set()

        async def collect(payload: object) -> None:
            for accession, filed, form, primary in _rows(payload):
                if len(result) >= maximum:
                    break
                if accession in seen or not primary or (forms and form not in forms):
                    continue
                if start <= iso_date(filed) <= end:
                    seen.add(accession)
                    result.append(await self._filing(ctx, cik, accession, primary, form, filed))

        await collect(filings.get("recent"))
        for archive in filings["files"]:
            if len(result) >= maximum:
                break
            if not isinstance(archive, dict):
                raise AdapterUnavailable("SEC filing history descriptor is malformed")
            name = archive.get("name")
            if not isinstance(name, str) or not re.fullmatch(
                rf"CIK{cik:010}-submissions-\d+\.json", name
            ):
                raise AdapterUnavailable("SEC filing history filename is malformed")
            if (
                iso_date(archive.get("filingTo")) < start
                or iso_date(archive.get("filingFrom")) > end
            ):
                continue
            await collect((await ctx.fetch(self.site_id, _SUBMISSIONS + name)).json_value())
        return result

    async def _search(
        self,
        task: AdapterTask,
        ctx: AdapterContext,
        cik: int | None,
        forms: list[str],
        start: date,
        end: date,
        maximum: int,
    ) -> list[FetchedDocument]:
        params = {"q": string_parameter(task, "query")}
        if cik is not None:
            params["ciks"] = f"{cik:010}"
        if forms:
            params["forms"] = ",".join(forms)
        if start != date.min or end != date.max:
            if start == date.min or end == date.max:
                raise invalid("Full-text search requires both dates for a custom range")
            params.update(dateRange="custom", startdt=start.isoformat(), enddt=end.isoformat())
        payload = (await ctx.fetch(self.site_id, f"{_SEARCH}?{urlencode(params)}")).json_value()
        envelope = payload.get("hits") if isinstance(payload, dict) else None
        hits = envelope.get("hits") if isinstance(envelope, dict) else None
        if not isinstance(hits, list):
            raise AdapterUnavailable("SEC full-text search response has no hit list")
        documents = []
        for hit in hits:
            source = hit.get("_source") if isinstance(hit, dict) else None
            identifier = hit.get("_id") if isinstance(hit, dict) else None
            if not isinstance(source, dict) or not isinstance(identifier, str):
                raise AdapterUnavailable("SEC full-text hit has no filing identity")
            accession, separator, name = identifier.partition(":")
            ciks = source.get("ciks")
            if not separator or not isinstance(ciks, list) or not ciks:
                raise AdapterUnavailable("SEC full-text hit has no archive path")
            owner = _cik(ciks[0])
            if cik is not None and cik not in {_cik(candidate) for candidate in ciks}:
                continue
            form, filed = source.get("form"), source.get("file_date")
            if not isinstance(form, str) or not isinstance(filed, str):
                raise AdapterUnavailable("SEC full-text hit has no form or filing date")
            if (not forms or form in forms) and start <= iso_date(filed) <= end:
                documents.append(await self._filing(ctx, owner, accession, name, form, filed))
            if len(documents) >= maximum:
                break
        return documents

    async def _filing(
        self,
        ctx: AdapterContext,
        cik: int,
        accession: str,
        primary: str,
        form: str,
        filed: str,
    ) -> FetchedDocument:
        if not _ACCESSION.fullmatch(accession) or not _PRIMARY.fullmatch(primary):
            raise AdapterUnavailable("SEC filing archive identity is malformed")
        url = f"{_ARCHIVE}{cik}/{accession.replace('-', '')}/{quote(primary)}"
        document = await ctx.fetch(self.site_id, url)
        identity: dict[str, Any] = {
            "accession_number": accession,
            "cik": str(cik),
            "form": form,
            "filed_on": filed,
        }
        return document.model_copy(update={"identity": identity})
