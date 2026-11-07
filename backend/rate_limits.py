"""Pace every provider request; share persistent cooldowns across SQLite workers."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from math import isfinite
from threading import Lock
from time import time
from typing import Protocol

from .db import Repository
from .evaluator import ProviderDeferred


@dataclass(frozen=True)
class RateDecision:
    delay: float = 0
    retry_at: float | None = None
    quota_exhausted: bool = False


def decide(next_at: float, blocked_until: float, quota: bool, now: float) -> RateDecision:
    if blocked_until > now:
        return RateDecision(retry_at=blocked_until, quota_exhausted=quota)
    return RateDecision(delay=max(0, next_at - now))


class RateStore(Protocol):
    def acquire(self, model: str, clock: Callable[[], float], interval: float) -> RateDecision: ...
    def defer(self, model: str, retry_at: float, quota_exhausted: bool) -> None: ...


class MemoryRateStore:
    """For standalone CLI use; application factories use SQLiteRateStore."""

    def __init__(self) -> None:
        self._states: dict[str, tuple[float, float, bool]] = {}
        self._lock = Lock()

    def acquire(self, model: str, clock: Callable[[], float], interval: float) -> RateDecision:
        with self._lock:
            now = clock()
            next_at, until, quota = self._states.get(model, (0, 0, False))
            result = decide(next_at, until, quota, now)
            if result.retry_at is None and result.delay == 0:
                self._states[model] = (now + interval, until, quota)
            return result

    def defer(self, model: str, retry_at: float, quota_exhausted: bool) -> None:
        with self._lock:
            next_at, until, quota = self._states.get(model, (0, 0, False))
            if retry_at >= until:
                self._states[model] = (
                    next_at,
                    retry_at,
                    quota_exhausted or (retry_at == until and quota),
                )


class SQLiteRateStore:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def acquire(self, model: str, clock: Callable[[], float], interval: float) -> RateDecision:
        with self.repository.connection(write=True) as conn:
            now = clock()
            conn.execute("INSERT OR IGNORE INTO provider_limits(model) VALUES (?)", (model,))
            row = conn.execute("SELECT * FROM provider_limits WHERE model=?", (model,)).fetchone()
            result = decide(row["next_at"], row["blocked_until"], bool(row["quota"]), now)
            if result.retry_at is None and result.delay == 0:
                conn.execute(
                    "UPDATE provider_limits SET next_at=? WHERE model=?", (now + interval, model)
                )
            return result

    def defer(self, model: str, retry_at: float, quota_exhausted: bool) -> None:
        with self.repository.connection(write=True) as conn:
            conn.execute(
                """INSERT INTO provider_limits(model,blocked_until,quota) VALUES (?,?,?)
                   ON CONFLICT(model) DO UPDATE SET blocked_until=excluded.blocked_until,
                   quota=CASE WHEN excluded.blocked_until=provider_limits.blocked_until
                              THEN MAX(excluded.quota,provider_limits.quota) ELSE excluded.quota END
                   WHERE excluded.blocked_until>=provider_limits.blocked_until""",
                (model, retry_at, int(quota_exhausted)),
            )


class RequestLimiter:
    def __init__(
        self,
        store: RateStore | None = None,
        requests_per_minute: float = 3,
        *,
        clock: Callable[[], float] = time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not isfinite(requests_per_minute) or requests_per_minute <= 0:
            raise ValueError("Provider RPM must be finite and positive")
        self.store = store if store is not None else MemoryRateStore()
        self.interval = 60 / requests_per_minute
        self.clock, self.sleep = clock, sleep

    async def wait(self, model: str) -> None:
        while True:
            result = await asyncio.to_thread(self.store.acquire, model, self.clock, self.interval)
            if result.retry_at is not None:
                raise ProviderDeferred(
                    result.retry_at,
                    quota_exhausted=result.quota_exhausted,
                    provider_not_called=True,
                )
            if result.delay == 0:
                return
            # No DB transaction or event-loop blocking IO is held while waiting.
            # Recheck cooldowns published by another worker before claiming a slot.
            await self.sleep(min(result.delay, 1))

    async def defer(self, model: str, retry_at: float, quota_exhausted: bool) -> None:
        await asyncio.to_thread(self.store.defer, model, retry_at, quota_exhausted)
