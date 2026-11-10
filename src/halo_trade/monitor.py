"""One shared loop that turns price moves into earlier decision cycles.

A wake only brings a wallet's next run forward. It never places, changes or closes anything, and
every limit applies to the cycle it schedules. One market read serves every wallet: the per-wallet
checks use only what is already in the database. If this loop fails, schedules simply continue.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Protocol

from . import features as feat
from .chain.base import ChainUnavailable
from .context import AppContext
from .limits import MARKET_MAX_AGE_MS
from .service import Service
from .txmachine import kill_switch_on, transaction

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketPulse:
    mark: Decimal
    atr_1h: Decimal


class PriceSource(Protocol):
    """Polling today. A chain stream can replace it by answering the same question."""

    async def read(self) -> Optional[MarketPulse]:
        """The latest mark and 1h ATR, or None when they cannot be trusted right now."""
        ...


class PollingPrices:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def read(self) -> Optional[MarketPulse]:
        now = self.ctx.clock()
        snapshot = await self.ctx.chain.snapshot()
        if snapshot.mark_price <= 0 or now - snapshot.as_of_ms >= MARKET_MAX_AGE_MS:
            return None
        usable = feat.complete_candles(await self.ctx.chain.candles_1h(feat.CANDLES_TO_REQUEST), now)
        if not usable or now - (usable[-1].open_ms + feat.HOUR_MS) > feat.MAX_CANDLE_AGE_MS:
            return None
        return MarketPulse(snapshot.mark_price, feat.atr(usable, feat.ATR_FAST))


class Monitor:
    def __init__(self, service: Service, source: Optional[PriceSource] = None) -> None:
        self.service = service
        self.ctx = service.ctx
        self.source = source or PollingPrices(service.ctx)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("price monitor tick failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.ctx.settings.monitor_interval_s)
            except TimeoutError:
                pass

    async def tick(self) -> int:
        """Returns how many wallets were woken. Writes nothing unless it wakes someone."""
        if self.service.kill_switch():
            return 0
        try:
            pulse = await self.source.read()
        except (ChainUnavailable, feat.InsufficientData):
            return 0
        if pulse is None or pulse.atr_1h <= 0:
            return 0
        now = self.ctx.clock()
        wakes = [(row["user_id"], reason) for row in self._candidates(now) if (reason := self._reason(row, pulse)) is not None]
        return await self._wake(wakes, now) if wakes else 0

    def _cooled_before(self, now: int) -> int:
        return now - self.ctx.settings.wake_cooldown_s * 1000

    def _candidates(self, now: int) -> list[sqlite3.Row]:
        """A wallet already due needs no wake, and one on cooldown may not have one."""
        return self.ctx.db.execute(
            "SELECT s.user_id, s.last_mark, i.side, i.stop_price FROM strategies s JOIN users u ON u.id=s.user_id "
            "LEFT JOIN intents i ON i.user_id=s.user_id AND i.stage='protected' "
            "WHERE u.status='active' AND s.next_run_ms>? AND (s.last_wake_ms IS NULL OR s.last_wake_ms<=?)",
            (now, self._cooled_before(now)),
        ).fetchall()

    def _reason(self, row: sqlite3.Row, pulse: MarketPulse) -> Optional[str]:
        settings = self.ctx.settings
        if row["stop_price"] is not None:
            stop = Decimal(row["stop_price"])
            room = pulse.mark - stop if row["side"] == "long" else stop - pulse.mark
            if room <= settings.wake_stop_atr * pulse.atr_1h:
                return "near_stop"
        if row["last_mark"] is not None and abs(pulse.mark - Decimal(row["last_mark"])) >= settings.wake_move_atr * pulse.atr_1h:
            return "price_move"
        return None

    async def _wake(self, wakes: list[tuple[int, str]], now: int) -> int:
        woken = 0
        async with self.ctx.db_lock:
            with transaction(self.ctx.db) as conn:
                if kill_switch_on(conn):
                    return 0
                for user_id, reason in wakes:
                    woken += conn.execute(
                        "UPDATE strategies SET next_run_ms=?, wake_reason=?, last_wake_ms=?, updated_ms=? "
                        "WHERE user_id=? AND next_run_ms>? AND (last_wake_ms IS NULL OR last_wake_ms<=?) "
                        "AND EXISTS (SELECT 1 FROM users WHERE id=strategies.user_id AND status='active')",
                        (now, reason, now, now, user_id, now, self._cooled_before(now)),
                    ).rowcount
        return woken
