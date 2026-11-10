"""The wider market's candles: what is accepted, and what happens when they are not available."""
from __future__ import annotations

import json
from decimal import Decimal as D

import httpx
import pytest

from halo_trade.chain.base import ChainUnavailable
from halo_trade.chain.reference import KLINE_URL, MAX_BODY_BYTES, ReferenceCandles, parse_rows, symbol_for
from tests.test_injective_unit import make

HOUR = 3_600_000
START = 1_794_150_000_000 - 1_794_150_000_000 % HOUR


def row(open_ms=START, o="100", h="110", l="90", c="105"):
    return [open_ms, o, h, l, c, "12.5", open_ms + HOUR - 1, "1250", 10]


def body(*rows) -> bytes:
    return json.dumps(list(rows)).encode()


def reader(handler) -> ReferenceCandles:
    return ReferenceCandles("BTCUSDT", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_symbol_comes_from_the_market_ticker():
    assert symbol_for("BTC/USDC PERP") == "BTCUSDT"
    with pytest.raises(ValueError):
        symbol_for("B&TC/USDC PERP")


def test_rows_are_read_oldest_first():
    candles = parse_rows(body(row(START + HOUR), row(START)), HOUR, START + 3 * HOUR, True)
    assert [candle.open_ms for candle in candles] == [START, START + HOUR]
    assert (candles[0].open, candles[0].high, candles[0].low, candles[0].close) == (D("100"), D("110"), D("90"), D("105"))


def test_the_bar_still_open_is_dropped_unless_asked_for():
    rows = body(row(START), row(START + HOUR))
    assert len(parse_rows(rows, HOUR, START + HOUR + 1, True)) == 1
    assert len(parse_rows(rows, HOUR, START + HOUR + 1, False)) == 2


@pytest.mark.parametrize(
    "bad",
    [
        row(c="-1"),
        row(c="NaN"),
        row(c="Infinity"),
        row(o="abc"),
        row(h="50"),  # a high under the close
        row(l="200"),  # a low over the open
        row(open_ms=START + 1),  # not on a bar boundary
        [True, "1", "2", "1", "2"],
        ["soon", "1", "2", "1", "2"],
        [START, "1"],
        "nope",
    ],
)
def test_one_bad_row_refuses_the_whole_answer(bad):
    with pytest.raises(ChainUnavailable):
        parse_rows(body(row(START - HOUR), bad), HOUR, START + 2 * HOUR, True)


@pytest.mark.parametrize("raw", [b"{", b'{"code":-1121}', b"null"])
def test_an_answer_that_is_not_a_list_is_refused(raw):
    with pytest.raises(ChainUnavailable):
        parse_rows(raw, HOUR, START, True)


async def test_read_asks_the_fixed_url_for_one_extra_row_and_keeps_the_limit():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(url=str(request.url.copy_with(query=None)), params=dict(request.url.params))
        return httpx.Response(200, content=body(row(START), row(START + HOUR), row(START + 2 * HOUR)))

    candles = await reader(handler).read(HOUR, 2, closed_only=True, now_ms=START + 2 * HOUR + 5)
    assert seen == {"url": KLINE_URL, "params": {"symbol": "BTCUSDT", "interval": "1h", "limit": "3"}}
    assert [candle.open_ms for candle in candles] == [START, START + HOUR]


async def test_a_refused_request_is_a_chain_failure():
    with pytest.raises(ChainUnavailable):
        await reader(lambda request: httpx.Response(451)).read(HOUR, 2, closed_only=True, now_ms=START)


async def test_a_redirect_is_not_followed():
    with pytest.raises(ChainUnavailable):
        await reader(lambda request: httpx.Response(302, headers={"location": "https://evil.example/"})).read(HOUR, 2, closed_only=True, now_ms=START)


async def test_an_oversized_answer_is_refused():
    with pytest.raises(ChainUnavailable):
        await reader(lambda request: httpx.Response(200, content=b"[" + b" " * (MAX_BODY_BYTES + 1))).read(HOUR, 2, closed_only=True, now_ms=START)


async def test_a_network_failure_is_a_chain_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(ChainUnavailable):
        await reader(handler).read(HOUR, 2, closed_only=True, now_ms=START)


class FakeReference:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def read(self, period_ms, limit, *, closed_only, now_ms):
        self.calls.append((period_ms, limit, closed_only))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def with_chart(backend):
    """Stands in for the chart API, and counts how often it was asked."""
    asked = []

    async def rpc(call, *args):
        call.close()
        asked.append(True)
        return {"t": [(START - HOUR) // 1000], "o": ["1"], "h": ["2"], "l": ["1"], "c": ["2"]}

    async def market():
        return type("Market", (), {"market_id": "0xabc"})()

    backend._rpc = rpc
    backend.market = market
    return asked


async def test_the_reference_feed_comes_first_and_is_named():
    backend = make(now_ms=START + 5)
    asked = with_chart(backend)
    reference = FakeReference(["a candle"])
    backend._reference_candles = reference
    assert await backend.candles_1h(3) == ["a candle"]
    assert reference.calls == [(HOUR, 3, True)]
    assert asked == [] and backend.candle_source == "binance"


async def test_the_open_bar_is_asked_for_only_by_the_page():
    backend = make(now_ms=START + 5)
    with_chart(backend)
    reference = FakeReference([])
    backend._reference_candles = reference
    await backend.live_candles_1h(4)
    assert reference.calls == [(HOUR, 4, False)]


async def test_the_chart_api_takes_over_when_the_reference_feed_fails():
    backend = make(now_ms=START + 5)
    asked = with_chart(backend)
    backend._reference_candles = FakeReference(ChainUnavailable("down"))
    candles = await backend.candles_1h(3)
    assert asked == [True] and len(candles) == 1
    assert backend.candle_source == "injective"
