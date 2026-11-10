"""Pure features from 1h candles. Decimal internally, converted to float only on the wire.

Only candles that closed before the current hour are used: a candle whose `open_ms` is in the
current hour is ignored even if the data source returned it. Rate-style outputs are percent,
except `round_trip_cost_bps`.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from .chain.base import Candle

HOUR_MS = 3_600_000
QUARTER_MS = 900_000
ATR_FAST = 14
ATR_SLOW = 50
EMA_FAST = 20
EMA_SLOW = 50
# 1 prior close for the first true range, ATR_SLOW true ranges, ATR_SLOW closes for the EMA.
MIN_CANDLES = ATR_SLOW + 1
MAX_CANDLE_AGE_MS = 2 * HOUR_MS  # hours without trades have no candle, longer gaps mean bad data
MAX_RETURN_GAP_MS = 2 * HOUR_MS  # how far before its target hour a return's base candle may be
CANDLES_TO_REQUEST = 80
CANDLES_15M_TO_REQUEST = 80
MAX_15M_CANDLE_AGE_MS = 2 * QUARTER_MS
MAX_15M_GAP_MS = 2 * QUARTER_MS
TREND_FLAT_BAND = Decimal("0.0005")  # ema20 within 5 bps of ema50 is flat
_HUNDRED = Decimal(100)
_TEN_THOUSAND = Decimal(10_000)


class InsufficientData(ValueError):
    """Not enough completed hourly candles to compute features."""


@dataclass(frozen=True)
class Features:
    atr_1h: Decimal  # ATR14
    atr_50: Decimal
    atr_1h_pct: Decimal
    ema20: Decimal
    ema50: Decimal
    trend: str
    ret_1h_pct: Decimal
    ret_4h_pct: Decimal
    ret_24h_pct: Decimal
    volatility_ratio: Decimal
    spread_bps: Decimal
    round_trip_cost_bps: Decimal


@dataclass(frozen=True)
class Features15m:
    ret_pct: Decimal
    atr_pct: Decimal
    trend: str


def complete_candles(candles: Sequence[Candle], now_ms: int, period_ms: int = HOUR_MS) -> list[Candle]:
    """Candles with `open_ms` strictly before the current bar, oldest first, de-duplicated."""
    bar_start = now_ms - now_ms % period_ms
    by_open = {candle.open_ms: candle for candle in candles if candle.open_ms < bar_start}
    return [by_open[key] for key in sorted(by_open)]


def true_ranges(candles: Sequence[Candle]) -> list[Decimal]:
    """True range for each candle after the first."""
    ranges = []
    for previous, current in zip(candles, candles[1:]):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return ranges


def atr(candles: Sequence[Candle], period: int) -> Decimal:
    """Average true range: the simple mean of the last `period` true ranges."""
    ranges = true_ranges(candles)
    if len(ranges) < period:
        raise InsufficientData(f"atr({period}) needs {period + 1} candles")
    window = ranges[-period:]
    return sum(window, Decimal(0)) / Decimal(period)


def ema(closes: Sequence[Decimal], period: int) -> Decimal:
    """Exponential moving average seeded with the simple mean of the first `period` closes."""
    if len(closes) < period:
        raise InsufficientData(f"ema({period}) needs {period} closes")
    value = sum(closes[:period], Decimal(0)) / Decimal(period)
    alpha = Decimal(2) / Decimal(period + 1)
    for close in closes[period:]:
        value = alpha * close + (1 - alpha) * value
    return value


def rsi(closes: Sequence[Decimal], period: int = ATR_FAST) -> Decimal:
    """Wilder's RSI over `period`, 0 to 100. A market that did not move reads as 50."""
    if len(closes) < period + 1:
        raise InsufficientData(f"rsi({period}) needs {period + 1} closes")
    moves = [later - earlier for earlier, later in zip(closes, closes[1:])]
    gains = sum((max(move, Decimal(0)) for move in moves[:period]), Decimal(0)) / period
    losses = sum((max(-move, Decimal(0)) for move in moves[:period]), Decimal(0)) / period
    for move in moves[period:]:
        gains = (gains * (period - 1) + max(move, Decimal(0))) / period
        losses = (losses * (period - 1) + max(-move, Decimal(0))) / period
    if gains == 0 and losses == 0:
        return Decimal(50)
    return _HUNDRED if losses == 0 else _HUNDRED - _HUNDRED / (1 + gains / losses)


def _return_over(candles: Sequence[Candle], span_ms: int, max_gap_ms: int) -> Decimal:
    """Percent return over clock time, not candle count, since a bar with no trades has no candle.
    The base is the candle at the target time, else the nearest earlier one within `max_gap_ms`."""
    target = candles[-1].open_ms - span_ms
    base = next((candle for candle in reversed(candles) if candle.open_ms <= target), None)
    if base is None or target - base.open_ms > max_gap_ms:
        raise InsufficientData(f"no candle near {span_ms} ms back")
    return (candles[-1].close - base.close) / base.close * _HUNDRED


def pct_return(candles: Sequence[Candle], hours_back: int) -> Decimal:
    return _return_over(candles, hours_back * HOUR_MS, MAX_RETURN_GAP_MS)


def trend_of(ema20: Decimal, ema50: Decimal) -> str:
    gap = (ema20 - ema50) / ema50
    if gap > TREND_FLAT_BAND:
        return "up"
    if gap < -TREND_FLAT_BAND:
        return "down"
    return "flat"


def spread_bps(best_bid: Decimal, best_ask: Decimal) -> Decimal:
    mid = (best_bid + best_ask) / 2
    return (best_ask - best_bid) / mid * _TEN_THOUSAND


def round_trip_cost_bps(taker_fee: Decimal, best_bid: Decimal, best_ask: Decimal) -> Decimal:
    """Taker fee on both legs plus the spread paid once across the round trip."""
    return taker_fee * 2 * _TEN_THOUSAND + spread_bps(best_bid, best_ask)


def compute(
    candles: Sequence[Candle],
    now_ms: int,
    taker_fee: Decimal,
    best_bid: Decimal,
    best_ask: Decimal,
) -> Features:
    usable = complete_candles(candles, now_ms)
    if len(usable) < MIN_CANDLES:
        raise InsufficientData(f"need {MIN_CANDLES} completed candles, got {len(usable)}")
    if now_ms - (usable[-1].open_ms + HOUR_MS) > MAX_CANDLE_AGE_MS:
        raise InsufficientData("the newest closed candle is too old")
    closes = [candle.close for candle in usable]
    fast = atr(usable, ATR_FAST)
    slow = atr(usable, ATR_SLOW)
    ema20 = ema(closes, EMA_FAST)
    ema50 = ema(closes, EMA_SLOW)
    return Features(
        atr_1h=fast,
        atr_50=slow,
        atr_1h_pct=fast / closes[-1] * _HUNDRED,
        ema20=ema20,
        ema50=ema50,
        trend=trend_of(ema20, ema50),
        ret_1h_pct=pct_return(usable, 1),
        ret_4h_pct=pct_return(usable, 4),
        ret_24h_pct=pct_return(usable, 24),
        volatility_ratio=fast / slow if slow != 0 else Decimal(0),
        spread_bps=spread_bps(best_bid, best_ask),
        round_trip_cost_bps=round_trip_cost_bps(taker_fee, best_bid, best_ask),
    )


def compute_15m(candles: Sequence[Candle], now_ms: int) -> Features15m:
    """From closed 15-minute candles only. Raises `InsufficientData` rather than ever guessing."""
    usable = complete_candles(candles, now_ms, QUARTER_MS)
    if len(usable) < EMA_SLOW:
        raise InsufficientData(f"need {EMA_SLOW} completed 15m candles, got {len(usable)}")
    if now_ms - (usable[-1].open_ms + QUARTER_MS) > MAX_15M_CANDLE_AGE_MS:
        raise InsufficientData("the newest closed 15m candle is too old")
    closes = [candle.close for candle in usable]
    return Features15m(
        ret_pct=_return_over(usable, QUARTER_MS, MAX_15M_GAP_MS),
        atr_pct=atr(usable, ATR_FAST) / closes[-1] * _HUNDRED,
        trend=trend_of(ema(closes, EMA_FAST), ema(closes, EMA_SLOW)),
    )
