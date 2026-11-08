"""Per-registry-entry token buckets and request slots."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from random import uniform
from time import monotonic

from gba_agent.api.errors import APIError


@dataclass(frozen=True)
class RateLimit:
    requests_per_second: float
    burst: int
    max_concurrent: int

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.requests_per_second)
            or min(self.requests_per_second, self.burst, self.max_concurrent) <= 0
        ):
            raise ValueError("Registry rate limits must be finite and positive")


@dataclass
class _Bucket:
    policy: RateLimit
    updated_at: float
    tokens: float = field(init=False)
    penalty_until: float = -math.inf
    blocked_until: float = -math.inf
    slots: asyncio.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        self.tokens = float(self.policy.burst)
        self.slots = asyncio.Semaphore(self.policy.max_concurrent)

    def refill(self, now: float) -> None:
        elapsed = now - self.updated_at
        penalized = max(0.0, min(now, self.penalty_until) - self.updated_at)
        self.tokens = min(
            self.policy.burst,
            self.tokens + (elapsed - penalized / 2) * self.policy.requests_per_second,
        )
        self.updated_at = now


class RateLimiter:
    """Acquire a token and slot together. Release the slot when the request ends."""

    def __init__(
        self,
        limits: Mapping[str, RateLimit],
        *,
        clock: Callable[[], float] = monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = uniform,
    ) -> None:
        self._buckets = {entry: _Bucket(limit, clock()) for entry, limit in limits.items()}
        self._clock, self._utcnow, self._sleep, self._jitter = clock, utcnow, sleep, jitter

    def _bucket(self, entry_id: str) -> _Bucket:
        bucket = self._buckets.get(entry_id)
        if bucket is None:
            raise APIError(403, "SG_URL_NOT_IN_REGISTRY", "Unknown registry entry")
        return bucket

    async def acquire(self, entry_id: str) -> None:
        bucket = self._bucket(entry_id)
        await bucket.slots.acquire()
        acquired = False
        try:
            while True:
                now = self._clock()
                bucket.refill(now)
                cooldown = bucket.blocked_until - now
                if bucket.tokens >= 1 and cooldown <= 0:
                    bucket.tokens -= 1
                    acquired = True
                    return
                wait = max(0.0, 1 - bucket.tokens) / bucket.policy.requests_per_second
                wait += min(wait, max(0.0, bucket.penalty_until - now) / 2)
                await self._sleep(max(wait, cooldown))
        finally:
            if not acquired:
                bucket.slots.release()

    def release(self, entry_id: str) -> None:
        self._bucket(entry_id).slots.release()

    @asynccontextmanager
    async def slot(self, entry_id: str) -> AsyncIterator[None]:
        await self.acquire(entry_id)
        try:
            yield
        finally:
            self.release(entry_id)

    def record_response(
        self, entry_id: str, status: int, *, retry_after: str | None = None, attempt: int = 1
    ) -> float:
        """Apply a shared cooldown and return the wait in seconds for a 429."""
        bucket = self._bucket(entry_id)
        if status not in {403, 429}:
            return 0.0
        now = self._clock()
        bucket.refill(now)
        bucket.penalty_until = now + 300
        if status == 403:
            return 0.0
        delay = self._retry_after(retry_after)
        if delay is None:
            delay = self.backoff(attempt)
        bucket.blocked_until = max(bucket.blocked_until, now + delay)
        return bucket.blocked_until - now

    def backoff(self, attempt: int) -> float:
        return float(2 ** min(3, max(1, attempt))) * self._jitter(0.8, 1.2)

    def _retry_after(self, value: str | None) -> float | None:
        if value is None:
            return None
        value = value.strip()
        if value.isascii() and value.isdecimal():
            digits = value.lstrip("0") or "0"
            return float(min(60, int(digits[:2]))) if len(digits) <= 2 else 60.0
        try:
            date = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        return min(60.0, max(0.0, (date - self._utcnow()).total_seconds()))
