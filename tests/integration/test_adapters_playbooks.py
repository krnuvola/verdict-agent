"""Synthetic source responses and real guarded Chromium playbook canaries."""

from __future__ import annotations

from collections import deque
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select

from gba_agent.access._registry import load_registry
from gba_agent.access.fetcher import Fetcher
from gba_agent.adapters.base import (
    AcquisitionDenied,
    AdapterContext,
    AdapterRegistry,
    AdapterTask,
    AdapterUnavailable,
    FetchedDocument,
    NotSupported,
)
from gba_agent.adapters.courtlistener import CourtListenerAdapter
from gba_agent.adapters.de_code import DelawareCodeAdapter
from gba_agent.adapters.de_courts import DelawareCourtsAdapter
from gba_agent.adapters.de_legis import DelawareLegislatureAdapter
from gba_agent.adapters.edgar import EdgarAdapter
from gba_agent.api.errors import APIError
from gba_agent.clients.courtlistener_client import CourtListenerClient
from gba_agent.db.models import PlaybookProposal
from gba_agent.db.session import session_scope
from gba_agent.operator.browser import BrowserRuntime
from gba_agent.operator.playbooks import (
    Expectation,
    LocatorSpec,
    Playbook,
    PlaybookStep,
    PlaybookStore,
)
from gba_agent.operator.repair import RepairQueue
from tests.integration.test_browser_operator import Replay, _executable, act
from tests.sitesim.fetcher import PDF, RUN_ID, FetchFixture
from tests.sitesim.fetcher import site as site

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "config" / "registry.yaml"
EDGAR_DOC = "https://www.sec.gov/Archives/edgar/data/1234/000000123426000001/fixture.htm"
COURT_ROOT = "https://courts.delaware.gov"
COURT_DOC = COURT_ROOT + "/opinions/download.aspx?id=7"
CL_ROOT = "https://www.courtlistener.com"
CL_LOOKUP = CL_ROOT + "/api/rest/v4/citation-lookup/"
CL_DOC = CL_ROOT + "/opinion/777/synthetic-case/"
CODE_DOC = "https://delcode.delaware.gov/title8/c001/sc04/index.html"
BILL = "https://legis.delaware.gov/BillDetail/1234"
BILL_TEXT = (
    "https://legis.delaware.gov/json/BillDetail/"
    "GenerateHtmlDocumentSessionLaw?docTypeId=13&sessionLawId=1234&sessionLawName=chp1"
)


class Wire:
    """Closed fixture transport. An unexpected URL never reaches a socket."""

    def __init__(self) -> None:
        self.replies: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []

    def json(self, url: str, data: object) -> None:
        self.replies[url] = {"json": data}

    def html(self, url: str, body: str) -> None:
        self.replies[url] = {"text": body, "headers": {"content-type": "text/html"}}

    def pdf(self, url: str) -> None:
        self.replies[url] = {"content": PDF, "headers": {"content-type": "application/pdf"}}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        if str(request.url) not in self.replies:
            raise AssertionError("An unrecorded source request reached the transport")
        response = self.replies[str(request.url)]
        return httpx.Response(**({"status_code": 200} | response))

    @property
    def urls(self) -> list[str]:
        return [str(item.url) for item in self.requests]


def context(
    site: FetchFixture, wire: Wire, browser: BrowserRuntime | None = None
) -> AdapterContext:
    registry = load_registry(REGISTRY)
    settings = site.settings.model_copy(update={"registry_path": REGISTRY})
    fetcher = Fetcher(
        settings,
        site.database,
        site.blobs,
        registry=registry,
        transport=httpx.MockTransport(wire),
        browser=browser,
        sessions=browser.sessions if browser else None,
        clock=lambda: site.time,
        sleep=site.sleep,
        jitter=lambda _a, _b: 1,
        now=site.now,
        emit=lambda *event: site.events.append(event),
    )
    return AdapterContext(fetcher, RUN_ID)


@pytest.mark.adapter_canary
@pytest.mark.asyncio
async def test_edgar_company_history_and_archive(site: FetchFixture) -> None:
    wire = Wire()
    wire.json(
        "https://www.sec.gov/files/company_tickers.json",
        {
            "0": {"cik_str": 1234, "ticker": "SYN", "title": "Synthetic Issuer Inc."},
        },
    )
    empty: dict[str, list[str]] = {
        key: [] for key in ("accessionNumber", "filingDate", "form", "primaryDocument")
    }
    wire.json(
        "https://data.sec.gov/submissions/CIK0000001234.json",
        {
            "filings": {
                "recent": empty,
                "files": [
                    {
                        "name": "CIK0000001234-submissions-001.json",
                        "filingFrom": "2026-01-01",
                        "filingTo": "2026-02-01",
                    }
                ],
            }
        },
    )
    wire.json(
        "https://data.sec.gov/submissions/CIK0000001234-submissions-001.json",
        {
            "accessionNumber": ["0000001234-26-000001"],
            "filingDate": ["2026-01-15"],
            "form": ["8-K"],
            "primaryDocument": ["fixture.htm"],
        },
    )
    wire.html(EDGAR_DOC, "<article>Synthetic filing</article>")
    ctx = context(site, wire)
    registry = AdapterRegistry.defaults(CourtListenerAdapter(site.database))
    task = AdapterTask(
        kind="find_filing",
        params={
            "company_name": "Synthetic Issuer Inc",
            "form_types": ["8-K"],
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
        },
    )
    assert isinstance(await registry.resolve("sec_edgar", task), EdgarAdapter)
    (document,) = await registry.run("sec_edgar", task, ctx)
    assert document.url == EDGAR_DOC and document.identity == {
        "accession_number": "0000001234-26-000001",
        "cik": "1234",
        "form": "8-K",
        "filed_on": "2026-01-15",
    }
    listed = await registry.run(
        "sec_edgar",
        AdapterTask(
            kind="list_filings",
            params={
                "cik": 1234,
                "limit": 1,
            },
        ),
        ctx,
    )
    assert listed == [document]
    (direct,) = await registry.run(
        "sec_edgar", AdapterTask(kind="fetch_url", params={"url": EDGAR_DOC}), ctx
    )
    assert direct.identity == {"accession_number": "0000001234-26-000001"}
    assert wire.urls.count(EDGAR_DOC) == 1
    assert all(not row.admitted for row in site.sources())


@pytest.mark.adapter_canary
@pytest.mark.asyncio
async def test_edgar_full_text_search(site: FetchFixture) -> None:
    wire = Wire()
    wire.json(
        "https://efts.sec.gov/LATEST/search-index?q=fixture&ciks=0000001234&forms=8-K"
        "&dateRange=custom&startdt=2026-01-01&enddt=2026-01-31",
        {
            "hits": {
                "hits": [
                    {
                        "_id": "0000001234-26-000001:fixture.htm",
                        "_source": {
                            "ciks": ["0000001234"],
                            "file_date": "2026-01-15",
                            "form": "8-K",
                        },
                    }
                ]
            },
        },
    )
    wire.html(EDGAR_DOC, "<article>Synthetic filing</article>")
    docs = await EdgarAdapter().run(
        AdapterTask(
            kind="find_filing",
            params={
                "cik": "1234",
                "query": "fixture",
                "form_types": ["8-K"],
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
            },
        ),
        context(site, wire),
    )
    assert [doc.url for doc in docs] == [EDGAR_DOC]


@pytest.mark.adapter_canary
@pytest.mark.asyncio
async def test_delaware_code_resolves_published_section(site: FetchFixture) -> None:
    wire = Wire()
    for number in range(1, 4):
        wire.html(CODE_DOC.replace("sc04", f"sc{number:02}"), "<h2>Another section</h2>")
    wire.html(CODE_DOC, '<a href="#s144">§ 144</a><h2 id="s144">§ 144. Synthetic heading</h2>')
    (result,) = await DelawareCodeAdapter().run(
        AdapterTask(
            kind="fetch_statute",
            params={"title": 8, "chapter": 1, "section": 144},
        ),
        context(site, wire),
    )
    assert result.identity == {"title": "8", "chapter": "1", "section": "144"}
    assert result.url == CODE_DOC and result.expected_doc_type == "statute"
    assert any(row.record["version_label"].startswith("retrieved ") for row in site.sources())


@pytest.mark.adapter_canary
@pytest.mark.asyncio
async def test_delaware_legislature_follows_published_session_law_link(site: FetchFixture) -> None:
    wire = Wire()
    wire.html(
        BILL,
        '<h2>153rd General Assembly</h2><a href="'
        + urlsplit(BILL_TEXT).path
        + "?"
        + urlsplit(BILL_TEXT).query.replace("&", "&amp;")
        + '">View HTML</a>',
    )
    wire.html(BILL_TEXT, "<article>Synthetic session law</article>")
    adapter = DelawareLegislatureAdapter()
    task = AdapterTask(
        kind="fetch_bill",
        params={
            "bill_id": 1234,
            "general_assembly": 153,
            "version": "session_law",
        },
    )
    (result,) = await adapter.run(task, context(site, wire))
    assert result.url == BILL_TEXT and result.expected_doc_type == "session_law"
    with pytest.raises(AdapterUnavailable):
        await adapter.run(
            task.model_copy(
                update={
                    "params": task.params
                    | {
                        "general_assembly": 152,
                    }
                }
            ),
            context(site, wire),
        )


@pytest.mark.adapter_canary
@pytest.mark.asyncio
async def test_courtlistener_search_fetch_and_exact_cached_crosscheck(site: FetchFixture) -> None:
    wire = Wire()
    wire.json(
        CL_ROOT + "/api/rest/v4/search/?q=synthetic&type=o&court=delch",
        {
            "results": [
                {
                    "absolute_url": "/opinion/777/synthetic-case/",
                    "caseName": "Harbor Co. v. Bay Co.",
                    "dateFiled": "2025-02-03",
                    "citation": ["99 A.3d 123"],
                    "cluster_id": 777,
                }
            ],
            "next": None,
        },
    )
    wire.html(CL_DOC, "<article>Synthetic opinion</article>")
    wire.json(CL_ROOT + "/api/rest/v4/opinions/12/", {"plain_text": "Synthetic opinion"})
    wire.json(
        CL_LOOKUP,
        [
            {
                "citation": "99 A.3d 123",
                "status": 200,
                "clusters": [
                    {
                        "case_name": "Harbor Co. v. Bay Co.",
                        "date_filed": "2025-02-03",
                        "absolute_url": "/opinion/777/synthetic-case/",
                    }
                ],
            }
        ],
    )
    ctx = context(site, wire)
    adapter = CourtListenerAdapter(site.database)
    (doc,) = await adapter.run(
        AdapterTask(
            kind="search_opinions",
            params={
                "query": "synthetic",
                "court": "delch",
            },
        ),
        ctx,
    )
    assert doc.identity and doc.identity["cluster_id"] == 777
    (fetched,) = await adapter.run(
        AdapterTask(kind="fetch_opinion", params={"opinion_id": 12}), ctx
    )
    assert fetched.json_value()["plain_text"] == "Synthetic opinion"
    client = CourtListenerClient(ctx, site.database, now=site.now)
    exact = await client.crosscheck_citation("99 A.3d 123", "HARBOR CO VS BAY CO", date(2025, 2, 3))
    assert exact.found and exact.url == CL_DOC
    assert (
        await client.crosscheck_citation("99 A 3d 123", "Harbor Co. v. Bay Co.", date(2025, 2, 3))
        == exact
    )
    assert wire.urls.count(CL_LOOKUP) == 1
    site.time += 31 * 86400
    assert (
        await client.crosscheck_citation("99 A.3d 123", "Harbor Co v Bay Co", date(2025, 2, 3))
        == exact
    )
    mismatch = await client.crosscheck_citation(
        "99 A.3d 123", "Harbor Co v Bay Co", date(2025, 2, 4)
    )
    assert not mismatch.found
    requests = [request for request in wire.requests if request.method != "GET"]
    assert len(requests) == 3
    assert all(str(request.url) == CL_LOOKUP for request in requests)
    assert all(
        parse_qs(request.content.decode()) == {"text": ["99 A.3d 123"]} for request in requests
    )


@pytest.mark.asyncio
async def test_citation_lookup_does_not_follow_redirect_or_cache_failure(
    site: FetchFixture,
) -> None:
    wire = Wire()
    wire.replies[CL_LOOKUP] = {"status_code": 307, "headers": {"location": CL_ROOT + "/unexpected"}}
    client = CourtListenerClient(context(site, wire), site.database)
    with pytest.raises(AcquisitionDenied):
        await client.crosscheck_citation("99 A.3d 123", "Harbor Co v Bay Co", date(2025, 2, 3))
    assert wire.urls == [CL_ROOT + "/robots.txt", CL_LOOKUP]
    assert not site.sources()


class Tier:
    site_id, version = "site", "1"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def supports(self, task: AdapterTask) -> bool:
        return task.kind == "fetch_url"

    async def run(self, task: AdapterTask, ctx: AdapterContext) -> list[FetchedDocument]:
        self.calls += 1
        if self.error:
            raise self.error
        return []


@pytest.mark.asyncio
async def test_registry_falls_back_on_nonblock_only(site: FetchFixture) -> None:
    official = Tier(NotSupported())
    scripted = Tier(AdapterUnavailable("Schema drift"))
    operator = Tier()
    registry = AdapterRegistry(official=[official], scripted=[scripted], operator=[operator])
    task = AdapterTask(kind="fetch_url", params={})
    ctx = context(site, Wire())
    assert await registry.resolve("site", task) is official
    assert await registry.run("site", task, ctx) == []
    assert [official.calls, scripted.calls, operator.calls] == [1, 1, 1]
    official.error = AcquisitionDenied("SOURCE_UNRETRIEVABLE")
    with pytest.raises(AcquisitionDenied):
        await registry.run("site", task, ctx)
    assert [official.calls, scripted.calls, operator.calls] == [2, 1, 1]
    with pytest.raises(NotSupported):
        await registry.resolve("unknown", task)


def _courts(wire: Wire, *, method: str = "get") -> None:
    wire.html(
        COURT_ROOT + "/opinions/",
        f"""
        <form action="/opinions/" method="{method}">
          <input name="q" placeholder="Search for..."><button>Search</button>
        </form><table><tr><td>Initial listing</td></tr></table>
    """,
    )
    wire.html(
        COURT_ROOT + "/opinions/?q=Harbor",
        """
        <table><tr><td><a href="/opinions/download.aspx?id=7">Harbor Co. v. Bay Co.</a></td>
        <td>02/03/2025</td><td>Fixture Docket</td><td>Court of Chancery</td></tr></table>
    """,
    )
    wire.pdf(COURT_DOC)


@pytest.mark.adapter_canary
@pytest.mark.asyncio
async def test_courts_search_uses_approved_chromium_playbook(site: FetchFixture) -> None:
    wire = Wire()
    _courts(wire)
    settings = site.settings.model_copy(
        update={
            "registry_path": REGISTRY,
            "playbook_dir": ROOT / "playbooks",
        }
    )
    async with BrowserRuntime.launch(
        settings,
        site.database,
        load_registry(REGISTRY),
        executable_path=_executable(),
    ) as browser:
        (result,) = await DelawareCourtsAdapter().run(
            AdapterTask(
                kind="search_opinions",
                params={"query": "Harbor"},
            ),
            context(site, wire, browser),
        )
    assert result.url == COURT_DOC and result.raw == PDF
    assert result.identity and result.identity["decided_on"] == "2025-02-03"
    assert wire.urls == [
        COURT_ROOT + "/robots.txt",
        COURT_ROOT + "/opinions/",
        COURT_ROOT + "/opinions/?q=Harbor",
        COURT_DOC,
    ]


@pytest.mark.asyncio
async def test_courts_playbook_cannot_submit_post(site: FetchFixture) -> None:
    wire = Wire()
    _courts(wire, method="post")
    settings = site.settings.model_copy(
        update={
            "registry_path": REGISTRY,
            "playbook_dir": ROOT / "playbooks",
        }
    )
    replay = Replay(deque())
    async with BrowserRuntime.launch(
        settings,
        site.database,
        load_registry(REGISTRY),
        executable_path=_executable(),
        llm=replay,
    ) as browser:
        with pytest.raises(AcquisitionDenied):
            await DelawareCourtsAdapter().run(
                AdapterTask(
                    kind="search_opinions",
                    params={"query": "Harbor"},
                ),
                context(site, wire, browser),
            )
    assert all(request.method == "GET" for request in wire.requests)
    assert wire.urls == [COURT_ROOT + "/robots.txt", COURT_ROOT + "/opinions/"]
    assert not replay.calls


@pytest.mark.asyncio
async def test_successful_step_repair_requires_human_release(
    site: FetchFixture, tmp_path: Path
) -> None:
    wire = Wire()
    _courts(wire)
    playbooks = PlaybookStore(tmp_path / "playbooks")
    base = PlaybookStore(ROOT / "playbooks").latest("de_courts", "search_opinions")
    broken = base.steps[0].model_copy(update={"locator": LocatorSpec(css="#retired-search")})
    original = base.model_copy(update={"steps": (broken, base.steps[1])})
    playbooks.release(original)
    settings = site.settings.model_copy(
        update={"registry_path": REGISTRY, "playbook_dir": playbooks.root}
    )
    replay = Replay(deque([act("type", 1, "Harbor")]))
    async with BrowserRuntime.launch(
        settings,
        site.database,
        load_registry(REGISTRY),
        executable_path=_executable(),
        llm=replay,
    ) as browser:
        (result,) = await DelawareCourtsAdapter().run(
            AdapterTask(
                kind="search_opinions",
                params={"query": "Harbor"},
            ),
            context(site, wire, browser),
        )
    assert result.url == COURT_DOC
    assert playbooks.latest("de_courts", "search_opinions") == original
    with session_scope(site.database) as db:
        proposal = db.scalars(select(PlaybookProposal)).one()
        assert proposal.status == "pending" and proposal.diff[
            "evidence_screenshot_hash"
        ].startswith("sha256:")
        proposal_id = proposal.id
    queue = RepairQueue(site.database, playbooks)
    released = queue.approve(proposal_id, "fixture reviewer")
    assert released.version == 2 and released.steps[0].locator != broken.locator
    assert released.steps[1] == original.steps[1]
    assert playbooks.load("de_courts", "search_opinions", 1) == original
    assert playbooks.latest("de_courts", "search_opinions") == released
    assert queue.approve(proposal_id, "fixture reviewer") == released


def test_playbook_validation_and_immutable_release(tmp_path: Path) -> None:
    store = PlaybookStore(tmp_path)
    step = PlaybookStep(
        id="navigate",
        action="navigate",
        value_template="{url}",
        expect=Expectation(url_pattern=r"/opinions/$"),
        timeout_s=3,
    )
    playbook = Playbook(site_id="de_courts", task="fetch_opinion", version=1, steps=(step,))
    store.release(playbook)
    store.release(playbook)
    with pytest.raises(APIError, match="immutable"):
        store.release(
            playbook.model_copy(update={"steps": (step.model_copy(update={"timeout_s": 4}),)})
        )
    with pytest.raises(APIError, match="parameter"):
        step.render({})
    with pytest.raises(ValueError, match="plain named"):
        PlaybookStep(
            id="navigate",
            action="navigate",
            value_template="{url.__class__}",
            expect=step.expect,
            timeout_s=3,
        )
    with pytest.raises(ValueError, match="exactly one"):
        LocatorSpec(role="button", label="Search")


@pytest.mark.asyncio
async def test_code_follows_chapter_navigation_and_can_take_a_subchapter(
    site: FetchFixture,
) -> None:
    wire = Wire()
    chapter = "https://delcode.delaware.gov/title12/c003/"
    section = chapter + "sc02/index.html"
    wire.html(chapter, '<a href="sc02/index.html">Public subchapter</a>')
    wire.html(section, "<h2>§ 312. Synthetic text</h2>")
    adapter = DelawareCodeAdapter()
    base: dict[str, object] = {"title": 12, "chapter": 3, "section": 312}
    (result,) = await adapter.run(
        AdapterTask(kind="fetch_statute", params=base), context(site, wire)
    )
    assert result.url == section
    (result,) = await adapter.run(
        AdapterTask(
            kind="fetch_statute",
            params=base | {"subchapter": 2},
        ),
        context(site, wire),
    )
    assert result.url == section
    (result,) = await adapter.run(
        AdapterTask(
            kind="fetch_statute",
            params=base | {"url": section},
        ),
        context(site, wire),
    )
    assert result.url == section
    with pytest.raises(AdapterUnavailable, match="not found"):
        await adapter.run(
            AdapterTask(
                kind="fetch_statute",
                params=base | {"section": 313},
            ),
            context(site, wire),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("title", "../8"),
        ("chapter", "0"),
        ("section", "§"),
        ("url", 12),
        ("subchapter", "../2"),
    ],
)
async def test_code_rejects_unsafe_parameters(site: FetchFixture, key: str, value: object) -> None:
    wire = Wire()
    with pytest.raises(APIError) as error:
        await DelawareCodeAdapter().run(
            AdapterTask(
                kind="fetch_statute",
                params={"title": 8, "chapter": 1, "section": 144, key: value},
            ),
            context(site, wire),
        )
    assert error.value.code == "VALIDATION_ERROR" and wire.urls == []


@pytest.mark.asyncio
async def test_courtlistener_crosscheck_task_and_token_scoping(site: FetchFixture) -> None:
    from pydantic import SecretStr

    wire = Wire()
    site.settings = site.settings.model_copy(
        update={"courtlistener_token": SecretStr("fixture-token")}
    )
    wire.json(
        CL_LOOKUP,
        [
            {
                "citation": "99 A.3d 123",
                "status": 200,
                "clusters": [
                    {
                        "case_name": "Harbor L.L.C. v. Bay Co.",
                        "date_filed": "2025-02-03",
                        "absolute_url": "/opinion/777/synthetic-case/",
                    }
                ],
            }
        ],
    )
    wire.html(CL_DOC, "<article>Synthetic public opinion</article>")
    adapter = CourtListenerAdapter(site.database)
    ctx = context(site, wire)
    assert await adapter.supports(AdapterTask(kind="crosscheck_citation", params={}))
    (document,) = await adapter.run(
        AdapterTask(
            kind="crosscheck_citation",
            params={
                "citation_string": "99 A.3d 123",
                "case_name": "Harbor LLC vs Bay Co",
                "decided_on": "2025-02-03",
            },
        ),
        ctx,
    )
    assert document.identity and document.identity["found"] is True
    (opinion,) = await adapter.run(AdapterTask(kind="fetch_opinion", params={"url": CL_DOC}), ctx)
    assert opinion.url == CL_DOC
    assert [request.headers.get("authorization") for request in wire.requests] == [
        None,
        "Token fixture-token",
        None,
    ]
    assert "fixture-token" not in repr(site.events)
    with pytest.raises(NotSupported):
        await adapter.run(AdapterTask(kind="fetch_bill", params={}), ctx)

