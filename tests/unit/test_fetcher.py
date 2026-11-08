"""Doc 01 section 6.7 using deterministic, request-recording fake HTTP servers."""

import asyncio
import logging
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from playwright.async_api import Browser

from gba_agent.access._registry import Registry, RegistryEntry, load_registry
from gba_agent.access.block_detector import DOMSignals, ResponseInfo
from gba_agent.access.fetcher import BrowserCapture, BrowserPermit, Fetcher, FetchRequest
from gba_agent.access.handoff import HandoffManager
from gba_agent.access.login import AuthCheck, LoginBrowser, LoginFormSpec, LoginSite
from gba_agent.access.session_manager import Session, SessionManager
from gba_agent.api.contracts_compat import RunConfig
from tests.sitesim.fetcher import (
    ALTERNATE,
    DOCUMENT,
    PDF,
    PRIMARY,
    RUN_ID,
    FetchFixture,
    Reply,
    entry,
)
from tests.sitesim.fetcher import site as site

REQ = FetchRequest(url=DOCUMENT, run_id=RUN_ID, expected_doc_type="opinion")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://other.test/documents/opinion.pdf",
        "https://primary.test.evil.test/documents/opinion.pdf",
        "https://primary.test:443@evil.test/documents/opinion.pdf",
        PRIMARY + "/documents2/opinion.pdf",
        PRIMARY + "/documents/%2E%2E/opinion.pdf",
        PRIMARY + "/documents/%252e%252e/opinion.pdf",
        PRIMARY + "/documents/%2fopinion.pdf",
    ],
)
async def test_registry_rejects_unregistered_or_ambiguous_urls(
    site: FetchFixture, url: str
) -> None:
    result = await site.fetcher().fetch(REQ.model_copy(update={"url": url}))
    assert result.reason_code == "SG_URL_NOT_IN_REGISTRY"
    assert site.http.urls == []
    assert not site.sources() and not site.blobs.data


@pytest.mark.asyncio
async def test_production_requires_human_verified_terms_before_robots(site: FetchFixture) -> None:
    site.settings = site.settings.model_copy(update={"gba_env": "prod"})
    outcome = await site.fetcher().fetch(REQ)
    assert outcome.reason_code == "SG_TERMS_UNVERIFIED"
    assert site.http.urls == []
    site.http.set(DOCUMENT, Reply())
    outcome = await site.fetcher(entry(terms_verified=True)).fetch(REQ)
    assert outcome.status == "ok"


def test_draft_registry_has_no_verified_terms_or_simulator() -> None:
    path = Path(__file__).resolve().parents[2] / "config" / "registry.yaml"
    registry = load_registry(path)
    assert {item.entry_id for item in registry.entries} == {
        "sec_edgar",
        "de_code_online",
        "de_legislature",
        "de_courts",
        "courtlistener",
    }
    assert all(not item.terms_verified for item in registry.entries)
    assert registry.match_url("https://www.sec.gov/robots.txt") is not None
    assert registry.match_url("https://www.sec.gov/unregistered") is None


@pytest.mark.asyncio
async def test_hash_blob_persistence_and_immutable_cache(site: FetchFixture) -> None:
    site.site("primary", f"immutable_url_prefixes: ['{PRIMARY}/documents/']")
    site.http.set(DOCUMENT, Reply())
    fetcher = site.fetcher()
    first = await fetcher.fetch(REQ)
    second = await fetcher.fetch(REQ)
    assert first == second
    assert first.source_record is not None
    record = first.source_record
    digest = sha256(PDF).hexdigest()
    assert record.doc_id == "sha256:" + digest
    assert record.byte_length == len(PDF) and record.media_type == "application/pdf"
    assert record.registry_entry_id == "primary" and record.fetch_method == "api"
    assert site.blobs.data == {"raw/" + digest: PDF}
    (row,) = site.sources()
    assert row.admitted is False and row.record == record.model_dump(mode="json")
    assert site.http.urls == [PRIMARY + "/robots.txt", DOCUMENT]
    assert all(agent == site.settings.agent_user_agent for _, _, agent in site.http.requests)
    assert [p["from_cache"] for _, kind, p in site.events if kind == "fetch"] == [False, True]
    assert "Example opinion bytes" not in repr(site.events)


@pytest.mark.asyncio
async def test_changed_immutable_url_is_never_stored(site: FetchFixture) -> None:
    site.site("primary", f"immutable_url_prefixes: ['{PRIMARY}/documents/']")
    site.http.set(DOCUMENT, Reply(), Reply(body=b"%PDF-1.7\nChanged\n"))
    fetcher = site.fetcher()
    assert (await fetcher.fetch(REQ)).status == "ok"
    site.blobs.data.clear()  # Metadata remains, so the new bytes must match its hash.
    outcome = await fetcher.fetch(REQ)
    assert outcome.reason_code == "SOURCE_UNRETRIEVABLE"
    assert [item["kind"] for item in outcome.blocks] == ["silent_content_change"] * 2
    assert site.sleeps == [5]
    assert len(site.sources()) == 1 and not site.blobs.data


@pytest.mark.asyncio
async def test_robots_wildcards_longest_match_and_cache_ttl(site: FetchFixture) -> None:
    robots = PRIMARY + "/robots.txt"
    site.http.set(
        robots,
        Reply(
            body=b"User-agent: *\nDisallow: /\n\nUser-agent: FetcherTest\n"
            b"Disallow: /documents/*.pdf$\nAllow: /documents/special.pdf$\n",
            media="text/plain",
        ),
        Reply(404, b"", "text/plain"),
    )
    allowed = PRIMARY + "/documents/special.pdf"
    site.http.set(allowed, Reply())
    site.http.set(DOCUMENT, Reply())
    fetcher = site.fetcher()
    blocked = await fetcher.fetch(REQ)
    assert [block["kind"] for block in blocked.blocks] == ["robots_disallowed"]
    assert site.http.urls == [robots]
    assert (await fetcher.fetch(REQ.model_copy(update={"url": allowed}))).status == "ok"
    assert site.http.urls == [robots, allowed]
    site.time += 86400
    assert (await fetcher.fetch(REQ)).status == "ok"
    assert site.http.urls == [robots, allowed, robots, DOCUMENT]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unregistered", "offsite_redirect", "forbidden", "html"])
async def test_robots_failure_stops_before_document(site: FetchFixture, mode: str) -> None:
    robots = PRIMARY + "/robots.txt"
    if mode == "offsite_redirect":
        site.http.set(
            robots, Reply(302, headers={"Location": "https://unregistered.test/robots.txt"})
        )
    elif mode == "forbidden":
        site.http.set(robots, Reply(403))
    elif mode == "html":
        site.http.set(robots, Reply(body=b"<html>fake robots</html>", media="text/html"))
    result = await site.fetcher(entry(robots=mode != "unregistered")).fetch(REQ)
    assert [block["kind"] for block in result.blocks] == ["robots_disallowed"]
    assert site.http.urls == ([] if mode == "unregistered" else [robots])
    assert not site.sources() and not site.blobs.data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "destination",
    [
        "https://unregistered.test/documents/opinion.pdf",
        ALTERNATE + "/documents/opinion.pdf",
        "/outside/opinion.pdf",
        "http://primary.test/documents/opinion.pdf",
        "/documents/%2e%2e/outside/opinion.pdf",
    ],
)
async def test_redirect_is_verified_before_following(site: FetchFixture, destination: str) -> None:
    site.http.set(DOCUMENT, Reply(302, headers={"Location": destination}))
    result = await site.fetcher(entry(), entry(ALTERNATE, "alternate")).fetch(REQ)
    assert result.reason_code == "SOURCE_UNRETRIEVABLE"
    assert site.http.urls == [PRIMARY + "/robots.txt", DOCUMENT]
    assert not site.sources() and not site.blobs.data


@pytest.mark.asyncio
async def test_registered_redirect_uses_final_url_and_checks_robots(site: FetchFixture) -> None:
    redirected = PRIMARY + "/documents/new.pdf"
    site.http.set(DOCUMENT, Reply(302, headers={"Location": "/documents/new.pdf"}))
    site.http.set(redirected, Reply())
    outcome = await site.fetcher().fetch(REQ)
    assert outcome.source_record is not None and outcome.source_record.url == redirected
    assert site.http.urls == [PRIMARY + "/robots.txt", DOCUMENT, redirected]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sequence", "delays", "kinds"),
    [
        ([Reply(429), Reply(429), Reply(429)], [2, 4], ["http_429"] * 3),
        ([Reply(429, headers={"Retry-After": "123"}), Reply()], [60], ["http_429"]),
        (
            [Reply(429, headers={"Retry-After": "Sat, 07 Nov 2026 00:00:07 GMT"}), Reply()],
            [7],
            ["http_429"],
        ),
        (
            [httpx.ReadTimeout("private"), httpx.ReadTimeout("private"), Reply()],
            [2, 4],
            ["timeout"] * 2,
        ),
        ([Reply(402), Reply()], [], ["paywall"]),
        (
            [Reply(200, b"<html>Subscribe to continue</html>", "text/html"), Reply()],
            [],
            ["paywall"],
        ),
        (
            [Reply(200, b"<html>not a PDF</html>", "text/html"), Reply()],
            [5],
            ["silent_content_change"],
        ),
        (
            [Reply(403), Reply(429), Reply(429), Reply()],
            [5, 2],
            ["http_403", "http_429", "http_429"],
        ),
    ],
)
async def test_exact_retry_table(
    site: FetchFixture, sequence: list[Reply | Exception], delays: list[float], kinds: list[str]
) -> None:
    site.http.set(DOCUMENT, *sequence)
    result = await site.fetcher().fetch(REQ)
    assert site.sleeps == delays
    assert [block["kind"] for block in result.blocks] == kinds
    assert site.http.urls.count(DOCUMENT) == len(delays) + 1
    assert all(not row.admitted for row in site.sources())
    if result.status == "unretrievable":
        assert result.reason_code == "SOURCE_UNRETRIEVABLE"
        assert not site.sources() and not site.blobs.data


@pytest.mark.asyncio
async def test_robots_429_is_rate_limited_before_document(site: FetchFixture) -> None:
    robots = PRIMARY + "/robots.txt"
    site.http.set(robots, Reply(429, headers={"Retry-After": "3"}), Reply(404))
    site.http.set(DOCUMENT, Reply())
    result = await site.fetcher().fetch(REQ)
    assert result.status == "ok" and [block["kind"] for block in result.blocks] == ["http_429"]
    assert site.http.urls == [robots, robots, DOCUMENT]
    assert site.sleeps == [3]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        Reply(404),
        Reply(500),
        Reply(206),
        Reply(body=b""),
        Reply(body=b"fake pdf"),
        Reply(body=PDF, media="application/octet-stream"),
        Reply(body=b"x" * 21000, media="text/html"),
    ],
)
async def test_invalid_response_cannot_be_persisted(site: FetchFixture, reply: Reply) -> None:
    site.http.set(DOCUMENT, reply)
    result = await site.fetcher().fetch(REQ)
    assert result.reason_code == "SOURCE_UNRETRIEVABLE"
    assert not site.sources() and not site.blobs.data


@pytest.mark.asyncio
async def test_alternate_requires_configured_entry_and_supported_document(
    site: FetchFixture,
) -> None:
    good = ALTERNATE + "/documents/opinion.pdf"
    site.site(
        "primary",
        f"""
alternates:
  opinion:
    - entry_id: alternate
      task: {{kind: fetch_url, params: {{url: 'https://unregistered.test/documents/opinion.pdf'}}}}
    - entry_id: wrong
      task: {{kind: fetch_url, params: {{url: '{good}'}}}}
    - entry_id: alternate
      task: {{kind: fetch_url, params: {{url: '{good}'}}}}
""",
    )
    site.http.set(DOCUMENT, Reply(403))
    site.http.set(good, Reply())
    result = await site.fetcher(entry(), entry(ALTERNATE, "alternate")).fetch(REQ)
    assert [block["kind"] for block in result.blocks] == ["http_403", "http_403"]
    assert (
        result.source_record is not None and result.source_record.registry_entry_id == "alternate"
    )
    assert site.http.urls == [
        PRIMARY + "/robots.txt",
        DOCUMENT,
        DOCUMENT,
        ALTERNATE + "/robots.txt",
        good,
    ]
    assert [p["action_taken"] for _, kind, p in site.events if kind == "block"] == [
        "retry",
        "switch_source",
    ]
    (stored,) = site.sources()
    assert not stored.admitted and stored.record["url"] == good


@pytest.mark.asyncio
async def test_method_hint_does_not_override_registry(site: FetchFixture) -> None:
    result = await site.fetcher().fetch(REQ.model_copy(update={"method_hint": "operator"}))
    assert result.status == "unretrievable" and not site.http.urls


@pytest.mark.asyncio
async def test_adapter_resolves_url_without_own_network_entrypoint(site: FetchFixture) -> None:
    fetcher = Fetcher(
        site.settings,
        site.database,
        site.blobs,
        registry=load_registry_fixture(entry(access="adapter")),
        transport=site.http.transport(),
        adapters={"primary": lambda request: request.url},
    )
    site.http.set(DOCUMENT, Reply())
    record = (await fetcher.fetch(REQ)).source_record
    assert record is not None and record.fetch_method == "adapter"
    assert site.http.urls == [PRIMARY + "/robots.txt", DOCUMENT]


def load_registry_fixture(*entries: RegistryEntry) -> Registry:
    return Registry(version=1, entries=list(entries))


@pytest.mark.asyncio
async def test_request_query_is_not_logged_or_emitted(
    site: FetchFixture, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    url = DOCUMENT + "?token=private-token"
    site.http.set(url, Reply(403))
    await site.fetcher().fetch(REQ.model_copy(update={"url": url}))
    assert "private-token" not in caplog.text and "private-token" not in repr(site.events)


def test_fault_proxy_and_ca_are_applied(
    site: FetchFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = MagicMock()
    monkeypatch.setattr(
        "gba_agent.access.polite_client.ssl.create_default_context", lambda: context
    )
    site.settings = site.settings.model_copy(update={"gba_ca_bundle": Path("fixture-ca.pem")})
    with pytest.raises(ValueError, match="cannot override"):
        Fetcher(
            site.settings,
            site.database,
            site.blobs,
            registry=load_registry_fixture(entry()),
            run_config=RunConfig(fault_proxy_url="http://proxy.test:8199"),
            transport=site.http.transport(),
        )
    fetcher = Fetcher(
        site.settings,
        site.database,
        site.blobs,
        registry=load_registry_fixture(entry()),
        run_config=RunConfig(fault_proxy_url="http://proxy.test:8199"),
    )
    context.load_verify_locations.assert_called_once_with(cafile="fixture-ca.pem")
    assert fetcher._client._proxy == "http://proxy.test:8199"


class BrowserFixture:
    def __init__(
        self, sessions: SessionManager, *, status: int, dom: DOMSignals, repeat: bool
    ) -> None:
        self.sessions, self.status, self.dom, self.repeat = sessions, status, dom, repeat
        self.attempts = 0
        self.probe: LoginProbe | None = None

    async def acquire(
        self,
        request: FetchRequest,
        permit: BrowserPermit,
        *,
        proxy_url: str | None,
        ca_bundle: Path | None,
    ) -> BrowserCapture:
        self.attempts += 1
        session = await self.sessions.get_or_create("primary", request.run_id)
        async with permit(request.url):
            blocked = self.attempts == 1 or self.repeat
            return BrowserCapture(
                ResponseInfo(
                    self.status if blocked else 200,
                    {},
                    request.url,
                    request.url,
                    "text/html" if blocked else "application/pdf",
                    "",
                    self.dom if blocked else DOMSignals(),
                    False,
                ),
                b"challenge secret body" if blocked else PDF,
                session,
                "opinion",
            )

    def login_browser(
        self,
        permit: BrowserPermit,
        *,
        proxy_url: str | None,
        ca_bundle: Path | None,
    ) -> LoginBrowser:
        self.probe = LoginProbe(permit)
        return self.probe


class LoginProbe:
    def __init__(self, permit: BrowserPermit) -> None:
        self.permit = permit
        self.checked = False

    async def prepare(self, session: Session, url: str) -> ResponseInfo:
        async with self.permit(url):
            return ResponseInfo(
                200,
                {},
                url,
                url,
                "text/html",
                "",
                DOMSignals(visible_text="Enter your one-time code"),
                False,
            )

    async def has_visible_selector(self, session: Session, selectors: Sequence[str]) -> bool:
        return False

    async def check_authenticated(self, session: Session, check: AuthCheck) -> bool:
        assert session.paused and check.selector == "#account"
        self.checked = True
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("challenge", "repeat", "resolution"),
    [
        ("mfa", False, "resolved"),
        ("captcha", False, "resolved"),
        ("captcha", True, "resolved"),
        ("captcha", False, "abandoned"),
    ],
)
async def test_guarded_challenge_handoff(
    site: FetchFixture, challenge: str, repeat: bool, resolution: str
) -> None:
    process_browser = AsyncMock(spec=Browser)
    process_browser.new_context.return_value.storage_state.return_value = {
        "cookies": [],
        "origins": [],
    }
    sessions = SessionManager(site.settings, cast(Browser, process_browser), site.database)
    handoffs = HandoffManager(
        site.settings, site.database, emit=lambda *args: site.events.append(args)
    )
    dom = (
        DOMSignals(iframe_sources=["https://captcha.test/widget"])
        if challenge == "captcha"
        else DOMSignals()
    )
    browser = BrowserFixture(
        sessions, status=200 if challenge == "captcha" else 401, dom=dom, repeat=repeat
    )
    login = LoginSite(
        site_id="primary",
        form=LoginFormSpec(
            username_selector="#user",
            password_selector="#password",
            submit_selector="#login",
            login_url=PRIMARY + "/documents/login",
        ),
        auth_check=AuthCheck(selector="#account"),
        login_url_patterns=[r"/login$"],
    )
    fetcher = site.fetcher(
        entry(access="operator", requires_login=challenge == "mfa"),
        browser=browser,
        sessions=sessions,
        handoffs=handoffs,
        login_site=login,
    )
    pending = asyncio.create_task(fetcher.fetch(REQ))
    for _ in range(40):
        await asyncio.sleep(0)
        if handoffs.list():
            break
    (handoff,) = handoffs.list()
    assert handoff.reason == challenge and handoff.viewer_url is not None
    assert not site.sources() and not site.blobs.data
    handoffs.resolve(
        handoff.handoff_id, "human", "resolved" if resolution == "resolved" else "abandoned"
    )
    outcome = await asyncio.wait_for(pending, timeout=1)
    assert browser.attempts == (2 if resolution == "resolved" else 1)
    assert outcome.status == ("ok" if resolution == "resolved" and not repeat else "unretrievable")
    assert [block["kind"] for block in outcome.blocks] == (
        ["captcha"] * (2 if repeat else 1) if challenge == "captcha" else ["login_wall"]
    )
    assert sum(event == "handoff_requested" for _, event, _ in site.events) == 1
    if challenge == "mfa":
        assert browser.probe is not None and browser.probe.checked
    assert all(not row.admitted for row in site.sources())
    assert all(value == PDF for value in site.blobs.data.values())
    assert "challenge secret body" not in repr(site.events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (401, b"authentication required", "login_wall"),
        (200, b"<iframe src='https://captcha.test/recaptcha'></iframe>", "captcha"),
    ],
)
async def test_blocked_http_page_never_becomes_evidence_without_browser(
    site: FetchFixture, status: int, body: bytes, kind: str
) -> None:
    site.http.set(DOCUMENT, Reply(status, body, "text/html"))
    outcome = await site.fetcher().fetch(REQ)
    assert [block["kind"] for block in outcome.blocks] == [kind]
    assert outcome.reason_code == "SOURCE_UNRETRIEVABLE"
    assert not site.sources() and not site.blobs.data


@pytest.mark.asyncio
async def test_redirect_target_cannot_bypass_robots(site: FetchFixture) -> None:
    robots = PRIMARY + "/robots.txt"
    site.http.set(
        robots,
        Reply(body=b"User-agent: *\nDisallow: /documents/private/", media="text/plain"),
    )
    site.http.set(DOCUMENT, Reply(302, headers={"Location": "/documents/private/opinion.pdf"}))
    result = await site.fetcher().fetch(REQ)
    assert [block["kind"] for block in result.blocks] == ["robots_disallowed"]
    assert site.http.urls == [robots, DOCUMENT]
    assert not site.sources()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing_permit", "post"])
async def test_browser_capture_requires_read_only_request_permit(
    site: FetchFixture, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_browser = AsyncMock(spec=Browser)
    sessions = SessionManager(site.settings, cast(Browser, process_browser), site.database)
    browser = BrowserFixture(sessions, status=200, dom=DOMSignals(), repeat=False)

    async def acquire(
        request: FetchRequest,
        permit: BrowserPermit,
        *,
        proxy_url: str | None,
        ca_bundle: Path | None,
    ) -> BrowserCapture:
        if mode == "post":
            async with permit(request.url, "POST"):
                pytest.fail("POST reached the browser request boundary")
        return BrowserCapture(
            ResponseInfo(
                200, {}, request.url, request.url, "application/pdf", "", DOMSignals(), False
            ),
            PDF,
        )

    monkeypatch.setattr(browser, "acquire", acquire)
    outcome = await site.fetcher(entry(access="operator"), browser=browser).fetch(REQ)
    assert outcome.reason_code == "SOURCE_UNRETRIEVABLE"
    assert site.http.urls == [] and not site.sources()


@pytest.mark.asyncio
async def test_http_captcha_handoff_retries_same_http_method(site: FetchFixture) -> None:
    process_browser = AsyncMock(spec=Browser)
    sessions = SessionManager(site.settings, cast(Browser, process_browser), site.database)
    handoffs = HandoffManager(site.settings, site.database)
    browser = BrowserFixture(sessions, status=200, dom=DOMSignals(), repeat=False)

    class Viewer(LoginProbe):
        async def prepare(self, session: Session, url: str) -> ResponseInfo:
            async with self.permit(url):
                return ResponseInfo(
                    200,
                    {},
                    url,
                    url,
                    "text/html",
                    "",
                    DOMSignals(visible_text="Verify you are human"),
                    False,
                )

    def viewer(
        permit: BrowserPermit, *, proxy_url: str | None, ca_bundle: Path | None
    ) -> LoginBrowser:
        return Viewer(permit)

    browser.login_browser = viewer  # type: ignore[method-assign]
    site.http.set(DOCUMENT, Reply(body=b"verify you are human", media="text/html"), Reply())
    fetcher = site.fetcher(browser=browser, sessions=sessions, handoffs=handoffs)
    pending = asyncio.create_task(fetcher.fetch(REQ))
    for _ in range(40):
        await asyncio.sleep(0)
        if handoffs.list():
            break
    (handoff,) = handoffs.list()
    assert handoff.reason == "captcha" and not site.sources()
    handoffs.resolve(handoff.handoff_id, "human", "resolved")
    outcome = await asyncio.wait_for(pending, timeout=1)
    assert outcome.source_record is not None and outcome.source_record.fetch_method == "api"
    assert browser.attempts == 0
    assert site.http.urls == [PRIMARY + "/robots.txt", DOCUMENT, DOCUMENT]
