"""In-memory Injective simulator for tests. Implements `ChainBackend` and enforces every
behaviour documented in `chain/base.py`. It never touches a network.

Model, in order, per block (`advance_block`):

1. Height advances, the clock advances `block_ms`, the mark price updates.
2. DeliverTx in mempool order: a tx with a wrong sequence or past `timeout_height` is dropped;
   otherwise the sequence is consumed and its messages applied atomically to a copy.
3. EndBlock: market orders execute, then conditional orders trigger on mark and execute,
   then reduce-only conditional orders are removed from any subaccount without a position.

Test controls: `advance_block`, `set_book_depth`, `inject`, `go_down`, `fail_calls`,
`freeze_market_data`, `set_market_as_of`, `set_read_lag`, `set_grantee_balance`, `arm_crash`, `throttle_calls`,
plus the `signed_log`, `broadcast_log` and `included` logs.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import Enum
from typing import Callable, Optional, Sequence

from .base import (
    CODE_DUPLICATE_CID,
    Account,
    Candle,
    ChainThrottled,
    ChainUnavailable,
    GrantStatus,
    MarketInfo,
    MarketSnapshot,
    OrderKind,
    OrderRequest,
    Position,
    RestingOrder,
    Side,
    SignedTx,
    TxResult,
    TxStatus,
)

HOUR_MS = 3_600_000
QUARTER_MS = 900_000

CODE_UNAUTHORIZED = 4
CODE_INVALID_PRICE = 16
CODE_INVALID_QUANTITY = 17
CODE_WORST_PRICE = 25  # the order does not cross, or the side it needs is empty
CODE_BANKRUPTCY = 30
CODE_SEQUENCE_MISMATCH = 32
CODE_MARKET_ORDER_EXISTS = 38
CODE_ORDER_NOT_FOUND = 77
CODE_NO_MARGIN_LOCKED = 81  # ErrNoMarginLocked
CODE_MIN_NOTIONAL = 100
CODE_BAD_TRIGGER = 61
CODE_CONDITIONAL_EXISTS = 104
CODE_INJECTED_STOP_FAILURE = 500

TX_FEE_INJ = Decimal("0.0001")
HISTORY_BLOCKS = 64

DEFAULT_MARKET = MarketInfo(
    market_id="0x" + "ab" * 32,
    ticker="BTC/USDC PERP",
    quote_denom="usdc",
    price_tick=Decimal("0.01"),
    quantity_tick=Decimal("0.0001"),
    min_notional=Decimal("5"),
    taker_fee=Decimal("0.0005"),
    maker_fee=Decimal("0.0002"),
    initial_margin_ratio=Decimal("0.05"),
)


def _wall_clock() -> int:
    return int(time.time() * 1000)


class MockClock:
    """Injectable millisecond clock: `ctx.clock` is an instance of this in tests."""

    def __init__(self, start_ms: int = 1_800_000_000_000) -> None:
        self.ms = start_ms

    def __call__(self) -> int:
        return self.ms

    def advance(self, delta_ms: int) -> int:
        self.ms += delta_ms
        return self.ms

    def set(self, ms: int) -> int:
        self.ms = ms
        return self.ms


class Failure(str, Enum):
    TRANSPORT_AFTER_INCLUSION = "transport_after_inclusion"
    TRANSPORT_BEFORE_INCLUSION = "transport_before_inclusion"
    NEVER_INCLUDED = "never_included"
    STOP_PLACEMENT = "stop_placement"
    STOP_TRIGGER = "stop_trigger"
    SEQUENCE_MISMATCH = "sequence_mismatch"
    PARTIAL_FILL = "partial_fill"
    CHECK_TX_FEE = "check_tx_fee"


class SimulatedCrash(BaseException):
    """Stands in for process death: nothing below `except Exception` may survive it."""


@dataclass
class _Position:
    side: Side
    quantity: Decimal
    entry_price: Decimal
    margin: Decimal


@dataclass
class _Sub:
    granter: str
    subaccount_id: str
    deposits: Decimal
    position: Optional[_Position] = None
    orders: list[RestingOrder] = field(default_factory=list)
    granted: bool = True
    missing_grants: tuple[str, ...] = ()
    available_override: Optional[Decimal] = None


@dataclass
class _SignedEntry:
    signed: SignedTx
    subaccount_id: str
    orders: tuple[OrderRequest, ...]
    cancels: tuple[RestingOrder, ...]
    bad_sequence: bool = False


MARKET_ORDER_SLOT = ""  # shares the block's cid set, so a failed tx rolls it back with the cids


def _floor(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def _ceil(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick


class MockChain:
    def __init__(
        self,
        *,
        clock: Optional[Callable[[], int]] = None,
        market: MarketInfo = DEFAULT_MARKET,
        mark: Decimal = Decimal("60000"),
        start_height: int = 1_000,
        tx_timeout_blocks: int = 40,
        signer: str = "inj1grantee00000000000000000000000000000",
        half_spread_bps: Decimal = Decimal("1"),
        depth_usd: Decimal = Decimal("1000000"),
        auto_mine: Optional[bool] = None,
        block_ms: int = 1_000,
    ) -> None:
        self._clock = clock or _wall_clock
        # A wall-clock mock has nobody calling advance_block, so it mines on its own.
        self.auto_mine = (clock is None) if auto_mine is None else auto_mine
        self.market_info = market
        self.mark = Decimal(mark)
        self.oracle: Optional[Decimal] = None
        self.height = start_height
        self.tx_timeout_blocks = tx_timeout_blocks
        self.signer = signer
        self.half_spread_bps = Decimal(half_spread_bps)
        self.bid_depth_usd = Decimal(depth_usd)
        self.ask_depth_usd = Decimal(depth_usd)
        self.funding_rate = Decimal(0)
        self.block_ms = block_ms
        self.candle_range = Decimal("300")
        self._last_block_ms = self._clock()
        self._frozen_as_of_ms: Optional[int] = None
        self._candles: Optional[list[Candle]] = None
        self._candles_15m: Optional[list[Candle]] = None
        self._subs: dict[str, _Sub] = {}
        self._sequences: dict[str, int] = {signer: 7}
        self._signed: dict[str, _SignedEntry] = {}
        self._mempool: list[str] = []
        self._never_included: set[str] = set()
        self._dropped: set[str] = set()
        self._block_market_orders: list[tuple[str, OrderRequest]] = []
        self._block_market_cids: set[tuple[str, str]] = set()
        self._failures: dict[Failure, int] = {}
        self._partial_ratio = Decimal("0.6")
        self._down = False
        self.grantee_inj = Decimal("10")
        self.read_lag = 0
        self._history: deque[tuple[int, Decimal, dict[str, _Sub]]] = deque(maxlen=HISTORY_BLOCKS)
        self._fail_calls: dict[str, list[int]] = {}  # method -> [skip, count]
        self._throttle_calls: dict[str, list[int]] = {}  # method -> [skip, count]
        self._retry_after_s: Optional[float] = None
        self.throttled_total = 0
        self._crashes: dict[tuple[str, str], list[int]] = {}  # (method, when) -> [skip, count]

        # Logs tests assert against.
        self.signed_log: list[SignedTx] = []
        self.broadcast_log: list[str] = []
        self.included: dict[str, TxResult] = {}
        self.triggered: list[tuple[RestingOrder, bool]] = []  # (order, executed)

    # ------------------------------------------------------------------ test setup
    def register_account(
        self, granter: str, subaccount_id: str, deposits: Decimal, *, granted: bool = True
    ) -> None:
        self._subs[subaccount_id] = _Sub(granter, subaccount_id, Decimal(deposits), granted=granted)

    def sub(self, subaccount_id: str) -> _Sub:
        return self._subs[subaccount_id]

    def deposit(self, subaccount_id: str, amount: Decimal) -> None:
        self._subs[subaccount_id].deposits += Decimal(amount)

    def withdraw(self, subaccount_id: str, amount: Decimal) -> None:
        self._subs[subaccount_id].deposits -= Decimal(amount)

    def set_available(self, subaccount_id: str, available: Optional[Decimal]) -> None:
        """Force `available` regardless of deposits (None restores the computed value)."""
        self._subs[subaccount_id].available_override = None if available is None else Decimal(available)

    def set_grant(self, subaccount_id: str, granted: bool, missing: tuple[str, ...] = ()) -> None:
        sub = self._subs[subaccount_id]
        sub.granted = granted
        sub.missing_grants = missing if not granted else ()

    def add_resting_order(self, subaccount_id: str, order: RestingOrder) -> None:
        """Place an order directly, skipping validation: stale orders the watcher must clean."""
        self._subs[subaccount_id].orders.append(order)

    def set_book_depth(self, bid: Optional[Decimal] = None, ask: Optional[Decimal] = None) -> None:
        if bid is not None:
            self.bid_depth_usd = Decimal(bid)
        if ask is not None:
            self.ask_depth_usd = Decimal(ask)

    def set_half_spread_bps(self, bps: Decimal) -> None:
        self.half_spread_bps = Decimal(bps)

    def set_candles_15m(self, candles: Sequence[Candle]) -> None:
        self._candles_15m = list(candles)

    def set_candles(self, candles: Sequence[Candle]) -> None:
        self._candles = list(candles)

    def freeze_market_data(self) -> None:
        """Stop `as_of_ms` from advancing: market data goes stale as the clock runs."""
        self._frozen_as_of_ms = self._snapshot_as_of()

    def unfreeze_market_data(self) -> None:
        self._frozen_as_of_ms = None

    def set_market_as_of(self, ms: Optional[int]) -> None:
        """Report this `as_of_ms` whatever the clock says (None returns to normal)."""
        self._frozen_as_of_ms = ms

    def set_read_lag(self, blocks: int) -> None:
        """`account` answers from a node this many blocks behind."""
        self.read_lag = blocks

    def set_grantee_balance(self, inj: Decimal) -> None:
        self.grantee_inj = Decimal(inj)

    def inject(self, kind: Failure, count: int = 1, ratio: Optional[Decimal] = None) -> None:
        self._failures[kind] = self._failures.get(kind, 0) + count
        if ratio is not None:
            self._partial_ratio = Decimal(ratio)

    def _take(self, kind: Failure) -> bool:
        if self._failures.get(kind, 0) > 0:
            self._failures[kind] -= 1
            return True
        return False

    def go_down(self) -> None:
        self._down = True

    def come_back(self) -> None:
        self._down = False

    def fail_calls(self, method: str, count: int = 1, skip: int = 0) -> None:
        """Calls to `method` raise ChainUnavailable: `skip` succeed, the next `count` fail."""
        self._fail_calls[method] = [skip, count]

    def throttle_calls(self, method: str, count: int = 1, skip: int = 0, retry_after_s: Optional[float] = None) -> None:
        """Calls to `method` are refused for rate, as a public node answers 429."""
        self._throttle_calls[method] = [skip, count]
        self._retry_after_s = retry_after_s

    def arm_crash(self, method: str, when: str = "before", count: int = 1, skip: int = 0) -> None:
        """Simulate process death before or after `method`: `skip` calls pass, the next `count` crash."""
        self._crashes[(method, when)] = [skip, count]

    @staticmethod
    def _armed(slot: Optional[list[int]]) -> bool:
        if slot is None:
            return False
        if slot[0] > 0:
            slot[0] -= 1
            return False
        if slot[1] > 0:
            slot[1] -= 1
            return True
        return False

    def _before(self, method: str) -> None:
        if self._armed(self._crashes.get((method, "before"))):
            raise SimulatedCrash(f"crash before {method}")
        if self._down:
            raise ChainUnavailable(method)
        if self._armed(self._throttle_calls.get(method)):
            self.throttled_total += 1
            raise ChainThrottled(self._retry_after_s)
        if self._armed(self._fail_calls.get(method)):
            raise ChainUnavailable(method)

    def _after(self, method: str) -> None:
        if self._armed(self._crashes.get((method, "after"))):
            raise SimulatedCrash(f"crash after {method}")

    # ------------------------------------------------------------------ book
    @property
    def best_bid(self) -> Decimal:
        return _floor(self.mark * (1 - self.half_spread_bps / Decimal(10_000)), self.market_info.price_tick)

    @property
    def best_ask(self) -> Decimal:
        return _ceil(self.mark * (1 + self.half_spread_bps / Decimal(10_000)), self.market_info.price_tick)

    # ------------------------------------------------------------------ blocks
    def advance_block(self, mark: Optional[Decimal] = None, blocks: int = 1) -> int:
        """Produce `blocks` blocks. `mark` applies from the first of them."""
        for index in range(blocks):
            self._produce_block(Decimal(mark) if (mark is not None and index == 0) else None)
        return self.height

    def _produce_block(self, mark: Optional[Decimal]) -> None:
        if mark is not None:
            self.mark = mark
        self.height += 1
        if hasattr(self._clock, "advance"):
            self._clock.advance(self.block_ms)
        self._last_block_ms = self._clock()
        pending, self._mempool = self._mempool, []
        for tx_hash in pending:
            self._deliver(tx_hash)
        self._end_block()
        self._history.append((self.height, self.mark, copy.deepcopy(self._subs)))

    def _deliver(self, tx_hash: str) -> None:
        entry = self._signed[tx_hash]
        tx = entry.signed
        if tx.timeout_height < self.height:
            self._dropped.add(tx_hash)  # too late: can never be included
            return
        expected = self._sequences.get(tx.signer, 0)
        if tx.sequence != expected:
            self._dropped.add(tx_hash)  # ante handler rejects it, sequence not consumed
            return
        self._sequences[tx.signer] = expected + 1  # consumed even if a message fails
        self.grantee_inj -= TX_FEE_INJ

        sub = self._subs[entry.subaccount_id]
        if not sub.granted:
            self.included[tx_hash] = TxResult(TxStatus.FAILED, CODE_UNAUTHORIZED, "authz grant not found", self.height)
            return
        if any(o.kind is OrderKind.STOP for o in entry.orders) and self._take(Failure.STOP_PLACEMENT):
            self.included[tx_hash] = TxResult(
                TxStatus.FAILED, CODE_INJECTED_STOP_FAILURE, "injected stop placement failure", self.height
            )
            return

        shadow = copy.deepcopy(sub)
        markets: list[tuple[str, OrderRequest]] = []
        cids: set[tuple[str, str]] = set(self._block_market_cids)
        for index, cancel in enumerate(entry.cancels):
            if not any(o.order_hash == cancel.order_hash for o in shadow.orders):
                self.included[tx_hash] = TxResult(TxStatus.FAILED, CODE_ORDER_NOT_FOUND, "order not found", self.height)
                return
            shadow.orders = [o for o in shadow.orders if o.order_hash != cancel.order_hash]
        for index, order in enumerate(entry.orders):
            code, log = self._apply_order(shadow, order, f"{tx_hash}:{index}", markets, cids)
            if code:
                self.included[tx_hash] = TxResult(TxStatus.FAILED, code, log, self.height)
                return
        self._subs[entry.subaccount_id] = shadow
        self._block_market_orders.extend(markets)
        self._block_market_cids = cids
        self.included[tx_hash] = TxResult(TxStatus.OK, 0, "", self.height)

    def _apply_order(
        self,
        sub: _Sub,
        order: OrderRequest,
        order_id: str,
        markets: list[tuple[str, OrderRequest]],
        cids: set[tuple[str, str]],
    ) -> tuple[int, str]:
        if order.kind is OrderKind.MARKET:
            key = (sub.subaccount_id, order.cid)
            if key in cids or any(o.cid == order.cid for o in sub.orders):
                return CODE_DUPLICATE_CID, "duplicate cid"
            slot = (sub.subaccount_id, MARKET_ORDER_SLOT)
            if slot in cids:
                return CODE_MARKET_ORDER_EXISTS, "a market order from this subaccount is already in this block"
            if order.reduce_only and (sub.position is None or sub.position.side is order.side):
                return CODE_NO_MARGIN_LOCKED, "reduce-only order has no position to reduce"
            code, log = self._check_ticks_and_notional(order)
            if code:
                return code, log
            buying = order.side is Side.LONG
            best, depth = (self.best_ask, self.ask_depth_usd) if buying else (self.best_bid, self.bid_depth_usd)
            if depth <= 0 or (order.worst_price < best if buying else order.worst_price > best):
                return CODE_WORST_PRICE, "orderbook cannot satisfy the worst price"
            if order.reduce_only and self._beyond_bankruptcy(sub.position, order.worst_price):
                return CODE_BANKRUPTCY, "price surpasses bankruptcy price"
            cids.add(key)
            cids.add(slot)
            markets.append((sub.subaccount_id, order))
            return 0, ""

        # Conditional market order (STOP or TAKE).
        if not order.reduce_only:
            return CODE_BAD_TRIGGER, "non reduce-only conditional is not modelled"
        if order.trigger_price is None:
            return CODE_BAD_TRIGGER, "conditional order needs a trigger price"
        if sub.position is None or sub.position.side is order.side:
            # Market orders are not margin-locked when placed, so an entry in the same tx
            # does not count: this is the code 81 the spec's separate-tx rule exists for.
            return CODE_NO_MARGIN_LOCKED, "reduce-only conditional needs an open position"
        below_mark = order.trigger_price < self.mark
        if order.trigger_price == self.mark:
            return CODE_BAD_TRIGGER, "trigger equals mark"
        expected_below = (order.kind is OrderKind.STOP) == (order.side is Side.SHORT)
        if below_mark != expected_below:
            return CODE_BAD_TRIGGER, "trigger on wrong side of mark"
        if any(o.cid == order.cid for o in sub.orders) or (sub.subaccount_id, order.cid) in cids:
            return CODE_DUPLICATE_CID, "duplicate cid"
        code, log = self._check_ticks_and_notional(order)
        if code:
            return code, log
        # One conditional market order per trigger direction (above or below mark). A stop and a
        # take-profit on one position trigger in opposite directions, so they can coexist.
        for existing in sub.orders:
            if existing.trigger_price is not None and (existing.trigger_price < self.mark) == below_mark:
                return CODE_CONDITIONAL_EXISTS, "conditional order already exists in this direction"
        sub.orders.append(
            RestingOrder(
                order_hash=hashlib.sha256(order_id.encode()).hexdigest(),
                cid=order.cid,
                kind=order.kind,
                side=order.side,
                quantity=order.quantity,
                trigger_price=order.trigger_price,
                worst_price=order.worst_price,
                reduce_only=True,
            )
        )
        return 0, ""

    def _check_ticks_and_notional(self, order: OrderRequest) -> tuple[int, str]:
        info = self.market_info
        if order.quantity % info.quantity_tick != 0:
            return CODE_INVALID_QUANTITY, "quantity off tick"
        if order.worst_price % info.price_tick != 0 or (order.trigger_price is not None and order.trigger_price % info.price_tick != 0):
            return CODE_INVALID_PRICE, "price off tick"
        if order.quantity * order.worst_price < info.min_notional:
            return CODE_MIN_NOTIONAL, "notional at worst price is below the minimum"
        return 0, ""

    @staticmethod
    def _beyond_bankruptcy(position: _Position, price: Decimal) -> bool:
        """A reduce-only order cannot trade past the price at which the margin is gone."""
        per_unit = position.margin / position.quantity
        if position.side is Side.LONG:
            return price < position.entry_price - per_unit
        return price > position.entry_price + per_unit

    def _end_block(self) -> None:
        orders, self._block_market_orders = self._block_market_orders, []
        self._block_market_cids = set()
        for subaccount_id, order in orders:
            self._execute_market(self._subs[subaccount_id], order)
        for sub in self._subs.values():
            for order in list(sub.orders):
                if self._triggered(order):
                    sub.orders.remove(order)
                    executed = False
                    if not (order.kind is OrderKind.STOP and self._take(Failure.STOP_TRIGGER)):
                        executed = self._execute_market(
                            sub,
                            OrderRequest(
                                kind=OrderKind.MARKET,
                                side=order.side,
                                quantity=order.quantity,
                                worst_price=order.worst_price,
                                cid=order.cid,
                                reduce_only=True,
                            ),
                        )
                    self.triggered.append((order, executed))
            if sub.position is None:
                sub.orders = [o for o in sub.orders if not o.reduce_only]

    def _triggered(self, order: RestingOrder) -> bool:
        if order.trigger_price is None:
            return False
        below_mark = (order.kind is OrderKind.STOP) == (order.side is Side.SHORT)
        return self.mark <= order.trigger_price if below_mark else self.mark >= order.trigger_price

    def _execute_market(self, sub: _Sub, order: OrderRequest) -> bool:
        position = sub.position
        quantity = order.quantity
        if order.reduce_only:
            if position is None or position.side is order.side:
                return False
            quantity = min(quantity, position.quantity)
        price = self.best_ask if order.side is Side.LONG else self.best_bid
        if order.side is Side.LONG and price > order.worst_price:
            return False
        if order.side is Side.SHORT and price < order.worst_price:
            return False
        if order.reduce_only and self._beyond_bankruptcy(position, price):
            return False
        depth = self.ask_depth_usd if order.side is Side.LONG else self.bid_depth_usd
        available_qty = _floor(depth / price, self.market_info.quantity_tick)
        fill = min(quantity, available_qty)
        if not order.reduce_only and self._take(Failure.PARTIAL_FILL):
            fill = _floor(quantity * self._partial_ratio, self.market_info.quantity_tick)
        if fill <= 0:
            return False
        margin = order.margin * fill / order.quantity if not order.reduce_only else Decimal(0)
        if not order.reduce_only and margin > self._available(sub):
            return False
        self._apply_fill(sub, order.side, fill, price, margin)
        return True

    def _apply_fill(self, sub: _Sub, side: Side, quantity: Decimal, price: Decimal, margin: Decimal) -> None:
        sub.deposits -= quantity * price * self.market_info.taker_fee
        position = sub.position
        if position is None:
            sub.position = _Position(side, quantity, price, margin)
        elif position.side is side:
            total = position.quantity + quantity
            position.entry_price = (position.entry_price * position.quantity + price * quantity) / total
            position.quantity = total
            position.margin += margin
        else:
            closed = min(position.quantity, quantity)
            direction = Decimal(1) if position.side is Side.LONG else Decimal(-1)
            sub.deposits += (price - position.entry_price) * closed * direction
            released = position.margin * closed / position.quantity
            position.margin -= released
            position.quantity -= closed
            if position.quantity == 0:
                sub.position = None

    def _available(self, sub: _Sub) -> Decimal:
        if sub.available_override is not None:
            return sub.available_override
        locked = sub.position.margin if sub.position else Decimal(0)
        return max(Decimal(0), sub.deposits - locked)

    def _unrealized(self, position: _Position, mark: Decimal) -> Decimal:
        direction = Decimal(1) if position.side is Side.LONG else Decimal(-1)
        return (mark - position.entry_price) * position.quantity * direction

    # ------------------------------------------------------------------ ChainBackend
    async def market(self) -> MarketInfo:
        self._before("market")
        return self.market_info

    def _snapshot_as_of(self) -> int:
        return self._clock() if self.auto_mine else self._last_block_ms

    async def snapshot(self) -> MarketSnapshot:
        self._before("snapshot")
        if self.auto_mine and not hasattr(self._clock, "advance"):
            self._last_block_ms = self._clock()
        as_of = self._frozen_as_of_ms if self._frozen_as_of_ms is not None else self._snapshot_as_of()
        result = MarketSnapshot(
            mark_price=self.mark,
            oracle_price=self.oracle if self.oracle is not None else self.mark,
            best_bid=self.best_bid if self.bid_depth_usd > 0 else Decimal(0),
            best_ask=self.best_ask if self.ask_depth_usd > 0 else Decimal(0),
            bid_depth_usd=self.bid_depth_usd,
            ask_depth_usd=self.ask_depth_usd,
            funding_rate=self.funding_rate,
            height=self.height,
            as_of_ms=as_of,
        )
        self._after("snapshot")
        return result

    async def candles_1h(self, limit: int) -> list[Candle]:
        self._before("candles_1h")
        return self._hourly(limit)

    async def live_candles_1h(self, limit: int) -> list[Candle]:
        self._before("live_candles_1h")
        return self._hourly(limit)

    def _hourly(self, limit: int) -> list[Candle]:
        """A list from `set_candles` is served up to the mock's clock, like a real chart."""
        now = self._clock()
        if self._candles is not None:
            return [candle for candle in self._candles if candle.open_ms <= now][-limit:]
        hour_start = now - now % HOUR_MS
        half = self.candle_range / 2
        candles = []
        for index in range(limit, 0, -1):
            open_ms = hour_start - (index - 1) * HOUR_MS
            candles.append(Candle(open_ms, self.mark, self.mark + half, self.mark - half, self.mark))
        return candles

    async def candles_15m(self, limit: int) -> list[Candle]:
        self._before("candles_15m")
        if self._candles_15m is not None:
            return list(self._candles_15m)[-limit:]
        bar_start = self._clock() - self._clock() % QUARTER_MS
        half = self.candle_range / 4
        return [Candle(bar_start - (index - 1) * QUARTER_MS, self.mark, self.mark + half, self.mark - half, self.mark) for index in range(limit, 0, -1)]

    def _sub_or_new(self, granter: str, subaccount_id: str) -> _Sub:
        """Unknown accounts read as empty (a real chain has every subaccount, empty or not)."""
        return self._subs.setdefault(subaccount_id, _Sub(granter, subaccount_id, Decimal(0)))

    def _read_at_lag(self, granter: str, subaccount_id: str) -> tuple[int, Decimal, _Sub]:
        """(height, mark, subaccount) as the answering node sees them: `read_lag` blocks behind."""
        current = self._sub_or_new(granter, subaccount_id)
        if self.read_lag:
            for height, mark, subs in reversed(self._history):
                if height <= self.height - self.read_lag and subaccount_id in subs:
                    return height, mark, subs[subaccount_id]
        return self.height, self.mark, current

    async def account(self, granter: str, subaccount_id: str) -> Account:
        self._before("account")
        height, mark, sub = self._read_at_lag(granter, subaccount_id)
        if sub.granter != granter:
            raise AssertionError("subaccount does not belong to this granter")
        position = None
        unrealized = Decimal(0)
        if sub.position is not None:
            unrealized = self._unrealized(sub.position, mark)
            position = Position(
                side=sub.position.side,
                quantity=sub.position.quantity,
                entry_price=sub.position.entry_price,
                margin=sub.position.margin,
                unrealized_pnl=unrealized,
            )
        result = Account(
            subaccount_id=subaccount_id,
            equity=sub.deposits + unrealized,
            available=self._available(sub),
            position=position,
            orders=tuple(sub.orders),
            height=height,
            mark_price=mark,
        )
        self._after("account")
        return result

    async def check_grant(self, granter: str, subaccount_id: str) -> GrantStatus:
        self._before("check_grant")
        sub = self._sub_or_new(granter, subaccount_id)
        return GrantStatus(ok=sub.granted, missing=sub.missing_grants)

    async def grantee_balance_inj(self) -> Decimal:
        self._before("grantee_balance_inj")
        return self.grantee_inj

    async def build_and_sign(
        self,
        granter: str,
        subaccount_id: str,
        orders: Sequence[OrderRequest],
        cancels: Sequence[RestingOrder] = (),
    ) -> SignedTx:
        self._before("build_and_sign")
        if not orders and not cancels:
            raise ValueError("empty tx")
        if subaccount_id not in self._subs:
            raise AssertionError("unknown subaccount")
        sequence = self._sequences.get(self.signer, 0)
        bad_sequence = self._take(Failure.SEQUENCE_MISMATCH)
        if bad_sequence:
            sequence -= 1
        timeout_height = self.height + self.tx_timeout_blocks
        body = {
            "signer": self.signer,
            "granter": granter,
            "subaccount": subaccount_id,
            "sequence": sequence,
            "timeout_height": timeout_height,
            "orders": [_order_json(o) for o in orders],
            "cancels": [o.order_hash for o in cancels],
        }
        tx_bytes = json.dumps(body, sort_keys=True).encode()
        tx_hash = hashlib.sha256(tx_bytes).hexdigest().upper()
        signed = SignedTx(tx_hash, tx_bytes, self.signer, sequence, timeout_height)
        self._signed[tx_hash] = _SignedEntry(signed, subaccount_id, tuple(orders), tuple(cancels), bad_sequence)
        self.signed_log.append(signed)
        self._after("build_and_sign")
        return signed

    async def broadcast(self, tx: SignedTx) -> None:
        self._before("broadcast")
        entry = self._signed.get(tx.tx_hash)
        if entry is None or entry.signed.tx_bytes != tx.tx_bytes:
            raise AssertionError("broadcast bytes were never signed by this key")
        self.broadcast_log.append(tx.tx_hash)
        if self._take(Failure.TRANSPORT_BEFORE_INCLUSION):
            raise ChainUnavailable("transport error before inclusion")
        transport_after = self._take(Failure.TRANSPORT_AFTER_INCLUSION)
        if tx.tx_hash not in self.included and tx.tx_hash not in self._mempool:
            if entry.bad_sequence or self._take(Failure.CHECK_TX_FEE) or self.grantee_inj < TX_FEE_INJ:
                pass  # CheckTx failure: never enters the mempool, consumes no sequence, reports nothing
            elif self._take(Failure.NEVER_INCLUDED):
                self._never_included.add(tx.tx_hash)
            elif tx.tx_hash not in self._never_included:
                self._mempool.append(tx.tx_hash)
        self._after("broadcast")
        if transport_after:
            raise ChainUnavailable("transport error after inclusion")

    async def lookup(self, tx: SignedTx) -> TxResult:
        self._before("lookup")
        if self.auto_mine:
            self._produce_block(None)
        result = self.included.get(tx.tx_hash)
        if result is None:
            result = (
                TxResult(TxStatus.EXPIRED)
                if self.height > tx.timeout_height
                else TxResult(TxStatus.PENDING)
            )
        self._after("lookup")
        return result


def _order_json(order: OrderRequest) -> dict:
    return {
        "kind": order.kind.value,
        "side": order.side.value,
        "quantity": str(order.quantity),
        "worst_price": str(order.worst_price),
        "cid": order.cid,
        "reduce_only": order.reduce_only,
        "margin": str(order.margin),
        "trigger_price": None if order.trigger_price is None else str(order.trigger_price),
    }
