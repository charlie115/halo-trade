"""Read-only views of one owner's account, for the dashboard, the Telegram commands and the chat assistant.

Straight from the chain and never through `Service.read_account`, which keeps money-path state
these lanes must not touch. The numbers here are for showing: no order is sized from them.
"""
from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from .chain.base import Account, ChainUnavailable, Position
from .service import Service, User, window_of

CHAIN_READ_TIMEOUT_S = 3.0  # overall, so a stalled chain cannot hold a worker
ACCOUNT_CACHE_MS = 10_000
UNAVAILABLE = "unavailable"


def clean_text(value: Any) -> str:
    """Text only, without the lone surrogates JSON can carry but SQLite and httpx cannot."""
    return value.encode("utf-8", "replace").decode("utf-8").strip() if isinstance(value, str) else ""


@dataclass(frozen=True)
class _CachedAccount:
    expires_ms: int
    account: Optional[Account]


_ACCOUNT_CACHE: "weakref.WeakKeyDictionary[Service, dict[int, _CachedAccount]]" = weakref.WeakKeyDictionary()


async def read_account(service: Service, user: User, *, fresh: bool = False) -> Optional[Account]:
    """None means it could not be read, which no caller may present as zero or flat. Cached per
    user, and a failed read counts too, so a burst of messages cannot pile onto a stalled chain."""
    now = service.ctx.clock()
    cache = _ACCOUNT_CACHE.setdefault(service, {})
    hit = cache.get(user.id)
    if fresh or hit is None or hit.expires_ms <= now:
        try:
            account = await asyncio.wait_for(service.chain.account(user.inj_address, user.subaccount_id), timeout=CHAIN_READ_TIMEOUT_S)
        except (ChainUnavailable, TimeoutError):
            account = None
        hit = _CachedAccount(now + ACCOUNT_CACHE_MS, account)
        cache[user.id] = hit
    return _unless_lagging(service, user, hit.account)


def _unless_lagging(service: Service, user: User, account: Optional[Account]) -> Optional[Account]:
    """A node behind what the money path already saw can claim a live position is gone. Reads the
    highest height the service has seen and never writes it."""
    if account is None or account.height < service._account_heights.get(user.id, 0):
        return None
    return account


def forget_account(service: Service, user_id: int) -> None:
    _ACCOUNT_CACHE.setdefault(service, {}).pop(user_id, None)


def position_view(service: Service, user: User, account: Optional[Account]) -> Any:
    """None only when the read succeeded and the account is flat."""
    if account is None:
        return UNAVAILABLE
    position = service._position_out(user, account, window_of(service.ctx.clock()))
    return None if position is None else position.model_dump()


async def read_status(service: Service, user: User) -> dict[str, Any]:
    strategy = service.conn.execute("SELECT preset, next_run_ms FROM strategies WHERE user_id=?", (user.id,)).fetchone()
    account = await read_account(service, user)
    next_decision_s = None
    if strategy is not None and user.status == "active":
        next_decision_s = max(0, (strategy["next_run_ms"] - service.ctx.clock()) // 1000)
    return {
        "agent": user.status,
        "halted_by_operator": service.kill_switch(),
        "preset": strategy["preset"] if strategy else None,
        "next_decision_in_s": next_decision_s,
        "equity_usd": UNAVAILABLE if account is None else float(account.equity),
        "available_usd": UNAVAILABLE if account is None else float(account.available),
        "position": position_view(service, user, account),
    }


# ---------------------------------------------------------------------- results in units of planned loss
def unrealized_r(position: Position, stop: Optional[Decimal]) -> Optional[float]:
    """Open profit in units of the planned loss, or None when there is no planned loss to compare with."""
    planned = None if stop is None else abs(position.entry_price - stop) * position.quantity
    return None if not planned else round(float(position.unrealized_pnl / planned), 2)


def result_r(row: Any) -> Optional[float]:
    """A closed trade's profit in units of its planned loss, or None when either side of that is not known."""
    try:
        risk = abs(Decimal(row["entry_price"]) - Decimal(row["stop_price"])) * Decimal(row["quantity"])
        return None if risk <= 0 else round(float(Decimal(row["pnl_usd"]) / risk), 3)
    except (InvalidOperation, TypeError):
        return None
