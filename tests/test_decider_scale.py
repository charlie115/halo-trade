"""The 15-minute features: closed candles only, gaps refused, and where the candles are read from."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from halo_trade import features as F
from halo_trade.chain.base import Candle


QUARTER = F.QUARTER_MS


# ---------------------------------------------------------------- 15-minute data
NOW = 500_000 * F.HOUR_MS + 1_800_000 + 60_000  # one minute into a quarter
THIS_BAR = NOW - NOW % QUARTER


def quarter(bars_ago: int, close: str, spread: str = "2") -> Candle:
    value = D(close)
    half = D(spread) / 2
    return Candle(THIS_BAR - bars_ago * QUARTER, value, value + half, value - half, value)


def rising(count: int = 60) -> list[Candle]:
    return [quarter(index, str(1000 + count - index)) for index in range(count, 0, -1)]


def test_15m_features_from_closed_candles():
    values = F.compute_15m(rising(), NOW)
    assert values.ret_pct == (D(1059) - D(1058)) / D(1058) * 100
    assert values.atr_pct > 0
    assert values.trend == "up"


def test_the_bar_still_forming_is_never_used():
    series = rising()
    poison = Candle(THIS_BAR, D(1), D(999_999), D(1), D(999_999))
    assert F.compute_15m(series + [poison], NOW) == F.compute_15m(series, NOW)


def test_a_15m_return_is_refused_across_a_gap_longer_than_two_bars():
    series = rising()[:-4] + [quarter(1, "1100")]  # bars 2, 3 and 4 ago have no candle
    with pytest.raises(F.InsufficientData):
        F.compute_15m(series, NOW)


def test_a_missing_bar_uses_the_nearest_earlier_candle():
    series = rising()[:-2] + [quarter(1, "1100")]  # the bar two ago has no candle: base is three ago
    assert F.compute_15m(series, NOW).ret_pct == (D("1100") - D(1057)) / D(1057) * 100


def test_too_few_15m_candles_is_insufficient():
    with pytest.raises(F.InsufficientData):
        F.compute_15m(rising(F.EMA_SLOW - 1), NOW)


def test_an_old_newest_15m_candle_is_insufficient():
    with pytest.raises(F.InsufficientData):
        F.compute_15m(rising(), NOW + 3 * QUARTER)


async def test_a_preview_does_not_read_15m_candles(env):
    user, _ = env.add_user()
    state = await env.svc.state(user)
    env.chain.fail_calls("candles_15m", count=5)
    from halo_trade.contracts import PreviewIn

    preview = await env.svc.preview(user, PreviewIn(observation_id=state.observation_id, side="long", stop_atr=2.0))
    assert preview.ticket
    assert env.chain._fail_calls["candles_15m"][1] == 5  # not one was consumed


async def test_the_injective_backend_reads_closed_15m_candles_from_the_chart_api(monkeypatch):
    from tests.test_injective_unit import make

    now_ms = 1_794_152_000_000
    bar = now_ms - now_ms % QUARTER
    seen = {}

    class Http:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params):
            import httpx

            seen.update(params)
            times = [(bar - 2 * QUARTER) // 1000, (bar - QUARTER) // 1000, bar // 1000]
            return httpx.Response(200, json={"t": times, "o": [1, 2, 3], "h": [1, 2, 3], "l": [1, 2, 3], "c": [1, 2, 3]}, request=httpx.Request("GET", url))

    monkeypatch.setattr("halo_trade.chain.injective.httpx.AsyncClient", Http)
    candles = await make(now_ms=now_ms).candles_15m(5)
    assert seen["resolution"] == "15"
    assert [candle.open_ms for candle in candles] == [bar - 2 * QUARTER, bar - QUARTER]  # the forming bar is dropped
