"""Candles from a deep market, for analysis only. Orders and every risk check still read the chain.

The on-chain BTC market can go hours without a trade, and an hour with no trade has no candle, so
the features would stop for no reason in a quiet stretch. Its price follows an oracle, so the wider
market's candles describe the same asset without the holes. The URL is fixed and every row is checked.
"""
from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

import httpx

from .base import Candle, ChainUnavailable

KLINE_URL = "https://fapi.binance.com/fapi/v1/klines"
SOURCE_NAME = "binance"
MAX_ROWS = 1500  # what the endpoint gives in one call
MAX_BODY_BYTES = 1_000_000
TIMEOUT_S = 8.0
_INTERVALS = {3_600_000: "1h", 900_000: "15m"}
_USER_AGENT = "halo-trade/1.0"


def symbol_for(ticker: str) -> str:
    """'BTC/USDC PERP' -> 'BTCUSDT'. The ticker comes from our own settings."""
    base = ticker.split("/")[0].strip().upper()
    if not base.isalnum():
        raise ValueError(f"cannot name a reference market for {ticker!r}")
    return f"{base}USDT"


def _price(value: Any) -> Decimal:
    try:
        price = Decimal(str(value))
    except InvalidOperation as exc:
        raise ChainUnavailable("bad candle price") from exc
    if not price.is_finite() or price <= 0:
        raise ChainUnavailable("bad candle price")
    return price


def parse_rows(body: bytes, period_ms: int, now_ms: int, closed_only: bool) -> list[Candle]:
    """Oldest first. One bad row refuses the whole answer: half a history would still look fine."""
    try:
        rows = json.loads(body)
    except ValueError as exc:
        raise ChainUnavailable("reference candles were not JSON") from exc
    if not isinstance(rows, list):
        raise ChainUnavailable("reference candles were not a list")
    candles: list[Candle] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 5 or isinstance(row[0], bool) or not isinstance(row[0], int):
            raise ChainUnavailable("bad candle row")
        open_ms = row[0]
        open_, high, low, close = (_price(value) for value in row[1:5])
        if high < max(open_, close) or low > min(open_, close) or open_ms % period_ms:
            raise ChainUnavailable("bad candle row")
        if not closed_only or open_ms + period_ms <= now_ms:
            candles.append(Candle(open_ms, open_, high, low, close))
    candles.sort(key=lambda candle: candle.open_ms)
    return candles


class ReferenceCandles:
    def __init__(self, symbol: str, http: Optional[httpx.AsyncClient] = None, clock_ms: Optional[Callable[[], int]] = None) -> None:
        self.symbol = symbol
        self._http = http
        self._clock_ms = clock_ms

    async def _get(self, params: dict[str, Any]) -> bytes:
        headers = {"User-Agent": _USER_AGENT}
        client = self._http or httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False, headers=headers)
        try:
            async with client.stream("GET", KLINE_URL, params=params) as response:
                if response.status_code != 200:
                    raise ChainUnavailable(f"reference candles: status {response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BODY_BYTES:
                        raise ChainUnavailable("reference candles: response too large")
                return bytes(body)
        except httpx.HTTPError as exc:
            raise ChainUnavailable(f"reference candles: {type(exc).__name__}") from exc
        finally:
            if self._http is None:
                await client.aclose()

    async def read(self, period_ms: int, limit: int, *, closed_only: bool, now_ms: int) -> list[Candle]:
        # One extra row, because the newest one the endpoint returns is the bar still open.
        wanted = max(1, min(MAX_ROWS, math.ceil(limit) + 1))
        body = await self._get({"symbol": self.symbol, "interval": _INTERVALS[period_ms], "limit": wanted})
        return parse_rows(body, period_ms, now_ms, closed_only)[-limit:]
