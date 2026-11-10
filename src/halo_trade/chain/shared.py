"""Market-level reads that every wallet needs, made once and shared.

A wallet's account is never here: an account read in the money path is always its own, always fresh.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Generic, Optional, TypeVar

from .base import Candle, ChainBackend, MarketSnapshot

T = TypeVar("T")

# Well inside MARKET_MAX_AGE_MS. Counted from when the read started, so it is the data's real age.
SNAPSHOT_TTL_MS = 1_500
HOUR_MS = 3_600_000
QUARTER_MS = 900_000
# The newest closed candle can appear a little after its bar ends, and an hour with no trades has none.
CANDLE_RETRY_MS = 30_000


def wall_clock_ms() -> int:
    return int(time.time() * 1000)


class SingleFlight(Generic[T]):
    """At most one read at a time, whose result or failure every waiter shares. Failures are not kept."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task[T]] = None

    async def run(self, read: Callable[[], Awaitable[T]]) -> T:
        if self._task is None:
            self._task = asyncio.ensure_future(read())
            self._task.add_done_callback(self._finished)
        # Shielded: one caller's deadline must not cancel a read others are waiting on.
        return await asyncio.shield(self._task)

    def _finished(self, task: "asyncio.Task[T]") -> None:
        self._task = None
        if not task.cancelled():
            task.exception()  # marks it seen, in case every waiter has gone


class SharedValue(Generic[T]):
    """One value, read at most once per `ttl_ms`."""

    def __init__(self, ttl_ms: int, clock_ms: Callable[[], int] = wall_clock_ms) -> None:
        self._ttl_ms = ttl_ms
        self._clock_ms = clock_ms
        self._flight: SingleFlight[T] = SingleFlight()
        self._value: Optional[T] = None
        self._at_ms = 0

    async def get(self, read: Callable[[], Awaitable[T]]) -> T:
        if self._value is not None and 0 <= self._clock_ms() - self._at_ms < self._ttl_ms:
            return self._value

        async def fresh() -> T:
            started = self._clock_ms()
            value = await read()
            self._value, self._at_ms = value, started
            return value

        return await self._flight.run(fresh)


class SharedCandles:
    """Closed candles for one bar length, kept until a new one can exist. Every read asks for the
    longest list any caller has wanted, so a new bar costs one request however many sizes follow."""

    def __init__(self, read: Callable[[int], Awaitable[list[Candle]]], period_ms: int, clock_ms: Callable[[], int] = wall_clock_ms) -> None:
        self._read = read
        self._period_ms = period_ms
        self._clock_ms = clock_ms
        self._flight: SingleFlight[list[Candle]] = SingleFlight()
        self._candles: Optional[list[Candle]] = None
        self._limit = 0
        self._widest = 0
        self._bar = -1
        self._complete = False
        self._retry_at_ms = 0

    def _usable(self, limit: int, now: int) -> bool:
        if self._candles is None or limit > self._limit or now // self._period_ms != self._bar:
            return False
        return self._complete or now < self._retry_at_ms

    async def get(self, limit: int) -> list[Candle]:
        self._widest = max(self._widest, limit)
        if self._usable(limit, self._clock_ms()):
            return self._window(self._candles, limit)  # type: ignore[arg-type]
        candles = await self._flight.run(self._fresh)
        if self._limit < limit:  # joined a shorter read that was already under way
            candles = await self._flight.run(self._fresh)
        return self._window(candles, limit)

    def _window(self, candles: list[Candle], limit: int) -> list[Candle]:
        """Only what a read of `limit` bars would have found. Without this, a market with holes in it
        would reach back weeks through the longest read and still look like `limit` recent bars."""
        since = self._clock_ms() - (limit + 2) * self._period_ms
        return [candle for candle in candles if candle.open_ms >= since][-limit:]

    async def _fresh(self) -> list[Candle]:
        now = self._clock_ms()
        limit = self._widest
        candles = await self._read(limit)
        bar_start = now - now % self._period_ms
        closed = [candle.open_ms for candle in candles if candle.open_ms < bar_start]
        self._candles, self._limit, self._bar = candles, limit, now // self._period_ms
        self._complete = bool(closed) and max(closed) == bar_start - self._period_ms
        self._retry_at_ms = now + CANDLE_RETRY_MS
        return candles


class SharedReads:
    """Wraps a chain backend so its market-level reads are shared. Everything else passes straight through."""

    def __init__(self, inner: ChainBackend, *, clock_ms: Callable[[], int] = wall_clock_ms) -> None:
        self._inner = inner
        self._snapshot: SharedValue[MarketSnapshot] = SharedValue(SNAPSHOT_TTL_MS, clock_ms)
        self._candles_1h = SharedCandles(inner.candles_1h, HOUR_MS, clock_ms)
        self._candles_15m = SharedCandles(inner.candles_15m, QUARTER_MS, clock_ms)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def snapshot(self) -> MarketSnapshot:
        return await self._snapshot.get(self._inner.snapshot)

    async def candles_1h(self, limit: int) -> list[Candle]:
        return await self._candles_1h.get(limit)

    async def candles_15m(self, limit: int) -> list[Candle]:
        return await self._candles_15m.get(limit)
