"""The money path: state, preview, submit, close, and the intent state machine.

Every intent advances only through `advance`, which reads the persisted stage and the `txs`
rows, never in-memory state. A crash at any point resumes from the same code when the watcher
calls `advance` after restart. Lock order is always: per-user lock, then the tx machine's
signer slot, then `ctx.db_lock`. Chain calls never happen while `db_lock` is held.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Optional

from . import alerts, features as feat, limits, notify, sizing, tuning as tunings
from .chain.base import (
    CODE_DUPLICATE_CID,
    urgent_requests,
    Account,
    ChainUnavailable,
    MarketInfo,
    MarketSnapshot,
    OrderKind,
    OrderRefused,
    OrderRequest,
    Position,
    RestingOrder,
    Side,
)
from .config import WINDOW_MS, Risk
from .context import AppContext
from .contracts import (
    AccountOut,
    CloseIn,
    DecisionOut,
    ErrorCode,
    ErrorOut,
    FeaturesOut,
    MarketOut,
    Outcome,
    PositionOut,
    PreviewIn,
    PreviewOut,
    StateOut,
    SubmitIn,
    SubmitOut,
)
from .limits import ApiError, EntryInputs, ObservationView, RiskState, rejected
from .txmachine import KILL_SWITCH_KEY, EntryHalted, SignerBusy, TxMachine, TxRow, kill_switch_on, make_cid, transaction

logger = logging.getLogger(__name__)

SUBMIT_BUDGET_S = 20.0
CHECK_STATE_AFTER_S = 30
DAY_MS = 86_400_000
MAX_CLOSE_ATTEMPTS = 3
CLOSE_RETRY_WINDOW_MS = 60_000  # a repeat /v1/close inside this window buys no new attempts
MAX_STOP_TRIES = 2
EXIT_MATCH_TOLERANCE = Decimal("0.001")

TERMINAL_STAGES = frozenset({"closed", "rejected", "failed", "flattened"})
PRE_PROTECT_STAGES = frozenset({"entry_pending", "entry_sent", "entry_filled", "stop_pending"})
COOLDOWN_REASONS = frozenset({"stop", "stop_trigger_failed"})


@dataclass(frozen=True)
class User:
    id: int
    inj_address: str
    subaccount_id: str
    status: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "User":
        return cls(row["id"], row["inj_address"], row["subaccount_id"], row["status"])


@dataclass(frozen=True)
class Intent:
    id: int
    user_id: int
    window_id: int
    ticket_id: str
    side: Side
    quantity: Decimal
    stop_price: Decimal
    take_profit_price: Optional[Decimal]
    stage: str
    outcome: Optional[str]
    error_code: Optional[str]
    entry_price: Optional[Decimal]
    equity_before: Optional[Decimal]
    result_json: Optional[str]
    created_ms: int
    updated_ms: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Intent":
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            window_id=row["window_id"],
            ticket_id=row["ticket_id"],
            side=Side(row["side"]),
            quantity=Decimal(row["quantity"]),
            stop_price=Decimal(row["stop_price"]),
            take_profit_price=_dec_or_none(row["take_profit_price"]),
            stage=row["stage"],
            outcome=row["outcome"],
            error_code=row["error_code"],
            entry_price=_dec_or_none(row["entry_price"]),
            equity_before=_dec_or_none(row["equity_before"]),
            result_json=row["result_json"],
            created_ms=row["created_ms"],
            updated_ms=row["updated_ms"],
        )


@dataclass
class Gathered:
    """A chain read for one user. `ok` is False if anything could not be read."""

    now_ms: int
    window_id: int
    ok: bool
    market: Optional[MarketInfo] = None
    snapshot: Optional[MarketSnapshot] = None
    account: Optional[Account] = None
    grant_ok: bool = True  # unknown is treated as MARKET_UNAVAILABLE, not GRANT_MISSING
    grant_expires_ms: Optional[int] = None
    gas_ok: bool = True
    features: Optional[feat.Features] = None
    features_15m: Optional[feat.Features15m] = None  # None means they could not be read


def _dec_or_none(value: Optional[str]) -> Optional[Decimal]:
    return None if value is None else Decimal(value)


def window_of(ms: int) -> int:
    return ms // WINDOW_MS


class Service:
    def __init__(
        self,
        ctx: AppContext,
        *,
        submit_budget_s: float = SUBMIT_BUDGET_S,
        poll_interval_s: float = 0.25,
    ) -> None:
        self.ctx = ctx
        self.submit_budget_s = submit_budget_s
        self.poll_interval_s = poll_interval_s
        self.tx = TxMachine(ctx, poll_interval_s=poll_interval_s)
        self._user_locks: dict[int, asyncio.Lock] = {}
        self._last_taker_fee = Decimal(0)
        self._account_heights: dict[int, int] = {}  # newest account read seen per user

    # ------------------------------------------------------------------ basic reads
    @property
    def chain(self):
        return self.ctx.chain

    @property
    def conn(self) -> sqlite3.Connection:
        return self.ctx.db

    def user_lock(self, user_id: int) -> asyncio.Lock:
        return self._user_locks.setdefault(user_id, asyncio.Lock())

    async def _acquire(self, lock: asyncio.Lock, deadline: float) -> bool:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            await asyncio.wait_for(lock.acquire(), timeout=remaining)
        except TimeoutError:
            return False
        return True

    def user(self, user_id: int) -> User:
        row = self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return User.from_row(row)

    def tuning_for(self, user_id: int) -> Optional[tuple[str, dict[str, Any]]]:
        """The wallet's preset and the settings in force for it, or None before it has chosen one."""
        row = self.conn.execute("SELECT preset, tuning FROM strategies WHERE user_id=?", (user_id,)).fetchone()
        if row is None or row["preset"] not in tunings.DEFAULT_TUNING:
            return None
        return row["preset"], tunings.resolve(row["preset"], row["tuning"], self.ctx.settings)

    def risk_for(self, user_id: int) -> Risk:
        """The limits every entry of this wallet is sized and checked against: the operator's, or
        tighter where the wallet chose so. Limits that protect the service itself stay the operator's."""
        chosen = self.tuning_for(user_id)
        operator = self.ctx.settings.risk
        return operator if chosen is None else tunings.risk_of(chosen[1], operator)

    def intent(self, intent_id: int) -> Intent:
        row = self.conn.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone()
        return Intent.from_row(row)

    def intent_by_ticket(self, ticket_id: str) -> Optional[Intent]:
        row = self.conn.execute("SELECT * FROM intents WHERE ticket_id=?", (ticket_id,)).fetchone()
        return Intent.from_row(row) if row else None

    def open_intent(self, user_id: int) -> Optional[Intent]:
        marks = ",".join("?" for _ in TERMINAL_STAGES)
        row = self.conn.execute(
            f"SELECT * FROM intents WHERE user_id=? AND stage NOT IN ({marks}) ORDER BY id DESC LIMIT 1",
            (user_id, *TERMINAL_STAGES),
        ).fetchone()
        return Intent.from_row(row) if row else None

    def _kv_get(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
        return None if row is None else row["value"]

    async def _kv_set(self, key: str, value: str) -> None:
        async with self.ctx.db_lock:
            with transaction(self.conn) as conn:
                conn.execute(
                    "INSERT INTO system_state(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )

    async def _kv_delete(self, key: str) -> None:
        async with self.ctx.db_lock:
            with transaction(self.conn) as conn:
                conn.execute("DELETE FROM system_state WHERE key=?", (key,))

    def kill_switch(self) -> bool:
        return kill_switch_on(self.conn)

    async def set_kill_switch(self, on: bool) -> None:
        await self._kv_set(KILL_SWITCH_KEY, "1" if on else "0")

    # ------------------------------------------------------------------ decision log
    async def _log_decision(self, user_id: int, window_id: int, kind: str, payload: dict[str, Any]) -> None:
        now = self.ctx.clock()
        async with self.ctx.db_lock:
            with transaction(self.conn) as conn:
                conn.execute(
                    "INSERT INTO decisions (user_id, window_id, kind, payload_json, created_ms) VALUES (?,?,?,?,?)",
                    (user_id, window_id, kind, json.dumps(payload, sort_keys=True, default=str), now),
                )

    def _last_decisions(self, user_id: int) -> list[DecisionOut]:
        rows = self.conn.execute(
            "SELECT window_id, kind, payload_json FROM decisions WHERE user_id=? AND kind IN ('preview','submit','close') ORDER BY id DESC LIMIT 5",
            (user_id,),
        ).fetchall()
        out = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            out.append(
                DecisionOut(
                    window_id=row["window_id"],
                    action=row["kind"],
                    side=payload.get("side"),
                    result=str(payload.get("result", "")),
                    # A code only: the agent's free-text reason is never fed back to it (rule 8).
                    reason=str(payload.get("code") or ""),
                )
            )
        return out

    # ------------------------------------------------------------------ chain reads
    def _flat_key(self, user_id: int) -> str:
        return f"flat_seen:{user_id}"

    async def read_account(self, user: User) -> Account:
        """An account read no older than one this service has already acted on."""
        account = await self.chain.account(user.inj_address, user.subaccount_id)
        if account.height < self._account_heights.get(user.id, 0):
            raise ChainUnavailable("account read older than one already seen")
        self._account_heights[user.id] = account.height
        if account.position is not None and self._kv_get(self._flat_key(user.id)) is not None:
            await self._kv_delete(self._flat_key(user.id))
        return account

    async def _position_gone(self, user: User, account: Account, own_tx: Optional[TxRow] = None) -> bool:
        """Whether a read of "no position" can be believed, since one lagging node must not end a
        trade: a read at or past our own landed close, or a second flat read at a higher height."""
        if own_tx is not None and own_tx.status == "ok" and own_tx.height and account.height >= own_tx.height:
            return True
        first = self._kv_get(self._flat_key(user.id))
        if first is None:
            await self._kv_set(self._flat_key(user.id), str(account.height))
            return False
        return account.height > int(first)

    # ------------------------------------------------------------------ gathering inputs
    async def _features_15m(self, now: int) -> Optional[feat.Features15m]:
        """Extra detail for the model. Without it the decision proceeds on hourly data."""
        try:
            return feat.compute_15m(await self.chain.candles_15m(feat.CANDLES_15M_TO_REQUEST), now)
        except (ChainUnavailable, feat.InsufficientData):
            return None

    async def _gather(self, user: User, *, need_features: bool, need_15m: bool = False) -> Gathered:
        now = self.ctx.clock()
        data = Gathered(now_ms=now, window_id=window_of(now), ok=False)
        try:
            market = await self.chain.market()
            snapshot = await self.chain.snapshot()
            if snapshot.best_bid <= 0 or snapshot.best_ask <= 0:
                return data  # an empty side has no price to cross and nothing to exit into
            account = await self.read_account(user)
            grant = await self.chain.check_grant(user.inj_address, user.subaccount_id)
            gas = await self.chain.grantee_balance_inj()
            features = None
            if need_features:
                candles = await self.chain.candles_1h(feat.CANDLES_TO_REQUEST)
                features = feat.compute(candles, now, market.taker_fee, snapshot.best_bid, snapshot.best_ask)
        except (ChainUnavailable, feat.InsufficientData):
            return data
        data.ok = True
        data.market, data.snapshot, data.account, data.features = market, snapshot, account, features
        if need_15m:
            data.features_15m = await self._features_15m(now)
        data.grant_ok = grant.ok
        data.grant_expires_ms = grant.expires_ms
        data.gas_ok = gas >= self.ctx.settings.grantee_min_inj
        return data

    def _risk_row(self, user_id: int) -> Optional[RiskState]:
        row = self.conn.execute("SELECT * FROM user_risk_state WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            return None
        return RiskState(row["day_id"], Decimal(row["day_start_equity"]), Decimal(row["peak_equity"]), row["cooldown_until_window"])

    def _risk_view(self, user_id: int, equity: Decimal, now_ms: int) -> RiskState:
        """Risk state with the peak applied. Never writes and never rolls the day: only a watcher
        snapshot does, or a stalled snapshot would rebase day-start on every read."""
        row = self._risk_row(user_id)
        return limits.advance_risk_state(row, equity, row.day_id if row else now_ms // DAY_MS)

    def _write_risk(self, conn: sqlite3.Connection, user_id: int, state: RiskState) -> None:
        conn.execute(
            "INSERT INTO user_risk_state (user_id, day_id, day_start_equity, peak_equity, cooldown_until_window) VALUES (?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET day_id=excluded.day_id, day_start_equity=excluded.day_start_equity, "
            "peak_equity=excluded.peak_equity, cooldown_until_window=excluded.cooldown_until_window",
            (user_id, state.day_id, str(state.day_start_equity), str(state.peak_equity), state.cooldown_until_window),
        )

    def _window_used(self, user_id: int, window_id: int) -> bool:
        row = self.conn.execute("SELECT 1 FROM intents WHERE user_id=? AND window_id=?", (user_id, window_id)).fetchone()
        return row is not None

    def _observation(self, observation_id: str) -> Optional[ObservationView]:
        row = self.conn.execute("SELECT * FROM observations WHERE id=?", (observation_id,)).fetchone()
        if row is None:
            return None
        return ObservationView(row["user_id"], row["window_id"], row["created_ms"])

    def _make_inputs(
        self,
        user: User,
        data: Gathered,
        observation: Optional[ObservationView],
        side: str,
        stop_atr: Decimal,
        take_profit_atr: Optional[Decimal],
        quantity: Decimal,
        take_profit_price: Optional[Decimal],
        stop_price: Decimal,
    ) -> EntryInputs:
        risk = self.risk_for(user.id)
        snap = data.snapshot
        account = data.account
        market = data.market
        now = data.now_ms
        if data.ok and snap is not None and account is not None and market is not None:
            mark = snap.mark_price
            view = self._risk_view(user.id, account.equity, now)
            notional = quantity * mark
            equity, day_start, peak, cooldown = account.equity, view.day_start_equity, view.peak_equity, view.cooldown_until_window
            has_position = account.position is not None or self.open_intent(user.id) is not None
            as_of: Optional[int] = snap.as_of_ms
            available = account.available
            min_notional = market.min_notional
            depth, exit_depth = (snap.ask_depth_usd, snap.bid_depth_usd) if side == "long" else (snap.bid_depth_usd, snap.ask_depth_usd)
            cost = feat.round_trip_cost_bps(market.taker_fee, snap.best_bid, snap.best_ask)
            tp_bps = limits.take_profit_bps(mark, take_profit_price)
            position_side = Side(side)
            entry_worst = sizing.entry_worst_price(position_side, mark, risk.collar, market.price_tick)
            stop_worst = sizing.stop_worst_price(position_side.opposite, stop_price, risk.stop_worst_pct, market.price_tick)
            worst_loss = sizing.worst_loss(quantity, entry_worst, stop_worst, market.taker_fee)
            stop_worst_notional = quantity * stop_worst
        else:
            notional = Decimal(0)
            equity = day_start = peak = available = min_notional = depth = exit_depth = cost = worst_loss = stop_worst_notional = Decimal(0)
            cooldown = 0
            has_position = False
            as_of = None
            tp_bps = None
        return EntryInputs(
            now_ms=now,
            window_id=data.window_id,
            user_id=user.id,
            kill_switch=self.kill_switch(),
            user_status=user.status,
            grant_ok=data.grant_ok,
            gas_ok=data.gas_ok,
            market_as_of_ms=as_of,
            observation=observation,
            side=side,
            stop_atr=stop_atr,
            take_profit_atr=take_profit_atr,
            has_position=has_position,
            window_used=self._window_used(user.id, data.window_id),
            cooldown_until_window=cooldown,
            equity=equity,
            day_start_equity=day_start,
            peak_equity=peak,
            daily_loss_limit=risk.daily_loss,
            max_drawdown=risk.max_drawdown,
            max_equity_usd=risk.max_equity_usd,
            take_profit_bps=tp_bps,
            round_trip_cost_bps=cost,
            min_tp_cost_mult=risk.min_tp_cost_mult,
            quantity=quantity,
            notional=notional,
            min_notional=min_notional,
            margin_needed=sizing.margin_needed(notional, risk.max_leverage),
            available=available,
            book_depth_usd=depth,
            exit_depth_usd=exit_depth,
            stop_price=stop_price,
            stop_worst_notional=stop_worst_notional,
            worst_loss=worst_loss,
            grant_expires_ms=data.grant_expires_ms,
        )

    def _limit_rejection(self, rejection: limits.Rejection, window_id: int, cooldown: int = 0) -> ApiError:
        retry = cooldown if rejection.code is ErrorCode.COOLDOWN and cooldown else window_id + 1
        return rejected(rejection.code, rejection.limit, retry)

    # ------------------------------------------------------------------ state
    async def state(self, user: User) -> StateOut:
        data = await self._gather(user, need_features=True, need_15m=True)
        if not data.ok or data.snapshot is None or data.account is None or data.market is None or data.features is None:
            await self._log_decision(user.id, data.window_id, "state", {"result": "rejected", "code": ErrorCode.MARKET_UNAVAILABLE.value})
            raise rejected(ErrorCode.MARKET_UNAVAILABLE, None, data.window_id + 1)
        snap, account, market, f = data.snapshot, data.account, data.market, data.features
        view = self._risk_view(user.id, account.equity, data.now_ms)
        inputs = self._make_inputs(user, data, None, "long", Decimal("2"), None, Decimal(0), None, Decimal(0))
        blocked = limits.blocked_reasons(inputs)
        observation_id = secrets.token_urlsafe(24)
        async with self.ctx.db_lock:
            with transaction(self.conn) as conn:
                conn.execute(
                    "INSERT INTO observations (id, user_id, window_id, mark, as_of_ms, created_ms) VALUES (?,?,?,?,?,?)",
                    (observation_id, user.id, data.window_id, str(snap.mark_price), snap.as_of_ms, data.now_ms),
                )
                if self._risk_row(user.id) is None:
                    self._write_risk(conn, user.id, view)
        await self._log_decision(
            user.id,
            data.window_id,
            "state",
            {"result": "ok", "mark": str(snap.mark_price), "equity": str(account.equity), "blocked": [c.value for c in blocked], "atr_1h": str(f.atr_1h), "trend": f.trend},
        )
        return StateOut(
            server_time_ms=data.now_ms,
            window_id=data.window_id,
            next_window_ms=(data.window_id + 1) * WINDOW_MS,
            observation_id=observation_id,
            market=MarketOut(
                ticker=market.ticker,
                mark_price=float(snap.mark_price),
                oracle_price=float(snap.oracle_price),
                best_bid=float(snap.best_bid),
                best_ask=float(snap.best_ask),
                spread_bps=float(f.spread_bps),
                funding_rate_bps=float(snap.funding_rate * 10_000),
            ),
            features=FeaturesOut(
                atr_1h=float(f.atr_1h),
                atr_1h_pct=float(f.atr_1h_pct),
                ema20_1h=float(f.ema20),
                ema50_1h=float(f.ema50),
                trend=f.trend,  # type: ignore[arg-type]
                ret_1h_pct=float(f.ret_1h_pct),
                ret_4h_pct=float(f.ret_4h_pct),
                ret_24h_pct=float(f.ret_24h_pct),
                volatility_ratio=float(f.volatility_ratio),
                round_trip_cost_bps=float(f.round_trip_cost_bps),
                ret_15m_pct=None if data.features_15m is None else float(data.features_15m.ret_pct),
                atr_15m_pct=None if data.features_15m is None else float(data.features_15m.atr_pct),
                trend_15m=None if data.features_15m is None else data.features_15m.trend,  # type: ignore[arg-type]
            ),
            account=AccountOut(
                equity_usd=float(account.equity),
                available_usd=float(account.available),
                day_pnl_pct=_pct(account.equity - view.day_start_equity, view.day_start_equity),
                drawdown_pct=_pct(view.peak_equity - account.equity, view.peak_equity),
            ),
            position=self._position_out(user, account, data.window_id),
            can_open=not blocked,
            blocked_reasons=blocked,
            last_decisions=self._last_decisions(user.id),
        )

    def _position_out(self, user: User, account: Account, window_id: int) -> Optional[PositionOut]:
        position = account.position
        if position is None:
            return None
        intent = self.open_intent(user.id)
        stop = _live_order(account, OrderKind.STOP, position.side, intent)
        take = _live_order(account, OrderKind.TAKE, position.side, intent)
        return PositionOut(
            side=position.side.value,
            quantity=float(position.quantity),
            entry_price=float(position.entry_price),
            unrealized_pnl_usd=float(position.unrealized_pnl),
            stop_price=float(stop.trigger_price) if stop and stop.trigger_price is not None else None,
            take_profit_price=float(take.trigger_price) if take and take.trigger_price is not None else None,
            windows_held=max(0, window_id - intent.window_id) if intent else 0,
        )

    # ------------------------------------------------------------------ preview
    async def preview(self, user: User, req: PreviewIn) -> PreviewOut:
        data = await self._gather(user, need_features=True)
        window = data.window_id
        stop_atr = Decimal(str(req.stop_atr))
        tp_atr = Decimal(str(req.take_profit_atr)) if req.take_profit_atr is not None else None
        sized: Optional[sizing.Sizing] = None
        risk = self.risk_for(user.id)
        if data.ok and data.snapshot is not None and data.account is not None and data.market is not None and data.features is not None:
            day_start = self._risk_view(user.id, data.account.equity, data.now_ms).day_start_equity
            day_budget = data.account.equity - day_start * (1 - risk.daily_loss)
            sized = sizing.compute(Side(req.side), data.account.equity, data.snapshot.mark_price, data.features.atr_1h, stop_atr, tp_atr, data.market, risk, day_budget)
        observation = self._observation(req.observation_id)
        inputs = self._make_inputs(
            user, data, observation, req.side, stop_atr, tp_atr,
            sized.quantity if sized else Decimal(0),
            sized.take_profit_price if sized else None,
            sized.stop_price if sized else Decimal(0),
        )
        rejection = limits.first_rejection(inputs)
        if rejection is not None or sized is None or data.snapshot is None or data.account is None:
            rejection = rejection or limits.Rejection(ErrorCode.MARKET_UNAVAILABLE)
            await self._log_decision(user.id, window, "preview", {"side": req.side, "result": "rejected", "code": rejection.code.value, "limit": rejection.limit, "agent_reason": req.reason})
            raise self._limit_rejection(rejection, window, inputs.cooldown_until_window)

        mark = data.snapshot.mark_price
        now = data.now_ms
        ticket_id = secrets.token_urlsafe(24)
        expires = now + limits.TICKET_TTL_MS
        async with self.ctx.db_lock:
            with transaction(self.conn) as conn:
                conn.execute(
                    "INSERT INTO tickets (id, user_id, window_id, observation_id, side, quantity, mark, stop_price, take_profit_price, stop_atr, take_profit_atr, reason, expires_ms, created_ms) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ticket_id, user.id, window, req.observation_id, req.side, str(sized.quantity), str(mark), str(sized.stop_price),
                     None if sized.take_profit_price is None else str(sized.take_profit_price), str(stop_atr), None if tp_atr is None else str(tp_atr), req.reason, expires, now),
                )
        view = self._risk_view(user.id, data.account.equity, now)
        day_loss_remaining = max(Decimal(0), data.account.equity - view.day_start_equity * (1 - risk.daily_loss))
        await self._log_decision(
            user.id, window, "preview",
            {"side": req.side, "result": "ok", "code": None, "stop_atr": str(stop_atr), "take_profit_atr": None if tp_atr is None else str(tp_atr),
             "quantity": str(sized.quantity), "mark": str(mark), "stop_price": str(sized.stop_price), "worst_loss": str(sized.worst_loss), "agent_reason": req.reason},
        )
        return PreviewOut(
            ticket=ticket_id,
            expires_ms=expires,
            side=req.side,
            quantity=float(sized.quantity),
            notional_usd=float(sized.notional),
            mark_price=float(mark),
            stop_price=float(sized.stop_price),
            take_profit_price=None if sized.take_profit_price is None else float(sized.take_profit_price),
            worst_loss_usd=float(sized.worst_loss),
            day_loss_remaining_usd=float(day_loss_remaining),
        )

    # ------------------------------------------------------------------ submit
    async def submit(self, user: User, req: SubmitIn) -> SubmitOut:
        window = window_of(self.ctx.clock())
        try:
            out = await self._submit_inner(user, req)
        except ApiError as err:
            await self._log_decision(user.id, window, "submit", {"result": err.out.outcome.value, "code": err.out.code.value, "ticket": req.ticket})
            raise
        await self._log_decision(user.id, window, "submit", {"side": out.side, "result": out.outcome.value, "code": out.code.value if out.code else None, "ticket": req.ticket})
        return out

    async def _submit_inner(self, user: User, req: SubmitIn) -> SubmitOut:
        deadline = time.monotonic() + self.submit_budget_s
        lock = self.user_lock(user.id)
        if not await self._acquire(lock, deadline):
            raise ApiError(ErrorOut(outcome=Outcome.REJECTED, code=ErrorCode.INTERNAL), 503)
        try:
            return await self._submit_locked(user, req, deadline)
        finally:
            lock.release()

    def _ticket(self, ticket_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()

    async def _submit_locked(self, user: User, req: SubmitIn, deadline: float) -> SubmitOut:
        ticket = self._ticket(req.ticket)
        if ticket is None or ticket["user_id"] != user.id:
            raise rejected(ErrorCode.TICKET_NOT_FOUND)
        existing = self.intent_by_ticket(ticket["id"])
        if existing is not None:
            return self._replay(existing)
        if ticket["used_ms"] is not None:
            raise rejected(ErrorCode.TICKET_USED)

        data = await self._gather(user, need_features=False)
        tp_price = _dec_or_none(ticket["take_profit_price"])
        quantity = Decimal(ticket["quantity"])
        inputs = self._make_inputs(
            user, data, self._observation(ticket["observation_id"]), ticket["side"],
            Decimal(ticket["stop_atr"]), _dec_or_none(ticket["take_profit_atr"]), quantity, tp_price, Decimal(ticket["stop_price"]),
        )
        now = self.ctx.clock()
        rejection = (
            limits.check_ticket_valid(now, ticket["expires_ms"], ticket["used_ms"])
            or limits.first_rejection(inputs)
            or (self._stale_decision(ticket, data) if data.snapshot and data.market else None)
        )
        error = None if rejection is None else self._limit_rejection(rejection, data.window_id, inputs.cooldown_until_window)
        intent_id, error = await self._claim_ticket(user, ticket, quantity, error, data, now, inputs)
        if error is not None:
            raise error
        if intent_id is None:
            raise self._limit_rejection(limits.Rejection(ErrorCode.WINDOW_USED, "per_window=1"), data.window_id)

        assert data.snapshot is not None and data.market is not None
        mark = data.snapshot.mark_price
        risk = self.risk_for(user.id)
        side = Side(ticket["side"])
        order = OrderRequest(
            kind=OrderKind.MARKET,
            side=side,
            quantity=quantity,
            worst_price=sizing.entry_worst_price(side, mark, risk.collar, data.market.price_tick),
            cid=make_cid(user.id, ticket["window_id"], "entry"),
            reduce_only=False,
            margin=sizing.margin_needed(quantity * mark, risk.max_leverage),
        )
        try:
            await self.tx.send(
                user_id=user.id, intent_id=intent_id, purpose="entry", granter=user.inj_address, subaccount_id=user.subaccount_id,
                orders=[order], stage="entry_sent", deadline=deadline,
            )
        except (SignerBusy, ChainUnavailable) as exc:
            # Nothing was signed, so this entry definitively did not and will not happen.
            if isinstance(exc, EntryHalted):
                code = exc.code
            else:
                code = ErrorCode.INTERNAL if isinstance(exc, SignerBusy) else ErrorCode.MARKET_UNAVAILABLE
            await self._fail(intent_id, code)
            return self._submit_result(self.intent(intent_id))
        intent = await self.advance(intent_id, deadline)
        return self._submit_result(intent)

    def _stale_decision(self, ticket: sqlite3.Row, data: Gathered) -> Optional[limits.Rejection]:
        """The ticket froze quantity and stop; the entry fills from today's mark plus the collar."""
        assert data.snapshot is not None and data.market is not None
        risk = self.ctx.settings.risk
        side = Side(ticket["side"])
        mark, ticket_mark = data.snapshot.mark_price, Decimal(ticket["mark"])
        return limits.check_drift(mark, ticket_mark, risk.drift_max) or limits.check_risk_growth(
            side.value,
            sizing.entry_worst_price(side, mark, risk.collar, data.market.price_tick),
            sizing.entry_worst_price(side, ticket_mark, risk.collar, data.market.price_tick),
            Decimal(ticket["stop_price"]),
            risk.max_risk_growth,
        )

    async def _claim_ticket(
        self, user: User, ticket: sqlite3.Row, quantity: Decimal, error: Optional[ApiError], data: Gathered, now: int, inputs: EntryInputs
    ) -> tuple[Optional[int], Optional[ApiError]]:
        """One BEGIN IMMEDIATE: burn the ticket and record the intent under UNIQUE(user, window).
        Status and the kill switch are read inside it, because `inputs` predates the chain reads."""
        window = ticket["window_id"]
        equity_before = None if data.account is None else str(data.account.equity)
        side, stop, tp = ticket["side"], ticket["stop_price"], ticket["take_profit_price"]
        intent_id: Optional[int] = None
        async with self.ctx.db_lock:
            with transaction(self.conn) as conn:
                if error is None:
                    live = replace(inputs, kill_switch=self.kill_switch(), user_status=self.user(user.id).status)
                    stopped = limits.check_kill_switch(live) or limits.check_user_status(live)
                    if stopped is not None:
                        error = self._limit_rejection(stopped, data.window_id)
                conn.execute("UPDATE tickets SET used_ms=? WHERE id=? AND used_ms IS NULL", (now, ticket["id"]))
                if error is not None:
                    body = error.out
                    conn.execute(
                        "INSERT OR IGNORE INTO intents (user_id, window_id, ticket_id, side, quantity, stop_price, take_profit_price, stage, outcome, error_code, result_json, equity_before, created_ms, updated_ms) "
                        "VALUES (?,?,?,?,?,?,?,'rejected','rejected',?,?,?,?,?)",
                        (user.id, window, ticket["id"], side, str(quantity), stop, tp, body.code.value, body.model_dump_json(), equity_before, now, now),
                    )
                else:
                    try:
                        cursor = conn.execute(
                            "INSERT INTO intents (user_id, window_id, ticket_id, side, quantity, stop_price, take_profit_price, stage, equity_before, created_ms, updated_ms) "
                            "VALUES (?,?,?,?,?,?,?,'entry_pending',?,?,?)",
                            (user.id, window, ticket["id"], side, str(quantity), stop, tp, equity_before, now, now),
                        )
                        intent_id = int(cursor.lastrowid)
                        # No position and no open intent got us here, so any pending close is stale
                        # and must not be applied to the position this entry is about to open.
                        conn.execute("DELETE FROM system_state WHERE key=?", (self._close_key(user.id),))
                    except sqlite3.IntegrityError:
                        intent_id = None
        return intent_id, error

    def _replay(self, intent: Intent) -> SubmitOut:
        """Same ticket again: the stored result, or the live state while it is unresolved."""
        if intent.result_json:
            data = json.loads(intent.result_json)
            if data.get("ok") is False:
                raise ApiError(ErrorOut.model_validate(data))
            return SubmitOut.model_validate(data)
        return self._intent_out(intent)

    def _submit_result(self, intent: Intent) -> SubmitOut:
        return self._intent_out(intent)

    def _intent_out(self, intent: Intent) -> SubmitOut:
        take = self.tx.latest(intent.id, "take")
        base: dict[str, Any] = {
            "window_id": intent.window_id,
            "side": intent.side.value,
            "quantity": float(intent.quantity),
            "entry_price": None if intent.entry_price is None else float(intent.entry_price),
            "stop_price": float(intent.stop_price),
            "take_profit_price": float(intent.take_profit_price) if intent.take_profit_price is not None and take is not None and take.status == "ok" else None,
            "tx_hashes": self.tx.hashes_for_intent(intent.id),
        }
        if intent.stage == "protected":
            return SubmitOut(outcome=Outcome.FILLED, stop_live=True, **base)
        if intent.stage == "closed":
            return SubmitOut(outcome=Outcome.CLOSED, stop_live=False, **base)
        if intent.stage == "flattened":
            return SubmitOut(outcome=Outcome.FAILED, code=ErrorCode.STOP_FAILED_FLATTENED, stop_live=False, **base)
        if intent.stage == "failed":
            return SubmitOut(outcome=Outcome.FAILED, code=ErrorCode(intent.error_code) if intent.error_code else ErrorCode.CHAIN_REJECTED, **base)
        if intent.stage == "rejected":
            return SubmitOut(outcome=Outcome.REJECTED, code=ErrorCode(intent.error_code) if intent.error_code else ErrorCode.INTERNAL, **base)
        return SubmitOut(outcome=Outcome.UNKNOWN, code=ErrorCode.OUTCOME_UNKNOWN, check_state_after_s=CHECK_STATE_AFTER_S, **base)

    # ------------------------------------------------------------------ intent machine
    async def _update_intent(self, intent_id: int, **fields: Any) -> None:
        now = self.ctx.clock()
        assignments = ", ".join(f"{k}=?" for k in fields)
        async with self.ctx.db_lock:
            with transaction(self.conn) as conn:
                conn.execute(f"UPDATE intents SET {assignments}, updated_ms=? WHERE id=?", (*fields.values(), now, intent_id))

    async def _fail(self, intent_id: int, code: ErrorCode) -> None:
        await self._update_intent(intent_id, stage="failed", outcome=Outcome.FAILED.value, error_code=code.value)

    async def advance(self, intent_id: int, deadline: float) -> Intent:
        """Drive an intent as far as chain state and the deadline allow. Safe to call repeatedly."""
        with urgent_requests():  # this is the path that puts a stop on the position
            return await self._advance(intent_id, deadline)

    async def _advance(self, intent_id: int, deadline: float) -> Intent:
        while True:
            intent = self.intent(intent_id)
            if intent.stage in TERMINAL_STAGES or intent.stage == "protected":
                break
            try:
                progressed = await self._step(intent, deadline)
            except Exception as exc:
                await self._step_failed(intent, exc)
                progressed = False
            if not progressed:
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(self.poll_interval_s)
        intent = self.intent(intent_id)
        if intent.stage == "protected":
            try:
                await self.maybe_place_take(intent, deadline)
            except Exception as exc:
                if not _is_transient(exc):
                    logger.exception("take-profit for intent %s failed", intent.id)
            intent = self.intent(intent_id)
        await self._record_result(intent)
        return self.intent(intent_id)

    async def _record_result(self, intent: Intent) -> None:
        """Store the submit result once the flow has reached a stable outcome."""
        if intent.result_json is not None:
            return
        if intent.stage == "protected" or intent.stage in ("failed", "flattened"):
            if intent.stage == "protected":
                # Before the write: a crash in between repeats this, and the dedupe key absorbs it.
                await self._push_opened(intent)
            out = self._intent_out(intent)
            await self._update_intent(intent.id, result_json=out.model_dump_json(), outcome=out.outcome.value)
        elif intent.stage not in TERMINAL_STAGES and intent.outcome is None:
            await self._update_intent(intent.id, outcome=Outcome.UNKNOWN.value)

    async def _step_failed(self, intent: Intent, exc: Exception) -> None:
        """A transport failure waits. Anything else on the way to a live stop is "stop not placed":
        alert and count it, so the next step retries and then flattens."""
        if _is_transient(exc):
            return
        logger.exception("intent %s stage %s failed", intent.id, intent.stage, exc_info=exc)
        await alerts.raise_alert(
            self.ctx, level="critical", kind="stop_missing", user_id=intent.user_id,
            detail=f"intent={intent.id} stage={intent.stage} error={type(exc).__name__}", dedupe_key=f"stop_error:{intent.id}",
        )
        if intent.stage in ("entry_filled", "stop_pending"):
            await self._kv_set(self._stop_error_key(intent), str(self._stop_errors(intent) + 1))

    def _stop_error_key(self, intent: Intent) -> str:
        return f"stop_error:{intent.id}"

    def _stop_errors(self, intent: Intent) -> int:
        return int(self._kv_get(self._stop_error_key(intent)) or 0)

    def _epoch(self, intent: Intent) -> int:
        return int(self._kv_get(f"stop_epoch:{intent.id}") or 0)

    def _latest_flatten(self, intent: Intent) -> Optional[TxRow]:
        rows = [t for t in self.tx.for_intent(intent.id, "flatten") if t.id > self._epoch(intent)]
        return rows[-1] if rows else None

    async def _step(self, intent: Intent, deadline: float) -> bool:
        if intent.stage == "entry_pending":
            # Only `submit` may start an entry. Reaching here means a crash before signing.
            await self._fail(intent.id, ErrorCode.INTERNAL)
            return True
        if intent.stage == "entry_sent":
            return await self._step_entry_sent(intent, deadline)
        if self._latest_flatten(intent) is not None or self._stop_errors(intent) >= MAX_STOP_TRIES:
            return await self._step_flatten(intent, deadline)
        if intent.stage == "entry_filled":
            return await self._step_place_stop(intent, deadline)
        if intent.stage == "stop_pending":
            return await self._step_stop_pending(intent, deadline)
        return False

    async def _step_entry_sent(self, intent: Intent, deadline: float) -> bool:
        tx = self.tx.latest(intent.id, "entry")
        if tx is None:
            await self._fail(intent.id, ErrorCode.INTERNAL)
            return True
        if tx.is_open:
            tx = await self.tx.resolve(tx)
            if tx.is_open:
                return False
        if tx.status != "ok":
            await self._fail(intent.id, ErrorCode.CHAIN_REJECTED)
            return True
        user = self.user(intent.user_id)
        account = await self.read_account(user)
        if account.height and tx.height and account.height < tx.height:
            return False  # this node has not seen the block yet; never conclude "no fill"
        position = account.position
        if position is None or position.side is not intent.side or position.quantity <= 0:
            await self._fail(intent.id, ErrorCode.CHAIN_REJECTED)
            return True
        now = self.ctx.clock()
        async with self.ctx.db_lock:
            with transaction(self.conn) as conn:
                conn.execute(
                    "UPDATE intents SET stage='entry_filled', quantity=?, entry_price=?, updated_ms=? WHERE id=?",
                    (str(position.quantity), str(position.entry_price), now, intent.id),
                )
                conn.execute(
                    "INSERT INTO trades (user_id, intent_id, side, quantity, entry_price, opened_ms) VALUES (?,?,?,?,?,?)",
                    (intent.user_id, intent.id, intent.side.value, str(position.quantity), str(position.entry_price), now),
                )
        return True

    def _crossed(self, intent: Intent, mark: Decimal) -> bool:
        return mark <= intent.stop_price if intent.side is Side.LONG else mark >= intent.stop_price

    def _stop_order(self, intent: Intent, quantity: Decimal, tick: Decimal) -> OrderRequest:
        order_side = intent.side.opposite
        return OrderRequest(
            kind=OrderKind.STOP,
            side=order_side,
            quantity=quantity,
            worst_price=sizing.stop_worst_price(order_side, intent.stop_price, self.ctx.settings.risk.stop_worst_pct, tick),
            cid=make_cid(intent.user_id, intent.window_id, "stop"),
            reduce_only=True,
            trigger_price=intent.stop_price,
        )

    async def _send_stop(self, intent: Intent, user: User, account: Account, deadline: float) -> None:
        """Place the stop for the whole position. A smaller one already on chain is replaced in
        the same tx, because the chain allows one conditional order per direction."""
        assert account.position is not None
        market = await self.chain.market()
        order = self._stop_order(intent, account.position.quantity, market.price_tick)
        short = _live_order(account, OrderKind.STOP, account.position.side, intent)
        await self.tx.send(
            user_id=intent.user_id, intent_id=intent.id, purpose="stop", granter=user.inj_address, subaccount_id=user.subaccount_id,
            orders=[order], cancels=[short] if short is not None else [], stage="stop_pending", deadline=deadline,
        )

    async def finalize_if_gone(self, intent: Intent, user: User, account: Account) -> bool:
        """Finish a trade whose position vanished on chain, once that read can be believed."""
        if not await self._position_gone(user, account):
            return False
        await self.finalize_flat(intent, reason=await self.infer_exit_reason(intent, account), account=account)
        return True

    async def _step_place_stop(self, intent: Intent, deadline: float) -> bool:
        user = self.user(intent.user_id)
        account = await self.read_account(user)
        if account.position is None:
            return await self.finalize_if_gone(intent, user, account)
        if self._crossed(intent, account.mark_price):
            # The trigger is already behind the mark: the chain would reject the stop.
            return await self._step_flatten(intent, deadline)
        await self._send_stop(intent, user, account, deadline)
        return True

    def _stop_attempts(self, intent: Intent) -> list[TxRow]:
        return [t for t in self.tx.for_intent(intent.id, "stop") if t.id > self._epoch(intent)]

    async def _step_stop_pending(self, intent: Intent, deadline: float) -> bool:
        attempts = self._stop_attempts(intent)
        if not attempts:
            return await self._step_place_stop(intent, deadline)
        last = attempts[-1]
        if last.is_open:
            last = await self.tx.resolve(last)
            if last.is_open:
                return False
        user = self.user(intent.user_id)
        # Code 97 means the chain still holds this stop's cid: an earlier attempt is live.
        if last.status == "ok" or last.code == CODE_DUPLICATE_CID:
            account = await self.read_account(user)
            if account.height and last.height and account.height < last.height:
                return False
            if account.position is None:
                return await self.finalize_if_gone(intent, user, account)
            if live_stop(account, intent) is not None:
                await self._update_intent(intent.id, stage="protected")
                await self._record_result(self.intent(intent.id))
                return True
            if last.code == CODE_DUPLICATE_CID and account.height <= (last.height or 0):
                return False  # re-read from a block after the one that reported the duplicate
        # Not live. Retry once with a fresh tx (the previous one is final), then flatten.
        if len(attempts) + self._stop_errors(intent) < MAX_STOP_TRIES:
            return await self._step_place_stop(intent, deadline)
        return await self._step_flatten(intent, deadline)

    async def _step_flatten(self, intent: Intent, deadline: float) -> bool:
        user = self.user(intent.user_id)
        last = self._latest_flatten(intent)
        if last is not None and last.is_open:
            last = await self.tx.resolve(last)
            if last.is_open:
                return False
        # A stop attempt sent after the last flatten, if the flatten left the position open.
        stop = self.tx.latest(intent.id, "stop")
        if stop is not None and (last is None or stop.id < last.id):
            stop = None
        if stop is not None and stop.is_open:
            stop = await self.tx.resolve(stop)
            if stop.is_open:
                return False
        account = await self.read_account(user)
        for tx in (last, stop):
            if tx is not None and account.height and tx.height and account.height < tx.height:
                return False
        if account.position is None:
            if not await self._position_gone(user, account, last):
                return False
            await self.finalize_flat(
                intent, reason="flatten", account=account, stage="flattened",
                outcome=Outcome.FAILED, code=ErrorCode.STOP_FAILED_FLATTENED,
            )
            await alerts.raise_alert(
                self.ctx, level="critical", kind="flatten", user_id=intent.user_id,
                detail=f"intent={intent.id}", dedupe_key=f"flatten:{intent.id}",
            )
            return True
        if stop is not None and (stop.status == "ok" or stop.code == CODE_DUPLICATE_CID) and live_stop(account, intent) is not None:
            await self._update_intent(intent.id, stage="protected")
            await self._record_result(self.intent(intent.id))
            return True
        if last is not None and stop is None and not self._crossed(intent, account.mark_price):
            # The flatten is final and the position is still open: one stop attempt before the next.
            await self._send_stop(intent, user, account, deadline)
            return True
        market = await self.chain.market()
        await self.tx.send(
            user_id=intent.user_id, intent_id=intent.id, purpose="flatten", granter=user.inj_address, subaccount_id=user.subaccount_id,
            orders=[self._reduce_only_market(intent.user_id, intent.window_id, "flatten", account.position.side.opposite, account.position.quantity, account.mark_price, market.price_tick)],
            deadline=deadline,
        )
        return True

    def _reduce_only_market(self, user_id: int, window_id: int, leg: str, side: Side, quantity: Decimal, mark: Decimal, tick: Decimal) -> OrderRequest:
        return OrderRequest(
            kind=OrderKind.MARKET,
            side=side,
            quantity=quantity,
            worst_price=sizing.close_worst_price(side, mark, self.ctx.settings.risk.close_worst_pct, tick),
            cid=make_cid(user_id, window_id, leg),
            reduce_only=True,
        )

    async def maybe_place_take(self, intent: Intent, deadline: float) -> None:
        """One best-effort take-profit once the stop is live. Failure never flattens."""
        if intent.take_profit_price is None or self.tx.latest(intent.id, "take") is not None:
            return
        user = self.user(intent.user_id)
        account = await self.read_account(user)
        if account.position is None:
            return
        market = await self.chain.market()
        tp = intent.take_profit_price
        through = account.mark_price >= tp if intent.side is Side.LONG else account.mark_price <= tp
        if through:
            return
        order_side = intent.side.opposite
        tx = await self.tx.send(
            user_id=intent.user_id, intent_id=intent.id, purpose="take", granter=user.inj_address, subaccount_id=user.subaccount_id,
            orders=[
                OrderRequest(
                    kind=OrderKind.TAKE,
                    side=order_side,
                    quantity=account.position.quantity,
                    worst_price=sizing.stop_worst_price(order_side, tp, self.ctx.settings.risk.stop_worst_pct, market.price_tick),
                    cid=make_cid(intent.user_id, intent.window_id, "take"),
                    reduce_only=True,
                    trigger_price=tp,
                )
            ],
            deadline=deadline,
        )
        if not tx.is_open and tx.status != "ok":
            await alerts.raise_alert(
                self.ctx, level="warning", kind="take_failed", user_id=intent.user_id,
                detail=f"intent={intent.id} code={tx.code}", dedupe_key=f"take_failed:{intent.id}",
            )

    async def _taker_fee(self) -> Decimal:
        """Used only to estimate exit prices from equity, so a stale value is harmless."""
        try:
            market = await self.chain.market()
        except ChainUnavailable:
            return self._last_taker_fee
        self._last_taker_fee = market.taker_fee
        return market.taker_fee

    async def infer_exit_reason(self, intent: Intent, account: Account) -> str:
        """No fills API: classify a chain-side exit by the money that actually moved."""
        if intent.equity_before is None or intent.entry_price is None or intent.quantity <= 0:
            return "external"
        pnl = account.equity - intent.equity_before
        exit_price = sizing.implied_exit_price(intent.side, intent.entry_price, intent.quantity, pnl, await self._taker_fee())
        tol = EXIT_MATCH_TOLERANCE
        stop, tp = intent.stop_price, intent.take_profit_price
        if intent.side is Side.LONG:
            if exit_price <= stop * (1 + tol):
                return "stop"
            if tp is not None and exit_price >= tp * (1 - tol):
                return "take_profit"
        else:
            if exit_price >= stop * (1 - tol):
                return "stop"
            if tp is not None and exit_price <= tp * (1 + tol):
                return "take_profit"
        # A transfer during the trade throws the money off, but a stop leaves the mark through it.
        return "stop" if self._crossed(intent, account.mark_price) else "external"

    async def finalize_flat(
        self,
        intent: Intent,
        *,
        reason: str,
        account: Account,
        stage: str = "closed",
        outcome: Outcome = Outcome.CLOSED,
        code: Optional[ErrorCode] = None,
    ) -> None:
        """The position is verified gone: finish the trade row, the intent and any cooldown."""
        now = self.ctx.clock()
        window = window_of(now)
        exit_price: Optional[str] = None
        pnl_str: Optional[str] = None
        if intent.equity_before is not None and intent.entry_price is not None and intent.quantity > 0:
            pnl = account.equity - intent.equity_before
            pnl_str = str(pnl)
            exit_price = str(sizing.implied_exit_price(intent.side, intent.entry_price, intent.quantity, pnl, await self._taker_fee()))
        async with self.ctx.db_lock:
            with transaction(self.conn) as conn:
                conn.execute(
                    "UPDATE trades SET exit_price=?, pnl_usd=?, close_reason=?, closed_ms=? WHERE intent_id=? AND closed_ms IS NULL",
                    (exit_price, pnl_str, reason, now, intent.id),
                )
                conn.execute(
                    "UPDATE intents SET stage=?, outcome=?, error_code=?, updated_ms=? WHERE id=?",
                    (stage, outcome.value, code.value if code else intent.error_code, now, intent.id),
                )
                if reason in COOLDOWN_REASONS or (reason != "take_profit" and pnl_str is not None and Decimal(pnl_str) < 0):
                    risk = self._risk_row(intent.user_id) or RiskState(now // DAY_MS, account.equity, account.equity)
                    until = window + 1 + self.ctx.settings.risk.cooldown_windows
                    self._write_risk(conn, intent.user_id, RiskState(risk.day_id, risk.day_start_equity, risk.peak_equity, max(risk.cooldown_until_window, until)))
        await self._push_finished(intent, reason, stage, pnl_str)
        await self._kv_delete(f"stop_epoch:{intent.id}")
        await self._kv_delete(self._stop_error_key(intent))
        await self._kv_delete(self._flat_key(intent.user_id))
        await self._record_result(self.intent(intent.id))

    async def rearm_stop(self, intent: Intent) -> None:
        """A protected position lost its stop: go back to placing one, counting attempts afresh."""
        last = self.tx.latest(intent.id, "stop")
        await self._kv_set(f"stop_epoch:{intent.id}", str(last.id if last else 0))
        await self._kv_delete(self._stop_error_key(intent))
        await self._update_intent(intent.id, stage="stop_pending")

    # ------------------------------------------------------------------ stopping the agent
    async def auto_pause(self, user_id: int, reason: str, equity: Optional[Decimal], text: str) -> bool:
        """Stop a running agent for a reason the owner did not choose. A position stays, with its
        stop. Only the step from active to paused notifies, so the owner hears about it once."""
        now = self.ctx.clock()
        async with self.ctx.db_lock:
            with transaction(self.conn) as conn:
                if conn.execute("UPDATE users SET status='paused' WHERE id=? AND status='active'", (user_id,)).rowcount == 0:
                    return False
                conn.execute(
                    "UPDATE strategy_runs SET status='stopped', stop_reason=?, end_equity=?, stopped_ms=? WHERE user_id=? AND status='active'",
                    (reason, None if equity is None else str(equity), now, user_id),
                )
        await self._push(user_id, f"agent_paused_{reason}", text, f"auto_pause:{reason}:{user_id}:{now}")
        return True

    # ------------------------------------------------------------------ owner notifications
    async def _push(self, user_id: int, kind: str, text: str, dedupe_key: str) -> None:
        """Queue a Telegram message. A failure here must never reach the trade."""
        try:
            await notify.enqueue(self.ctx, user_id, kind, text, dedupe_key=dedupe_key)
        except Exception:
            logger.exception("notification %s failed", kind)

    def _position_label(self, intent: Intent) -> str:
        return f"{intent.side.value} {_plain(intent.quantity)} {self.ctx.settings.market_ticker}"

    async def _push_opened(self, intent: Intent) -> None:
        text = f"Opened {self._position_label(intent)} at {_plain(intent.entry_price)}. Stop at {_plain(intent.stop_price)}."
        await self._push(intent.user_id, "position_opened", text, f"opened:{intent.id}")

    async def _push_finished(self, intent: Intent, reason: str, stage: str, pnl_usd: Optional[str]) -> None:
        pnl = "P&L could not be read." if pnl_usd is None else f"P&L: {_signed_usd(Decimal(pnl_usd))}."
        label = self._position_label(intent)
        if stage == "flattened":
            kind, text = "position_flattened", f"A stop could not be placed, so your {label} was closed at market. {pnl}"
        elif reason in COOLDOWN_REASONS:
            kind, text = "stop_hit", f"Stop hit on your {label}. {pnl}"
        elif reason == "take_profit":
            kind, text = "take_profit_hit", f"Take-profit hit on your {label}. {pnl}"
        else:
            kind, text = "position_closed", f"Your {label} was closed. {pnl}"
        await self._push(intent.user_id, kind, text, f"closed:{intent.id}")

    # ------------------------------------------------------------------ close
    def _close_key(self, user_id: int) -> str:
        return f"close_req:{user_id}"

    def close_requested(self, user_id: int) -> bool:
        return self._kv_get(self._close_key(user_id)) is not None

    def _close_attempts(self, user_id: int) -> int:
        raw = self._kv_get(self._close_key(user_id))
        return int(json.loads(raw)["attempts"]) if raw else 0

    async def _bump_close_attempts(self, user_id: int) -> None:
        raw = self._kv_get(self._close_key(user_id))
        if raw:
            data = json.loads(raw)
            data["attempts"] = int(data["attempts"]) + 1
            await self._kv_set(self._close_key(user_id), json.dumps(data))

    async def _arm_close(self, user_id: int, now: int) -> None:
        """A repeat request inside the window keeps its attempt count, so a failing close cannot
        be re-armed by calling again. The first request after the window starts a new count."""
        raw = self._kv_get(self._close_key(user_id))
        if raw is None or now - int(json.loads(raw)["ms"]) >= CLOSE_RETRY_WINDOW_MS:
            await self._kv_set(self._close_key(user_id), json.dumps({"ms": now, "attempts": 0}))

    async def close(self, user: User, req: CloseIn) -> SubmitOut:
        """Never blocked by limits, the kill switch or pause. Only auth can refuse it."""
        with urgent_requests():
            return await self._close(user, req)

    async def _close(self, user: User, req: CloseIn) -> SubmitOut:
        now = self.ctx.clock()
        window = window_of(now)
        await self._arm_close(user.id, now)
        deadline = time.monotonic() + self.submit_budget_s
        lock = self.user_lock(user.id)
        code: Optional[str] = None
        try:
            if not await self._acquire(lock, deadline):
                out = self._close_unknown(window)
            else:
                try:
                    out = await self._close_locked(user, window, deadline)
                finally:
                    lock.release()
        except ApiError as err:
            # Nothing was attempted, so repeating a close on a flat account must not grow the log.
            if err.out.code is not ErrorCode.NO_POSITION:
                await self._log_decision(user.id, window, "close", {"result": err.out.outcome.value, "code": err.out.code.value, "agent_reason": req.reason})
            raise
        code = out.code.value if out.code else None
        await self._log_decision(user.id, window, "close", {"side": out.side, "result": out.outcome.value, "code": code, "agent_reason": req.reason})
        return out

    def _close_unknown(self, window: int) -> SubmitOut:
        return SubmitOut(outcome=Outcome.UNKNOWN, window_id=window, code=ErrorCode.OUTCOME_UNKNOWN, check_state_after_s=CHECK_STATE_AFTER_S)

    async def _close_locked(self, user: User, window: int, deadline: float) -> SubmitOut:
        intent = self.open_intent(user.id)
        if intent is not None and intent.stage in PRE_PROTECT_STAGES:
            intent = await self.advance(intent.id, deadline)
        result, position_side, position_qty, entry = await self._drive_close(user, deadline, reason="close", max_attempts=MAX_CLOSE_ATTEMPTS)
        hashes: list[str] = []
        latest = self.tx.latest_for_user(user.id, "close")
        if latest is not None:
            hashes = [latest.tx_hash]
        if result == "no_position":
            raise rejected(ErrorCode.NO_POSITION, None, window + 1)
        if result == "closed":
            return SubmitOut(outcome=Outcome.CLOSED, window_id=window, side=position_side, quantity=position_qty, entry_price=entry, stop_live=False, tx_hashes=hashes)
        if result == "failed":
            return SubmitOut(outcome=Outcome.FAILED, window_id=window, side=position_side, quantity=position_qty, entry_price=entry, code=ErrorCode.CHAIN_REJECTED, tx_hashes=hashes)
        out = self._close_unknown(window)
        return out.model_copy(update={"side": position_side, "quantity": position_qty, "tx_hashes": hashes})

    async def drive_close_request(self, user: User, deadline: float) -> None:
        """Watcher entry point: continue a close that the API could not finish in its budget."""
        with urgent_requests():
            result, _, _, _ = await self._drive_close(user, deadline, reason="close", max_attempts=MAX_CLOSE_ATTEMPTS)
        if result == "failed":
            await alerts.raise_alert(self.ctx, level="critical", kind="close_failed", user_id=user.id, detail="attempts_exhausted", dedupe_key=f"close_failed:{user.id}:{self.ctx.clock() // WINDOW_MS}")

    async def force_close(self, user: User, intent: Intent, deadline: float) -> None:
        """Watcher entry point: the stop's trigger is breached but the position is still open."""
        with urgent_requests():
            await self._drive_close(user, deadline, reason="stop_trigger_failed", max_attempts=None)

    async def _drive_close(
        self, user: User, deadline: float, *, reason: str, max_attempts: Optional[int]
    ) -> tuple[str, Optional[str], Optional[float], Optional[float]]:
        """Returns (result, side, quantity, entry_price). result: closed | failed | pending | no_position."""
        sent_any = False
        side: Optional[str] = None
        quantity: Optional[float] = None
        entry: Optional[float] = None
        while True:
            last = self.tx.latest_for_user(user.id, "close")
            if last is not None and last.is_open:
                last = await self.tx.resolve(last)
                if last.is_open:
                    if time.monotonic() >= deadline:
                        return "pending", side, quantity, entry
                    await asyncio.sleep(self.poll_interval_s)
                    continue
                sent_any = True
            try:
                account = await self.read_account(user)
                market = await self.chain.market()
            except ChainUnavailable:
                return "pending", side, quantity, entry
            if last is not None and last.height and account.height < last.height:
                return "pending", side, quantity, entry  # behind our own close
            position = account.position
            intent = self.open_intent(user.id)
            if position is None:
                if intent is not None and intent.stage in ("entry_pending", "entry_sent"):
                    return "pending", side, quantity, entry  # the entry may still land
                scope = intent.id if intent is not None else None
                we_closed = last is not None and last.status == "ok" and last.intent_id == scope and (intent is not None or sent_any)
                if intent is not None:
                    if not await self._position_gone(user, account, last if we_closed else None):
                        if time.monotonic() >= deadline:
                            return "pending", side, quantity, entry
                        await asyncio.sleep(self.poll_interval_s)
                        continue
                    side, quantity = intent.side.value, float(intent.quantity)
                    entry = float(intent.entry_price) if intent.entry_price is not None else None
                    exit_reason = reason if we_closed else await self.infer_exit_reason(intent, account)
                    await self.finalize_flat(intent, reason=exit_reason, account=account)
                await self._kv_delete(self._close_key(user.id))
                await self.cancel_leftovers(user, account, deadline)
                return ("closed" if we_closed else "no_position"), side, quantity, entry

            side = position.side.value
            quantity = float(position.quantity)
            entry = float(position.entry_price)
            if max_attempts is not None and self._close_attempts(user.id) >= max_attempts:
                await self._expire_close_request(user.id)
                return "failed", side, quantity, entry
            if time.monotonic() >= deadline:
                return "pending", side, quantity, entry
            order = self._reduce_only_market(user.id, window_of(self.ctx.clock()), "close", position.side.opposite, position.quantity, account.mark_price, market.price_tick)
            try:
                tx = await self.tx.send(
                    user_id=user.id, intent_id=intent.id if intent else None, purpose="close",
                    granter=user.inj_address, subaccount_id=user.subaccount_id, orders=[order], deadline=deadline,
                    limited=max_attempts is None,
                )
            except (SignerBusy, ChainUnavailable):
                return "pending", side, quantity, entry
            sent_any = True
            await self._bump_close_attempts(user.id)
            if tx.is_open:
                return "pending", side, quantity, entry

    async def _expire_close_request(self, user_id: int) -> None:
        """Keep an exhausted request until its window ends, or a repeat call would buy new attempts."""
        raw = self._kv_get(self._close_key(user_id))
        if raw is not None and self.ctx.clock() - int(json.loads(raw)["ms"]) >= CLOSE_RETRY_WINDOW_MS:
            await self._kv_delete(self._close_key(user_id))

    async def cancel_leftovers(self, user: User, account: Account, deadline: float) -> None:
        if not account.orders:
            return
        try:
            await self.tx.send(
                user_id=user.id, intent_id=None, purpose="cancel", granter=user.inj_address, subaccount_id=user.subaccount_id,
                orders=[], cancels=list(account.orders), deadline=deadline,
            )
        except (SignerBusy, ChainUnavailable):
            logger.warning("cancel of leftover orders postponed for user %s", user.id)

    async def flatten_orphan(self, user: User, account: Account, deadline: float) -> None:
        """A position with no intent has no known stop price: it cannot be protected, so close it."""
        position = account.position
        if position is None:
            return
        with urgent_requests():
            await self._send_orphan_flatten(user, position, account, deadline)

    async def _send_orphan_flatten(self, user: User, position: Position, account: Account, deadline: float) -> None:
        market = await self.chain.market()
        await self.tx.send(
            user_id=user.id, intent_id=None, purpose="flatten", granter=user.inj_address, subaccount_id=user.subaccount_id,
            orders=[self._reduce_only_market(user.id, window_of(self.ctx.clock()), "flatten", position.side.opposite, position.quantity, account.mark_price, market.price_tick)],
            deadline=deadline,
        )


def _is_transient(exc: Exception) -> bool:
    return isinstance(exc, (SignerBusy, ChainUnavailable)) and not isinstance(exc, OrderRefused)


def _leg_kind(order: RestingOrder, intent: Optional[Intent]) -> OrderKind:
    """The kind by the cid we gave the order. The chain reports no type, and inferring one from
    the mark flips once the mark passes the trigger."""
    if intent is not None:
        if order.cid == make_cid(intent.user_id, intent.window_id, "stop"):
            return OrderKind.STOP
        if order.cid == make_cid(intent.user_id, intent.window_id, "take"):
            return OrderKind.TAKE
    return order.kind


def _live_order(account: Account, kind: OrderKind, position_side: Side, intent: Optional[Intent] = None) -> Optional[RestingOrder]:
    wanted = position_side.opposite
    for order in account.orders:
        if _leg_kind(order, intent) is kind and order.reduce_only and order.side is wanted:
            return order
    return None


def live_stop(account: Account, intent: Optional[Intent] = None) -> Optional[RestingOrder]:
    """A reduce-only stop covering the whole position. A smaller one counts as no stop."""
    position = account.position
    if position is None:
        return None
    stop = _live_order(account, OrderKind.STOP, position.side, intent)
    return stop if stop is not None and stop.quantity >= position.quantity else None


def _pct(numerator: Decimal, denominator: Decimal) -> float:
    if denominator == 0:
        return 0.0
    return float(numerator / denominator * 100)


def _plain(value: Optional[Decimal]) -> str:
    """A number as a person writes it: never scientific notation, no trailing zeros."""
    return "unavailable" if value is None else format(value.normalize(), "f")


def _signed_usd(value: Decimal) -> str:
    return f"{'+' if value >= 0 else '-'}${abs(value):.2f}"
