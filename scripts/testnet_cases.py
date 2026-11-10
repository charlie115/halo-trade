#!/usr/bin/env python
"""Test Injective's stop behaviour through InjectiveChain. Injective testnet only.

Run scripts/testnet_setup.py first. With funded wallets every case sends real testnet txs:

    a   market entry + reduce-only stop in one tx: rejected as a whole, code 81
    b   limit entry + reduce-only stop in one tx: both placed
    c   what a stop does after a partial fill
    d   stop removed by the chain after the limit entry expires unfilled
    e   stop on a thin book triggers and fails (needs an oracle that moves)
    v1  market entry, confirm position, stop in a separate tx through the grant, close
        (a code-0 stop here is the on-chain proof that the market-scoped grant covers it)

b, c and d rest limit orders through the grant, which needs the test-only extra that
testnet_setup.py issues. The maker wallet signs its own house liquidity and needs no grant.

--dry-run signs every case with placeholder account numbers and asks the node to simulate it.
Nothing is broadcast. Without funds, the simulation can only report the missing account.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable

import grpc

sys.path.insert(0, str(Path(__file__).resolve().parent))

from testnet_setup import (  # noqa: E402
    DEFAULT_TICKER,
    TX_TIMEOUT_BLOCKS,
    address_of,
    funding_shortfalls,
    grant_problems,
    load_or_create_wallets,
    make_testnet_chain,
    private_key,
    require_testnet,
    require_testnet_chain,
    send_and_wait,
    subaccount_of,
)

from halo_trade.chain.base import (  # noqa: E402
    Account,
    MarketInfo,
    MarketSnapshot,
    OrderKind,
    OrderRequest,
    RestingOrder,
    Side,
    SignedTx,
    TxResult,
    TxStatus,
)
from halo_trade.chain.injective import (  # noqa: E402
    GAS_ANTE,
    GAS_CANCEL_ORDER,
    GAS_LIMIT_ORDER,
    InFlightTx,
    InjectiveChain,
    gas_limit_for,
    quantize,
    sign_transaction,
)

STOP_DISTANCE = Decimal("0.02")
WORST_DISTANCE = Decimal("0.03")
TAKER_SLIPPAGE = Decimal("0.03")
LEVERAGE = Decimal("2")
SETTLE_S = 3.0
ORDER_LOTS = 3
IN_FLIGHT_RETRIES = 15


@dataclass
class Row:
    case: str
    expected: str
    outcome: str = "NOT RUN"
    detail: str = ""
    tx_hashes: list[str] = field(default_factory=list)


@dataclass
class Ctx:
    chain: InjectiveChain
    keys: dict[str, str]
    info: MarketInfo
    dry_run: bool

    @property
    def trader(self) -> str:
        return address_of(self.keys["trader"])

    @property
    def trader_sub(self) -> str:
        return subaccount_of(self.keys["trader"])

    @property
    def maker(self) -> str:
        return address_of(self.keys["maker"])

    @property
    def maker_sub(self) -> str:
        return subaccount_of(self.keys["maker"])


# Order helpers ------------------------------------------------------------------------------

def lots(ctx: Ctx, count: int, mark: Decimal) -> Decimal:
    """`count` units of the smallest size that clears min notional at this price."""
    tick = ctx.info.quantity_tick
    minimum = max(tick, quantize(ctx.info.min_notional * Decimal("1.5") / mark, tick, ROUND_CEILING))
    return minimum * count


def margin_for(quantity: Decimal, price: Decimal) -> Decimal:
    return quantity * price / LEVERAGE


def market_entry(quantity: Decimal, mark: Decimal, cid: str) -> OrderRequest:
    worst = mark * (1 + TAKER_SLIPPAGE)
    return OrderRequest(
        kind=OrderKind.MARKET,
        side=Side.LONG,
        quantity=quantity,
        worst_price=worst,
        cid=cid,
        reduce_only=False,
        margin=margin_for(quantity, worst),
    )


def market_close(position_quantity: Decimal, mark: Decimal, cid: str) -> OrderRequest:
    return OrderRequest(
        kind=OrderKind.MARKET,
        side=Side.SHORT,
        quantity=position_quantity,
        worst_price=mark * (1 - TAKER_SLIPPAGE),
        cid=cid,
        reduce_only=True,
    )


def stop_for_long(quantity: Decimal, mark: Decimal, cid: str, distance: Decimal = STOP_DISTANCE) -> OrderRequest:
    trigger = mark * (1 - distance)
    return OrderRequest(
        kind=OrderKind.STOP,
        side=Side.SHORT,
        quantity=quantity,
        worst_price=trigger * (1 - WORST_DISTANCE),
        cid=cid,
        reduce_only=True,
        trigger_price=trigger,
    )


def limit_entry_message(ctx: Ctx, price: Decimal, quantity: Decimal, cid: str, expiration_block: int | None = None) -> Any:
    return ctx.chain.limit_order_message(
        ctx.info,
        granter=ctx.trader,
        subaccount_id=ctx.trader_sub,
        side=Side.LONG,
        price=price,
        quantity=quantity,
        margin=margin_for(quantity, price),
        cid=cid,
        expiration_block=expiration_block,
    )


def order_message(ctx: Ctx, request: OrderRequest) -> Any:
    return ctx.chain.order_request_message(ctx.info, ctx.trader, ctx.trader_sub, request)


# Chain helpers ------------------------------------------------------------------------------

async def height(ctx: Ctx) -> int:
    return await ctx.chain._latest_height()


async def wait_blocks(ctx: Ctx, count: int) -> None:
    target = await height(ctx) + count
    while await height(ctx) < target:
        await asyncio.sleep(0.5)


async def state(ctx: Ctx) -> Account:
    return await ctx.chain.account(ctx.trader, ctx.trader_sub)


async def resting_limit_orders(ctx: Ctx, subaccount_id: str) -> list[dict]:
    response = await ctx.chain._rpc(
        ctx.chain._ensure_client().fetch_chain_trader_derivative_orders(
            market_id=ctx.info.market_id, subaccount_id=subaccount_id
        )
    )
    return response.get("orders", [])


async def sign_when_previous_is_final(sign: Callable[[], Awaitable[SignedTx]]) -> SignedTx:
    """The node can trail the tx we just saw land, so its sequence may be one behind."""
    for _ in range(IN_FLIGHT_RETRIES):
        try:
            return await sign()
        except InFlightTx:
            await asyncio.sleep(1)
    return await sign()


async def send_grantee_orders(
    ctx: Ctx, orders: list[OrderRequest], row: Row, cancels: tuple[RestingOrder, ...] = ()
) -> TxResult:
    tx = await sign_when_previous_is_final(lambda: ctx.chain.build_and_sign(ctx.trader, ctx.trader_sub, orders, cancels))
    row.tx_hashes.append(tx.tx_hash)
    return await send_and_wait(ctx.chain, tx)


async def send_grantee_messages(ctx: Ctx, messages: list[Any], row: Row) -> TxResult:
    tx = await sign_when_previous_is_final(lambda: ctx.chain.sign_exec_messages(messages))
    row.tx_hashes.append(tx.tx_hash)
    return await send_and_wait(ctx.chain, tx)


async def maker_order(ctx: Ctx, side: Side, price: Decimal, quantity: Decimal) -> TxResult:
    message = ctx.chain.limit_order_message(
        ctx.info,
        granter=ctx.maker,
        subaccount_id=ctx.maker_sub,
        side=side,
        price=price,
        quantity=quantity,
        margin=margin_for(quantity, price),
        cid="",
    )
    gas = GAS_ANTE + GAS_LIMIT_ORDER
    tx = await ctx.chain.sign_wallet_tx(private_key(ctx.keys["maker"]), [message], gas)
    return await send_and_wait(ctx.chain, tx)


async def maker_cancel_all(ctx: Ctx) -> None:
    for order in await resting_limit_orders(ctx, ctx.maker_sub):
        message = ctx.chain._composer.msg_cancel_derivative_order(
            market_id=ctx.info.market_id,
            sender=ctx.maker,
            subaccount_id=ctx.maker_sub,
            is_buy=bool(order.get("isBuy")),
            is_market_order=False,
            is_conditional=False,
            order_hash=order["orderHash"],
        )
        tx = await ctx.chain.sign_wallet_tx(private_key(ctx.keys["maker"]), [message], GAS_ANTE + GAS_CANCEL_ORDER)
        await send_and_wait(ctx.chain, tx)


async def trader_reset(ctx: Ctx, row: Row) -> None:
    """Back to flat with nothing resting. Maker bids must exist if a position is open."""
    for order in await resting_limit_orders(ctx, ctx.trader_sub):
        message = ctx.chain._composer.msg_cancel_derivative_order(
            market_id=ctx.info.market_id,
            sender=ctx.trader,
            subaccount_id=ctx.trader_sub,
            is_buy=bool(order.get("isBuy")),
            is_market_order=False,
            is_conditional=False,
            order_hash=order["orderHash"],
        )
        await send_grantee_messages(ctx, [message], row)

    account = await state(ctx)
    if account.orders:
        await send_grantee_orders(ctx, [], row, cancels=account.orders)

    account = await state(ctx)
    if account.position is not None:
        snapshot = await ctx.chain.snapshot()
        await maker_order(ctx, Side.LONG, snapshot.mark_price * Decimal("0.999"), account.position.quantity)
        await asyncio.sleep(SETTLE_S)
        await send_grantee_orders(ctx, [market_close(account.position.quantity, snapshot.mark_price, "reset-close")], row)
        await asyncio.sleep(SETTLE_S)


async def snapshot_for_case(ctx: Ctx) -> MarketSnapshot:
    await maker_cancel_all(ctx)
    return await ctx.chain.snapshot()


# Cases --------------------------------------------------------------------------------------

async def case_a(ctx: Ctx, row: Row) -> None:
    snapshot = await snapshot_for_case(ctx)
    mark = snapshot.mark_price
    quantity = lots(ctx, ORDER_LOTS, mark)
    await maker_order(ctx, Side.SHORT, mark * Decimal("1.001"), quantity * 2)
    await asyncio.sleep(SETTLE_S)

    result = await send_grantee_orders(ctx, [market_entry(quantity, mark, "a-entry"), stop_for_long(quantity, mark, "a-stop")], row)
    await asyncio.sleep(SETTLE_S)
    account = await state(ctx)
    row.detail = f"status={result.status.value} code={result.code} position={account.position is not None} orders={len(account.orders)}"
    row.outcome = "PASS" if result.status is TxStatus.FAILED and result.code == 81 and account.position is None else "FAIL"
    await maker_cancel_all(ctx)


async def case_b(ctx: Ctx, row: Row) -> None:
    snapshot = await snapshot_for_case(ctx)
    mark = snapshot.mark_price
    quantity = lots(ctx, ORDER_LOTS, mark)
    # Below any maker ask, so it rests.
    messages = [
        limit_entry_message(ctx, mark * Decimal("0.995"), quantity, "b-entry"),
        order_message(ctx, stop_for_long(quantity, mark, "b-stop", distance=Decimal("0.03"))),
    ]
    result = await send_grantee_messages(ctx, messages, row)
    await asyncio.sleep(SETTLE_S)
    limits = await resting_limit_orders(ctx, ctx.trader_sub)
    account = await state(ctx)
    row.detail = f"status={result.status.value} code={result.code} limit_orders={len(limits)} conditional_orders={len(account.orders)}"
    row.outcome = "PASS" if result.status is TxStatus.OK and len(limits) == 1 and len(account.orders) == 1 else "FAIL"
    await trader_reset(ctx, row)


async def case_c(ctx: Ctx, row: Row) -> None:
    snapshot = await snapshot_for_case(ctx)
    mark = snapshot.mark_price
    quantity = lots(ctx, ORDER_LOTS, mark)
    partial = lots(ctx, 1, mark)
    await maker_order(ctx, Side.SHORT, mark * Decimal("1.001"), partial)
    await asyncio.sleep(SETTLE_S)

    # Crosses the maker's ask for one lot, the rest rests.
    messages = [
        limit_entry_message(ctx, mark * Decimal("1.002"), quantity, "c-entry"),
        order_message(ctx, stop_for_long(quantity, mark, "c-stop", distance=Decimal("0.03"))),
    ]
    result = await send_grantee_messages(ctx, messages, row)
    await wait_blocks(ctx, 3)
    account = await state(ctx)
    limits = await resting_limit_orders(ctx, ctx.trader_sub)
    position_quantity = account.position.quantity if account.position else Decimal(0)
    stop_quantity = account.orders[0].quantity if account.orders else Decimal(0)
    row.detail = (
        f"status={result.status.value} code={result.code} ordered={quantity} position={position_quantity} "
        f"stop_quantity={stop_quantity} resting_limits={len(limits)}"
    )
    # This case records what the chain does, so a stop that survives is the pass condition.
    row.outcome = "PASS" if result.status is TxStatus.OK and position_quantity > 0 and account.orders else "FAIL"
    await trader_reset(ctx, row)
    await maker_cancel_all(ctx)


async def case_d(ctx: Ctx, row: Row) -> None:
    snapshot = await snapshot_for_case(ctx)
    mark = snapshot.mark_price
    quantity = lots(ctx, ORDER_LOTS, mark)
    expires = await height(ctx) + TX_TIMEOUT_BLOCKS + 5
    messages = [
        limit_entry_message(ctx, mark * Decimal("0.99"), quantity, "d-entry", expiration_block=expires),
        order_message(ctx, stop_for_long(quantity, mark, "d-stop", distance=Decimal("0.04"))),
    ]
    result = await send_grantee_messages(ctx, messages, row)
    await asyncio.sleep(SETTLE_S)
    before = await state(ctx)
    while await height(ctx) <= expires:
        await asyncio.sleep(1)
    await wait_blocks(ctx, 5)
    after = await state(ctx)
    limits = await resting_limit_orders(ctx, ctx.trader_sub)
    row.detail = (
        f"status={result.status.value} code={result.code} expires={expires} stops_before={len(before.orders)} "
        f"stops_after={len(after.orders)} limits_after={len(limits)}"
    )
    row.outcome = "PASS" if result.status is TxStatus.OK and len(before.orders) == 1 and not after.orders and not limits else "FAIL"
    await trader_reset(ctx, row)


async def case_e(ctx: Ctx, row: Row) -> None:
    snapshot = await snapshot_for_case(ctx)
    mark = snapshot.mark_price
    quantity = lots(ctx, 1, mark)
    await maker_order(ctx, Side.SHORT, mark * Decimal("1.001"), quantity)
    await asyncio.sleep(SETTLE_S)
    entry = await send_grantee_orders(ctx, [market_entry(quantity, mark, "e-entry")], row)
    await asyncio.sleep(SETTLE_S)
    account = await state(ctx)
    if entry.status is not TxStatus.OK or account.position is None:
        row.outcome = "FAIL"
        row.detail = f"could not open the position: code={entry.code}"
        return

    # No maker bids from here, so the triggered sell has only whatever the book already holds.
    await maker_cancel_all(ctx)
    current = await ctx.chain.snapshot()
    trigger_distance = ctx.info.price_tick * 3 / current.mark_price
    stop = await send_grantee_orders(ctx, [stop_for_long(quantity, current.mark_price, "e-stop", distance=trigger_distance)], row)
    if stop.status is not TxStatus.OK:
        row.outcome = "FAIL"
        row.detail = f"stop rejected: code={stop.code}"
        await trader_reset(ctx, row)
        return

    triggered = False
    for _ in range(120):
        await asyncio.sleep(1)
        account = await state(ctx)
        if not account.orders:
            triggered = True
            break
    if not triggered:
        row.outcome = "BLOCKED"
        row.detail = "mark never reached the trigger in 120 s; this market's oracle may be static"
    else:
        book = await ctx.chain.snapshot()
        still_open = account.position is not None
        row.detail = f"triggered=True position_still_open={still_open} best_bid={book.best_bid} mark={book.mark_price}"
        row.outcome = "PASS" if still_open else "FAIL"
    await trader_reset(ctx, row)


async def case_v1(ctx: Ctx, row: Row) -> None:
    snapshot = await snapshot_for_case(ctx)
    mark = snapshot.mark_price
    quantity = lots(ctx, ORDER_LOTS, mark)
    await maker_order(ctx, Side.SHORT, mark * Decimal("1.001"), quantity * 2)
    await maker_order(ctx, Side.LONG, mark * Decimal("0.999"), quantity * 2)
    await asyncio.sleep(SETTLE_S)

    entry = await send_grantee_orders(ctx, [market_entry(quantity, mark, "v1-entry")], row)
    await asyncio.sleep(SETTLE_S)
    account = await state(ctx)
    if entry.status is not TxStatus.OK or account.position is None:
        row.outcome = "FAIL"
        row.detail = f"entry failed: status={entry.status.value} code={entry.code}"
        await maker_cancel_all(ctx)
        return

    stop = await send_grantee_orders(ctx, [stop_for_long(account.position.quantity, mark, "v1-stop")], row)
    await asyncio.sleep(SETTLE_S)
    with_stop = await state(ctx)
    grant_covers_stop = stop.status is TxStatus.OK and len(with_stop.orders) == 1

    close = await send_grantee_orders(ctx, [market_close(account.position.quantity, mark, "v1-close")], row)
    await wait_blocks(ctx, 3)
    after = await state(ctx)
    row.detail = (
        f"entry={entry.status.value} position={account.position.quantity} stop_code={stop.code} "
        f"market_grant_covers_stop={grant_covers_stop} close_code={close.code} "
        f"flat={after.position is None} stops_after_close={len(after.orders)}"
    )
    row.outcome = "PASS" if grant_covers_stop and close.status is TxStatus.OK and after.position is None else "FAIL"
    await trader_reset(ctx, row)
    await maker_cancel_all(ctx)


CASES: dict[str, tuple[str, Callable[[Ctx, Row], Awaitable[None]]]] = {
    "a": ("market entry + stop in one tx rejected, code 81", case_a),
    "b": ("limit entry + stop in one tx both placed", case_b),
    "c": ("stop survives a partial fill (quantities recorded)", case_c),
    "d": ("chain removes stop when limit entry expires unfilled", case_d),
    "e": ("stop triggers on a thin book and fails, position stays", case_e),
    "v1": ("market entry, stop in separate tx under grant, close", case_v1),
}


# Dry run ------------------------------------------------------------------------------------

def sign_placeholder(ctx: Ctx, inner: list[Any], timeout_height: int) -> SignedTx:
    """Sign under the grant with placeholder account fields, without reading the account."""
    composer = ctx.chain._composer
    grantee = address_of(ctx.keys["grantee"])
    gas = gas_limit_for(inner)
    tx_bytes, tx_hash = sign_transaction(
        private_key(ctx.keys["grantee"]),
        [composer.msg_exec(grantee=grantee, msgs=inner)],
        account_number=0,
        sequence=0,
        chain_id=ctx.chain._network.chain_id,
        gas_limit=gas,
        fee_coin=composer.coin(amount=gas * 160_000_000, denom="inj"),
        timeout_height=timeout_height,
    )
    return SignedTx(tx_hash=tx_hash, tx_bytes=tx_bytes, signer=grantee, sequence=0, timeout_height=timeout_height)


async def dry_run_rows(ctx: Ctx) -> list[Row]:
    snapshot = await ctx.chain.snapshot()
    mark = snapshot.mark_price
    quantity = lots(ctx, ORDER_LOTS, mark)
    tip = snapshot.height + TX_TIMEOUT_BLOCKS
    entry = order_message(ctx, market_entry(quantity, mark, "a-entry"))
    plans = {
        "a": [entry, order_message(ctx, stop_for_long(quantity, mark, "a-stop"))],
        "b": [
            limit_entry_message(ctx, mark * Decimal("0.995"), quantity, "b-entry"),
            order_message(ctx, stop_for_long(quantity, mark, "b-stop", distance=Decimal("0.03"))),
        ],
        "c": [
            limit_entry_message(ctx, mark * Decimal("1.002"), quantity, "c-entry"),
            order_message(ctx, stop_for_long(quantity, mark, "c-stop", distance=Decimal("0.03"))),
        ],
        "d": [
            limit_entry_message(ctx, mark * Decimal("0.99"), quantity, "d-entry", expiration_block=tip + 5),
            order_message(ctx, stop_for_long(quantity, mark, "d-stop", distance=Decimal("0.04"))),
        ],
        "e": [order_message(ctx, stop_for_long(quantity, mark, "e-stop", distance=ctx.info.price_tick * 3 / mark))],
        "v1": [entry],
    }
    rows = []
    for name, inner in plans.items():
        row = Row(case=name, expected=CASES[name][0])
        tx = sign_placeholder(ctx, inner, tip)
        row.tx_hashes.append(tx.tx_hash)
        try:
            simulated = await ctx.chain.simulate(tx)
            row.outcome = "DRY-RUN"
            row.detail = f"msgs={len(inner)} bytes={len(tx.tx_bytes)} gas_limit={gas_limit_for(inner)} gas_used={simulated.get('gasInfo', {}).get('gasUsed')}"
        except grpc.aio.AioRpcError as exc:
            row.outcome = "DRY-RUN"
            reason = "".join(ch if ch.isascii() and ch.isprintable() else "?" for ch in (exc.details() or ""))
            row.detail = f"msgs={len(inner)} bytes={len(tx.tx_bytes)} gas_limit={gas_limit_for(inner)} simulate={exc.code().name}: {reason[:100]}"
        rows.append(row)
    return rows


def print_table(rows: list[Row]) -> None:
    print(f"\n{'case':5s} {'result':8s} expected / observed")
    print("-" * 78)
    for row in rows:
        print(f"{row.case:5s} {row.outcome:8s} {row.expected}")
        if row.detail:
            print(f"{'':14s} {row.detail}")
        for tx_hash in row.tx_hashes:
            print(f"{'':14s} tx {tx_hash}")


async def run(ticker: str, selected: list[str], dry_run: bool) -> int:
    keys = load_or_create_wallets()
    chain = make_testnet_chain(keys, ticker)
    try:
        await require_testnet_chain(chain)
        info = await chain.market()
        ctx = Ctx(chain=chain, keys=keys, info=info, dry_run=dry_run)
        print(f"market: {info.ticker} {info.market_id}")

        shortfalls = await funding_shortfalls(chain, keys, info.quote_denom, chain._quote_decimals)
        if shortfalls and not dry_run:
            print("BLOCKED, wallets are not funded:")
            for line in shortfalls:
                print(f"  {line}")
            print_table([Row(case=name, expected=CASES[name][0], outcome="BLOCKED", detail="wallets not funded") for name in selected])
            return 2

        if dry_run:
            print_table([row for row in await dry_run_rows(ctx) if row.case in selected])
            return 0

        problems = await grant_problems(chain, keys, info.market_id)
        if problems:
            print(f"BLOCKED, grant is not in place: {problems}. Run testnet_setup.py.")
            return 2

        rows = []
        for name in selected:
            expected, function = CASES[name]
            row = Row(case=name, expected=expected)
            try:
                await function(ctx, row)
            except Exception as exc:  # A failed case must not stop the ones after it.
                row.outcome = "ERROR"
                row.detail = type(exc).__name__
            rows.append(row)
            print(f"{name}: {row.outcome}", flush=True)
        print_table(rows)
        return 0 if all(row.outcome == "PASS" for row in rows) else 1
    finally:
        await chain.aclose()


def main() -> None:
    require_testnet()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--market-ticker", default=DEFAULT_TICKER)
    parser.add_argument("--cases", default=",".join(CASES))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    selected = [name.strip() for name in args.cases.split(",") if name.strip()]
    unknown = [name for name in selected if name not in CASES]
    if unknown:
        raise SystemExit(f"unknown cases: {unknown}")
    sys.exit(asyncio.run(run(args.market_ticker, selected, args.dry_run)))


if __name__ == "__main__":
    main()
