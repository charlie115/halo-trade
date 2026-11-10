"""The real chain backend: injective-py v2, every order inside an authz MsgExec.

What this module deliberately does not take from the SDK's broadcaster:

- `AsyncClient.get_sequence()` returns and then increments a local counter, before simulation
  and before anyone knows the tx will land. We read the sequence from the chain for each tx.
- `AsyncClient.timeout_height` is refreshed by a background task every 20 s, so it can already
  be in the past when a tx is signed. We read the height for each tx and cancel that task.
- `GasHeuristicsGasLimitEstimator` allows 105k for a derivative market order. The chain's fixed
  gas for it is 190k (keeper/fixed_gas.go). Our limits below come from the chain's numbers.
- On a sequence mismatch nothing here re-signs or re-sends. `broadcast` reports nothing about
  CheckTx because a re-broadcast of an already included tx also fails CheckTx with a mismatch;
  only `lookup` decides, by hash and timeout_height.

Prices, quantities, margins and deposits on the v2 API are human values times 1e18 (measured on
testnet 2026-11-09, chain v1.20.4-bl.2).
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import logging
import os
import stat
import time
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Awaitable, Callable, Optional, Sequence

import grpc
import httpx
from google.protobuf import json_format
from pyinjective.async_client_v2 import AsyncClient
from pyinjective.client.model.pagination import PaginationOption
from pyinjective.composer_v2 import Composer
from pyinjective.constant import GAS_PRICE
from pyinjective.core.network import Network
from pyinjective.proto.injective.exchange.v2 import order_pb2, tx_pb2 as exchange_tx_pb2
from pyinjective.proto.injective.oracle.v1beta1 import (
    oracle_pb2,
    query_pb2 as oracle_query_pb2,
    query_pb2_grpc as oracle_query_grpc,
)
from pyinjective.transaction import Transaction
from pyinjective.wallet import Address, PrivateKey

from . import grants as grants_module
from .base import (
    Account,
    Candle,
    ChainThrottled,
    ChainUnavailable,
    GrantStatus,
    MarketInfo,
    MarketSnapshot,
    OrderKind,
    OrderRefused,
    OrderRequest,
    Position,
    RestingOrder,
    Side,
    SignedTx,
    TxResult,
    TxStatus,
    requests_are_urgent,
    urgent_requests,
)
from .limiter import RequestLimiter
from .shared import SNAPSHOT_TTL_MS, SharedValue

logger = logging.getLogger(__name__)

E18 = Decimal(10) ** 18

MAINNET_CHAIN_ID = "injective-1"
TESTNET_CHAIN_ID = "injective-888"

# CheckTx: the node already holds this exact tx. Same bytes sent twice, which is what we want.
CODE_TX_IN_MEMPOOL_CACHE = 19

# The chain never includes a tx after timeout_height, but the node answering GetTx can trail the
# node that told us the height. Calling a tx expired too early would allow a second execution,
# so it needs this many extra blocks of "not found". About 2 s at 0.6 s blocks.
EXPIRY_MARGIN_BLOCKS = 3

# Limits with headroom over the chain's fixed gas (market 190k, limit 210k, cancel 80k). Unused
# gas is not refunded, but at 1.6e8 inj per gas a 2-order tx costs about 0.0001 INJ.
GAS_ANTE = 150_000
GAS_MSG_EXEC = 30_000
GAS_MARKET_ORDER = 250_000
GAS_LIMIT_ORDER = 270_000
GAS_CANCEL_ORDER = 110_000

# CONDITIONAL | TYPE_MARKET and no direction bit. For conditional orders the direction bits mean
# trigger higher/lower than mark, not buy/sell, so a TAKE_SELL needs HIGHER. With neither bit set
# the chain looks in both directions (keeper/derivative/orders.go processOrderMaskFlags).
CANCEL_CONDITIONAL_MARKET_MASK = int(order_pb2.OrderMask.CONDITIONAL) + int(order_pb2.OrderMask.TYPE_MARKET)

MAX_CID_LENGTH = 36
ORDERBOOK_LEVELS = 200
MARKET_CACHE_S = 60
ONE_HOUR_MS = 3_600_000
FIFTEEN_MIN_MS = 900_000
MIN_CALL_S = 0.05  # a call that waited for its slot still gets this long

CHART_BASE_URLS = {
    "testnet": "https://k8s.testnet.chart.grpc-web.injective.network",
    "mainnet": "https://k8s.global.mainnet.chart.grpc-web.injective.network",
}

_TRANSPORT_ERRORS = (grpc.RpcError, asyncio.TimeoutError, ConnectionError, OSError, httpx.HTTPError)

# Every state query is answered with the height its node served it at (checked on testnet).
HEIGHT_HEADER = "x-cosmos-block-height"
_response_heights: contextvars.ContextVar[Optional[list[int]]] = contextvars.ContextVar("response_heights", default=None)


def record_response_height(height: int) -> None:
    heights = _response_heights.get()
    if heights is not None:
        heights.append(height)


def _retry_after_seconds(value: Any) -> Optional[float]:
    """Retry-After as a number of seconds. The HTTP-date form falls back to our own backoff."""
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _grpc_metadata(exc: Any, key: str) -> Optional[str]:
    for read in ("trailing_metadata", "initial_metadata"):
        method = getattr(exc, read, None)
        for name, value in (method() if callable(method) else None) or ():
            if name == key:
                return value
    return None


def rate_refusal(exc: BaseException) -> Optional[ChainThrottled]:
    """A rate refusal from either transport, also when something already wrapped it."""
    source = exc.__cause__ if isinstance(exc, ChainUnavailable) and exc.__cause__ is not None else exc
    if isinstance(source, httpx.HTTPStatusError) and source.response.status_code == 429:
        return ChainThrottled(_retry_after_seconds(source.response.headers.get("retry-after")))
    if isinstance(source, grpc.RpcError):
        code = getattr(source, "code", None)
        details = getattr(source, "details", None)
        if (callable(code) and code() is grpc.StatusCode.RESOURCE_EXHAUSTED) or "429" in str(details() if callable(details) else ""):
            return ChainThrottled(_retry_after_seconds(_grpc_metadata(source, "retry-after")))
    return None


class _HeightCookies:
    """Wraps the SDK's cookie assistant, the only hook it offers on a response's gRPC metadata."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def process_response_metadata(self, grpc_call: Any) -> None:
        await self._inner.process_response_metadata(grpc_call)
        metadata = await grpc_call.initial_metadata()
        value = metadata.get(HEIGHT_HEADER)
        if value is not None:
            record_response_height(int(value))


class MainnetRefused(RuntimeError):
    """Signing or broadcasting on mainnet without settings.allow_mainnet."""


class GranteeAccountNotFound(ChainUnavailable):
    """The signing key has no account on chain yet, so it has never been funded."""


class InFlightTx(ChainUnavailable):
    """The previous tx from this key may still land. Wait until it is final before signing."""


def from_chain(value: Any) -> Decimal:
    """v2 API value (human value times 1e18) to a human Decimal."""
    return Decimal(str(value)) / E18 if value not in (None, "") else Decimal(0)


def quantize(value: Decimal, tick: Decimal, rounding: str) -> Decimal:
    return (value / tick).to_integral_value(rounding=rounding) * tick


def is_buy(side: Side) -> bool:
    return side is Side.LONG


def conditional_order_type(kind: OrderKind, side: Side) -> str:
    """STOP_SELL closes a long. `side` is the direction of the order itself."""
    prefix = "STOP" if kind is OrderKind.STOP else "TAKE"
    return f"{prefix}_{'BUY' if is_buy(side) else 'SELL'}"


def trigger_is_above_mark(kind: OrderKind, side: Side) -> bool:
    """STOP_BUY and TAKE_SELL sit above mark, STOP_SELL and TAKE_BUY below.

    This follows CheckValidConditionalPrice in the chain code. Injective's trading docs have TAKE
    the other way round.
    """
    return (kind is OrderKind.STOP) == is_buy(side)


def round_worst_price(price: Decimal, tick: Decimal, buying: bool) -> Decimal:
    """A buy never pays more than asked, a sell never receives less."""
    return quantize(price, tick, ROUND_FLOOR if buying else ROUND_CEILING)


def round_trigger(price: Decimal, tick: Decimal, kind: OrderKind, side: Side) -> Decimal:
    """Rounds toward mark, so the distance to the trigger does not grow."""
    return quantize(price, tick, ROUND_FLOOR if trigger_is_above_mark(kind, side) else ROUND_CEILING)


def classify_conditional(buying: bool, trigger: Decimal, mark: Decimal) -> OrderKind:
    """Chain queries omit the order type, so infer it from direction and side of mark."""
    return OrderKind.STOP if buying == (trigger > mark) else OrderKind.TAKE


def predicted_funding_rate(market_info: dict, funding_info: dict) -> Decimal:
    """Rate for the current interval, the same arithmetic as the chain's hourly funding."""
    cumulative_price = from_chain(funding_info.get("cumulativePrice"))
    interval = int(market_info.get("fundingInterval") or 0)
    elapsed = int(funding_info.get("lastTimestamp") or 0) + interval - int(market_info.get("nextFundingTimestamp") or 0)
    twap = cumulative_price / (Decimal(elapsed) * 24) if elapsed > 0 else Decimal(0)
    rate = twap + from_chain(market_info.get("hourlyInterestRate"))
    cap = from_chain(market_info.get("hourlyFundingRateCap"))
    return max(-cap, min(cap, rate))


def depth_within_collar(levels: Sequence[dict], limit_price: Decimal, is_bid: bool) -> Decimal:
    total = Decimal(0)
    for level in levels:
        price = from_chain(level["p"])
        if (price >= limit_price) if is_bid else (price <= limit_price):
            total += price * from_chain(level["q"])
    return total


def gas_limit_for(messages: Sequence[Any]) -> int:
    gas = GAS_ANTE + GAS_MSG_EXEC
    for message in messages:
        if isinstance(message, exchange_tx_pb2.MsgCreateDerivativeMarketOrder):
            gas += GAS_MARKET_ORDER
        elif isinstance(message, exchange_tx_pb2.MsgCreateDerivativeLimitOrder):
            gas += GAS_LIMIT_ORDER
        elif isinstance(message, exchange_tx_pb2.MsgCancelDerivativeOrder):
            gas += GAS_CANCEL_ORDER
        else:
            raise ValueError(f"no gas budget for {type(message).__name__}")
    return gas


def sign_transaction(
    private_key: PrivateKey,
    messages: Sequence[Any],
    account_number: int,
    sequence: int,
    chain_id: str,
    gas_limit: int,
    fee_coin: Any,
    timeout_height: int,
) -> tuple[bytes, str]:
    """One signature over one sign doc. Returns the raw tx and its hash."""
    public_key = private_key.to_public_key()
    transaction = (
        Transaction()
        .with_messages(*messages)
        .with_sequence(sequence)
        .with_account_num(account_number)
        .with_chain_id(chain_id)
        .with_gas(gas_limit)
        .with_fee([fee_coin])
        .with_memo("")
        .with_timeout_height(timeout_height)
    )
    signature = private_key.sign(transaction.get_sign_doc(public_key).SerializeToString())
    tx_bytes = transaction.get_tx_data(signature, public_key)
    return tx_bytes, hashlib.sha256(tx_bytes).hexdigest().upper()


def load_grantee_key(path: str) -> str:
    """Read a hex private key from a file only its owner can read."""
    if not path:
        raise ValueError("HALO_GRANTEE_KEY_PATH is not set")
    mode = os.stat(path).st_mode
    if stat.S_IMODE(mode) & 0o077:
        raise PermissionError(f"{path} must not be readable by group or others (chmod 600)")
    with open(path, encoding="ascii") as handle:
        key = handle.read().strip()
    return key[2:] if key.startswith("0x") else key


class InjectiveChain:
    """Implements `ChainBackend` for one perpetual market and one grantee key."""

    def __init__(
        self,
        network: str,
        grantee_private_key: str,
        market_ticker: str,
        tx_timeout_blocks: int,
        collar: Decimal,
        allow_mainnet: bool = False,
        market_id: Optional[str] = None,
        client: Optional[Any] = None,
        clock_ms: Optional[Callable[[], int]] = None,
        chart_base_url: Optional[str] = None,
        rpc_timeout_s: float = 10.0,
        candle_network: Optional[str] = None,
        candle_client: Optional[Any] = None,
        reference_candles: Optional[Any] = None,
        chain_rps: float = 15,
        indexer_rps: float = 30,
    ) -> None:
        if network == "testnet":
            self._network = Network.testnet()
        elif network == "mainnet":
            self._network = Network.mainnet()
        else:
            raise ValueError(f"unsupported network {network!r}")
        self._allow_mainnet = allow_mainnet
        self._key = PrivateKey.from_hex(grantee_private_key)
        self.grantee_address = self._key.to_public_key().to_address().to_acc_bech32()
        self._market_ticker = market_ticker
        self._market_id = market_id
        self._tx_timeout_blocks = tx_timeout_blocks
        self._collar = collar
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._candle_network = candle_network or self._network.env
        if self._candle_network != self._network.env and (self._network.env, self._candle_network) != ("testnet", "mainnet"):
            raise ValueError("candles may come from another network only on testnet, and only from mainnet")
        self._chart_base_url = chart_base_url or CHART_BASE_URLS[self._candle_network]
        self._candle_client = candle_client
        self._candle_market_id: Optional[str] = None
        self._reference_candles = reference_candles
        self.candle_source = "injective"  # where the newest candle read came from, for the page's credit line
        self._rpc_timeout_s = rpc_timeout_s
        self._composer = Composer(network=self._network.string())
        self._client = client
        self._oracle_stub: Any = None
        self._market_info: Optional[MarketInfo] = None
        self._market_info_at_ms = 0
        self._quote_decimals = 6
        self._sign_lock = asyncio.Lock()
        self._market_lock = asyncio.Lock()
        self._last_signed: Optional[SignedTx] = None
        self._chain_limiter = RequestLimiter(chain_rps)
        self._indexer_limiter = RequestLimiter(indexer_rps)
        self._raw_market: SharedValue[dict] = SharedValue(SNAPSHOT_TTL_MS, self._clock_ms)

    @property
    def throttled_total(self) -> int:
        return self._chain_limiter.throttled + self._indexer_limiter.throttled

    # Guards -----------------------------------------------------------------------------------

    @property
    def is_mainnet(self) -> bool:
        return self._network.env == "mainnet" or self._network.chain_id == MAINNET_CHAIN_ID

    def _assert_signing_allowed(self) -> None:
        if self.is_mainnet and not self._allow_mainnet:
            raise MainnetRefused("mainnet signing and broadcasting need settings.allow_mainnet")

    @staticmethod
    def _assert_subaccount_of(granter: str, subaccount_id: str) -> None:
        owner = Address.from_acc_bech32(granter).get_ethereum_address().lower()
        if not subaccount_id.lower().startswith(owner) or len(subaccount_id) != 66:
            raise ValueError("subaccount does not belong to granter")

    def _ensure_client(self) -> Any:
        if self._client is None:
            self._network.chain_cookie_assistant = _HeightCookies(self._network.chain_cookie_assistant)
            client = AsyncClient(self._network)
            # Nothing here uses the cached timeout height, so stop the task that refreshes it.
            client._cancel_timeout_height_sync_task()
            self._client = client
        return self._client

    async def _limited(self, awaitable: Awaitable[Any], limiter: RequestLimiter) -> Any:
        """A request slot, then the call, both inside one `rpc_timeout_s`. Raw errors pass through,
        except a rate refusal, which pauses the bucket and becomes `ChainThrottled`."""
        started = time.monotonic()
        try:
            await limiter.acquire(urgent=requests_are_urgent(), timeout_s=self._rpc_timeout_s)
        except BaseException:
            getattr(awaitable, "close", lambda: None)()
            raise
        try:
            result = await asyncio.wait_for(awaitable, max(MIN_CALL_S, self._rpc_timeout_s - (time.monotonic() - started)))
        except Exception as exc:
            throttled = rate_refusal(exc)
            if throttled is None:
                raise
            limiter.refused(throttled.retry_after_s)
            raise throttled from exc
        limiter.succeeded()
        return result

    async def _rpc(self, awaitable: Awaitable[Any], limiter: Optional[RequestLimiter] = None) -> Any:
        try:
            return await self._limited(awaitable, limiter or self._chain_limiter)
        except _TRANSPORT_ERRORS as exc:
            raise ChainUnavailable("chain request failed") from exc
        except (json_format.Error, TypeError, KeyError, ValueError) as exc:
            # A response we cannot read says nothing about what happened on chain.
            raise ChainUnavailable("chain response could not be parsed") from exc

    async def _state_read(self, awaitable: Awaitable[Any]) -> tuple[Any, Optional[int]]:
        """One state query, and the height its node served it at when the response says."""
        seen: list[int] = []
        token = _response_heights.set(seen)
        try:
            return await self._rpc(awaitable), (seen[-1] if seen else None)
        finally:
            _response_heights.reset(token)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close_chain_channel()
        if self._candle_client is not None and hasattr(self._candle_client, "close_chain_channel"):
            await self._candle_client.close_chain_channel()

    # Reads ------------------------------------------------------------------------------------

    async def _latest_height(self) -> int:
        return (await self._latest_block())[0]

    async def _latest_block(self) -> tuple[int, int]:
        """(height, header time in ms). The chain's own clock, which stops when the chain does."""
        block = await self._rpc(self._ensure_client().fetch_latest_block())
        header = block["block"]["header"]
        return int(header["height"]), int(datetime.fromisoformat(header["time"]).timestamp() * 1000)

    async def _shared_raw_market(self) -> dict:
        return await self._raw_market.get(self._fetch_raw_market)

    async def _fetch_raw_market(self) -> dict:
        """Urgent always: the close and stop paths share this one read with everyone else."""
        with urgent_requests():
            return await self._read_raw_market()

    async def _read_raw_market(self) -> dict:
        client = self._ensure_client()
        if self._market_id:
            response = await self._rpc(client.fetch_chain_derivative_market(market_id=self._market_id))
            return response["market"]
        response = await self._rpc(client.fetch_chain_derivative_markets(status="Active"))
        for entry in response.get("markets", []):
            if entry["market"]["ticker"] == self._market_ticker:
                return entry
        raise ChainUnavailable(f"market {self._market_ticker!r} not found among active markets")

    async def market(self) -> MarketInfo:
        now_ms = self._clock_ms()
        if self._market_info is not None and now_ms - self._market_info_at_ms < MARKET_CACHE_S * 1000:
            return self._market_info
        async with self._market_lock:
            if self._market_info is not None and self._clock_ms() - self._market_info_at_ms < MARKET_CACHE_S * 1000:
                return self._market_info
            raw = await self._fetch_raw_market()
            market = raw["market"]
            if market.get("status") != "Active":
                raise ChainUnavailable(f"market status is {market.get('status')}")
            self._market_id = market["marketId"]
            self._quote_decimals = int(market.get("quoteDecimals") or 6)
            self._market_info = MarketInfo(
                market_id=market["marketId"],
                ticker=market["ticker"],
                quote_denom=market["quoteDenom"],
                price_tick=from_chain(market["minPriceTickSize"]),
                quantity_tick=from_chain(market["minQuantityTickSize"]),
                min_notional=from_chain(market.get("minNotional")),
                taker_fee=from_chain(market["takerFeeRate"]),
                maker_fee=from_chain(market["makerFeeRate"]),
                initial_margin_ratio=from_chain(market["initialMarginRatio"]),
            )
            self._market_info_at_ms = self._clock_ms()
            return self._market_info

    async def _oracle_state(self, market: dict, mark: Decimal) -> tuple[Decimal, Optional[int]]:
        """(oracle pair price, ms of the older of its two sources), for the market's oracle type.

        The price is display-only, so a failed read falls back to the mark. Its time reads as 0,
        which entries see as stale. The chain's OraclePrice query refuses the Provider type (oracle
        code 32); a perpetual's mark comes from its oracle, so there the mark is the oracle price
        and no oracle time is known.
        """
        if market["oracleType"] == "Provider":
            return mark, None
        client = self._ensure_client()
        if self._oracle_stub is None:
            self._oracle_stub = oracle_query_grpc.QueryStub(client.chain_channel)
        request = oracle_query_pb2.QueryOraclePriceRequest(
            oracle_type=oracle_pb2.OracleType.Value(market["oracleType"]),
            base=market["oracleBase"],
            quote=market["oracleQuote"],
        )
        try:
            response = await self._rpc(self._oracle_stub.OraclePrice(request))
        except ChainUnavailable:
            return mark, 0
        state = response.price_pair_state
        if not state.pair_price:
            return mark, 0
        return from_chain(state.pair_price), min(state.base_timestamp, state.quote_timestamp) * 1000

    async def snapshot(self) -> MarketSnapshot:
        info = await self.market()
        client = self._ensure_client()
        raw, book, (height, block_ms) = await asyncio.gather(
            self._shared_raw_market(),
            self._rpc(
                client.fetch_chain_derivative_orderbook(
                    market_id=info.market_id,
                    pagination=PaginationOption(limit=ORDERBOOK_LEVELS),
                )
            ),
            self._latest_block(),
        )
        mark = from_chain(raw["markPrice"])
        oracle_price, oracle_ms = await self._oracle_state(raw["market"], mark)
        bids = book.get("buysPriceLevel") or []
        asks = book.get("sellsPriceLevel") or []
        perpetual = raw.get("perpetualInfo") or {}
        return MarketSnapshot(
            mark_price=mark,
            oracle_price=oracle_price,
            best_bid=max((from_chain(level["p"]) for level in bids), default=Decimal(0)),
            best_ask=min((from_chain(level["p"]) for level in asks), default=Decimal(0)),
            bid_depth_usd=depth_within_collar(bids, mark * (1 - self._collar), is_bid=True),
            ask_depth_usd=depth_within_collar(asks, mark * (1 + self._collar), is_bid=False),
            funding_rate=predicted_funding_rate(
                perpetual.get("marketInfo") or {},
                perpetual.get("fundingInfo") or {},
            ),
            height=height,
            as_of_ms=block_ms if oracle_ms is None else min(block_ms, oracle_ms),
        )

    async def candles_1h(self, limit: int) -> list[Candle]:
        """Closed hourly candles, oldest first.

        Hours with no trades have no candle. Testnet markets can return almost none.
        """
        return await self._closed_candles("60", ONE_HOUR_MS, limit)

    async def candles_15m(self, limit: int) -> list[Candle]:
        """Closed 15-minute candles, oldest first."""
        return await self._closed_candles("15", FIFTEEN_MIN_MS, limit)

    async def live_candles_1h(self, limit: int) -> list[Candle]:
        """Hourly candles including the bar still open. Hours with no trades have no candle, the open one too."""
        return await self._closed_candles("60", ONE_HOUR_MS, limit, closed_only=False)

    async def _closed_candles(self, resolution: str, period_ms: int, limit: int, *, closed_only: bool = True) -> list[Candle]:
        now_ms = self._clock_ms()
        if self._reference_candles is not None:
            try:
                candles = await self._reference_candles.read(period_ms, limit, closed_only=closed_only, now_ms=now_ms)
                self.candle_source = "binance"
                return candles
            except ChainUnavailable as exc:
                logger.warning("reference candles failed, using the chart API: %s", exc)
        self.candle_source = "injective"
        info = await self.market()
        to_s = now_ms // 1000
        params = {
            "marketId": await self._chart_market_id(info.market_id),
            "resolution": resolution,
            "from": to_s - (limit + 2) * (period_ms // 1000),
            "to": to_s,
        }
        url = f"{self._chart_base_url}/api/chart/v1/derivative/history"

        async def fetch() -> Any:
            async with httpx.AsyncClient(timeout=self._rpc_timeout_s, follow_redirects=False) as http:
                response = await http.get(url, params=params)
                response.raise_for_status()
                return response.json()

        data = await self._rpc(fetch(), self._indexer_limiter)

        candles = [
            Candle(
                open_ms=int(data["t"][i]) * 1000,
                open=Decimal(str(data["o"][i])),
                high=Decimal(str(data["h"][i])),
                low=Decimal(str(data["l"][i])),
                close=Decimal(str(data["c"][i])),
            )
            for i in range(len(data.get("t") or []))
            if not closed_only or int(data["t"][i]) * 1000 + period_ms <= now_ms
        ]
        candles.sort(key=lambda candle: candle.open_ms)
        return candles[-limit:]

    async def _chart_market_id(self, own_market_id: str) -> str:
        """The id the chart API knows the market by. Only differs when candles come from mainnet."""
        if self._candle_network == self._network.env:
            return own_market_id
        if self._candle_market_id is None:
            if self._candle_client is None:
                # Read-only: this client is never handed to anything that signs or broadcasts.
                self._candle_client = AsyncClient(Network.mainnet())
                self._candle_client._cancel_timeout_height_sync_task()
            response = await self._rpc(self._candle_client.fetch_chain_derivative_markets(status="Active"))
            matches = [entry["market"]["marketId"] for entry in response.get("markets", []) if entry["market"]["ticker"] == self._market_ticker]
            if not matches:
                raise ChainUnavailable(f"market {self._market_ticker!r} not found on {self._candle_network}")
            self._candle_market_id = matches[0]
        return self._candle_market_id

    async def account(self, granter: str, subaccount_id: str) -> Account:
        info = await self.market()
        client = self._ensure_client()
        raw, *reads = await asyncio.gather(
            self._shared_raw_market(),
            self._state_read(client.fetch_subaccount_deposit(subaccount_id=subaccount_id, denom=info.quote_denom)),
            self._state_read(
                client.fetch_chain_subaccount_position_in_market(
                    subaccount_id=subaccount_id, market_id=info.market_id
                )
            ),
            self._state_read(
                client.fetch_trader_derivative_conditional_orders(
                    subaccount_id=subaccount_id, market_id=info.market_id
                )
            ),
        )
        (deposit, _), (position_raw, _), (conditional, _) = reads
        heights = [height for _, height in reads]
        # Each query can be answered by a different node: this read is only as new as its oldest.
        height = min(heights) if None not in heights else await self._latest_height()
        mark = from_chain(raw["markPrice"])
        funding_info = (raw.get("perpetualInfo") or {}).get("fundingInfo") or {}
        position = self._parse_position(position_raw.get("state"), mark, funding_info)
        deposits = deposit.get("deposits") or {}
        total = from_chain(deposits.get("totalBalance"))

        # Opening a position moves its margin out of the deposit, so an account value has to add
        # it back. Otherwise equity would fall by the margin the moment a position opens.
        equity = total
        if position is not None:
            equity += position.margin + position.unrealized_pnl

        return Account(
            subaccount_id=subaccount_id,
            equity=equity,
            available=from_chain(deposits.get("availableBalance")),
            position=position,
            orders=tuple(self._parse_conditional(order, mark) for order in conditional.get("orders", []) if not order.get("isLimit")),
            height=height,
            mark_price=mark,
        )

    @staticmethod
    def _parse_position(state: Optional[dict], mark: Decimal, funding_info: dict) -> Optional[Position]:
        if not state or from_chain(state.get("quantity")) <= 0:
            return None
        long = bool(state.get("isLong"))
        quantity = from_chain(state["quantity"])
        entry = from_chain(state["entryPrice"])
        price_pnl = (mark - entry) * quantity if long else (entry - mark) * quantity
        funding_due = (from_chain(funding_info.get("cumulativeFunding")) - from_chain(state.get("cumulativeFundingEntry"))) * quantity
        return Position(
            side=Side.LONG if long else Side.SHORT,
            quantity=quantity,
            entry_price=entry,
            margin=from_chain(state["margin"]),
            unrealized_pnl=price_pnl - funding_due if long else price_pnl + funding_due,
        )

    @staticmethod
    def _parse_conditional(order: dict, mark: Decimal) -> RestingOrder:
        buying = bool(order.get("isBuy"))
        trigger = from_chain(order["triggerPrice"])
        return RestingOrder(
            order_hash=order.get("orderHash", ""),
            cid=order.get("cid", ""),
            kind=classify_conditional(buying, trigger, mark),
            side=Side.LONG if buying else Side.SHORT,
            quantity=from_chain(order["quantity"]),
            trigger_price=trigger,
            worst_price=from_chain(order["price"]),
            reduce_only=from_chain(order.get("margin")) == 0,
        )

    async def check_grant(self, granter: str, subaccount_id: str) -> GrantStatus:
        info = await self.market()
        client = self._ensure_client()
        return await self._rpc(
            grants_module.verify_grant_on_chain(
                client.stubAuthz,
                granter=granter,
                grantee=self.grantee_address,
                subaccount_id=subaccount_id,
                market_id=info.market_id,
                now_ms=self._clock_ms(),
            )
        )

    async def grantee_balance_inj(self) -> Decimal:
        """The signer's gas balance. A tx it cannot pay for fails CheckTx and consumes no sequence."""
        response = await self._rpc(
            self._ensure_client().fetch_bank_balance(address=self.grantee_address, denom=self._network.fee_denom)
        )
        return from_chain((response.get("balance") or {}).get("amount"))

    # Building orders --------------------------------------------------------------------------

    def _build_order_message(self, info: MarketInfo, granter: str, subaccount_id: str, request: OrderRequest) -> Any:
        if len(request.cid) > MAX_CID_LENGTH:
            raise ValueError("cid is longer than the chain allows")

        quantity = quantize(request.quantity, info.quantity_tick, ROUND_FLOOR)
        if quantity <= 0:
            raise ValueError("quantity rounds to zero")
        buying = is_buy(request.side)
        worst_price = round_worst_price(request.worst_price, info.price_tick, buying)

        if request.kind is OrderKind.MARKET:
            if request.trigger_price is not None:
                raise ValueError("a market order has no trigger price")
            if request.reduce_only != (request.margin == 0):
                # The chain has no reduce-only flag: margin 0 is what makes an order reduce-only.
                raise ValueError("market order: reduce_only must match margin == 0")
            order_type = "BUY" if buying else "SELL"
            trigger_price = None
            margin = self._round_margin(request.margin)
        else:
            # A conditional order carrying margin would open a new position if the entry had
            # never filled. A stop or take here is always reduce-only.
            if not request.reduce_only or request.margin != 0:
                raise ValueError("stop and take orders must be reduce-only with margin 0")
            if request.trigger_price is None or request.trigger_price <= 0:
                raise ValueError("stop and take orders need a positive trigger price")
            order_type = conditional_order_type(request.kind, request.side)
            trigger_price = round_trigger(request.trigger_price, info.price_tick, request.kind, request.side)
            margin = Decimal(0)

        return self._composer.msg_create_derivative_market_order(
            market_id=info.market_id,
            sender=granter,
            subaccount_id=subaccount_id,
            fee_recipient=granter,
            price=worst_price,
            quantity=quantity,
            margin=margin,
            order_type=order_type,
            cid=request.cid or None,
            trigger_price=trigger_price,
        )

    def _round_margin(self, margin: Decimal) -> Decimal:
        """Up to the quote token's precision, so margin is never below what the order needs."""
        return margin.quantize(Decimal(1).scaleb(-self._quote_decimals), rounding=ROUND_CEILING)

    @staticmethod
    def _build_cancel_message(info: MarketInfo, granter: str, subaccount_id: str, order: RestingOrder) -> Any:
        if order.kind is OrderKind.MARKET:
            raise ValueError("only conditional orders can be cancelled here")
        if not order.order_hash and not order.cid:
            raise ValueError("cancel needs an order hash or a cid")
        return exchange_tx_pb2.MsgCancelDerivativeOrder(
            sender=granter,
            market_id=info.market_id,
            subaccount_id=subaccount_id,
            order_hash=order.order_hash or None,
            cid=None if order.order_hash else order.cid,
            order_mask=CANCEL_CONDITIONAL_MARKET_MASK,
        )

    # Signing ----------------------------------------------------------------------------------

    async def _read_account(self, address: str) -> tuple[int, int]:
        """Account number and sequence from the chain, with no local counter."""
        try:
            response = await self._limited(self._ensure_client().auth_api.fetch_account(address=address), self._chain_limiter)
        except grpc.aio.AioRpcError as exc:
            if exc.code() == grpc.StatusCode.NOT_FOUND:
                raise GranteeAccountNotFound(f"no account on chain for {address}") from exc
            raise ChainUnavailable("account request failed") from exc
        except _TRANSPORT_ERRORS as exc:
            raise ChainUnavailable("account request failed") from exc
        account = response["account"]
        base = account.get("baseAccount") or account
        return int(base.get("accountNumber") or 0), int(base.get("sequence") or 0)

    async def _gas_fee(self, gas_limit: int) -> Any:
        client = self._ensure_client()
        gas_price = max(await self._rpc(client.current_chain_gas_price()), GAS_PRICE)
        return self._composer.coin(amount=gas_limit * gas_price * 11 // 10, denom=self._network.fee_denom)

    def _assert_no_in_flight(self, sequence: int, height: int) -> None:
        """Refuse a new signature while the previous tx from this key could still land."""
        last = self._last_signed
        if last is not None and sequence <= last.sequence and height <= last.timeout_height + EXPIRY_MARGIN_BLOCKS:
            raise InFlightTx(f"tx {last.tx_hash} is not final yet")

    async def _sign_with_grantee(self, inner_messages: Sequence[Any]) -> SignedTx:
        """Wrap in one MsgExec and sign once, with a fresh sequence and timeout height."""
        gas_limit = gas_limit_for(inner_messages)
        exec_message = self._composer.msg_exec(grantee=self.grantee_address, msgs=list(inner_messages))
        async with self._sign_lock:
            account_number, sequence = await self._read_account(self.grantee_address)
            height = await self._latest_height()
            self._assert_no_in_flight(sequence, height)
            timeout_height = height + self._tx_timeout_blocks
            tx_bytes, tx_hash = sign_transaction(
                self._key,
                [exec_message],
                account_number=account_number,
                sequence=sequence,
                chain_id=self._network.chain_id,
                gas_limit=gas_limit,
                fee_coin=await self._gas_fee(gas_limit),
                timeout_height=timeout_height,
            )
            signed = SignedTx(
                tx_hash=tx_hash,
                tx_bytes=tx_bytes,
                signer=self.grantee_address,
                sequence=sequence,
                timeout_height=timeout_height,
            )
            self._last_signed = signed
            return signed

    async def build_and_sign(
        self,
        granter: str,
        subaccount_id: str,
        orders: Sequence[OrderRequest],
        cancels: Sequence[RestingOrder] = (),
    ) -> SignedTx:
        with urgent_requests():
            return await self._build_and_sign(granter, subaccount_id, orders, cancels)

    async def _build_and_sign(
        self,
        granter: str,
        subaccount_id: str,
        orders: Sequence[OrderRequest],
        cancels: Sequence[RestingOrder],
    ) -> SignedTx:
        self._assert_signing_allowed()
        info = await self.market()
        try:
            if not orders and not cancels:
                raise ValueError("nothing to sign")
            self._assert_subaccount_of(granter, subaccount_id)
            # Cancels go first so a replacement stop fits the one-per-direction rule in the same tx.
            inner = [self._build_cancel_message(info, granter, subaccount_id, order) for order in cancels]
            inner += [self._build_order_message(info, granter, subaccount_id, order) for order in orders]
        except ValueError as exc:
            raise OrderRefused(str(exc)) from exc
        return await self._sign_with_grantee(inner)

    async def broadcast(self, tx: SignedTx) -> None:
        """Send the exact bytes in sync mode. A CheckTx rejection is logged, never acted on.

        A re-broadcast of a tx that was already included is rejected by CheckTx with a sequence
        mismatch, so a CheckTx code cannot tell us the tx failed. `lookup` decides that.
        """
        self._assert_signing_allowed()
        with urgent_requests():
            response = await self._rpc(self._ensure_client().broadcast_tx_sync_mode(tx.tx_bytes))
        code = int((response.get("txResponse") or {}).get("code") or 0)
        if code not in (0, CODE_TX_IN_MEMPOOL_CACHE):
            logger.warning("CheckTx rejected tx %s with code %s", tx.tx_hash, code)

    async def lookup(self, tx: SignedTx) -> TxResult:
        """By hash through chain gRPC. The height is read first so a late tx is not missed."""
        with urgent_requests():
            return await self._lookup(tx)

    async def _lookup(self, tx: SignedTx) -> TxResult:
        client = self._ensure_client()
        height = await self._latest_height()
        try:
            response = await self._limited(client.tx_api.fetch_tx(hash=tx.tx_hash), self._chain_limiter)
        except grpc.aio.AioRpcError as exc:
            if exc.code() != grpc.StatusCode.NOT_FOUND:
                raise ChainUnavailable("tx lookup failed") from exc
            if height > tx.timeout_height + EXPIRY_MARGIN_BLOCKS:
                return TxResult(status=TxStatus.EXPIRED)
            return TxResult(status=TxStatus.PENDING)
        except _TRANSPORT_ERRORS as exc:
            raise ChainUnavailable("tx lookup failed") from exc
        except (json_format.Error, TypeError, ValueError) as exc:
            raise ChainUnavailable("tx response could not be parsed") from exc

        result = response.get("txResponse") or {}
        if not result.get("height"):
            raise ChainUnavailable("tx response has no inclusion height")
        code = int(result.get("code") or 0)
        return TxResult(
            status=TxStatus.OK if code == 0 else TxStatus.FAILED,
            code=code,
            raw_log=result.get("rawLog", ""),
            height=int(result["height"]),
        )

    # Used only by scripts/testnet_cases.py and scripts/testnet_setup.py -----------------------

    def limit_order_message(
        self,
        info: MarketInfo,
        granter: str,
        subaccount_id: str,
        side: Side,
        price: Decimal,
        quantity: Decimal,
        margin: Decimal,
        cid: str,
        expiration_block: Optional[int] = None,
    ) -> Any:
        """A vanilla limit order. Not part of `ChainBackend`, which has market entries only."""
        return self._composer.msg_create_derivative_limit_order(
            market_id=info.market_id,
            sender=granter,
            subaccount_id=subaccount_id,
            fee_recipient=granter,
            price=quantize(price, info.price_tick, ROUND_FLOOR if is_buy(side) else ROUND_CEILING),
            quantity=quantize(quantity, info.quantity_tick, ROUND_FLOOR),
            margin=self._round_margin(margin),
            order_type="BUY" if is_buy(side) else "SELL",
            cid=cid or None,
            expiration_block=expiration_block,
        )

    def order_request_message(self, info: MarketInfo, granter: str, subaccount_id: str, request: OrderRequest) -> Any:
        return self._build_order_message(info, granter, subaccount_id, request)

    async def sign_exec_messages(self, inner_messages: Sequence[Any]) -> SignedTx:
        """Sign arbitrary order messages under the grant, for the testnet cases."""
        self._assert_signing_allowed()
        return await self._sign_with_grantee(inner_messages)

    async def sign_wallet_tx(self, private_key: PrivateKey, messages: Sequence[Any], gas_limit: int) -> SignedTx:
        """Sign directly with a wallet key (grant issue, deposits, house liquidity)."""
        self._assert_signing_allowed()
        address = private_key.to_public_key().to_address().to_acc_bech32()
        account_number, sequence = await self._read_account(address)
        timeout_height = await self._latest_height() + self._tx_timeout_blocks
        tx_bytes, tx_hash = sign_transaction(
            private_key,
            list(messages),
            account_number=account_number,
            sequence=sequence,
            chain_id=self._network.chain_id,
            gas_limit=gas_limit,
            fee_coin=await self._gas_fee(gas_limit),
            timeout_height=timeout_height,
        )
        return SignedTx(
            tx_hash=tx_hash,
            tx_bytes=tx_bytes,
            signer=address,
            sequence=sequence,
            timeout_height=timeout_height,
        )

    async def simulate(self, tx: SignedTx) -> dict:
        """Dry-run signed bytes. Raises grpc.aio.AioRpcError with the chain's reason."""
        return await self._limited(self._ensure_client().simulate(tx.tx_bytes), self._chain_limiter)
