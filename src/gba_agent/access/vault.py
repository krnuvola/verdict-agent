"""Credential vaults. Import this module only from the login flow."""

from __future__ import annotations

import base64
import json
import logging
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import keyring
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from keyring.errors import KeyringError
from playwright.async_api import Page

from gba_agent.api.errors import APIError
from gba_agent.guards.reason_codes import AG_LOGIN_VIA_VAULT_ONLY

if TYPE_CHECKING:
    from gba_agent.access.login import LoginFormSpec


class Vault(Protocol):
    def has(self, site_id: str, principal: str) -> bool: ...

    async def fill_login(
        self, page: Page, site_id: str, principal: str, spec: LoginFormSpec
    ) -> None: ...


class _SecretFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self._values: set[str] = set()

    def register(self, *values: str) -> None:
        self._values.update(value for value in values if value)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self._redact(value) for value in record.args)
        elif record.args:
            record.args = {name: self._redact(value) for name, value in record.args.items()}
        return True

    def _redact(self, value: object) -> object:
        return "***" if isinstance(value, str) and value in self._values else value


_secret_filter = _SecretFilter()


def _register_secrets(*values: str) -> None:
    _secret_filter.register(*values)
    root = logging.getLogger()
    pending = [root]
    while pending:
        logger = pending.pop()
        pending.extend(logger.getChildren())
        for handler in logger.handlers:
            handler.addFilter(_secret_filter)
    try:
        trajectory = import_module("gba_contracts.trajectory")
    except ModuleNotFoundError as exc:
        if exc.name != "gba_contracts":
            raise
        trajectory = import_module("gba_agent.trajectory")
    for value in values:
        if value:
            trajectory.register_secret(value)


class _HumanOnlyVault:
    async def fill_login(
        self, page: Page, site_id: str, principal: str, spec: LoginFormSpec
    ) -> None:
        # The workspace forbids agent LOGIN actions, including vault-driven submission.
        raise APIError(403, AG_LOGIN_VIA_VAULT_ONLY, "Login requires a human handoff")


class KeyringVault(_HumanOnlyVault):
    def has(self, site_id: str, principal: str) -> bool:
        try:
            password = keyring.get_password(f"gba-agent:{site_id}", principal)
        except KeyringError:
            raise APIError(503, "UPSTREAM_UNAVAILABLE", "Credential vault is unavailable") from None
        if not password:
            return False
        _register_secrets(principal, password)
        return True


class FileVault(_HumanOnlyVault):
    """Reads nonce-prefixed AES-256-GCM JSON keyed by site ID and principal."""

    def __init__(self, path: Path, key: str) -> None:
        decoded = base64.b64decode(key, validate=True)
        if len(decoded) != 32:
            raise ValueError("File vault requires a 32-byte key")
        self._path, self._cipher = path, AESGCM(decoded)

    def has(self, site_id: str, principal: str) -> bool:
        try:
            data = self._path.read_bytes()
            secrets = json.loads(self._cipher.decrypt(data[:12], data[12:], None))
            if not isinstance(secrets, dict):
                raise ValueError
            site = secrets.get(site_id, {})
            if not isinstance(site, dict):
                raise ValueError
            if principal not in site:
                return False
            credential = site[principal]
            if not isinstance(credential, dict):
                raise ValueError
            username, password = credential.get("username"), credential.get("password")
            if not isinstance(username, str) or not isinstance(password, str):
                raise ValueError
            if not username or not password:
                raise ValueError
        except (OSError, InvalidTag, ValueError):
            raise APIError(503, "UPSTREAM_UNAVAILABLE", "Credential vault is unavailable") from None
        _register_secrets(username, password)
        return True


class NullVault(_HumanOnlyVault):
    def has(self, site_id: str, principal: str) -> bool:
        return False
