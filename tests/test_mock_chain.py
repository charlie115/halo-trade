"""The mock must enforce the Injective behaviour documented in chain/base.py."""
from decimal import Decimal as D

import pytest

from halo_trade.chain.base import CODE_DUPLICATE_CID, ChainUnavailable, OrderKind, OrderRequest, RestingOrder, Side, TxStatus
from halo_trade.chain.mock import (
    CODE_BANKRUPTCY,
    CODE_CONDITIONAL_EXISTS,
    CODE_INVALID_PRICE,
    CODE_INVALID_QUANTITY,
    CODE_MARKET_ORDER_EXISTS,
    CODE_MIN_NOTIONAL,
    CODE_NO_MARGIN_LOCKED,
    CODE_WORST_PRICE,
    Failure,
    MockChain,
    MockClock,
)

GRANTER, SUB = "inj1a", "0xa"


@pytest.fixture
def chain():
    c = MockChain(clock=MockClock(1_800_000_060_000), auto_mine=False)
    c.register_account(GRANTER, SUB, D("1000"))
    return c


def market(side=Side.LONG, qty="0.01", cid="m", reduce_only=False, margin="400", worst=None):
    worst = worst or (D("70000") if side is Side.LONG else D("50000"))
    return OrderRequest(OrderKind.MARKET, side, D(qty), D(worst), cid, reduce_only, margin=D(margin))


def conditional(kind, side, trigger, qty="0.01", cid="s"):
    return OrderRequest(kind, side, D(qty), D(trigger) * D("0.5") if side is Side.SHORT else D(trigger) * 2, cid, True, trigger_price=D(trigger))


async def apply(chain, orders, cancels=()):
    tx = await chain.build_and_sign(GRANTER, SUB, orders, cancels)
    await chain.broadcast(tx)
    chain.advance_block()
    return tx, await chain.lookup(tx)


async def open_long(chain, qty="0.01"):
    tx, result = await apply(chain, [market(qty=qty)])
    assert result.status is TxStatus.OK
    return tx


async def test_market_order_executes_at_end_of_block_not_when_placed(chain):
    tx = await chain.build_and_sign(GRANTER, SUB, [market()])
    await chain.broadcast(tx)
    assert (await chain.account(GRANTER, SUB)).position is None
    chain.advance_block()
    assert (await chain.account(GRANTER, SUB)).position is not None


async def test_market_entry_plus_reduce_only_stop_in_one_tx_fails_code_81_and_consumes_sequence(chain):
    sequence_before = chain._sequences[chain.signer]
    tx, result = await apply(chain, [market(), conditional(OrderKind.STOP, Side.SHORT, "59000")])
    assert result.status is TxStatus.FAILED
    assert result.code == CODE_NO_MARGIN_LOCKED
    assert chain._sequences[chain.signer] == sequence_before + 1
    assert (await chain.account(GRANTER, SUB)).position is None


async def test_reduce_only_conditional_requires_position_code_81(chain):
    _, result = await apply(chain, [conditional(OrderKind.STOP, Side.SHORT, "59000")])
    assert result.code == CODE_NO_MARGIN_LOCKED


async def test_reduce_only_conditional_allowed_after_entry_in_separate_tx(chain):
    await open_long(chain)
    _, result = await apply(chain, [conditional(OrderKind.STOP, Side.SHORT, "59000")])
    assert result.status is TxStatus.OK


async def test_one_conditional_per_trigger_direction(chain):
    await open_long(chain)
    await apply(chain, [conditional(OrderKind.STOP, Side.SHORT, "59000")])
    _, result = await apply(chain, [conditional(OrderKind.STOP, Side.SHORT, "58000", cid="s2")])
    assert result.status is TxStatus.FAILED
    assert result.code == CODE_CONDITIONAL_EXISTS


async def test_stop_and_take_profit_trigger_in_opposite_directions_coexist(chain):
    await open_long(chain)
    await apply(chain, [conditional(OrderKind.STOP, Side.SHORT, "59000")])
    _, result = await apply(chain, [conditional(OrderKind.TAKE, Side.SHORT, "61000", cid="t")])
    assert result.status is TxStatus.OK
    assert len(chain.sub(SUB).orders) == 2


@pytest.mark.parametrize(
    "kind,side,trigger",
    [
        (OrderKind.STOP, Side.SHORT, "60500"),  # STOP_SELL must be below mark
        (OrderKind.STOP, Side.LONG, "59500"),  # STOP_BUY must be above mark
        (OrderKind.TAKE, Side.SHORT, "59500"),  # TAKE_SELL must be above mark
        (OrderKind.TAKE, Side.LONG, "60500"),  # TAKE_BUY must be below mark
    ],
)
async def test_trigger_must_be_on_correct_side_of_mark(chain, kind, side, trigger):
    await apply(chain, [market(side=side.opposite, reduce_only=False)])
    _, result = await apply(chain, [conditional(kind, side, trigger)])
    assert result.status is TxStatus.FAILED


async def test_conditional_triggers_on_mark_at_end_of_block(chain):
    await open_long(chain)
    await apply(chain, [conditional(OrderKind.STOP, Side.SHORT, "59000")])
    chain.advance_block(mark=D("59500"))
    assert chain.sub(SUB).position is not None
    chain.advance_block(mark=D("58999"))
    assert chain.sub(SUB).position is None


async def test_triggered_conditional_can_fail_on_thin_book_and_position_remains(chain):
    await open_long(chain)
    await apply(chain, [conditional(OrderKind.STOP, Side.SHORT, "59000")])
    chain.set_book_depth(bid=D("1"))
    chain.advance_block(mark=D("58000"))
    position = chain.sub(SUB).position
    assert position is not None
    assert chain.sub(SUB).orders == []  # the order was consumed by its trigger
    assert chain.triggered[-1][1] is False


async def test_reduce_only_conditional_removed_when_position_gone(chain):
    await open_long(chain)
    await apply(chain, [conditional(OrderKind.STOP, Side.SHORT, "59000")])
    await apply(chain, [market(side=Side.SHORT, reduce_only=True, cid="c")])
    assert chain.sub(SUB).position is None
    assert chain.sub(SUB).orders == []


async def test_tx_is_atomic_across_messages(chain):
    await open_long(chain)
    await apply(chain, [conditional(OrderKind.STOP, Side.SHORT, "59000")])
    resting = chain.sub(SUB).orders[0]
    bad = conditional(OrderKind.STOP, Side.SHORT, "61000", cid="bad")
    _, result = await apply(chain, [bad], [resting])
    assert result.status is TxStatus.FAILED
    assert [o.cid for o in chain.sub(SUB).orders] == ["s"]  # cancel was reverted


async def test_cancel_of_missing_order_fails_tx(chain):
    ghost = RestingOrder("deadbeef", "c", OrderKind.STOP, Side.SHORT, D("1"), D("1"), D("1"), True)
    _, result = await apply(chain, [market()], [ghost])
    assert result.status is TxStatus.FAILED
    assert chain.sub(SUB).position is None


async def test_duplicate_cid_rejected_while_earlier_order_live(chain):
    await open_long(chain)
    await apply(chain, [conditional(OrderKind.STOP, Side.SHORT, "59000", cid="dup")])
    _, result = await apply(chain, [conditional(OrderKind.TAKE, Side.SHORT, "61000", cid="dup")])
    assert result.code == CODE_DUPLICATE_CID


async def test_duplicate_cid_allowed_after_earlier_order_is_gone(chain):
    await open_long(chain)
    await apply(chain, [conditional(OrderKind.STOP, Side.SHORT, "59000", cid="dup")])
    resting = chain.sub(SUB).orders[0]
    await apply(chain, [], [resting])
    _, result = await apply(chain, [conditional(OrderKind.TAKE, Side.SHORT, "61000", cid="dup")])
    assert result.status is TxStatus.OK


async def test_market_order_cid_lives_one_block(chain):
    tx1 = await chain.build_and_sign(GRANTER, SUB, [market(cid="same", qty="0.001")])
    await chain.broadcast(tx1)
    tx2 = await chain.build_and_sign(GRANTER, SUB, [market(cid="same", qty="0.002", margin="100")])
    # The second tx in the same block carries the next sequence.
    chain._signed[tx2.tx_hash].signed = tx2.__class__(tx2.tx_hash, tx2.tx_bytes, tx2.signer, tx2.sequence + 1, tx2.timeout_height)
    await chain.broadcast(chain._signed[tx2.tx_hash].signed)
    chain.advance_block()
    assert (await chain.lookup(tx1)).status is TxStatus.OK
    second = await chain.lookup(chain._signed[tx2.tx_hash].signed)
    assert second.code == CODE_DUPLICATE_CID
    # A later block accepts the same cid.
    _, result = await apply(chain, [market(cid="same", qty="0.001", margin="100")])
    assert result.status is TxStatus.OK


async def test_tx_not_included_by_timeout_never_included(chain):
    chain.inject(Failure.NEVER_INCLUDED)
    tx = await chain.build_and_sign(GRANTER, SUB, [market()])
    await chain.broadcast(tx)
    assert (await chain.lookup(tx)).status is TxStatus.PENDING
    chain.advance_block(blocks=chain.tx_timeout_blocks + 1)
    assert (await chain.lookup(tx)).status is TxStatus.EXPIRED
    assert chain.sub(SUB).position is None


async def test_tx_delivered_after_timeout_is_dropped(chain):
    tx = await chain.build_and_sign(GRANTER, SUB, [market()])
    chain.height += chain.tx_timeout_blocks + 1
    await chain.broadcast(tx)
    chain.advance_block()
    assert (await chain.lookup(tx)).status is TxStatus.EXPIRED


async def test_broadcast_is_idempotent_for_same_bytes(chain):
    tx = await chain.build_and_sign(GRANTER, SUB, [market()])
    await chain.broadcast(tx)
    await chain.broadcast(tx)
    chain.advance_block()
    assert (await chain.lookup(tx)).status is TxStatus.OK
    assert chain.sub(SUB).position.quantity == D("0.01")


async def test_broadcast_after_inclusion_does_not_execute_twice(chain):
    tx = await chain.build_and_sign(GRANTER, SUB, [market()])
    await chain.broadcast(tx)
    chain.advance_block()
    await chain.broadcast(tx)
    chain.advance_block()
    assert chain.sub(SUB).position.quantity == D("0.01")


async def test_transport_error_after_inclusion_still_executes(chain):
    chain.inject(Failure.TRANSPORT_AFTER_INCLUSION)
    tx = await chain.build_and_sign(GRANTER, SUB, [market()])
    with pytest.raises(ChainUnavailable):
        await chain.broadcast(tx)
    chain.advance_block()
    assert (await chain.lookup(tx)).status is TxStatus.OK


async def test_transport_error_before_inclusion_does_not_execute(chain):
    chain.inject(Failure.TRANSPORT_BEFORE_INCLUSION)
    tx = await chain.build_and_sign(GRANTER, SUB, [market()])
    with pytest.raises(ChainUnavailable):
        await chain.broadcast(tx)
    chain.advance_block()
    assert (await chain.lookup(tx)).status is TxStatus.PENDING
    assert chain.sub(SUB).position is None


async def test_sequence_mismatch_never_included_and_sequence_not_consumed(chain):
    chain.inject(Failure.SEQUENCE_MISMATCH)
    sequence = chain._sequences[chain.signer]
    tx = await chain.build_and_sign(GRANTER, SUB, [market()])
    assert tx.sequence == sequence - 1
    await chain.broadcast(tx)
    chain.advance_block()
    assert (await chain.lookup(tx)).status is TxStatus.PENDING
    assert chain._sequences[chain.signer] == sequence
    chain.advance_block(blocks=chain.tx_timeout_blocks + 1)
    assert (await chain.lookup(tx)).status is TxStatus.EXPIRED


async def test_stop_placement_injection_fails_tx_but_consumes_sequence(chain):
    await open_long(chain)
    chain.inject(Failure.STOP_PLACEMENT)
    sequence = chain._sequences[chain.signer]
    _, result = await apply(chain, [conditional(OrderKind.STOP, Side.SHORT, "59000")])
    assert result.status is TxStatus.FAILED
    assert chain._sequences[chain.signer] == sequence + 1
    assert chain.sub(SUB).orders == []


async def test_failing_tx_consumes_sequence_on_message_error(chain):
    sequence = chain._sequences[chain.signer]
    await apply(chain, [conditional(OrderKind.STOP, Side.SHORT, "59000")])
    assert chain._sequences[chain.signer] == sequence + 1


async def test_market_order_that_does_not_cross_fails_code_25(chain):
    _, result = await apply(chain, [market(worst="59000")])  # ask is around 60006
    assert (result.status, result.code) == (TxStatus.FAILED, CODE_WORST_PRICE)
    assert chain.sub(SUB).position is None


async def test_market_order_on_an_empty_side_fails_code_25(chain):
    chain.set_book_depth(ask=D("0"))
    _, result = await apply(chain, [market()])
    assert (result.status, result.code) == (TxStatus.FAILED, CODE_WORST_PRICE)
    snapshot = await chain.snapshot()
    assert snapshot.best_ask == 0 and snapshot.best_bid > 0


async def test_second_market_order_in_a_block_fails_code_38(chain):
    first = await chain.build_and_sign(GRANTER, SUB, [market(cid="one", qty="0.001", margin="30")])
    await chain.broadcast(first)
    second = await chain.build_and_sign(GRANTER, SUB, [market(cid="two", qty="0.001", margin="30")])
    resigned = second.__class__(second.tx_hash, second.tx_bytes, second.signer, second.sequence + 1, second.timeout_height)
    chain._signed[second.tx_hash].signed = resigned
    await chain.broadcast(resigned)
    chain.advance_block()
    assert (await chain.lookup(first)).status is TxStatus.OK
    assert (await chain.lookup(resigned)).code == CODE_MARKET_ORDER_EXISTS
    assert chain.sub(SUB).position.quantity == D("0.001")


async def test_failed_tx_does_not_use_up_the_blocks_market_order_slot(chain):
    bad = await chain.build_and_sign(GRANTER, SUB, [market(cid="bad", worst="59000")])
    await chain.broadcast(bad)
    good = await chain.build_and_sign(GRANTER, SUB, [market(cid="good", qty="0.001", margin="30")])
    resigned = good.__class__(good.tx_hash, good.tx_bytes, good.signer, good.sequence + 1, good.timeout_height)
    chain._signed[good.tx_hash].signed = resigned
    await chain.broadcast(resigned)
    chain.advance_block()
    assert (await chain.lookup(bad)).code == CODE_WORST_PRICE
    assert (await chain.lookup(resigned)).status is TxStatus.OK


@pytest.mark.parametrize(
    "order,code",
    [
        (market(qty="0.00001", worst="70000"), CODE_INVALID_QUANTITY),
        (market(worst="70000.001"), CODE_INVALID_PRICE),
        (market(qty="0.0001", worst="40000"), CODE_MIN_NOTIONAL),  # 4 USD at worst price
        (conditional(OrderKind.STOP, Side.SHORT, "59000.001"), CODE_INVALID_PRICE),
    ],
)
async def test_ticks_and_min_notional_are_enforced_at_worst_price(chain, order, code):
    if order.kind is OrderKind.STOP:
        await open_long(chain)
    _, result = await apply(chain, [order])
    assert (result.status, result.code) == (TxStatus.FAILED, code)


async def test_stop_below_min_notional_at_worst_price_fails(chain):
    await open_long(chain)
    tiny = OrderRequest(OrderKind.STOP, Side.SHORT, D("0.0001"), D("40000"), "s", True, trigger_price=D("59000"))
    _, result = await apply(chain, [tiny])
    assert (result.status, result.code) == (TxStatus.FAILED, CODE_MIN_NOTIONAL)


async def test_reduce_only_close_past_bankruptcy_fails_code_30(chain):
    await open_long(chain)
    chain.sub(SUB).position.margin = D("10")  # 1000 per unit: bankrupt below about 59006
    _, result = await apply(chain, [market(side=Side.SHORT, reduce_only=True, cid="c", worst="58000")])
    assert (result.status, result.code) == (TxStatus.FAILED, CODE_BANKRUPTCY)
    assert chain.sub(SUB).position is not None


async def test_triggered_stop_fills_what_the_book_holds_and_position_remains(chain):
    await open_long(chain, qty="0.01")
    await apply(chain, [conditional(OrderKind.STOP, Side.SHORT, "59000")])
    chain.set_book_depth(bid=D("290"))  # 290 USD at 58994 buys 0.0049 of the 0.01
    chain.advance_block(mark=D("59000"))
    assert chain.sub(SUB).position.quantity == D("0.0051")
    assert chain.triggered[-1][1] is True
    assert chain.sub(SUB).orders == []  # the order is consumed by its trigger


async def test_triggered_stop_cannot_fill_past_bankruptcy(chain):
    await open_long(chain)
    await apply(chain, [conditional(OrderKind.STOP, Side.SHORT, "59000")])
    chain.sub(SUB).position.margin = D("0.01")
    chain.advance_block(mark=D("59000"))
    assert chain.sub(SUB).position is not None
    assert chain.triggered[-1][1] is False


async def test_check_tx_fee_failure_never_lands_and_consumes_no_sequence(chain):
    chain.inject(Failure.CHECK_TX_FEE)
    sequence = chain._sequences[chain.signer]
    tx = await chain.build_and_sign(GRANTER, SUB, [market()])
    await chain.broadcast(tx)  # CheckTx rejections are not raised, by design
    chain.advance_block()
    assert (await chain.lookup(tx)).status is TxStatus.PENDING
    assert chain._sequences[chain.signer] == sequence
    assert chain.sub(SUB).position is None


async def test_unpaid_grantee_cannot_get_a_tx_past_check_tx(chain):
    chain.set_grantee_balance(D("0"))
    tx = await chain.build_and_sign(GRANTER, SUB, [market()])
    await chain.broadcast(tx)
    chain.advance_block()
    assert (await chain.lookup(tx)).status is TxStatus.PENDING
    assert await chain.grantee_balance_inj() == 0


async def test_delivered_tx_spends_grantee_gas(chain):
    before = chain.grantee_inj
    await open_long(chain)
    assert chain.grantee_inj < before


async def test_lagging_read_reports_the_old_height_and_state(chain):
    await open_long(chain)
    chain.advance_block(blocks=3)
    chain.set_read_lag(3)
    account = await chain.account(GRANTER, SUB)
    assert account.height == chain.height - 3
    assert account.position is not None
    chain.set_read_lag(0)
    assert (await chain.account(GRANTER, SUB)).height == chain.height


async def test_lagging_read_can_show_a_position_already_gone_or_not_yet_there(chain):
    chain.advance_block()  # gives the lagging node a block to answer from
    tx = await chain.build_and_sign(GRANTER, SUB, [market()])
    await chain.broadcast(tx)
    chain.advance_block()
    chain.set_read_lag(1)
    behind = await chain.account(GRANTER, SUB)
    assert behind.position is None  # the block with the fill has not reached this node
    chain.set_read_lag(0)
    assert (await chain.account(GRANTER, SUB)).position is not None


async def test_market_as_of_is_settable_independently(chain):
    chain.set_market_as_of(123)
    assert (await chain.snapshot()).as_of_ms == 123
    chain.set_market_as_of(None)
    assert (await chain.snapshot()).as_of_ms > 123


async def test_account_reports_the_mark(chain):
    assert (await chain.account(GRANTER, SUB)).mark_price == chain.mark


async def test_build_and_sign_does_not_broadcast(chain):
    await chain.build_and_sign(GRANTER, SUB, [market()])
    assert chain.broadcast_log == []
    assert chain.sub(SUB).position is None


async def test_timeout_height_is_read_fresh_per_tx(chain):
    tx1 = await chain.build_and_sign(GRANTER, SUB, [market()])
    chain.advance_block(blocks=3)
    tx2 = await chain.build_and_sign(GRANTER, SUB, [market()])
    assert tx2.timeout_height == tx1.timeout_height + 3


async def test_go_down_raises_chain_unavailable(chain):
    chain.go_down()
    with pytest.raises(ChainUnavailable):
        await chain.snapshot()


async def test_fail_calls_skip_then_fail(chain):
    chain.fail_calls("snapshot", count=1, skip=1)
    await chain.snapshot()
    with pytest.raises(ChainUnavailable):
        await chain.snapshot()
    await chain.snapshot()


async def test_partial_fill_injection_reduces_quantity(chain):
    chain.inject(Failure.PARTIAL_FILL, ratio=D("0.5"))
    await open_long(chain, qty="0.01")
    assert chain.sub(SUB).position.quantity == D("0.005")


async def test_equity_includes_unrealized_and_fees(chain):
    await open_long(chain, qty="0.01")
    account = await chain.account(GRANTER, SUB)
    assert account.equity < D("1000")  # fee paid, mark below ask
    chain.advance_block(mark=D("61000"))
    assert (await chain.account(GRANTER, SUB)).position.unrealized_pnl > 0


async def test_thin_book_partial_fill_on_market_order(chain):
    chain.set_book_depth(ask=D("300"))
    await open_long(chain, qty="0.01")
    # 300 USD at the ask 60006 only buys 0.0049 of the requested 0.01.
    assert chain.sub(SUB).position.quantity == D("0.0049")


async def test_clock_advances_with_blocks():
    clock = MockClock(1_000)
    chain = MockChain(clock=clock, block_ms=500)
    chain.advance_block(blocks=4)
    assert clock() == 3_000


async def test_candles_include_current_hour_partial(chain):
    candles = await chain.candles_1h(3)
    assert len(candles) == 3
    assert candles[-1].open_ms >= 1_800_000_060_000 - 3_600_000
