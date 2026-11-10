"""Pure entry checks. The order of ENTRY_CHECKS is the order in the spec and is load-bearing.

Each check takes `EntryInputs` and returns a `Rejection` or None. No clock reads, no I/O.
`ApiError` and the code-to-HTTP-status map also live here because they are pure.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

from .contracts import STOP_ATR_MENU, TAKE_PROFIT_ATR_MENU, ErrorCode, ErrorOut, Outcome

MARKET_MAX_AGE_MS = 30_000
OBSERVATION_MAX_AGE_MS = 120_000
TICKET_TTL_MS = 30_000
GRANT_MIN_LEFT_MS = 3_600_000  # the grant must outlive the entry, its stop and any close
TRANSFER_JUMP_PCT = Decimal("0.01")
_TEN_THOUSAND = Decimal(10_000)

STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.PAUSED: 403,
    ErrorCode.GRANT_MISSING: 403,
    ErrorCode.GRANTEE_LOW_GAS: 503,
    ErrorCode.MARKET_UNAVAILABLE: 503,
    ErrorCode.STALE_OBSERVATION: 409,
    ErrorCode.STALE_DECISION: 409,
    ErrorCode.INVALID_INTENT: 422,
    ErrorCode.POSITION_OPEN: 409,
    ErrorCode.NO_POSITION: 409,
    ErrorCode.WINDOW_USED: 409,
    ErrorCode.COOLDOWN: 409,
    ErrorCode.DAILY_LOSS_LIMIT: 409,
    ErrorCode.DRAWDOWN_LIMIT: 409,
    ErrorCode.OVER_CAP: 409,
    ErrorCode.TP_TOO_CLOSE: 422,
    ErrorCode.SIZE_TOO_SMALL: 422,
    ErrorCode.BALANCE_TOO_LOW: 409,
    ErrorCode.BOOK_TOO_THIN: 409,
    ErrorCode.SETUP_NOT_MET: 409,
    ErrorCode.TICKET_EXPIRED: 410,
    ErrorCode.TICKET_USED: 409,
    ErrorCode.TICKET_NOT_FOUND: 404,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.CHAIN_REJECTED: 502,
    ErrorCode.INTERNAL: 500,
}


@dataclass(frozen=True)
class Rejection:
    code: ErrorCode
    limit: Optional[str] = None


class ApiError(Exception):
    """Carries an `ErrorOut` and HTTP status up to the route handler."""

    def __init__(self, out: ErrorOut, status: Optional[int] = None) -> None:
        super().__init__(out.code.value)
        self.out = out
        self.status = status if status is not None else STATUS_BY_CODE.get(out.code, 500)


def rejected(code: ErrorCode, limit: Optional[str] = None, retry_after_window: Optional[int] = None) -> ApiError:
    return ApiError(
        ErrorOut(outcome=Outcome.REJECTED, code=code, limit=limit, retry_after_window=retry_after_window)
    )


@dataclass(frozen=True)
class ObservationView:
    user_id: int
    window_id: int
    created_ms: int


@dataclass(frozen=True)
class EntryInputs:
    """Everything an entry decision depends on, already read and sized by the caller."""

    now_ms: int
    window_id: int
    user_id: int
    kill_switch: bool
    user_status: str
    grant_ok: bool
    gas_ok: bool  # the signing key can pay for the entry, its stop and any close
    market_as_of_ms: Optional[int]  # None means the market could not be read
    observation: Optional[ObservationView]
    side: str
    stop_atr: Decimal
    take_profit_atr: Optional[Decimal]
    has_position: bool  # a position, or an intent that is not yet terminal
    window_used: bool
    cooldown_until_window: int
    equity: Decimal
    day_start_equity: Decimal
    peak_equity: Decimal
    daily_loss_limit: Decimal
    max_drawdown: Decimal
    max_equity_usd: Decimal
    take_profit_bps: Optional[Decimal]  # distance of the take-profit from mark
    round_trip_cost_bps: Decimal
    min_tp_cost_mult: Decimal
    quantity: Decimal
    notional: Decimal
    min_notional: Decimal
    margin_needed: Decimal
    available: Decimal
    book_depth_usd: Decimal  # on the side the entry consumes
    exit_depth_usd: Decimal  # on the side a stop or close consumes
    stop_price: Decimal
    stop_worst_notional: Decimal  # quantity at the stop's worst fill price
    worst_loss: Decimal
    grant_expires_ms: Optional[int] = None  # None when the backend reports no expiry


def check_kill_switch(x: EntryInputs) -> Optional[Rejection]:
    return Rejection(ErrorCode.PAUSED, "kill_switch=1") if x.kill_switch else None


def check_user_status(x: EntryInputs) -> Optional[Rejection]:
    if x.user_status == "active":
        return None
    if x.user_status == "paused":
        return Rejection(ErrorCode.PAUSED, "user_status=paused")
    return Rejection(ErrorCode.UNAUTHORIZED, f"user_status={x.user_status}")


def check_grant(x: EntryInputs) -> Optional[Rejection]:
    if not x.grant_ok:
        return Rejection(ErrorCode.GRANT_MISSING)
    if x.grant_expires_ms is not None and x.grant_expires_ms - x.now_ms < GRANT_MIN_LEFT_MS:
        return Rejection(ErrorCode.GRANT_MISSING, f"grant_min_left_ms={GRANT_MIN_LEFT_MS}")
    return None


def check_grantee_gas(x: EntryInputs) -> Optional[Rejection]:
    return None if x.gas_ok else Rejection(ErrorCode.GRANTEE_LOW_GAS)


def check_market_fresh(x: EntryInputs) -> Optional[Rejection]:
    if x.market_as_of_ms is None:
        return Rejection(ErrorCode.MARKET_UNAVAILABLE)
    if x.now_ms - x.market_as_of_ms >= MARKET_MAX_AGE_MS:
        return Rejection(ErrorCode.MARKET_UNAVAILABLE, f"market_age_ms={MARKET_MAX_AGE_MS}")
    return None


def check_observation(x: EntryInputs) -> Optional[Rejection]:
    obs = x.observation
    if obs is None or obs.user_id != x.user_id or obs.window_id != x.window_id:
        return Rejection(ErrorCode.STALE_OBSERVATION)
    if x.now_ms - obs.created_ms >= OBSERVATION_MAX_AGE_MS:
        return Rejection(ErrorCode.STALE_OBSERVATION, f"observation_age_ms={OBSERVATION_MAX_AGE_MS}")
    return None


def check_intent_menu(x: EntryInputs) -> Optional[Rejection]:
    if x.side not in ("long", "short"):
        return Rejection(ErrorCode.INVALID_INTENT, "side=long|short")
    if float(x.stop_atr) not in STOP_ATR_MENU:
        return Rejection(ErrorCode.INVALID_INTENT, "stop_atr=menu")
    if x.take_profit_atr is not None and float(x.take_profit_atr) not in TAKE_PROFIT_ATR_MENU:
        return Rejection(ErrorCode.INVALID_INTENT, "take_profit_atr=menu")
    return None


def check_stop_price(x: EntryInputs) -> Optional[Rejection]:
    """A stop distance can reach past the mark on a bad ATR print, leaving no price to send."""
    return Rejection(ErrorCode.INVALID_INTENT, "stop_price>0") if x.stop_price <= 0 else None


def check_no_position(x: EntryInputs) -> Optional[Rejection]:
    return Rejection(ErrorCode.POSITION_OPEN) if x.has_position else None


def check_window_unused(x: EntryInputs) -> Optional[Rejection]:
    return Rejection(ErrorCode.WINDOW_USED, "per_window=1") if x.window_used else None


def check_cooldown(x: EntryInputs) -> Optional[Rejection]:
    if x.window_id < x.cooldown_until_window:
        return Rejection(ErrorCode.COOLDOWN, f"cooldown_until_window={x.cooldown_until_window}")
    return None


def check_daily_loss(x: EntryInputs) -> Optional[Rejection]:
    """Already over the limit, or this entry's worst loss would not fit what is left of it."""
    if x.day_start_equity <= 0:
        return None
    lost = x.day_start_equity - x.equity
    if lost / x.day_start_equity >= x.daily_loss_limit or x.worst_loss > x.day_start_equity * x.daily_loss_limit - lost:
        return Rejection(ErrorCode.DAILY_LOSS_LIMIT, f"daily_loss={x.daily_loss_limit}")
    return None


def check_drawdown(x: EntryInputs) -> Optional[Rejection]:
    if x.peak_equity > 0 and (x.peak_equity - x.equity) / x.peak_equity >= x.max_drawdown:
        return Rejection(ErrorCode.DRAWDOWN_LIMIT, f"max_drawdown={x.max_drawdown}")
    return None


def check_over_cap(x: EntryInputs) -> Optional[Rejection]:
    if x.equity > x.max_equity_usd:
        return Rejection(ErrorCode.OVER_CAP, f"max_equity_usd={x.max_equity_usd}")
    return None


def check_tp_distance(x: EntryInputs) -> Optional[Rejection]:
    if x.take_profit_bps is None:
        return None
    required = x.round_trip_cost_bps * x.min_tp_cost_mult
    if x.take_profit_bps < required:
        return Rejection(ErrorCode.TP_TOO_CLOSE, f"min_tp_bps={required}")
    return None


def check_size(x: EntryInputs) -> Optional[Rejection]:
    if x.quantity <= 0 or x.notional < x.min_notional or x.stop_worst_notional < x.min_notional:
        return Rejection(ErrorCode.SIZE_TOO_SMALL, f"min_notional={x.min_notional}")
    return None


def check_balance(x: EntryInputs) -> Optional[Rejection]:
    if x.margin_needed > x.available:
        return Rejection(ErrorCode.BALANCE_TOO_LOW, f"margin_needed={x.margin_needed}")
    return None


def check_depth(x: EntryInputs) -> Optional[Rejection]:
    """Both sides: enough to enter, and enough to get out again at the stop or a close."""
    depth = min(x.book_depth_usd, x.exit_depth_usd)
    if x.notional > depth:
        return Rejection(ErrorCode.BOOK_TOO_THIN, f"book_depth_usd={depth}")
    return None


Check = Callable[[EntryInputs], Optional[Rejection]]

ENTRY_CHECKS: tuple[Check, ...] = (
    check_kill_switch,
    check_user_status,
    check_grant,
    check_grantee_gas,
    check_market_fresh,
    check_observation,
    check_intent_menu,
    check_stop_price,
    check_no_position,
    check_window_unused,
    check_cooldown,
    check_daily_loss,
    check_drawdown,
    check_over_cap,
    check_tp_distance,
    check_size,
    check_balance,
    check_depth,
)

# What /v1/state reports: the checks that do not depend on a specific intent.
STATE_CHECKS: tuple[Check, ...] = (
    check_kill_switch,
    check_user_status,
    check_grant,
    check_grantee_gas,
    check_market_fresh,
    check_no_position,
    check_window_unused,
    check_cooldown,
    check_daily_loss,
    check_drawdown,
    check_over_cap,
)


def first_rejection(inputs: EntryInputs, checks: tuple[Check, ...] = ENTRY_CHECKS) -> Optional[Rejection]:
    for check in checks:
        rejection = check(inputs)
        if rejection is not None:
            return rejection
    return None


def blocked_reasons(inputs: EntryInputs) -> list[ErrorCode]:
    """Every state-level reason an entry is currently blocked, in spec order."""
    return [r.code for r in (check(inputs) for check in STATE_CHECKS) if r is not None]


def check_ticket_valid(now_ms: int, expires_ms: int, used_ms: Optional[int]) -> Optional[Rejection]:
    if used_ms is not None:
        return Rejection(ErrorCode.TICKET_USED)
    if now_ms >= expires_ms:
        return Rejection(ErrorCode.TICKET_EXPIRED, f"ticket_ttl_ms={TICKET_TTL_MS}")
    return None


def check_drift(mark_now: Decimal, ticket_mark: Decimal, drift_max: Decimal) -> Optional[Rejection]:
    if abs(mark_now - ticket_mark) / ticket_mark > drift_max:
        return Rejection(ErrorCode.STALE_DECISION, f"drift_max={drift_max}")
    return None


def check_risk_growth(side: str, worst_entry_now: Decimal, worst_entry_at_ticket: Decimal, stop_price: Decimal, max_growth: Decimal) -> Optional[Rejection]:
    """Quantity and stop are frozen at the ticket, so risk grows only as the mark moves against them."""
    risk_now = worst_entry_now - stop_price if side == "long" else stop_price - worst_entry_now
    if risk_now > abs(worst_entry_at_ticket - stop_price) * max_growth:
        return Rejection(ErrorCode.STALE_DECISION, f"max_risk_growth={max_growth}")
    return None


def take_profit_bps(mark: Decimal, take_profit_price: Optional[Decimal]) -> Optional[Decimal]:
    if take_profit_price is None:
        return None
    return abs(take_profit_price - mark) / mark * _TEN_THOUSAND


# ---------------------------------------------------------------------- risk state


@dataclass(frozen=True)
class RiskState:
    day_id: int
    day_start_equity: Decimal
    peak_equity: Decimal
    cooldown_until_window: int = 0


def is_transfer(prev_equity: Optional[Decimal], prev_flat: Optional[bool], equity: Decimal, flat: bool, trade_closed_between: bool) -> bool:
    """A balance move with no realized pnl possible in between can only be a transfer.

    Both values exclude unrealized pnl, so this also holds across two snapshots in a position.
    Realized pnl must never be classified as a transfer: that would rebase losses away and defeat
    the daily-loss and drawdown limits. `trade_closed_between` says pnl may be in the difference.
    """
    if prev_equity is None or prev_flat is None or prev_flat != flat or trade_closed_between:
        return False
    if prev_equity == 0:
        return equity != 0
    return abs(equity - prev_equity) / prev_equity > TRANSFER_JUMP_PCT


def rebased(state: RiskState, reference: Optional[Decimal], equity: Decimal) -> RiskState:
    """Shift day-start and peak by the transfer ratio, preserving day pnl % and drawdown %.

    A transfer must neither create nor erase a loss. Setting both to the new equity would let
    a small deposit clear a tripped daily-loss or drawdown limit. `reference` is the last
    non-zero balance, so withdrawing everything and redepositing clears nothing either.
    """
    if equity == 0:
        return state
    if not reference:
        return RiskState(state.day_id, equity, equity, state.cooldown_until_window)
    ratio = equity / reference
    return RiskState(
        state.day_id,
        state.day_start_equity * ratio if state.day_start_equity > 0 else equity,
        state.peak_equity * ratio if state.peak_equity > 0 else equity,
        state.cooldown_until_window,
    )


def advance_risk_state(
    state: Optional[RiskState],
    equity: Decimal,
    day_id: int,
    *,
    prev_equity: Optional[Decimal] = None,
    prev_flat: Optional[bool] = None,
    flat: bool = False,
    trade_closed_between: bool = False,
    balance: Optional[Decimal] = None,
    rebase_from: Optional[Decimal] = None,
) -> RiskState:
    """One snapshot's worth of change: create, rebase on a transfer, roll the day, track peak.

    `prev_equity` and `balance` exclude unrealized pnl (`balance` defaults to `equity`, the same
    thing when flat). `rebase_from` is the last non-zero balance, defaulting to `prev_equity`.
    """
    if state is None:
        return RiskState(day_id, equity, equity)
    current = state
    now_balance = equity if balance is None else balance
    if is_transfer(prev_equity, prev_flat, now_balance, flat, trade_closed_between):
        current = rebased(current, rebase_from if rebase_from is not None else prev_equity, now_balance)
    if current.day_id != day_id:
        current = RiskState(day_id, equity, current.peak_equity, current.cooldown_until_window)
    if equity > current.peak_equity:
        current = RiskState(current.day_id, current.day_start_equity, equity, current.cooldown_until_window)
    return current
