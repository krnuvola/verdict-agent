"""Registry policy. Use the shared loader when the contracts package is installed."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Literal, cast
from urllib.parse import SplitResult, unquote, urlsplit

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, model_validator

from gba_agent.access.rate_limiter import RateLimit
from gba_agent.api.errors import APIError

AccessMethod = Literal["api", "bulk", "adapter", "operator"]


def safe_url(url: str) -> SplitResult | None:
    if not url or not url.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or port == 0
        or parsed.username is not None
        or "#" in url
        or "\\" in url
        or "%" in parsed.netloc
    ):
        return None
    decoded = unquote(parsed.path)
    if any(part in {".", ".."} for part in decoded.split("/")) or re.search(
        r"%2f|%5c|%25|%3f|%23|%0[0-9a-f]|%1[0-9a-f]|%7f", parsed.path, re.I
    ):
        return None
    return parsed


def origin(url: SplitResult) -> tuple[str, str | None, int]:
    port = url.port or (443 if url.scheme == "https" else 80)
    return url.scheme, url.hostname, port


def matches_prefix(url: str, prefix: str) -> bool:
    if not url.startswith(prefix):
        return False
    return len(url) == len(prefix) or prefix.endswith("/") or url[len(prefix)] in "/?"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class RegistryEntry(_Model):
    entry_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    name: str
    role: Literal["primary", "crosscheck"]
    authoritative: bool
    base_domains: list[str] = Field(min_length=1)
    url_prefixes: list[str] = Field(min_length=1)
    doc_types: list[str] = Field(min_length=1)
    access: AccessMethod
    requires_login: bool
    rate_limit: RateLimit
    robots_respect: bool
    allowed_action_classes: list[Literal["READ", "SEARCH", "LOGIN", "SPEND", "UPLOAD", "COMMIT"]]
    terms_verified: bool
    terms_url: str | None = None
    terms_checked_on: date | None = None
    phase: Literal["MVP", "P6", "optional"]

    @model_validator(mode="after")
    def validate_policy(self) -> RegistryEntry:
        if self.terms_verified and (
            self.terms_url is None
            or safe_url(self.terms_url) is None
            or self.terms_checked_on is None
        ):
            raise ValueError("Verified terms require a terms URL and review date")
        for prefix in self.url_prefixes:
            parsed = safe_url(prefix)
            if (
                parsed is None
                or "?" in prefix
                or not {parsed.hostname, parsed.netloc}.intersection(self.base_domains)
            ):
                raise ValueError("A registry prefix must use a listed domain and a plain path")
        return self


class Registry(_Model):
    version: Literal[1]
    entries: list[RegistryEntry]

    @model_validator(mode="after")
    def validate_unique_entries(self) -> Registry:
        ids = [entry.entry_id for entry in self.entries]
        prefixes = [prefix for entry in self.entries for prefix in entry.url_prefixes]
        if len(set(ids)) != len(ids) or len(set(prefixes)) != len(prefixes):
            raise ValueError("Registry IDs and prefixes must be unique")
        return self

    def match_url(self, url: str) -> RegistryEntry | None:
        parsed = safe_url(url)
        if parsed is None:
            return None
        matches = (
            (len(prefix), entry)
            for entry in self.entries
            for prefix in entry.url_prefixes
            if matches_prefix(url, prefix) and origin(parsed) == origin(urlsplit(prefix))
        )
        best = max(matches, key=lambda item: item[0], default=None)
        return best[1] if best else None


def load_registry(path: Path) -> Registry:
    try:
        from gba_contracts.registry import load_registry as shared_load  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        if exc.name != "gba_contracts":
            raise
        return Registry.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    return cast(Registry, shared_load(path))


def load_site_metadata(registry_path: Path, entry_id: str) -> dict[str, object]:
    if re.fullmatch(r"[a-zA-Z0-9_-]+", entry_id) is None:
        raise APIError(500, "INTERNAL_ERROR", "Registry entry identifier is invalid")
    try:
        data = yaml.safe_load(
            (registry_path.parent / "sites" / f"{entry_id}.yaml").read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, yaml.YAMLError):
        raise APIError(500, "INTERNAL_ERROR", "Site access policy is invalid") from None
    if data is None:
        return {}
    if not isinstance(data, dict) or not all(isinstance(key, str) for key in data):
        raise APIError(500, "INTERNAL_ERROR", "Site access policy is invalid")
    return data
