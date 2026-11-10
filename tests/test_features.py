"""Pure feature computations from hourly candles."""
from decimal import Decimal as D

import pytest

from halo_trade import features as F
from halo_trade.chain.base import Candle

HOUR = F.HOUR_MS
NOW = 500_000 * HOUR + 1_800_000  # 30 minutes into an hour
THIS_HOUR = NOW - NOW % HOUR


def candle(hours_ago: int, o: str, h: str, l: str, c: str) -> Candle:
    return Candle(THIS_HOUR - hours_ago * HOUR, D(o), D(h), D(l), D(c))


def flat_series(n: int, close: str = "100", rng: str = "2") -> list[Candle]:
    half = D(rng) / 2
    return [Candle(THIS_HOUR - i * HOUR, D(close), D(close) + half, D(close) - half, D(close)) for i in range(n, 0, -1)]


def test_current_hour_candle_is_excluded():
    series = flat_series(60)
    poison = Candle(THIS_HOUR, D("1"), D("100000"), D("1"), D("100000"))
    assert F.complete_candles(series + [poison], NOW) == series


def test_candle_opening_exactly_at_hour_start_is_current_hour():
    series = [candle(1, "1", "2", "1", "1"), candle(0, "9", "9", "9", "9")]
    assert F.complete_candles(series, NOW) == [series[0]]


def test_complete_candles_sorted_and_deduped():
    a = candle(2, "1", "1", "1", "1")
    b = candle(1, "2", "2", "2", "2")
    b2 = candle(1, "3", "3", "3", "3")
    assert F.complete_candles([b, a, b2], NOW) == [a, b2]


def test_atr_is_simple_mean_of_true_ranges():
    series = [
        candle(4, "100", "101", "99", "100"),
        candle(3, "100", "102", "100", "101"),  # TR 2 (h-l=2)
        candle(2, "101", "103", "101", "102"),  # TR 2
        candle(1, "102", "106", "102", "105"),  # TR 4
    ]
    assert F.atr(series, 3) == (D("2") + D("2") + D("4")) / 3


def test_true_range_uses_previous_close_gap():
    series = [candle(2, "100", "100", "100", "100"), candle(1, "110", "111", "109", "110")]
    assert F.true_ranges(series) == [D("11")]  # |high - prev close| = 11 beats h-l = 2


def test_atr_requires_enough_candles():
    with pytest.raises(F.InsufficientData):
        F.atr(flat_series(5), 14)


def test_ema_of_constant_series_is_constant():
    closes = [D("50")] * 30
    assert F.ema(closes, 20) == D("50")


def test_ema_reacts_to_new_closes():
    closes = [D("100")] * 20 + [D("110")] * 5
    value = F.ema(closes, 20)
    assert D("100") < value < D("110")


def closes_at(*pairs: tuple[int, str]) -> list[Candle]:
    """(hours ago, close) pairs, oldest first."""
    return [candle(hours_ago, close, close, close, close) for hours_ago, close in pairs]


def test_pct_return():
    series = closes_at((4, "100"), (3, "100"), (2, "100"), (1, "110"))
    assert F.pct_return(series, 1) == D("10")
    assert F.pct_return(series, 3) == D("10")


def test_pct_return_requires_history():
    with pytest.raises(F.InsufficientData):
        F.pct_return(closes_at((2, "1"), (1, "2")), 24)


def test_pct_return_uses_the_candle_at_the_target_hour():
    series = closes_at((4, "50"), (3, "100"), (2, "75"), (1, "110"))
    assert F.pct_return(series, 2) == D("10")  # the candle two hours before the last, not two candles


def test_pct_return_falls_back_to_the_nearest_earlier_candle():
    # The 1h base (hours_ago=2) has no candle: the nearest earlier one is 1 hour before it.
    series = closes_at((5, "1"), (3, "100"), (1, "110"))
    assert F.pct_return(series, 1) == D("10")


def test_pct_return_accepts_a_gap_of_exactly_two_hours():
    series = closes_at((4, "100"), (1, "110"))  # the 1h target is 2 hours ago, 2 hours after this base
    assert F.pct_return(series, 1) == D("10")


def test_pct_return_refuses_a_base_more_than_two_hours_before_its_target():
    series = closes_at((5, "100"), (1, "110"))  # labelled 1h, it would span 4 hours
    with pytest.raises(F.InsufficientData):
        F.pct_return(series, 1)


def test_pct_return_never_uses_a_later_candle_as_its_base():
    # The only candle within reach is after the target, which would make the return shorter than its label.
    series = closes_at((3, "100"), (1, "110"))
    assert F.pct_return(series, 1) == D("10")  # base is 3 hours ago, not a later one
    with pytest.raises(F.InsufficientData):
        F.pct_return(closes_at((1, "110")), 1)


def test_a_gap_in_the_candles_is_market_unavailable_not_a_mislabelled_return():
    series = flat_series(66)[:60] + [candle(1, "110", "111", "109", "110")]  # 60 flat, a 5 hour gap, then one more
    with pytest.raises(F.InsufficientData):
        F.compute(series, NOW, D("0.001"), D("110"), D("110.1"))


def test_trend_up_down_flat():
    assert F.trend_of(D("101"), D("100")) == "up"
    assert F.trend_of(D("99"), D("100")) == "down"
    assert F.trend_of(D("100.01"), D("100")) == "flat"


def test_round_trip_cost_is_two_taker_fees_plus_spread():
    # fee 5 bps per leg => 10 bps; spread 100.0 / 100.02 mid 100.01 => about 2 bps
    cost = F.round_trip_cost_bps(D("0.0005"), D("100"), D("100.02"))
    assert D("11.9") < cost < D("12.1")


def test_compute_uses_only_completed_candles():
    series = flat_series(60, close="100", rng="2")
    clean = F.compute(series, NOW, D("0.0005"), D("99.99"), D("100.01"))
    poison = Candle(THIS_HOUR, D("100"), D("999"), D("1"), D("999"))
    polluted = F.compute(series + [poison], NOW, D("0.0005"), D("99.99"), D("100.01"))
    assert clean == polluted


def test_compute_fields_on_rising_series():
    series = []
    for i in range(80, 0, -1):
        price = D(1000 - i)
        series.append(Candle(THIS_HOUR - i * HOUR, price, price + 1, price - 1, price))
    f = F.compute(series, NOW, D("0.0005"), D("998"), D("1002"))
    assert f.trend == "up"
    assert f.ret_1h_pct > 0
    assert f.ret_24h_pct > f.ret_4h_pct > f.ret_1h_pct
    assert f.volatility_ratio == f.atr_1h / f.atr_50
    assert f.atr_1h_pct == f.atr_1h / D(999) * 100
    assert f.ema20 > f.ema50


def test_compute_refuses_short_history():
    with pytest.raises(F.InsufficientData):
        F.compute(flat_series(F.MIN_CANDLES - 1), NOW, D("0.0005"), D("99"), D("101"))
