"""Human-assisted login using Fetcher-provided navigation and read-only checks."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gba_agent.access.block_detector import BlockAction, BlockDetector, BlockEvent, ResponseInfo
from gba_agent.access.handoff import HandoffManager, HandoffReason
from gba_agent.access.session_manager import Session, SessionManager
from gba_agent.access.vault import FileVault, KeyringVault, NullVault, Vault
from gba_agent.api.errors import APIError
from gba_agent.config import Settings


class LoginFormSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username_selector: str
    password_selector: str
    submit_selector: str
    mfa_hint_selectors: list[str] = Field(default_factory=list)
    login_url: str


class AuthCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selector: str | None = None
    url_pattern: str | None = None

    @model_validator(mode="after")
    def validate_check(self) -> AuthCheck:
        if bool(self.selector) == bool(self.url_pattern):
            raise ValueError("Specify one authentication selector or URL pattern")
        if self.url_pattern:
            re.compile(self.url_pattern)
        return self


class LoginSite(BaseModel):
    site_id: str
    form: LoginFormSpec
    auth_check: AuthCheck
    login_url_patterns: list[str] = Field(default_factory=list)


class LoginBrowser(Protocol):
    """Fetcher integration. Navigation must enforce registry and Action Guard policy."""

    async def prepare(self, session: Session, url: str) -> ResponseInfo: ...

    async def has_visible_selector(self, session: Session, selectors: Sequence[str]) -> bool: ...

    async def check_authenticated(self, session: Session, check: AuthCheck) -> bool: ...


_INSTRUCTIONS: dict[HandoffReason, str] = {
    "login": "Sign in using the browser viewer, then resolve the handoff.",
    "mfa": "Complete the site's multi-factor authentication in the browser viewer.",
    "captcha": "Complete the site's CAPTCHA in the browser viewer.",
    "consent": "Review the site's consent request in the browser viewer.",
}
_MFA = re.compile(
    r"multi[- ]factor authentication|two[- ]factor|verification code|one[- ]time code"
)
_CONSENT = re.compile(r"consent required|accept cookies|agree to (the )?terms")


def create_vault(settings: Settings) -> Vault:
    if settings.vault_backend == "keyring":
        return KeyringVault()
    if settings.vault_backend == "file":
        if settings.vault_file is None or settings.vault_file_key is None:
            raise ValueError("File vault configuration is missing")
        return FileVault(settings.vault_file, settings.vault_file_key.get_secret_value())
    return NullVault()


class LoginFlow:
    def __init__(
        self,
        browser: LoginBrowser,
        detector: BlockDetector,
        handoffs: HandoffManager,
        sessions: SessionManager,
    ) -> None:
        self._browser, self._detector = browser, detector
        self._handoffs, self._sessions = handoffs, sessions

    async def run(self, session: Session, site: LoginSite) -> None:
        if session.site_id != site.site_id or session.paused:
            raise APIError(503, "SOURCE_UNRETRIEVABLE", "Session cannot start login")

        response = await self._browser.prepare(session, site.form.login_url)
        event = self._detector.detect(
            response,
            login_url_patterns=site.login_url_patterns,
            run_id=session.run_id,
            action_taken=self._block_action,
        )
        if event is not None and self._block_action(event) != "handoff":
            raise APIError(503, "SOURCE_UNRETRIEVABLE", "The login page is blocked")

        text = (response.dom_signals.visible_text or response.body_sample)[:8192].casefold()
        reason: HandoffReason = "login"
        if event is not None and event.kind == "captcha":
            reason = "captcha"
        elif _MFA.search(text) or (
            site.form.mfa_hint_selectors
            and await self._browser.has_visible_selector(session, site.form.mfa_hint_selectors)
        ):
            reason = "mfa"
        elif _CONSENT.search(text):
            reason = "consent"

        # The human alone controls the browser while the request is open.
        if session.paused:
            raise APIError(503, "SOURCE_UNRETRIEVABLE", "Session is waiting for human handoff")
        request = self._handoffs.request(
            session.run_id, site.site_id, reason, _INSTRUCTIONS[reason]
        )
        session.paused = True
        try:
            result = await self._handoffs.wait(request.handoff_id)
        except asyncio.CancelledError:
            # Keep the context alive and paused until the human's access ends.
            await self._handoffs.wait(request.handoff_id)
            session.paused = False
            await self._sessions.mark_expired(session, "SOURCE_UNRETRIEVABLE")
            raise
        authenticated = False
        try:
            if result.status == "expired":
                raise APIError(503, "HANDOFF_EXPIRED", "The human handoff expired")
            if result.status == "resolved":
                authenticated = await self._browser.check_authenticated(session, site.auth_check)
            if not authenticated:
                raise APIError(503, "SOURCE_UNRETRIEVABLE", "Authentication was not confirmed")
        finally:
            session.paused = False
            if not authenticated:
                await self._sessions.mark_expired(session, "SOURCE_UNRETRIEVABLE")
        await self._sessions.save(session)

    @staticmethod
    def _block_action(event: BlockEvent) -> BlockAction:
        if event.kind in {"captcha", "login_wall", "session_expired"}:
            return "handoff"
        return "mark_unretrievable"
