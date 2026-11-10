"""The only interface between trading logic and the chain.

`chain/injective.py` implements it for real, `chain/mock.py` for tests. The mock must
enforce the Injective behaviour documented here (read from public chain code
release/v1.20.x, 2026-09-02; mainnet code is private, so testnet cases confirm it):

- Market orders execute at end of block. They do not count as locked margin when placed.
- A reduce-only conditional order needs an open position or a resting vanilla order on the
  subaccount, otherwise the message fails with code 81 (ErrNoMarginLocked). So a market entry
  plus reduce-only stop in ONE tx fails as a whole. Separate txs are required.
- One conditional market order per (subaccount, market, direction).
- STOP_SELL/TAKE_BUY trigger price must be below mark; STOP_BUY/TAKE_SELL above mark.
- Conditional orders trigger on mark price at end of block and become market orders, which can
  fail on a thin book (position remains).
- A reduce-only conditional order is removed by the chain when the position is gone.
- A tx is atomic across its messages. A tx included with a failing message consumes the sequence.
- Duplicate cid is rejected only while the earlier order is live; market order cid lives one block.
- A tx not included by timeout_height will never be included.
- A market order must cross the book at its worst price, otherwise code 25. Empty side included.
- One market order per (subaccount, market, block), otherwise code 38.
- Quantity and prices must sit on the market ticks, and quantity * worst price must meet the
  market's minimum notional, for stops and closes too (code 100).
- A reduce-only order's price cannot pass the position's bankruptcy price (code 30).
- A triggered conditional order fills what the book holds; the rest of the position stays.
- A tx failing CheckTx (fee or gas) never enters the mempool and consumes no sequence.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Iterator, Optional, Protocol, Sequence


# The chain's own error code for a cid that is still live on an order, on either backend.
CODE_DUPLICATE_CID = 97


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def opposite(self) -> "Side":
        return Side.SHORT if self is Side.LONG else Side.LONG


class OrderKind(str, Enum):
    MARKET = "market"
    STOP = "stop"  # reduce-only, margin 0, conditional market
    TAKE = "take"  # reduce-only, margin 0, conditional market


class TxStatus(str, Enum):
    PENDING = "pending"  # not found yet, timeout_height not passed
    OK = "ok"  # included, code 0
    FAILED = "failed"  # included, code != 0, sequence consumed
    EXPIRED = "expired"  # latest height > timeout_height and not found: never executes


@dataclass(frozen=True)
class MarketInfo:
    market_id: str
    ticker: str
    quote_denom: str
    price_tick: Decimal
    quantity_tick: Decimal
    min_notional: Decimal
    taker_fee: Decimal
    maker_fee: Decimal
    initial_margin_ratio: Decimal


@dataclass(frozen=True)
class MarketSnapshot:
    mark_price: Decimal
    oracle_price: Decimal
    best_bid: Decimal
    best_ask: Decimal
    bid_depth_usd: Decimal  # within COLLAR of mark
    ask_depth_usd: Decimal
    funding_rate: Decimal  # per funding interval
    height: int
    as_of_ms: int


@dataclass(frozen=True)
class Candle:
    open_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class Position:
    side: Side
    quantity: Decimal
    entry_price: Decimal
    margin: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class RestingOrder:
    order_hash: str
    cid: str
    kind: OrderKind
    side: Side  # direction of the order itself (a stop for a long is SHORT)
    quantity: Decimal
    trigger_price: Optional[Decimal]
    worst_price: Decimal
    reduce_only: bool


@dataclass(frozen=True)
class Account:
    subaccount_id: str
    equity: Decimal  # deposits + unrealized pnl
    available: Decimal
    position: Optional[Position]
    orders: tuple[RestingOrder, ...] = ()
    height: int = 0  # oldest block height any part of this read was served at
    mark_price: Decimal = Decimal(0)  # the mark this read valued the position at


@dataclass(frozen=True)
class OrderRequest:
    kind: OrderKind
    side: Side
    quantity: Decimal
    worst_price: Decimal
    cid: str
    reduce_only: bool
    margin: Decimal = Decimal(0)
    trigger_price: Optional[Decimal] = None


@dataclass(frozen=True)
class SignedTx:
    tx_hash: str
    tx_bytes: bytes
    signer: str
    sequence: int
    timeout_height: int


@dataclass(frozen=True)
class TxResult:
    status: TxStatus
    code: int = 0
    raw_log: str = ""
    height: Optional[int] = None


@dataclass(frozen=True)
class GrantStatus:
    ok: bool
    missing: tuple[str, ...] = field(default_factory=tuple)
    expires_ms: Optional[int] = None


class ChainBackend(Protocol):
    """All calls may raise `ChainUnavailable` on transport failure."""

    async def market(self) -> MarketInfo: ...
    async def snapshot(self) -> MarketSnapshot: ...
    async def candles_1h(self, limit: int) -> list[Candle]: ...
    async def candles_15m(self, limit: int) -> list[Candle]: ...
    async def live_candles_1h(self, limit: int) -> list[Candle]:
        """Hourly candles including the bar still open, oldest first. For the page's chart only."""
        ...
    async def account(self, granter: str, subaccount_id: str) -> Account: ...
    async def check_grant(self, granter: str, subaccount_id: str) -> GrantStatus: ...
    async def grantee_balance_inj(self) -> Decimal: ...

    async def build_and_sign(
        self,
        granter: str,
        subaccount_id: str,
        orders: Sequence[OrderRequest],
        cancels: Sequence[RestingOrder] = (),
    ) -> SignedTx:
        """Sign exactly once with an explicit timeout_height read fresh for this tx.

        Does not broadcast. Orders and cancels in one call go in one atomic tx as individual
        messages wrapped in authz MsgExec. Never uses MsgBatchUpdateOrders.
        """
        ...

    async def broadcast(self, tx: SignedTx) -> None:
        """Send the exact bytes. Idempotent for the same bytes. Raises ChainUnavailable if unsure."""
        ...

    async def lookup(self, tx: SignedTx) -> TxResult:
        """Resolve by hash against chain gRPC, never the indexer."""
        ...


class ChainUnavailable(Exception):
    """Transport-level failure: the state of the chain is unknown to us."""


class ChainThrottled(ChainUnavailable):
    """A node refused the request for rate. The value is unknown, which is never the same as empty or zero."""

    def __init__(self, retry_after_s: Optional[float] = None) -> None:
        super().__init__("chain request throttled")
        self.retry_after_s = retry_after_s


_urgent_requests: contextvars.ContextVar[bool] = contextvars.ContextVar("urgent_chain_requests", default=False)


@contextmanager
def urgent_requests() -> Iterator[None]:
    """Chain requests made inside this block go ahead of ordinary reads: the close and stop paths."""
    token = _urgent_requests.set(True)
    try:
        yield
    finally:
        _urgent_requests.reset(token)


def requests_are_urgent() -> bool:
    return _urgent_requests.get()


class OrderRefused(ChainUnavailable, ValueError):
    """The backend refused to build this order. Nothing was signed, and retrying cannot help."""
