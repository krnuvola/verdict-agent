"""Deterministic in-process HTTP origins for Fetcher tests."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from gba_agent.access._registry import AccessMethod, Registry, RegistryEntry
from gba_agent.access.fetcher import Fetcher, GuardedBrowser
from gba_agent.access.handoff import HandoffManager
from gba_agent.access.login import LoginSite
from gba_agent.access.rate_limiter import RateLimit
from gba_agent.access.session_manager import SessionManager
from gba_agent.config import Settings
from gba_agent.db.models import Base, Run, SourceRecord
from gba_agent.db.repositories import RunsRepository
from gba_agent.db.session import create_session_factory, session_scope

RUN_ID = "run_01K9F6F6WF57TVGC0EFAGSKX67"
PRIMARY = "https://primary.test"
ALTERNATE = "https://alternate.test"
DOCUMENT = PRIMARY + "/documents/opinion.pdf"
PDF = b"%PDF-1.7\nExample opinion bytes\n"


def entry(
    host: str = PRIMARY,
    entry_id: str = "primary",
    *,
    access: AccessMethod = "api",
    requires_login: bool = False,
    terms_verified: bool = False,
    robots: bool = True,
) -> RegistryEntry:
    return RegistryEntry(
        entry_id=entry_id,
        name=entry_id,
        role="primary",
        authoritative=True,
        base_domains=[host.split("://")[1]],
        url_prefixes=[host + "/documents/"] + ([host + "/robots.txt"] if robots else []),
        doc_types=["opinion", "proxy_statement"],
        access=access,
        requires_login=requires_login,
        rate_limit=RateLimit(10, 8, 2),
        robots_respect=True,
        allowed_action_classes=["READ"],
        terms_verified=terms_verified,
        terms_url=host + "/terms" if terms_verified else None,
        terms_checked_on=datetime(2026, 11, 7, tzinfo=UTC).date() if terms_verified else None,
        phase="MVP",
    )


@dataclass
class Reply:
    status: int = 200
    body: bytes = PDF
    media: str = "application/pdf"
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class HTTPOrigins:
    replies: dict[str, deque[Reply | Exception]] = field(default_factory=dict)
    requests: list[tuple[str, str, str]] = field(default_factory=list)

    def set(self, url: str, *responses: Reply | Exception) -> None:
        self.replies[url] = deque(responses)

    async def respond(self, request: Request) -> Response:
        url = str(request.url)
        self.requests.append((url, request.method, request.headers.get("user-agent", "")))
        queue = self.replies.get(url)
        if queue is None:
            if request.url.path == "/robots.txt":
                return Response("User-agent: *\nDisallow:\n", media_type="text/plain")
            raise AssertionError(f"Unexpected outgoing request: {url}")
        reply = queue.popleft() if len(queue) > 1 else queue[0]
        if isinstance(reply, Exception):
            raise reply
        return Response(
            reply.body, status_code=reply.status, headers=reply.headers, media_type=reply.media
        )

    def transport(self) -> httpx.ASGITransport:
        return httpx.ASGITransport(app=Starlette(routes=[Route("/{path:path}", self.respond)]))

    @property
    def urls(self) -> list[str]:
        return [url for url, _, _ in self.requests]


class MemoryBlobs:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, content_type: str) -> None:
        previous = self.data.setdefault(key, data)
        if previous != data:
            raise ValueError("Immutable test blob conflict")

    def get(self, key: str) -> bytes:
        return self.data[key]

    def exists(self, key: str) -> bool:
        return key in self.data


@dataclass
class FetchFixture:
    settings: Settings
    database: sessionmaker[Session]
    http: HTTPOrigins = field(default_factory=HTTPOrigins)
    blobs: MemoryBlobs = field(default_factory=MemoryBlobs)
    events: list[tuple[str, str, dict[str, object]]] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)
    time: float = 0

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.time += seconds

    def now(self) -> datetime:
        return datetime(2026, 11, 7, tzinfo=UTC) + timedelta(seconds=self.time)

    def fetcher(
        self,
        *entries: RegistryEntry,
        browser: GuardedBrowser | None = None,
        sessions: SessionManager | None = None,
        handoffs: HandoffManager | None = None,
        login_site: LoginSite | None = None,
    ) -> Fetcher:
        return Fetcher(
            self.settings,
            self.database,
            self.blobs,
            registry=Registry(version=1, entries=list(entries) or [entry()]),
            emit=lambda *args: self.events.append(args),
            transport=self.http.transport(),
            clock=lambda: self.time,
            sleep=self.sleep,
            jitter=lambda _low, _high: 1,
            now=self.now,
            browser=browser,
            sessions=sessions,
            handoffs=handoffs,
            login_sites={login_site.site_id: login_site} if login_site else None,
        )

    def site(self, entry_id: str, config: str) -> None:
        directory = self.settings.registry_path.parent / "sites"
        directory.mkdir(exist_ok=True)
        (directory / f"{entry_id}.yaml").write_text(config, encoding="utf-8")

    def sources(self) -> list[SourceRecord]:
        with session_scope(self.database) as db:
            return list(db.scalars(select(SourceRecord)))


@pytest.fixture
def site(tmp_path: Path) -> Iterator[FetchFixture]:
    settings = Settings.model_validate(
        {
            "gba_env": "test",
            "service_token": "fake",
            "database_url": "postgresql+psycopg://u:p@localhost/db",
            "llm_provider": "anthropic",
            "llm_mode": "replay",
            "llm_model_planner": "planner",
            "support_judge": "nli",
            "agent_user_agent": "FetcherTest/1.0 (contact: tests@example.test)",
            "registry_path": tmp_path / "registry.yaml",
            "session_dir": tmp_path / "sessions",
            "session_enc_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        }
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'fetcher.db'}")
    Base.metadata.create_all(engine)
    database = create_session_factory(engine)
    try:
        with session_scope(database) as db:
            RunsRepository(db).add(
                Run(
                    run_id=RUN_ID,
                    state="running",
                    matter_id="mat_fixture",
                    matter={},
                    config={},
                )
            )
        yield FetchFixture(settings, database)
    finally:
        engine.dispose()
