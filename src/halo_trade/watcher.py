"""Background reconciliation: runs every WATCH_INTERVAL_S and once on startup.

Everything here is derived from persisted rows and chain state, so a restart resumes where
the previous process stopped. The watcher never starts an entry and never re-sends an entry
from a closed window.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Awaitable, Callable, Optional

from . import alerts, limits
from .chain.base import Account, ChainUnavailable, Side
from .config import WINDOW_MS
from .service import DAY_MS, PRE_PROTECT_STAGES, Intent, Service, User, live_stop, window_of
from .txmachine import SignerBusy, grant_bad_key, transaction

logger = logging.getLogger(__name__)

SNAPSHOT_INTERVAL_MS = 60_000
UNKNOWN_ALERT_AGE_MS = 120_000
GRANT_CHECK_INTERVAL_MS = 60_000
GAS_CHECK_INTERVAL_MS = 60_000
USER_STEP_GRACE_S = 0.5  # on top of the user budget, before a stalled step is abandoned
PURGE_INTERVAL_MS = 3_600_000
PURGE_AGE_MS = 7 * DAY_MS
PURGE_LAST_KEY = "watcher_last_purge_ms"  # persisted, so a restart loop cannot purge more often
GRANT_WARN_LEFT_MS = 3 * DAY_MS
THROTTLE_ALERT_MS = 3_600_000  # one alert per hour while a node keeps refusing us
BEAT_DURING_TICK_S = 15.0  # a long tick over many wallets is progress, not a stalled watcher
TERMINAL_STAGES = ("closed", "rejected", "failed", "flattened")

GRANT_EXPIRING_NOTICE = "Your trading grant to this service expires in under 3 days. Renew it on the site or your agent will stop."
GRANT_LOST_NOTICE = "Your agent stopped because its on-chain trading grant is gone. Any open position stays open with the stop it has. Grant access again on the site to restart it."
BALANCE_LOW_NOTICE = "Your agent stopped because your balance fell below the minimum deposit. Add funds on the site to restart it."


@dataclass(frozen=True)
class _Snapshot:
    ts_ms: int
    equity: Decimal
    flat: bool
    balance: Decimal


class Watcher:
    def __init__(self, service: Service, *, user_budget_s: float = 2.0) -> None:
        self.service = service
        self.ctx = service.ctx
        self.user_budget_s = user_budget_s
        self._gas_checked_ms: Optional[int] = None
        self._tick_ms = 0  # one time for the whole tick, so "about once a minute" does not drift
        self._next_flat_poll_ms: dict[int, int] = {}
        self._holding: set[int] = set()  # the last read showed a position
        self._tick_accounts: dict[int, Account] = {}  # read this tick: later steps reuse them
        self._throttled_seen = 0
        self._beat_at = 0.0

    @property
    def conn(self) -> sqlite3.Connection:
        return self.ctx.db

    # ------------------------------------------------------------------ loop
    async def run(self, stop: Optional[asyncio.Event] = None) -> None:
        """Tick immediately (restart recovery) and then every WATCH_INTERVAL_S."""
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("watcher tick failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.ctx.settings.watch_interval_s)
            except TimeoutError:
                pass

    async def watchdog(self, stop: Optional[asyncio.Event] = None) -> None:
        """Runs beside `run`: detects a stalled or dead watcher from its heartbeat row."""
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(1, self.ctx.settings.watch_interval_s))
            except TimeoutError:
                pass
            try:
                await alerts.check_watcher_heartbeat(self.ctx)
            except Exception:
                logger.exception("watcher watchdog check failed")

    async def tick(self) -> None:
        svc = self.service
        self._tick_ms = self.ctx.clock()
        self._tick_accounts = {}
        await self._heartbeat()
        limit_s = self.user_budget_s + USER_STEP_GRACE_S
        await self._guard(svc.tx.resolve_all_open(), limit_s=limit_s)
        await self._guard(self._alert_unknown())
        await self._guard(self._check_grantee_gas(), limit_s=limit_s)
        users = self._due_users()
        await self._for_each(users, self._check_user, limit_s)
        await self._for_each(users, self._snapshot_equity, limit_s)
        await self._guard(self._alert_throttled())
        await self._guard(self._purge_old_rows())
        await self._heartbeat()
        await self._guard(alerts.check_watcher_heartbeat(self.ctx))
        await self._guard(alerts.flush(self.ctx))

    async def _guard(self, awaitable, user: Optional[int] = None, limit_s: Optional[float] = None) -> None:
        """One user's or one step's failure or hang must not stop the rest of the tick."""
        try:
            await asyncio.wait_for(awaitable, limit_s)
        except (ChainUnavailable, SignerBusy, TimeoutError):
            pass
        except Exception:
            logger.exception("watcher step failed (user=%s)", user)

    async def _heartbeat(self) -> None:
        self._beat_at = time.monotonic()
        await self.service._kv_set(alerts.HEARTBEAT_KEY, str(self.ctx.clock()))

    async def _beat_if_due(self) -> None:
        if time.monotonic() - self._beat_at >= BEAT_DURING_TICK_S:
            await self._heartbeat()

    async def _for_each(self, users: list[User], step: Callable[[User], Awaitable[None]], limit_s: float) -> None:
        """A bounded number at once. The limit starts when a user gets a slot, not while it queues."""
        slots = asyncio.Semaphore(self.ctx.settings.watch_concurrency)

        async def one(user: User) -> None:
            async with slots:
                await self._guard(step(user), user=user.id, limit_s=limit_s)
                await self._beat_if_due()

        await asyncio.gather(*(one(user) for user in users))

    def _users(self) -> list[User]:
        """Active and paused users, plus anyone still holding a non-terminal intent."""
        rows = self.conn.execute(
            "SELECT DISTINCT u.* FROM users u LEFT JOIN intents i ON i.user_id=u.id "
            "AND i.stage NOT IN (?,?,?,?) "
            "WHERE u.status IN ('active','paused') OR i.id IS NOT NULL ORDER BY u.id",
            TERMINAL_STAGES,
        ).fetchall()
        return [User.from_row(r) for r in rows]

    def _fast_users(self) -> set[int]:
        """Who needs a read every tick: an unfinished order or tx, a close being driven, a wallet
        just started, or a position on the last read. All from our own rows, so no chain cost."""
        flat_ms = self.ctx.settings.watch_flat_interval_s * 1000
        rows = self.conn.execute(
            "SELECT user_id FROM intents WHERE stage NOT IN (?,?,?,?) "
            "UNION SELECT user_id FROM txs WHERE status IN ('signed','sent') "
            "UNION SELECT user_id FROM strategy_runs WHERE status='active' AND started_ms>?",
            (*TERMINAL_STAGES, self._tick_ms - flat_ms),
        ).fetchall()
        closing = self.conn.execute("SELECT key FROM system_state WHERE key LIKE 'close_req:%'").fetchall()
        return {row["user_id"] for row in rows} | {int(row["key"].split(":")[1]) for row in closing} | self._holding

    def _due_users(self) -> list[User]:
        fast = self._fast_users()
        due = []
        for user in self._users():
            if user.id in fast or self._tick_ms >= self._next_flat_poll_ms.get(user.id, 0):
                due.append(user)
                self._next_flat_poll_ms[user.id] = self._next_flat_poll(user.id)
        return due

    def _next_flat_poll(self, user_id: int) -> int:
        """After a wallet's first read its next one is spread over the interval, so wallets
        started together are not all read in the same tick. After that, one interval apart."""
        flat_s = self.ctx.settings.watch_flat_interval_s
        if user_id in self._next_flat_poll_ms or flat_s == 0:
            return self._tick_ms + flat_s * 1000
        return self._tick_ms + (user_id * 37 % flat_s + 1) * 1000

    # ------------------------------------------------------------------ per user
    async def _check_user(self, user: User) -> None:
        lock = self.service.user_lock(user.id)
        if lock.locked():
            return  # a request is working on this user right now
        async with lock:
            deadline = time.monotonic() + self.user_budget_s
            await self._manage_user(user, deadline)

    async def _manage_user(self, user: User, deadline: float) -> None:
        svc = self.service
        intent = svc.open_intent(user.id)
        if intent is not None and intent.stage in PRE_PROTECT_STAGES:
            intent = await svc.advance(intent.id, deadline)
        if svc.close_requested(user.id):
            await svc.drive_close_request(user, deadline)
        intent = svc.open_intent(user.id)
        account = await self._read_for_tick(user)
        await self._reconcile(user, intent, account, deadline)
        await self._check_grant(user, account)

    async def _read_for_tick(self, user: User) -> Account:
        account = await self.service.read_account(user)
        self._tick_accounts[user.id] = account
        if account.position is None:
            self._holding.discard(user.id)
        else:
            self._holding.add(user.id)
        return account

    def _key(self, kind: str, user_id: int) -> str:
        return f"{kind}:{user_id}"

    def _since_height(self, kind: str, user_id: int) -> Optional[int]:
        raw = self.service._kv_get(self._key(kind, user_id))
        return None if raw is None else int(raw)

    async def _clear(self, kind: str, user_id: int) -> None:
        await self.service._kv_delete(self._key(kind, user_id))

    async def _first_seen(self, kind: str, user_id: int, height: int) -> int:
        existing = self._since_height(kind, user_id)
        if existing is not None:
            return existing
        await self.service._kv_set(self._key(kind, user_id), str(height))
        return height

    async def _alert_if_stopless(self, user: User, intent: Intent, account: Account) -> None:
        """Whatever stage the intent is stuck in, a position past grace with no stop is reported."""
        if live_stop(account, intent) is not None:
            await self._clear("stopless", user.id)
            return
        first = await self._first_seen("stopless", user.id, account.height)
        if account.height - first > self.ctx.settings.stop_grace_blocks:
            await alerts.raise_alert(
                self.ctx, level="critical", kind="stop_missing", user_id=user.id,
                detail=f"intent={intent.id} stage={intent.stage}", dedupe_key=f"stop_missing:{intent.id}:{window_of(self.ctx.clock())}",
            )

    async def _reconcile(self, user: User, intent: Optional[Intent], account: Account, deadline: float) -> None:
        svc = self.service
        grace = self.ctx.settings.stop_grace_blocks
        position = account.position

        if position is None:
            await self._clear("stopless", user.id)
            await self._clear("breach", user.id)
            if intent is not None and intent.stage == "protected":
                if not await svc.finalize_if_gone(intent, user, account):
                    return
                intent = None
            if account.orders and not svc.tx.open_for_user(user.id) and (intent is None or intent.stage not in PRE_PROTECT_STAGES):
                await svc.cancel_leftovers(user, account, deadline)
            return

        if intent is None:
            # A position this service did not open has no stop price to place.
            if live_stop(account) is not None:
                await self._clear("stopless", user.id)
                return
            first = await self._first_seen("stopless", user.id, account.height)
            if account.height - first > grace:
                await alerts.raise_alert(
                    self.ctx, level="critical", kind="stop_missing", user_id=user.id,
                    detail=f"orphan_position qty={position.quantity}", dedupe_key=f"stop_missing:orphan:{user.id}:{window_of(self.ctx.clock())}",
                )
                await svc.flatten_orphan(user, account, deadline)
            return

        if intent.stage != "protected":
            await self._alert_if_stopless(user, intent, account)
            return

        # A live position past its own stop: the trigger fired on a thin book or has not fired.
        if self._breached(intent, account.mark_price):
            first = await self._first_seen("breach", user.id, account.height)
            if account.height - first > grace:
                await alerts.raise_alert(
                    self.ctx, level="critical", kind="stop_missing", user_id=user.id,
                    detail=f"trigger_breached intent={intent.id}", dedupe_key=f"stop_breach:{intent.id}",
                )
                await svc.force_close(user, intent, deadline)
                refreshed = svc.open_intent(user.id)
                if refreshed is None:
                    await self._clear("breach", user.id)
            return
        await self._clear("breach", user.id)

        if live_stop(account, intent) is not None:
            await self._clear("stopless", user.id)
            await svc.maybe_place_take(intent, deadline)
            await svc._record_result(svc.intent(intent.id))
            return

        first = await self._first_seen("stopless", user.id, account.height)
        if account.height - first > grace:
            await alerts.raise_alert(
                self.ctx, level="critical", kind="stop_missing", user_id=user.id,
                detail=f"intent={intent.id}", dedupe_key=f"stop_missing:{intent.id}:{first}",
            )
            await svc.rearm_stop(intent)
            await svc.advance(intent.id, deadline)
            await self._clear("stopless", user.id)

    def _breached(self, intent: Intent, mark: Decimal) -> bool:
        return mark <= intent.stop_price if intent.side is Side.LONG else mark >= intent.stop_price

    # ------------------------------------------------------------------ alerts
    async def _alert_unknown(self) -> None:
        cutoff = self.ctx.clock() - UNKNOWN_ALERT_AGE_MS
        for tx in self.service.tx.unresolved_older_than(cutoff):
            await alerts.raise_alert(
                self.ctx, level="critical", kind="tx_unknown", user_id=tx.user_id,
                detail=f"purpose={tx.purpose} tx={tx.tx_hash}", dedupe_key=f"tx_unknown:{tx.tx_hash}",
            )

    async def _alert_throttled(self) -> None:
        """Counted in the chain layer, which has no database. Reported here, at most hourly."""
        total = getattr(self.service.chain, "throttled_total", 0)
        if total <= self._throttled_seen:
            return
        self._throttled_seen = total
        await alerts.raise_alert(
            self.ctx, level="warning", kind="chain_throttled",
            detail=f"total={total}", dedupe_key=f"chain_throttled:{self._tick_ms // THROTTLE_ALERT_MS}",
        )

    async def _check_grantee_gas(self) -> None:
        now = self.ctx.clock()
        if self._gas_checked_ms is not None and now - self._gas_checked_ms < GAS_CHECK_INTERVAL_MS:
            return
        balance = await self.service.chain.grantee_balance_inj()
        self._gas_checked_ms = now
        if balance < self.ctx.settings.grantee_min_inj:
            await alerts.raise_alert(
                self.ctx, level="critical", kind="grantee_low_gas",
                detail=f"inj={balance}", dedupe_key=f"grantee_low_gas:{now // DAY_MS}",
            )

    async def _check_grant(self, user: User, account: Account) -> None:
        row = self.conn.execute("SELECT grant_checked_ms FROM users WHERE id=?", (user.id,)).fetchone()
        now = self._tick_ms
        if row["grant_checked_ms"] is not None and now - row["grant_checked_ms"] < GRANT_CHECK_INTERVAL_MS:
            return
        grant = await self.service.chain.check_grant(user.inj_address, user.subaccount_id)
        if grant.ok:
            await self.service._kv_delete(grant_bad_key(user.id))
        else:
            await self.service._kv_set(grant_bad_key(user.id), str(now))
        async with self.ctx.db_lock:
            with transaction(self.conn) as conn:
                conn.execute("UPDATE users SET grant_checked_ms=? WHERE id=?", (now, user.id))
        if not grant.ok and user.status != "revoked":
            await alerts.raise_alert(
                self.ctx, level="critical", kind="grant_lost", user_id=user.id,
                detail=f"missing={len(grant.missing)}", dedupe_key=f"grant_lost:{user.id}:{now // DAY_MS}",
            )
            await self.service.auto_pause(user.id, "grant_lost", account.equity, GRANT_LOST_NOTICE)
        elif grant.ok and grant.expires_ms is not None and grant.expires_ms - now < GRANT_WARN_LEFT_MS:
            # Keyed by the expiry, so a renewed grant can warn again when its own time comes.
            await self.service._push(user.id, "grant_expiring", GRANT_EXPIRING_NOTICE, f"grant_expiring:{user.id}:{grant.expires_ms}")

    # ------------------------------------------------------------------ retention
    async def _purge_old_rows(self) -> None:
        """Drop observations and spent tickets past the retention age, at most once an hour."""
        now = self.ctx.clock()
        last = self.service._kv_get(PURGE_LAST_KEY)
        if last is not None and now - int(last) < PURGE_INTERVAL_MS:
            return
        cutoff = now - PURGE_AGE_MS
        async with self.ctx.db_lock:
            with transaction(self.conn) as conn:
                conn.execute("DELETE FROM observations WHERE created_ms<?", (cutoff,))
                conn.execute("DELETE FROM tickets WHERE created_ms<? AND (used_ms IS NOT NULL OR expires_ms<=?)", (cutoff, now))
        await self.service._kv_set(PURGE_LAST_KEY, str(now))

    # ------------------------------------------------------------------ equity
    def _last_snapshot(self, user_id: int) -> Optional[_Snapshot]:
        row = self.conn.execute(
            "SELECT ts_ms, equity, flat, balance FROM equity_snapshots WHERE user_id=? ORDER BY ts_ms DESC LIMIT 1", (user_id,)
        ).fetchone()
        if row is None:
            return None
        return _Snapshot(row["ts_ms"], Decimal(row["equity"]), bool(row["flat"]), Decimal(row["balance"] or row["equity"]))

    def _last_nonzero_balance(self, user_id: int) -> Optional[Decimal]:
        row = self.conn.execute(
            "SELECT COALESCE(balance, equity) AS value FROM equity_snapshots WHERE user_id=? AND CAST(COALESCE(balance, equity) AS REAL)<>0 "
            "ORDER BY ts_ms DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return None if row is None else Decimal(row["value"])

    def _trade_closed_between(self, user_id: int, since_ms: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM trades WHERE user_id=? AND closed_ms IS NOT NULL AND closed_ms>=? LIMIT 1", (user_id, since_ms)
        ).fetchone()
        return row is not None

    def _pnl_may_have_moved(self, user: User, account: Account, last: Optional[_Snapshot]) -> bool:
        """Realized pnl may be in the balance difference: a trade closed since the last snapshot,
        or an open intent whose position is gone or changed size and is not settled yet."""
        if last is not None and self._trade_closed_between(user.id, last.ts_ms):
            return True
        intent = self.service.open_intent(user.id)
        return intent is not None and (account.position is None or account.position.quantity != intent.quantity)

    async def _snapshot_equity(self, user: User) -> None:
        now = self._tick_ms
        last = self._last_snapshot(user.id)
        if last is not None and now - last.ts_ms < SNAPSHOT_INTERVAL_MS:
            return
        account = self._tick_accounts.get(user.id) or await self._read_for_tick(user)
        flat = account.position is None
        balance = account.equity if flat else account.equity - account.position.unrealized_pnl
        day_id = now // DAY_MS
        async with self.ctx.db_lock:
            with transaction(self.conn) as conn:
                # Read inside the transaction: a close elsewhere may have just set a cooldown.
                new_state = limits.advance_risk_state(
                    self.service._risk_row(user.id),
                    account.equity,
                    day_id,
                    prev_equity=last.balance if last else None,
                    prev_flat=last.flat if last else None,
                    flat=flat,
                    trade_closed_between=self._pnl_may_have_moved(user, account, last),
                    balance=balance,
                    rebase_from=self._last_nonzero_balance(user.id),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO equity_snapshots (user_id, ts_ms, equity, flat, balance) VALUES (?,?,?,?,?)",
                    (user.id, now, str(account.equity), 1 if flat else 0, str(balance)),
                )
                self.service._write_risk(conn, user.id, new_state)
        await self._pause_if_below_minimum(user, account)

    async def _pause_if_below_minimum(self, user: User, account: Account) -> None:
        """Only once flat and settled: with a position, equity moves with the mark."""
        if account.position is not None or self.service.open_intent(user.id) is not None:
            return
        if account.equity < self.ctx.settings.risk.min_deposit_usd:
            await self.service.auto_pause(user.id, "balance_low", account.equity, BALANCE_LOW_NOTICE)


__all__ = ["Watcher", "WINDOW_MS"]
