"""Fixtures: an in-memory chain, a real SQLite file, and a restartable service."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

import httpx
import pytest

from halo_trade import plugin
from halo_trade.chain.base import Candle, OrderKind
from halo_trade.chain.mock import MockChain, MockClock
from halo_trade.config import WINDOW_MS, Risk, Settings
from halo_trade.context import AppContext
from halo_trade.contracts import CloseIn, PreviewIn, SubmitIn, SubmitOut
from halo_trade.db import connect
from halo_trade.service import Service, User
from halo_trade.watcher import Watcher
from tests import standin

D = Decimal
HOUR_MS = 3_600_000
WINDOW_START = 2_000_000 * WINDOW_MS  # also an exact UTC hour
START_MS = WINDOW_START + 60_000


def rising_candles(now_ms: int, *, mark: str = "60000", step: str = "30", hours: int = 1500, ahead: int = 240) -> list[Candle]:
    """Hourly candles climbing into `mark` at the current hour and on past it, so a test can move its
    clock forward and stay inside the data. The candle range matches the mock's flat candles, so ATR
    and position size are what they were, and the 1h and 4h EMAs both read as up."""
    bar, step_d, half = now_ms - now_ms % HOUR_MS, Decimal(step), Decimal("150")
    candles = []
    for offset in range(-hours, ahead + 1):
        close = Decimal(mark) + step_d * offset
        candles.append(Candle(bar + offset * HOUR_MS, close - step_d, close + half, close - half, close))
    return candles


def falling_candles(now_ms: int, *, mark: str = "60000", step: str = "30", hours: int = 1500, ahead: int = 240) -> list[Candle]:
    """The mirror of `rising_candles`: the same range, closes stepping down instead of up."""
    return rising_candles(now_ms, mark=mark, step=f"-{step}", hours=hours, ahead=ahead)


def set_uptrend(env: "Env") -> None:
    env.chain.set_candles(rising_candles(env.clock()))


@dataclass
class Env:
    tmp: Path
    clock: MockClock
    chain: MockChain
    ctx: AppContext
    svc: Service
    watcher: Watcher
    db_path: str
    users: dict[int, User] = field(default_factory=dict)
    seq: int = 0

    def conn(self):
        return self.ctx.db

    def add_user(self, deposits: str = "100", status: str = "active", granted: bool = True) -> tuple[User, str]:
        self.seq += 1
        idx = self.seq
        inj, sub, token = f"inj1user{idx}", f"0xsub{idx}", f"tok-{idx}"
        self.chain.register_account(inj, sub, D(deposits), granted=granted)
        cursor = self.ctx.db.execute(
            "INSERT INTO users (inj_address, eth_address, subaccount_id, status, token_hash, created_ms) VALUES (?,?,?,?,?,?)",
            (inj, f"0xeth{idx}", sub, status, hashlib.sha256(token.encode()).hexdigest(), self.clock()),
        )
        user = User(int(cursor.lastrowid), inj, sub, status)
        self.users[user.id] = user
        return user, token

    def set_risk(self, user: User, day_start: str, peak: str, cooldown: int = 0, day_id: Optional[int] = None) -> None:
        day = self.clock() // 86_400_000 if day_id is None else day_id
        self.ctx.db.execute(
            "INSERT OR REPLACE INTO user_risk_state (user_id, day_id, day_start_equity, peak_equity, cooldown_until_window) VALUES (?,?,?,?,?)",
            (user.id, day, day_start, peak, cooldown),
        )

    def window(self) -> int:
        return self.clock() // WINDOW_MS

    def next_window(self) -> None:
        self.clock.set((self.window() + 1) * WINDOW_MS + 60_000)

    async def state(self, user: User):
        return await self.svc.state(user)

    async def preview(self, user: User, side: str = "long", stop_atr: float = 2.0, tp: Optional[float] = None, reason: str = ""):
        state = await self.svc.state(user)
        return await self.svc.preview(
            user, PreviewIn(observation_id=state.observation_id, side=side, stop_atr=stop_atr, take_profit_atr=tp, reason=reason)
        )

    async def submit_ticket(self, user: User, ticket: str) -> SubmitOut:
        return await self.svc.submit(user, SubmitIn(ticket=ticket))

    async def enter(self, user: User, side: str = "long", stop_atr: float = 2.0, tp: Optional[float] = None) -> SubmitOut:
        preview = await self.preview(user, side, stop_atr, tp)
        return await self.submit_ticket(user, preview.ticket)

    async def close(self, user: User, reason: str = "") -> SubmitOut:
        return await self.svc.close(user, CloseIn(reason=reason))

    async def tick(self, count: int = 1) -> None:
        for _ in range(count):
            await self.watcher.tick()

    async def settle(self) -> None:
        """Let a position that vanished on chain finalize: two flat reads at increasing heights."""
        await self.tick()
        self.chain.advance_block()
        await self.tick()

    def restart(self) -> None:
        """Simulate process death: new connection, service and watcher over the same DB and chain."""
        self.ctx.db.close()
        ctx = AppContext(settings=self.ctx.settings, db=connect(self.db_path), chain=self.chain, clock=self.clock)
        self.ctx = ctx
        self.svc = make_service(ctx)
        self.watcher = Watcher(self.svc)

    def intent(self, user: User):
        return self.ctx.db.execute("SELECT * FROM intents WHERE user_id=? ORDER BY id DESC LIMIT 1", (user.id,)).fetchone()

    def intents(self, user: User):
        return self.ctx.db.execute("SELECT * FROM intents WHERE user_id=? ORDER BY id", (user.id,)).fetchall()

    def txs(self, user: Optional[User] = None, purpose: Optional[str] = None):
        sql, params = "SELECT * FROM txs WHERE 1=1", []
        if user is not None:
            sql += " AND user_id=?"
            params.append(user.id)
        if purpose is not None:
            sql += " AND purpose=?"
            params.append(purpose)
        return self.ctx.db.execute(sql + " ORDER BY id", params).fetchall()

    def trades(self, user: User):
        return self.ctx.db.execute("SELECT * FROM trades WHERE user_id=? ORDER BY id", (user.id,)).fetchall()

    def alerts(self, kind: Optional[str] = None):
        sql = "SELECT * FROM alerts" + (" WHERE kind=?" if kind else "")
        return self.ctx.db.execute(sql, (kind,) if kind else ()).fetchall()

    def risk(self, user: User):
        return self.ctx.db.execute("SELECT * FROM user_risk_state WHERE user_id=?", (user.id,)).fetchone()

    def position(self, user: User):
        return self.chain.sub(user.subaccount_id).position

    def orders(self, user: User):
        return self.chain.sub(user.subaccount_id).orders

    def signed_orders(self):
        """(kinds in tx) for every tx ever signed, in order."""
        return [[o.kind for o in entry.orders] for entry in self.chain._signed.values()]

    def stop_orders(self, user: User):
        return [o for o in self.orders(user) if o.kind is OrderKind.STOP]


def make_service(ctx: AppContext) -> Service:
    return Service(ctx, submit_budget_s=1.5, poll_interval_s=0.001)


@pytest.fixture(autouse=True)
def an_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests pass with or without the private engine: without it, a stand-in that never decides is used."""
    if plugin.engine() is None:
        monkeypatch.setattr(plugin, "engine", lambda: standin)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    clock = MockClock(START_MS)
    chain = MockChain(clock=clock, auto_mine=True)
    db_path = str(tmp_path / "halo.sqlite")
    # Every wallet read every tick, no cycle skipped for "no change", and no research step, so a
    # scripted LLM is asked only what its test expects: tests of those set them back.
    settings = Settings(
        db_path=db_path, chain_backend="mock", network="testnet", watch_interval_s=1, watch_flat_interval_s=0, skip_move_atr=Decimal(0), research_user_daily_calls=0
    )
    ctx = AppContext(settings=settings, db=connect(db_path), chain=chain, clock=clock)
    svc = make_service(ctx)
    return Env(tmp_path, clock, chain, ctx, svc, Watcher(svc), db_path)


def api_client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
