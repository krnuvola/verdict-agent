"""Deterministic classification of access blocks, without retaining page text."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

BlockKind = Literal[
    "http_403",
    "http_429",
    "captcha",
    "login_wall",
    "session_expired",
    "silent_content_change",
    "robots_disallowed",
    "timeout",
    "paywall",
]
BlockAction = Literal["retry", "switch_source", "mark_unretrievable", "handoff"]
EventSink = Callable[[str, str, dict[str, object]], None]


@dataclass(frozen=True)
class DOMSignals:
    iframe_sources: Sequence[str] = field(default_factory=tuple, repr=False)
    visible_text: str = field(default="", repr=False)
    login_element: bool = False
    password_input: bool = False


class _HTMLSignals(HTMLParser):
    def __init__(self, sample: str) -> None:
        super().__init__()
        self.frames: list[str] = []
        self.password = False
        self.feed(sample)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "iframe" and (source := attributes.get("src")):
            self.frames.append(source)
        if tag == "input" and (attributes.get("type") or "").lower() == "password":
            self.password = True


def html_signals(sample: str, media_type: str) -> DOMSignals:
    if media_type not in {"text/html", "application/xhtml+xml"}:
        return DOMSignals()
    parsed = _HTMLSignals(sample)
    return DOMSignals(iframe_sources=parsed.frames, password_input=parsed.password)


@dataclass(frozen=True)
class ResponseInfo:
    status: int | None
    headers: Mapping[str, str] = field(repr=False)
    final_url: str = field(repr=False)
    requested_url: str = field(repr=False)
    media_type: str | None
    body_sample: str = field(repr=False)
    dom_signals: DOMSignals = field(repr=False)
    had_session: bool
    robots_disallowed: bool = False
    timed_out: bool = False
    requested_document: bool = False
    content_length: int | None = None
    body_hash: str | None = None


@dataclass(frozen=True)
class BlockEvent:
    kind: BlockKind
    evidence: dict[str, str | int]


_PAYWALL = ("subscribe to continue", "purchase this document", "log in to view full text")
_BLOCK_PAGE = (
    "undeclared automated tool",
    "access denied",
    "request blocked",
    "automated requests",
)
_CAPTCHA_FRAME = re.compile(r"recaptcha|hcaptcha|turnstile|captcha", re.IGNORECASE)
_CAPTCHA_TEXT = re.compile(r"verify you are human|unusual traffic", re.IGNORECASE)


class BlockDetector:
    def __init__(self, emit: EventSink | None = None) -> None:
        self.counts: Counter[BlockKind] = Counter()
        self._emit = emit

    def detect(
        self,
        response: ResponseInfo,
        *,
        login_url_patterns: Sequence[str] = (),
        minimum_content_length: int | None = None,
        immutable_hash: str | None = None,
        run_id: str | None = None,
        attempt: int = 1,
        action_taken: BlockAction | Callable[[BlockEvent], BlockAction] = "mark_unretrievable",
    ) -> BlockEvent | None:
        event = self._classify(response, login_url_patterns, minimum_content_length, immutable_hash)
        if event is not None:
            self.counts[event.kind] += 1
            if self._emit is not None and run_id is not None:
                url = urlsplit(response.requested_url)
                self._emit(
                    run_id,
                    "block",
                    {
                        "url": urlunsplit(
                            (url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", "")
                        ),
                        "kind": event.kind,
                        "attempt": attempt,
                        "action_taken": action_taken(event)
                        if callable(action_taken)
                        else action_taken,
                    },
                )
        return event

    @staticmethod
    def _classify(
        response: ResponseInfo,
        patterns: Sequence[str],
        minimum: int | None,
        immutable_hash: str | None,
    ) -> BlockEvent | None:
        if response.robots_disallowed:
            return BlockEvent("robots_disallowed", {})
        if response.timed_out:
            return BlockEvent("timeout", {})
        if response.status == 429:
            return BlockEvent("http_429", {"status": 429})

        sample = response.body_sample.encode()[:8192].decode(errors="ignore").casefold()
        dom = response.dom_signals
        if response.status == 402:
            return BlockEvent("paywall", {"status": 402})
        if not (dom.login_element or dom.password_input):
            for signature in _PAYWALL:
                if signature in sample:
                    return BlockEvent("paywall", {"signature": signature})
        if any(_CAPTCHA_FRAME.search(src) for src in dom.iframe_sources) or (
            _CAPTCHA_TEXT.search(dom.visible_text or sample)
        ):
            return BlockEvent("captcha", {})
        if (
            response.status == 401
            or any(re.search(pattern, response.final_url) for pattern in patterns)
            or (
                dom.password_input
                and not any(re.search(pattern, response.requested_url) for pattern in patterns)
            )
        ):
            return BlockEvent("session_expired" if response.had_session else "login_wall", {})
        if response.status == 403:
            return BlockEvent("http_403", {"status": 403})
        for signature in _BLOCK_PAGE:
            if signature in sample:
                return BlockEvent("http_403", {"signature": signature})

        size = response.content_length
        if size is None:
            for name, value in response.headers.items():
                if name.casefold() == "content-length":
                    if value.isascii() and value.isdecimal() and len(value) <= 19:
                        size = int(value)
                    break
        media_type = (response.media_type or "").split(";", 1)[0].strip().lower()
        if size is not None:
            if response.requested_document and media_type == "text/html" and size < 20 * 1024:
                return BlockEvent("silent_content_change", {"signature": "unexpected_html"})
            if minimum is not None and size < minimum:
                return BlockEvent("silent_content_change", {"signature": "content_below_minimum"})
        if immutable_hash is not None and response.body_hash not in (None, immutable_hash):
            return BlockEvent("silent_content_change", {"signature": "immutable_hash_changed"})
        return None
