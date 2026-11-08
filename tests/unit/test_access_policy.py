"""Offline access block and limiter contract tests."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from gba_agent.access.block_detector import BlockDetector, DOMSignals, ResponseInfo
from gba_agent.access.rate_limiter import RateLimit, RateLimiter
from gba_agent.api.errors import APIError

RESPONSE = ResponseInfo(
    200,
    {},
    "https://example.test/doc",
    "https://example.test/doc",
    "application/pdf",
    "",
    DOMSignals(),
    False,
)


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (
            replace(RESPONSE, robots_disallowed=True, timed_out=True, status=429),
            "robots_disallowed",
        ),
        (replace(RESPONSE, timed_out=True, status=429), "timeout"),
        (replace(RESPONSE, status=429, body_sample="access denied"), "http_429"),
        (replace(RESPONSE, status=402), "paywall"),
        (replace(RESPONSE, body_sample="Subscribe to continue"), "paywall"),
        (replace(RESPONSE, body_sample="purchase this document"), "paywall"),
        (replace(RESPONSE, body_sample="log in to view full text"), "paywall"),
        (
            replace(
                RESPONSE,
                body_sample="log in to view full text",
                dom_signals=DOMSignals(login_element=True),
            ),
            None,
        ),
        (
            replace(
                RESPONSE,
                status=403,
                dom_signals=DOMSignals(iframe_sources=("https://widget.test/turnstile",)),
            ),
            "captcha",
        ),
        (
            replace(
                RESPONSE,
                status=401,
                dom_signals=DOMSignals(visible_text="Please verify you are human"),
            ),
            "captcha",
        ),
        (replace(RESPONSE, status=401, body_sample="Unusual traffic"), "captcha"),
        (replace(RESPONSE, status=401), "login_wall"),
        (
            replace(RESPONSE, final_url="https://example.test/login", had_session=True),
            "session_expired",
        ),
        (replace(RESPONSE, dom_signals=DOMSignals(password_input=True)), "login_wall"),
        (
            replace(
                RESPONSE,
                requested_url="https://example.test/login",
                dom_signals=DOMSignals(password_input=True),
            ),
            None,
        ),
        (replace(RESPONSE, status=403), "http_403"),
        (replace(RESPONSE, body_sample="Undeclared Automated Tool"), "http_403"),
        (replace(RESPONSE, body_sample="Access denied"), "http_403"),
        (replace(RESPONSE, body_sample="Request blocked"), "http_403"),
        (replace(RESPONSE, body_sample="Automated requests"), "http_403"),
        (
            replace(
                RESPONSE,
                requested_document=True,
                media_type="text/html; charset=utf-8",
                headers={"Content-Length": "1000"},
            ),
            "silent_content_change",
        ),
        (replace(RESPONSE, content_length=9), "silent_content_change"),
        (replace(RESPONSE, body_hash="changed"), "silent_content_change"),
        (replace(RESPONSE, body_hash="known", headers={"Content-Length": "unknown"}), None),
        (replace(RESPONSE, body_sample="a" * 8192 + "Access denied"), None),
        (RESPONSE, None),
    ],
)
def test_block_precedence(response: ResponseInfo, kind: str | None) -> None:
    detector = BlockDetector()
    event = detector.detect(
        response,
        login_url_patterns=[r"/login$"],
        minimum_content_length=10,
        immutable_hash="known",
    )
    assert (event.kind if event else None) == kind
    assert sum(detector.counts.values()) == int(kind is not None)


def test_block_trajectory_has_no_untrusted_body_or_query() -> None:
    emitted = []
    detector = BlockDetector(lambda *event: emitted.append(event))
    event = detector.detect(
        replace(
            RESPONSE,
            requested_url="https://user:pass@example.test/doc?token=secret#fragment",
            body_sample="access denied raw private text",
            headers={"Cookie": "private cookie"},
        ),
        run_id="run_id",
        attempt=2,
        action_taken="retry",
    )
    assert event is not None
    assert event.evidence == {"signature": "access denied"}
    assert emitted == [
        (
            "run_id",
            "block",
            {
                "url": "https://example.test/doc",
                "kind": "http_403",
                "attempt": 2,
                "action_taken": "retry",
            },
        )
    ]


@pytest.mark.asyncio
async def test_rate_refill_and_independent_entries() -> None:
    now = 0.0
    waits = []

    async def sleep(seconds: float) -> None:
        nonlocal now
        waits.append(seconds)
        now += seconds

    limiter = RateLimiter(
        {"a": RateLimit(2, 2, 1), "b": RateLimit(2, 1, 1)}, clock=lambda: now, sleep=sleep
    )
    for entry in ("a", "a", "b", "a"):
        async with limiter.slot(entry):
            pass
    assert waits == [0.5]
    with pytest.raises(APIError) as error:
        await limiter.acquire("unregistered")
    assert error.value.code == "SG_URL_NOT_IN_REGISTRY"


@pytest.mark.asyncio
async def test_concurrency_and_cancelled_acquire_do_not_leak_slots() -> None:
    sleeping = asyncio.Event()

    async def sleep(_: float) -> None:
        sleeping.set()
        await asyncio.Future[None]()

    limiter = RateLimiter({"a": RateLimit(1, 1, 1)}, clock=lambda: 0, sleep=sleep)
    async with limiter.slot("a"):
        pending = asyncio.create_task(limiter.acquire("a"))
        await asyncio.sleep(0)
        assert not sleeping.is_set()
    await asyncio.wait_for(sleeping.wait(), timeout=1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    limiter._buckets["a"].tokens = 1
    await asyncio.wait_for(limiter.acquire("a"), timeout=1)
    limiter.release("a")


@pytest.mark.asyncio
async def test_403_penalty_and_429_retry_after() -> None:
    now = 0.0
    waits = []

    async def sleep(seconds: float) -> None:
        nonlocal now
        waits.append(seconds)
        now += seconds

    limiter = RateLimiter(
        {"a": RateLimit(1, 1, 1)},
        clock=lambda: now,
        sleep=sleep,
        utcnow=lambda: datetime(2026, 11, 7, tzinfo=UTC),
        jitter=lambda low, high: (low + high) / 2,
    )
    await limiter.acquire("a")
    limiter.release("a")
    assert limiter.record_response("a", 200) == 0
    assert limiter.record_response("a", 403) == 0
    async with limiter.slot("a"):
        assert waits == [2]
        assert limiter.record_response("a", 429, retry_after="0005") == 5
    async with limiter.slot("a"):
        assert waits == [2, 5]
    now = 400
    async with limiter.slot("a"):
        pass
    async with limiter.slot("a"):
        assert waits == [2, 5, 1]
    assert limiter.record_response("a", 429, retry_after="nonsense", attempt=2) == 4
    assert limiter.record_response("a", 429, attempt=3) == 8
    assert limiter.record_response("a", 429, retry_after="Sat, 07 Nov 2026 00:00:30 GMT") == 30
    assert limiter.record_response("a", 429, retry_after="999") == 60


def test_invalid_rate_configuration() -> None:
    with pytest.raises(ValueError, match="positive"):
        RateLimit(float("nan"), 1, 1)
    with pytest.raises(ValueError, match="positive"):
        RateLimit(1, 1, 0)
