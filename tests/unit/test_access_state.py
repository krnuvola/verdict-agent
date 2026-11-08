"""Offline session, login, vault, and handoff lifecycle tests."""

import asyncio
import base64
import json
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient
from playwright.async_api import Browser, StorageState
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as DBSession
from sqlalchemy.orm import sessionmaker

from gba_agent.access.block_detector import BlockDetector, DOMSignals, ResponseInfo
from gba_agent.access.handoff import HandoffManager
from gba_agent.access.login import AuthCheck, LoginFlow, LoginFormSpec, LoginSite, create_vault
from gba_agent.access.session_manager import Session, SessionManager
from gba_agent.api.errors import APIError
from gba_agent.api.main import app
from gba_agent.config import Settings
from gba_agent.db.models import Base, Run
from gba_agent.db.repositories import RunsRepository, SessionsRepository
from gba_agent.db.session import create_session_factory, session_scope

KEY = bytes(range(32))
ENCODED_KEY = base64.b64encode(KEY).decode()
RUN_ID = "run_01K9F6F6WF57TVGC0EFAGSKX67"
STORAGE: StorageState = {
    "cookies": [],
    "origins": [
        {
            "origin": "https://example.test",
            "localStorage": [{"name": "auth", "value": "private-session-token"}],
        }
    ],
}
SITE = LoginSite(
    site_id="site",
    form=LoginFormSpec(
        username_selector="#name",
        password_selector="#password",
        submit_selector="#submit",
        mfa_hint_selectors=["#code"],
        login_url="https://example.test/login",
    ),
    auth_check=AuthCheck(selector="#account"),
    login_url_patterns=[r"/login$"],
)


@dataclass
class Clock:
    current: datetime = datetime(2026, 11, 7, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current


@dataclass
class Fixture:
    settings: Settings
    database: sessionmaker[DBSession]
    clock: Clock = field(default_factory=Clock)
    events: list[tuple[str, str, dict[str, object]]] = field(default_factory=list)

    def sessions(self, browser: AsyncMock) -> SessionManager:
        return SessionManager(
            self.settings,
            cast(Browser, browser),
            self.database,
            session_ttl_seconds={"site": 30},
            now=self.clock.now,
            emit=lambda *event: self.events.append(event),
        )

    def handoffs(self) -> HandoffManager:
        return HandoffManager(
            self.settings,
            self.database,
            now=self.clock.now,
            emit=lambda *event: self.events.append(event),
        )

    def run_state(self) -> str:
        with session_scope(self.database) as db:
            run = RunsRepository(db).get(RUN_ID)
            assert run is not None
            return run.state


@pytest.fixture
def fixture(tmp_path: Path) -> Iterator[Fixture]:
    settings = Settings.model_validate(
        {
            "service_token": "test-token",
            "database_url": "postgresql+psycopg://u:p@localhost/db",
            "llm_provider": "anthropic",
            "llm_mode": "replay",
            "llm_model_planner": "planner",
            "support_judge": "nli",
            "agent_user_agent": "GBA (contact: test@example.test)",
            "session_dir": tmp_path / "sessions",
            "session_enc_key": ENCODED_KEY,
            "novnc_base_url": "http://localhost:6080/",
            "handoff_timeout_seconds": 10,
        }
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    database = create_session_factory(engine)
    try:
        with session_scope(database) as db:
            RunsRepository(db).add(
                Run(run_id=RUN_ID, state="running", matter_id="mat_fixture", matter={}, config={})
            )
        yield Fixture(settings, database)
    finally:
        engine.dispose()


def fake_browser() -> AsyncMock:
    browser = AsyncMock(spec=Browser)
    context = browser.new_context.return_value
    context.storage_state.return_value = STORAGE
    return browser


@pytest.mark.asyncio
async def test_session_checkpoint_round_trip_and_principal_isolation(fixture: Fixture) -> None:
    browser = fake_browser()
    manager = fixture.sessions(browser)
    session = await manager.get_or_create("site", RUN_ID, "alice")
    assert session.session_id.startswith("ses_")
    assert session.expires_at == fixture.clock.now() + timedelta(seconds=30)
    assert await manager.get_or_create("site", RUN_ID, "alice") is session
    assert (await manager.get_or_create("site", RUN_ID, "bob")).session_id != session.session_id
    assert (await manager.get_or_create("other", RUN_ID, "alice")).session_id != session.session_id
    await manager.save(session)
    path = fixture.settings.session_dir / f"{session.session_id}.enc"
    first = path.read_bytes()
    assert b"private-session-token" not in first
    assert not list(path.parent.glob("*.json"))
    await manager.close(session)
    assert path.read_bytes()[:12] != first[:12]
    with session_scope(fixture.database) as db:
        row = SessionsRepository(db).get(session.session_id)
        assert row is not None and row.state == "closed" and row.enc_path == str(path)
    new_browser = fake_browser()
    restored = await fixture.sessions(new_browser).get_or_create("site", RUN_ID, "alice")
    assert restored.session_id == session.session_id
    new_browser.new_context.assert_awaited_once_with(
        storage_state=STORAGE, user_agent=fixture.settings.agent_user_agent
    )
    assert [payload["event"] for _, kind, payload in fixture.events if kind == "session"] == [
        "created",
        "created",
        "created",
        "refreshed",
        "refreshed",
        "closed",
        "restored",
    ]
    assert "private-session-token" not in repr(fixture.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("in_memory", [True, False])
async def test_expired_session_never_restores(fixture: Fixture, in_memory: bool) -> None:
    browser = fake_browser()
    manager = fixture.sessions(browser)
    old = await manager.get_or_create("site", RUN_ID)
    await manager.save(old)
    fixture.clock.current += timedelta(seconds=30)
    if not in_memory:
        manager = fixture.sessions(browser)
    new = await manager.get_or_create("site", RUN_ID)
    assert new.session_id != old.session_id
    assert browser.new_context.await_args.kwargs["storage_state"] is None
    with session_scope(fixture.database) as db:
        row = SessionsRepository(db).get(old.session_id)
        assert row is not None and row.state == "expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["ciphertext", "principal", "expiry", "path", "missing"])
async def test_session_tampering_is_rejected_before_browser_context(
    fixture: Fixture, tamper: str
) -> None:
    manager = fixture.sessions(fake_browser())
    session = await manager.get_or_create("site", RUN_ID)
    await manager.close(session)
    path = fixture.settings.session_dir / f"{session.session_id}.enc"
    if tamper == "ciphertext":
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
    if tamper == "missing":
        path.unlink()
    principal = "another" if tamper == "principal" else "default"
    with session_scope(fixture.database) as db:
        row = SessionsRepository(db).get(session.session_id)
        assert row is not None
        row.principal = principal
        if tamper == "expiry":
            row.expires_at = fixture.clock.now() + timedelta(hours=3)
        if tamper == "path":
            row.enc_path = str(path.parent / "different.enc")
    browser = fake_browser()
    with pytest.raises(APIError, match="verification") as error:
        await fixture.sessions(browser).get_or_create("site", RUN_ID, principal)
    assert error.value.code == "SOURCE_UNRETRIEVABLE"
    browser.new_context.assert_not_awaited()


def test_file_and_null_vault_are_secret_safe(
    fixture: Fixture, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert not create_vault(fixture.settings).has("site", "alice")
    path = tmp_path / "vault.enc"
    nonce = b"abcdefghijkl"
    payload = {"site": {"alice": {"username": "private-user", "password": "private-password"}}}
    path.write_bytes(nonce + AESGCM(KEY).encrypt(nonce, json.dumps(payload).encode(), None))
    vault = create_vault(
        fixture.settings.model_copy(
            update={
                "vault_backend": "file",
                "vault_file": path,
                "vault_file_key": fixture.settings.session_enc_key,
            }
        )
    )
    assert vault.has("site", "alice") is True
    assert vault.has("site", "unknown") is False
    logging.getLogger(__name__).warning("Testing %s and %s", "private-user", "private-password")
    logging.getLogger(__name__).warning("Testing %(secret)s", {"secret": "private-password"})
    assert "private-user" not in caplog.text and "private-password" not in caplog.text
    assert "Testing *** and ***" in caplog.text
    path.write_bytes(b"invalid")
    with pytest.raises(APIError, match="vault is unavailable"):
        vault.has("site", "alice")


@pytest.mark.asyncio
async def test_vault_fill_refuses_login_actions(fixture: Fixture) -> None:
    page = AsyncMock()
    with pytest.raises(APIError) as error:
        await create_vault(fixture.settings).fill_login(page, "site", "default", SITE.form)
    assert error.value.code == "AG_LOGIN_VIA_VAULT_ONLY"
    assert not page.mock_calls


def test_keyring_vault_does_not_return_password(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_get_password(service: str, principal: str) -> str | None:
        assert service == "gba-agent:site"
        return "fake-keyring-secret" if principal == "alice" else None

    monkeypatch.setattr("keyring.get_password", fake_get_password)
    vault = create_vault(fixture.settings.model_copy(update={"vault_backend": "keyring"}))
    assert vault.has("site", "alice") is True
    assert vault.has("site", "unknown") is False


@pytest.mark.asyncio
async def test_handoff_persists_resolves_wakes_waiter_and_resumes_run(fixture: Fixture) -> None:
    manager = fixture.handoffs()
    request = manager.request(RUN_ID, "site", "mfa", "Use the viewer.")
    assert request.viewer_url == "http://localhost:6080/vnc.html?autoconnect=1&resize=scale"
    assert fixture.run_state() == "waiting_handoff"
    assert fixture.handoffs().list() == [request]
    waiting = asyncio.create_task(manager.wait(request.handoff_id))
    await asyncio.sleep(0)
    result = manager.resolve(request.handoff_id, "reviewer", "resolved")
    assert await asyncio.wait_for(waiting, 0.5) == result
    assert result.status == "resolved" and result.resolved_by == "reviewer"
    assert fixture.run_state() == "running"
    assert fixture.handoffs().resolve(request.handoff_id, "different", "abandoned") == result
    assert [event for _, event, _ in fixture.events] == ["handoff_requested", "handoff_resolved"]


@pytest.mark.asyncio
async def test_handoff_deadline_takes_priority_over_late_resolution(fixture: Fixture) -> None:
    manager = fixture.handoffs()
    request = manager.request(RUN_ID, "site", "captcha", "Use the viewer.")
    fixture.clock.current += timedelta(seconds=10)
    result = fixture.handoffs().resolve(request.handoff_id, "reviewer", "resolved")
    assert result.status == "expired" and result.resolved_by is None
    assert await manager.wait(request.handoff_id) == result
    assert manager.list() == []
    assert manager.list("expired") == [result]
    assert fixture.run_state() == "running"
    assert fixture.events[-1][2]["reason_code"] == "HANDOFF_EXPIRED"


def test_handoff_api_uses_existing_routes(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gba_agent.api.main.get_settings", lambda: fixture.settings)
    monkeypatch.setattr(app.state, "handoffs", fixture.handoffs(), raising=False)
    request = fixture.handoffs().request(RUN_ID, "site", "consent", "Review in the viewer.")
    headers = {"Authorization": "Bearer test-token", "X-Contract-Version": "1.0.0"}
    client = TestClient(app)
    try:
        response = client.get("/orch/v1/handoffs", headers=headers)
        assert response.json() == {"items": [request.model_dump(mode="json")]}
        response = client.post(
            f"/orch/v1/handoffs/{request.handoff_id}/resolve",
            headers=headers,
            json={"resolved_by": "human", "outcome": "abandoned"},
        )
        assert response.status_code == 200 and response.json()["status"] == "abandoned"
        missing = client.post(
            "/orch/v1/handoffs/missing/resolve",
            headers=headers,
            json={"resolved_by": "human", "outcome": "resolved"},
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "HANDOFF_NOT_FOUND"
    finally:
        client.close()


@dataclass
class LoginProbe:
    dom: DOMSignals
    status: int = 200
    selector: bool = False
    authenticated: bool = True
    checked: bool = False

    async def prepare(self, session: Session, url: str) -> ResponseInfo:
        assert url == SITE.form.login_url
        return ResponseInfo(self.status, {}, url, url, "text/html", "", self.dom, False)

    async def has_visible_selector(self, session: Session, selectors: Sequence[str]) -> bool:
        assert list(selectors) == SITE.form.mfa_hint_selectors
        return self.selector

    async def check_authenticated(self, session: Session, check: AuthCheck) -> bool:
        assert check == SITE.auth_check
        assert session.paused
        self.checked = True
        return self.authenticated


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dom", "hint", "reason"),
    [
        (DOMSignals(visible_text="Enter your one-time code"), False, "mfa"),
        (DOMSignals(), True, "mfa"),
        (DOMSignals(iframe_sources=["https://example.test/hcaptcha"]), False, "captcha"),
        (DOMSignals(visible_text="Accept cookies"), False, "consent"),
        (DOMSignals(password_input=True), False, "login"),
    ],
)
async def test_login_pauses_for_human_challenge_and_confirms_authentication(
    fixture: Fixture, dom: DOMSignals, hint: bool, reason: str
) -> None:
    browser = fake_browser()
    manager, handoffs = fixture.sessions(browser), fixture.handoffs()
    session = await manager.get_or_create("site", RUN_ID)
    probe = LoginProbe(dom, selector=hint)
    pending = asyncio.create_task(
        LoginFlow(probe, BlockDetector(), handoffs, manager).run(session, SITE)
    )
    await asyncio.sleep(0)
    (request,) = handoffs.list()
    assert session.paused and request.reason == reason
    assert not probe.checked
    with pytest.raises(APIError, match="waiting"):
        await manager.save(session)
    browser.new_context.return_value.close.assert_not_awaited()
    handoffs.resolve(request.handoff_id, "reviewer", "resolved")
    await asyncio.wait_for(pending, 0.5)
    assert probe.checked and not session.paused
    assert (fixture.settings.session_dir / f"{session.session_id}.enc").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("invalid_auth", "SOURCE_UNRETRIEVABLE"),
        ("timeout", "HANDOFF_EXPIRED"),
        ("abandoned", "SOURCE_UNRETRIEVABLE"),
    ],
)
async def test_login_failure_expires_context(fixture: Fixture, mode: str, code: str) -> None:
    browser = fake_browser()
    manager, handoffs = fixture.sessions(browser), fixture.handoffs()
    session = await manager.get_or_create("site", RUN_ID)
    probe = LoginProbe(DOMSignals(), authenticated=False)
    pending = asyncio.create_task(
        LoginFlow(probe, BlockDetector(), handoffs, manager).run(session, SITE)
    )
    await asyncio.sleep(0)
    (request,) = handoffs.list()
    if mode == "timeout":
        fixture.clock.current += timedelta(seconds=10)
    handoffs.resolve(
        request.handoff_id, "reviewer", "abandoned" if mode == "abandoned" else "resolved"
    )
    with pytest.raises(APIError) as error:
        await asyncio.wait_for(pending, 0.5)
    assert error.value.code == code
    assert probe.checked == (mode == "invalid_auth")
    assert not session.paused
    browser.new_context.return_value.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_login_keeps_context_paused_until_handoff_ends(fixture: Fixture) -> None:
    browser = fake_browser()
    manager, handoffs = fixture.sessions(browser), fixture.handoffs()
    session = await manager.get_or_create("site", RUN_ID)
    flow = LoginFlow(LoginProbe(DOMSignals()), BlockDetector(), handoffs, manager)
    pending = asyncio.create_task(flow.run(session, SITE))
    await asyncio.sleep(0)
    (request,) = handoffs.list()
    pending.cancel()
    await asyncio.sleep(0)
    assert session.paused
    assert fixture.run_state() == "waiting_handoff"
    assert not pending.done()
    handoffs.resolve(request.handoff_id, "reviewer", "abandoned")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, timeout=0.5)
    browser.new_context.return_value.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_login_http_block_creates_no_handoff(fixture: Fixture) -> None:
    manager, handoffs = fixture.sessions(fake_browser()), fixture.handoffs()
    session = await manager.get_or_create("site", RUN_ID)
    response = LoginProbe(DOMSignals(), status=429)
    with pytest.raises(APIError, match="login page is blocked"):
        await LoginFlow(response, BlockDetector(), handoffs, manager).run(session, SITE)
    assert handoffs.list() == []
    assert fixture.run_state() == "running"


def test_handoff_rejects_credential_bearing_viewer_url(fixture: Fixture) -> None:
    settings = fixture.settings.model_copy(
        update={"novnc_base_url": "https://alice:secret@example.test"}
    )
    with pytest.raises(ValueError, match="omit user info") as error:
        HandoffManager(settings, fixture.database)
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_session_uses_site_yaml_ttl_and_rejects_invalid_policy(
    fixture: Fixture, tmp_path: Path
) -> None:
    site_directory = tmp_path / "sites"
    site_directory.mkdir()
    (site_directory / "site.yaml").write_text("session_ttl_seconds: 42\n", encoding="utf-8")
    (site_directory / "invalid.yaml").write_text("session_ttl_seconds: true\n", encoding="utf-8")
    settings = fixture.settings.model_copy(update={"registry_path": tmp_path / "registry.yaml"})
    browser = fake_browser()
    manager = SessionManager(
        settings, cast(Browser, browser), fixture.database, now=fixture.clock.now
    )
    session = await manager.get_or_create("site", RUN_ID)
    assert session.expires_at == fixture.clock.now() + timedelta(seconds=42)
    with pytest.raises(APIError, match="positive integer"):
        await manager.get_or_create("invalid", RUN_ID)
    with pytest.raises(APIError) as outside:
        await manager.get_or_create("../invalid", RUN_ID)
    assert outside.value.code == "SG_URL_NOT_IN_REGISTRY"
    browser.new_context.assert_awaited_once()
