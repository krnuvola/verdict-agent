"""Internal HTTP transport for Fetcher. Every hop requires its policy gate."""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic
from urllib.parse import SplitResult, urljoin, urlsplit

import httpx

from gba_agent.access._registry import RegistryEntry, origin, safe_url
from gba_agent.access.rate_limiter import RateLimiter
from gba_agent.api.errors import APIError
from gba_agent.config import Settings

_REDIRECTS = {301, 302, 303, 307, 308}
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_ESCAPED = re.compile(r"%[0-9a-fA-F]{2}")


def _robots_octets(text: str) -> str:
    # Decode unreserved escapes only. Encoded delimiters retain their meaning.
    encoded = "".join(
        char if char.isascii() else "".join(f"%{byte:02X}" for byte in char.encode())
        for char in text
    )
    return _ESCAPED.sub(
        lambda m: chr(int(m[0][1:], 16)) if chr(int(m[0][1:], 16)) in _UNRESERVED else m[0].upper(),
        encoded,
    )


@dataclass(frozen=True)
class _RobotsRule:
    allowed: bool
    length: int
    parts: tuple[str, ...]
    exact: bool

    def matches(self, path: str) -> bool:
        first, *rest = self.parts
        if not rest:
            return path == first if self.exact else path.startswith(first)
        if not path.startswith(first):
            return False
        start, end = len(first), len(path)
        if self.exact:
            last = rest.pop()
            if not path.endswith(last):
                return False
            end -= len(last)
        for part in rest:
            index = path.find(part, start, end)
            if index == -1:
                return False
            start = index + len(part)
        return start <= end


@dataclass(frozen=True)
class _Robots:
    rules: tuple[_RobotsRule, ...] = ()
    deny_all: bool = False

    @classmethod
    def parse(cls, text: str, product: str) -> _Robots:
        groups: list[tuple[set[str], list[tuple[bool, str]]]] = []
        agents: set[str] = set()
        rules: list[tuple[bool, str]] = []
        saw_rule = False
        for line in text.removeprefix("\ufeff").splitlines():
            name, _, value = line.partition("#")[0].partition(":")
            name, value = name.strip().lower(), value.strip()
            if name == "user-agent":
                if saw_rule:
                    groups.append((agents, rules))
                    agents, rules, saw_rule = set(), [], False
                agents.add(value.casefold())
            elif agents and name in {"allow", "disallow"}:
                saw_rule = True
                if value.startswith("/"):
                    rules.append((name == "allow", value))
        groups.append((agents, rules))
        selected = product if any(product in agents for agents, _ in groups) else "*"
        patterns = []
        for agents, rules in groups:
            if selected not in agents:
                continue
            for allowed, rule in rules:
                rule = _robots_octets(rule)
                exact = rule.endswith("$")
                if exact:
                    rule = rule[:-1]
                patterns.append(
                    _RobotsRule(allowed, len(rule.replace("*", "")), tuple(rule.split("*")), exact)
                )
        return cls(tuple(patterns))

    def permits(self, target: SplitResult) -> bool:
        if self.deny_all:
            return False
        path = target.path or "/"
        if target.query:
            path += "?" + target.query
        normalized = _robots_octets(path)
        _, allowed = max(
            ((rule.length, rule.allowed) for rule in self.rules if rule.matches(normalized)),
            default=(0, True),
        )
        return allowed


class PoliteClient:
    def __init__(
        self,
        settings: Settings,
        limiter: RateLimiter,
        authorize: Callable[[str], RegistryEntry],
        *,
        fault_proxy_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._authorize, self._limiter, self._clock = authorize, limiter, clock
        self.observe: Callable[[dict[str, object]], None] | None = None
        self._product = re.split(r"[/\s]", settings.agent_user_agent.strip(), maxsplit=1)[0].lower()
        self._user_agent = settings.agent_user_agent
        self._courtlistener_token = settings.courtlistener_token
        self._transport = transport
        if fault_proxy_url is not None and safe_url(fault_proxy_url) is None:
            raise ValueError("The configured fault proxy must be an HTTP(S) URL")
        if fault_proxy_url is not None and transport is not None:
            raise ValueError(
                "An injected HTTP transport cannot override the configured fault proxy"
            )
        self._proxy = fault_proxy_url
        self._tls = ssl.create_default_context()
        if fault_proxy_url is not None and settings.gba_ca_bundle is not None:
            self._tls.load_verify_locations(cafile=str(settings.gba_ca_bundle))
        self._robots: dict[tuple[str, str | None, int], tuple[float, _Robots]] = {}
        self._robots_locks: dict[tuple[str, str | None, int], asyncio.Lock] = {}

    def _entry_for_hop(self, url: str, entry: RegistryEntry) -> None:
        if self._authorize(url).entry_id != entry.entry_id:
            raise APIError(403, "SG_URL_NOT_IN_REGISTRY", "Redirect leaves the selected source")
        if self._authorize(str(httpx.URL(url))).entry_id != entry.entry_id:
            raise APIError(
                403, "SG_URL_NOT_IN_REGISTRY", "Normalized URL leaves the selected source"
            )

    async def _send(self, url: str, entry: RegistryEntry, *, robots: bool) -> httpx.Response:
        async with self._limiter.slot(entry.entry_id):
            return await self._exchange(url, entry, robots=robots)

    async def _exchange(
        self,
        url: str,
        entry: RegistryEntry,
        *,
        robots: bool = False,
        method: str = "GET",
        request_headers: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers = {**(request_headers or {}), "User-Agent": self._user_agent}
        parsed = urlsplit(url)
        if (
            not robots
            and entry.entry_id == "courtlistener"
            and self._courtlistener_token
            and origin(parsed) == ("https", "www.courtlistener.com", 443)
            and parsed.path.startswith("/api/rest/")
        ):
            headers["Authorization"] = "Token " + self._courtlistener_token.get_secret_value()
        async with httpx.AsyncClient(
            verify=self._tls,
            proxy=self._proxy,
            transport=self._transport,
            trust_env=False,
            follow_redirects=False,
            timeout=30.0,
        ) as client:
            suppress = _suppress_request_logs.set(True)
            started = self._clock()

            def record(phase: str, status: int | str) -> None:
                if self.observe:
                    self.observe(
                        {
                            "url": url,
                            "phase": phase,
                            "status": status,
                            "elapsed_ms": (self._clock() - started) * 1000,
                        }
                    )

            try:
                record("started", "pending")
                response = await client.request(method, url, headers=headers, data=form)
                record("finished", response.status_code)
                return response
            except BaseException:
                record("failed", "error")
                raise
            finally:
                _suppress_request_logs.reset(suppress)

    async def _citation_lookup(
        self,
        url: str,
        entry: RegistryEntry,
        citation: str,
    ) -> httpx.Response | None:
        if (
            entry.entry_id != "courtlistener"
            or url != "https://www.courtlistener.com/api/rest/v4/citation-lookup/"
        ):
            raise APIError(403, "SG_URL_NOT_IN_REGISTRY", "Citation operation is not registered")
        self._entry_for_hop(url, entry)
        if not await self._allowed_by_robots(url, entry):
            return None
        async with self._limiter.slot(entry.entry_id):
            return await self._exchange(url, entry, method="POST", form={"text": citation})

    async def _get(
        self,
        url: str,
        entry: RegistryEntry,
        *,
        robots: bool = False,
    ) -> httpx.Response | None:
        seen: set[str] = set()
        for _ in range(6):
            self._entry_for_hop(url, entry)
            if url in seen:
                break
            seen.add(url)
            if not robots:
                if urlsplit(url).path == "/robots.txt":
                    raise APIError(403, "SOURCE_UNRETRIEVABLE", "Robots policy is not a document")
                if not await self._allowed_by_robots(url, entry):
                    return None
            response = await self._send(url, entry, robots=robots)
            if response.status_code not in _REDIRECTS:
                return response
            location = response.headers.get("location")
            if not location or any(ord(char) <= 32 or ord(char) == 127 for char in location):
                break
            destination = urljoin(url, location)
            before, after = urlsplit(url), safe_url(destination)
            if after is None or (before.scheme == "https" and after.scheme != "https"):
                break
            if robots and origin(before) != origin(after):
                break
            url = destination
        raise APIError(503, "SOURCE_UNRETRIEVABLE", "Redirect could not be verified")

    async def _allowed_by_robots(self, url: str, entry: RegistryEntry) -> bool:
        parsed = urlsplit(url)
        key = origin(parsed)
        async with self._robots_locks.setdefault(key, asyncio.Lock()):
            cached = self._robots.get(key)
            if cached is None or cached[0] <= self._clock():
                robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
                try:
                    response = await self._get(robots_url, entry, robots=True)
                except APIError:
                    return False
                assert response is not None
                if response.status_code == 403:
                    self._limiter.record_response(entry.entry_id, 403)
                if response.status_code in {404, 410}:
                    policy = _Robots()
                elif (
                    response.status_code == 200
                    and len(response.content) <= 512_000
                    and not response.content.lstrip().startswith(b"<")
                ):
                    policy = _Robots.parse(
                        response.content.decode("utf-8", "replace"), self._product
                    )
                elif response.status_code == 429:
                    raise _RobotsThrottle(response.headers.get("retry-after"))
                else:
                    policy = _Robots(deny_all=True)
                cached = self._clock() + 86400, policy
                self._robots[key] = cached
            return cached[1].permits(parsed)


class _RobotsThrottle(Exception):
    def __init__(self, retry_after: str | None) -> None:
        self.retry_after = retry_after


_suppress_request_logs = ContextVar("suppress_external_request_logs", default=False)


class _NoHttpRequestLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _suppress_request_logs.get()


# HTTPX and httpcore debug messages can contain URLs or request headers.
for _name in (
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
):
    logging.getLogger(_name).addFilter(_NoHttpRequestLogs())
