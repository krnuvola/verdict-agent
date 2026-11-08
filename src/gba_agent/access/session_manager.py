"""Persistent browser sessions with authenticated, encrypted checkpoints."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import cast

import yaml  # type: ignore[import-untyped]
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from playwright.async_api import Browser, BrowserContext, StorageState, async_playwright
from sqlalchemy.orm import Session as DBSession
from sqlalchemy.orm import sessionmaker

from gba_agent.access.block_detector import EventSink
from gba_agent.api.errors import APIError
from gba_agent.api.trace import is_ulid, new_ulid
from gba_agent.config import Settings
from gba_agent.db.models import BrowserSession
from gba_agent.db.repositories import SessionsRepository
from gba_agent.db.session import session_scope


@dataclass(eq=False)
class Session:
    session_id: str
    site_id: str
    principal: str = field(repr=False)
    context: BrowserContext = field(repr=False)
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime | None
    run_id: str
    paused: bool = field(default=False, init=False)


class SessionManager:
    """Manage contexts on the process-owned Chromium browser."""

    def __init__(
        self,
        settings: Settings,
        browser: Browser,
        database: sessionmaker[DBSession],
        *,
        session_ttl_seconds: Mapping[str, int] | None = None,
        emit: EventSink | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        block_service_workers: bool = False,
    ) -> None:
        self._cipher = AESGCM(settings.require_session_encryption_key())
        self._directory = settings.session_dir.resolve()
        self._browser = browser
        self._block_service_workers = block_service_workers
        self._user_agent = settings.agent_user_agent
        self._site_directory = settings.registry_path.parent / "sites"
        self._database = database
        self._ttl = dict(session_ttl_seconds or {})
        if any(ttl <= 0 for ttl in self._ttl.values()):
            raise ValueError("Session TTL must be positive")
        self._emit, self._now = emit, now
        self._contexts: dict[tuple[str, str], Session] = {}
        self._lock = asyncio.Lock()

    @classmethod
    @asynccontextmanager
    async def launch(
        cls, settings: Settings, database: sessionmaker[DBSession], *, emit: EventSink | None = None
    ) -> AsyncIterator[SessionManager]:
        """Own a single Chromium browser for the lifetime of the access runtime."""
        settings.require_session_encryption_key()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=settings.browser_mode == "headless")
            try:
                yield cls(settings, browser, database, emit=emit)
            finally:
                await browser.close()

    def _site_ttl(self, site_id: str) -> int:
        if site_id in self._ttl:
            return self._ttl[site_id]
        if re.fullmatch(r"[a-zA-Z0-9_-]+", site_id) is None:
            raise APIError(403, "SG_URL_NOT_IN_REGISTRY", "Invalid registry entry identifier")
        try:
            path = self._site_directory / f"{site_id}.yaml"
            config = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 3600
        except (OSError, UnicodeError, yaml.YAMLError):
            raise APIError(500, "INTERNAL_ERROR", "Site session policy could not be read") from None
        if config is None:
            return 3600
        ttl = config.get("session_ttl_seconds", 3600) if isinstance(config, dict) else None
        if type(ttl) is not int or ttl <= 0:
            raise APIError(500, "INTERNAL_ERROR", "Site session TTL must be a positive integer")
        return ttl

    def _event(self, session: Session, event: str) -> None:
        self._emit_event(session.run_id, session.session_id, session.site_id, event)

    def _emit_event(self, run_id: str, session_id: str, site_id: str, event: str) -> None:
        if self._emit is not None:
            self._emit(
                run_id,
                "session",
                {
                    "session_id": session_id,
                    "site_id": site_id,
                    "event": event,
                },
            )

    @staticmethod
    def _aad(row: BrowserSession) -> bytes:
        expires_at = row.expires_at.replace(tzinfo=UTC).isoformat() if row.expires_at else None
        return json.dumps([row.session_id, row.site_id, row.principal, expires_at]).encode()

    def _path(self, session_id: str) -> Path:
        if not session_id.startswith("ses_") or not is_ulid(session_id[4:]):
            raise ValueError("Invalid stored session identifier")
        return self._directory / f"{session_id}.enc"

    def _restore(self, row: BrowserSession) -> StorageState | None:
        if row.enc_path is None:
            return None
        path = self._path(row.session_id)
        if Path(row.enc_path) != path:
            raise ValueError("Invalid stored session path")
        encrypted = path.read_bytes()
        if len(encrypted) < 28:
            raise ValueError("Truncated session ciphertext")
        state = json.loads(self._cipher.decrypt(encrypted[:12], encrypted[12:], self._aad(row)))
        if not isinstance(state, dict) or not all(
            isinstance(state.get(key), list) for key in ("cookies", "origins")
        ):
            raise ValueError("Invalid session storage state")
        return cast(StorageState, state)

    def _set_state(self, session_id: str, state: str) -> None:
        with session_scope(self._database) as db:
            row = SessionsRepository(db).get(session_id)
            if row is None:
                raise APIError(503, "SOURCE_UNRETRIEVABLE", "Session metadata is unavailable")
            row.state = state

    async def get_or_create(self, site_id: str, run_id: str, principal: str = "default") -> Session:
        key = (site_id, principal)
        async with self._lock:
            now = self._now()
            current = self._contexts.get(key)
            if current is not None:
                self._require_available(current)
                if current.expires_at is None or current.expires_at > now:
                    self._touch(current, now)
                    current.run_id = run_id
                    return current
                await self._expire(current)

            with session_scope(self._database) as db:
                repository = SessionsRepository(db)
                stored = repository.find_latest(site_id, principal)
            storage = None
            if stored is not None and stored.state != "expired":
                expiry = stored.expires_at
                if expiry is not None and expiry.replace(tzinfo=UTC) <= now:
                    self._set_state(stored.session_id, "expired")
                    self._emit_event(run_id, stored.session_id, site_id, "expired")
                else:
                    try:
                        storage = self._restore(stored)
                    except (OSError, InvalidTag, ValueError):
                        self._set_state(stored.session_id, "expired")
                        self._emit_event(run_id, stored.session_id, site_id, "expired")
                        raise APIError(
                            503, "SOURCE_UNRETRIEVABLE", "Session checkpoint failed verification"
                        ) from None

            restored = storage is not None and stored is not None
            if restored and stored is not None:
                row = stored
            else:
                row = BrowserSession(
                    session_id=f"ses_{new_ulid()}",
                    site_id=site_id,
                    principal=principal,
                    created_at=now,
                    expires_at=now + timedelta(seconds=self._site_ttl(site_id)),
                    state="active",
                )
            if self._block_service_workers:
                context = await self._browser.new_context(
                    storage_state=storage,
                    user_agent=self._user_agent,
                    service_workers="block",
                )
            else:
                context = await self._browser.new_context(
                    storage_state=storage,
                    user_agent=self._user_agent,
                )
            try:
                with session_scope(self._database) as db:
                    repo = SessionsRepository(db)
                    if restored:
                        db.add(row)
                    else:
                        repo.add(row)
                    row.state = "active"
                    row.last_used_at = now
            except BaseException:
                await context.close()
                raise
            session = Session(
                row.session_id,
                site_id,
                principal,
                context,
                row.created_at.replace(tzinfo=UTC),
                now,
                row.expires_at.replace(tzinfo=UTC) if row.expires_at else None,
                run_id,
            )
            self._contexts[key] = session
            self._event(session, "restored" if restored else "created")
            return session

    def _touch(self, session: Session, now: datetime) -> None:
        with session_scope(self._database) as db:
            row = SessionsRepository(db).get(session.session_id)
            if row is None or row.state != "active":
                raise APIError(503, "SOURCE_UNRETRIEVABLE", "Session is unavailable")
            row.last_used_at = now
        session.last_used_at = now

    def _require_available(self, session: Session) -> None:
        if self._contexts.get((session.site_id, session.principal)) is not session:
            raise APIError(503, "SOURCE_UNRETRIEVABLE", "Session is unavailable")
        if session.paused:
            raise APIError(503, "SOURCE_UNRETRIEVABLE", "Session is waiting for human handoff")

    async def _save(self, session: Session) -> None:
        self._require_available(session)
        now = self._now()
        if session.expires_at is not None and session.expires_at <= now:
            await self._expire(session)
            raise APIError(503, "SOURCE_UNRETRIEVABLE", "Session has expired")
        state = await session.context.storage_state()
        path = self._path(session.session_id)
        with session_scope(self._database) as db:
            row = SessionsRepository(db).get(session.session_id)
            if row is None or row.state != "active":
                raise APIError(503, "SOURCE_UNRETRIEVABLE", "Session is unavailable")
            nonce = os.urandom(12)
            ciphertext = nonce + self._cipher.encrypt(
                nonce, json.dumps(state).encode(), self._aad(row)
            )
            self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=self._directory, delete=False) as file:
                temporary = Path(file.name)
                try:
                    file.write(ciphertext)
                    file.flush()
                    os.fsync(file.fileno())
                    file.close()
                    temporary.replace(path)
                finally:
                    file.close()
                    temporary.unlink(missing_ok=True)
            row.enc_path = str(path)
            row.last_used_at = now
        session.last_used_at = now
        self._event(session, "refreshed")

    async def save(self, session: Session) -> None:
        async with self._lock:
            await self._save(session)

    async def _expire(self, session: Session) -> None:
        self._set_state(session.session_id, "expired")
        del self._contexts[session.site_id, session.principal]
        await session.context.close()
        self._event(session, "expired")

    async def mark_expired(self, session: Session, reason: str) -> None:
        # The supplied reason can contain untrusted text and is never emitted.
        async with self._lock:
            self._require_available(session)
            await self._expire(session)

    async def close(self, session: Session) -> None:
        async with self._lock:
            self._require_available(session)
            if session.expires_at is not None and session.expires_at <= self._now():
                await self._expire(session)
                return
            await self._save(session)
            self._set_state(session.session_id, "closed")
            del self._contexts[session.site_id, session.principal]
            await session.context.close()
            self._event(session, "closed")
