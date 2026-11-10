"""Pure position sizing, per the spec. Decimal only; every rule is a separate function.

Rounding (hard rule 9): prices and quantities go to market ticks in the direction that cannot
increase risk. Quantity rounds down; a stop rounds toward the mark; take-profit rounds toward
the mark; a market entry's worst price rounds so it never exceeds the collar; a stop's worst
price rounds so it never exceeds `STOP_WORST_PCT`.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Optional

from .chain.base import MarketInfo, Side
from .config import Risk


def round_down(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def round_up(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def risk_usd(equity: Decimal, risk_per_trade: Decimal) -> Decimal:
    return equity * risk_per_trade


def stop_distance(mark: Decimal, atr_1h: Decimal, stop_atr: Decimal, min_stop_pct: Decimal) -> Decimal:
    return max(stop_atr * atr_1h, mark * min_stop_pct)


def stop_price(side: Side, mark: Decimal, distance: Decimal, tick: Decimal) -> Decimal:
    """Rounded so the stop distance does not grow."""
    if side is Side.LONG:
        return round_up(mark - distance, tick)
    return round_down(mark + distance, tick)


def raw_quantity(risk: Decimal, distance: Decimal) -> Decimal:
    return risk / distance


def cap_quantity(quantity: Decimal, equity: Decimal, max_leverage: Decimal, internal_cap: Decimal, mark: Decimal) -> Decimal:
    return min(quantity, equity * max_leverage * internal_cap / mark)


def round_quantity(quantity: Decimal, tick: Decimal) -> Decimal:
    return round_down(quantity, tick)


def notional(quantity: Decimal, mark: Decimal) -> Decimal:
    return quantity * mark


def margin_needed(notional_usd: Decimal, max_leverage: Decimal) -> Decimal:
    return notional_usd / max_leverage


def take_profit_price(side: Side, mark: Decimal, atr_1h: Decimal, tp_atr: Optional[Decimal], tick: Decimal) -> Optional[Decimal]:
    """Rounded toward the mark, so rounding can only bring the target closer."""
    if tp_atr is None:
        return None
    distance = tp_atr * atr_1h
    if side is Side.LONG:
        return round_down(mark + distance, tick)
    return round_up(mark - distance, tick)


def worst_loss(quantity: Decimal, entry_worst: Decimal, stop_worst: Decimal, taker_fee: Decimal) -> Decimal:
    """Entry filled at the collar, stop filled at its worst price, the taker fee on both legs."""
    return quantity * abs(entry_worst - stop_worst) + quantity * (entry_worst + stop_worst) * taker_fee


def entry_worst_price(side: Side, mark: Decimal, collar: Decimal, tick: Decimal) -> Decimal:
    """Worst acceptable fill for a market entry: rounded so it never leaves the collar."""
    if side is Side.LONG:
        return round_down(mark * (1 + collar), tick)
    return round_up(mark * (1 - collar), tick)


def stop_worst_price(order_side: Side, trigger: Decimal, stop_worst_pct: Decimal, tick: Decimal) -> Decimal:
    """Worst acceptable fill for a stop. `order_side` is the stop's own direction."""
    if order_side is Side.SHORT:
        return round_up(trigger * (1 - stop_worst_pct), tick)
    return round_down(trigger * (1 + stop_worst_pct), tick)


def close_worst_price(order_side: Side, mark: Decimal, close_worst_pct: Decimal, tick: Decimal) -> Decimal:
    """Worst acceptable fill for a reduce-only market close. Wider than a stop: getting out matters most."""
    return stop_worst_price(order_side, mark, close_worst_pct, tick)


@dataclass(frozen=True)
class Sizing:
    risk_usd: Decimal
    stop_distance: Decimal
    stop_price: Decimal
    take_profit_price: Optional[Decimal]
    quantity: Decimal
    notional: Decimal
    margin_needed: Decimal
    worst_loss: Decimal
    stop_worst_price: Decimal
    entry_worst_price: Decimal


def compute(
    side: Side,
    equity: Decimal,
    mark: Decimal,
    atr_1h: Decimal,
    stop_atr: Decimal,
    take_profit_atr: Optional[Decimal],
    market: MarketInfo,
    risk: Risk,
    day_budget: Optional[Decimal] = None,
) -> Sizing:
    risk_amount = risk_usd(equity, risk.risk_per_trade)
    if day_budget is not None:  # what is left of today's loss limit caps the worst case too
        risk_amount = min(risk_amount, max(day_budget, Decimal(0)))
    distance = stop_distance(mark, atr_1h, stop_atr, risk.min_stop_pct)
    stop = stop_price(side, mark, distance, market.price_tick)
    entry_worst = entry_worst_price(side, mark, risk.collar, market.price_tick)
    stop_worst = stop_worst_price(side.opposite, stop, risk.stop_worst_pct, market.price_tick)
    # `risk_per_trade` budgets the worst case: entry at the collar, stop at its worst fill, both fees.
    quantity = raw_quantity(risk_amount, worst_loss(Decimal(1), entry_worst, stop_worst, market.taker_fee))
    quantity = cap_quantity(quantity, equity, risk.max_leverage, risk.internal_cap, mark)
    quantity = round_quantity(quantity, market.quantity_tick)
    notional_usd = notional(quantity, mark)
    return Sizing(
        risk_usd=risk_amount,
        stop_distance=distance,
        stop_price=stop,
        take_profit_price=take_profit_price(side, mark, atr_1h, take_profit_atr, market.price_tick),
        quantity=quantity,
        notional=notional_usd,
        margin_needed=margin_needed(notional_usd, risk.max_leverage),
        worst_loss=worst_loss(quantity, entry_worst, stop_worst, market.taker_fee),
        stop_worst_price=stop_worst,
        entry_worst_price=entry_worst,
    )


def implied_exit_price(side: Side, entry_price: Decimal, quantity: Decimal, pnl: Decimal, taker_fee: Decimal) -> Decimal:
    """Exit price implied by an equity change net of taker fees on both legs.

    The chain interface reports no fills, so this reconstructs the exit from the money that
    actually moved. `pnl` is net of fees.
    """
    per_unit = pnl / quantity
    if side is Side.LONG:
        return (per_unit + entry_price * (1 + taker_fee)) / (1 - taker_fee)
    return (entry_price * (1 - taker_fee) - per_unit) / (1 + taker_fee)
