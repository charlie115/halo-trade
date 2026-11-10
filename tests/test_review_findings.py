"""Proofs for the 2026-11-09 code review (kept outside this repository), numbered as it numbered them (p01...).

Each asserts the correct behaviour, so it fails if the fix is removed. Where one fix changed
what the original proof could observe (two flat reads at increasing heights, the new DRIFT_MAX,
worst-loss admission, an empty book being refused up front), the scenario is adjusted to reach the
same dangerous state, and says so.

Not ported: p23 (one user's stuck tx starving another's close) is the accepted cost of the single
grantee key the findings keep for the MVP. test_sizing_cross_check_rounding_invariants backed no
finding and the rounding rules it checks are unchanged.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import grpc
import pytest

from halo_trade import limits
from halo_trade.chain.base import ChainUnavailable, Candle, OrderKind, OrderRequest, RestingOrder, Side, TxStatus
from halo_trade.chain.injective import _HeightCookies, _response_heights, InjectiveChain
from halo_trade.chain.mock import Failure, SimulatedCrash
from halo_trade.config import Risk, Settings
from halo_trade.context import AppContext
from halo_trade.contracts import ErrorCode as E, Outcome, PreviewIn
from halo_trade.db import connect
from halo_trade.limits import ApiError
from halo_trade.service import Service, live_stop
from halo_trade.txmachine import GRANT_BAD_WINDOW_MS, RETRY_WINDOW_MS, SignerBusy, TxMachine, make_cid
from halo_trade.watcher import SNAPSHOT_INTERVAL_MS, Watcher

from .test_injective_unit import GRANTER, SUBACCOUNT, FakeClient, load_script, make, market_order, stop_order
from .test_service_entry import empty_bids_after_entry, rejected_with


def manual(env):
    env.chain.auto_mine = False


def auto(env):
    env.chain.auto_mine = True


def use_risk(env, **changes):
    env.ctx.settings = replace(env.ctx.settings, risk=replace(env.ctx.settings.risk, **changes))


async def enter_protected(env, tp=None, deposits="100"):
    user, _ = env.add_user(deposits)
    out = await env.enter(user, tp=tp)
    assert out.outcome is Outcome.FILLED
    return user


async def unresolved_entry(env, user):
    """An entry submitted and not yet mined, with a short budget so the test stays fast."""
    manual(env)
    env.chain.advance_block(blocks=3)  # gives a lagging node something older to answer from
    env.svc.submit_budget_s = 0.5
    preview = await env.preview(user)
    await env.submit_ticket(user, preview.ticket)
    return env.intent(user)["id"]


async def step(env, intent_id):
    return await env.svc.advance(intent_id, time.monotonic() + 0.3)


# ---------------------------------------------------------------- H2 exceptions in the stop path


async def test_p01_non_chain_exception_placing_stop_must_still_end_protected_or_flat(env):
    user, _ = env.add_user()
    real_build = env.chain.build_and_sign

    async def build(granter, sub, orders, cancels=()):
        if any(o.kind is OrderKind.STOP for o in orders):
            raise ValueError("stop and take orders need a positive trigger price")
        return await real_build(granter, sub, orders, cancels)

    env.chain.build_and_sign = build  # type: ignore[method-assign]
    preview = await env.preview(user)
    try:
        await env.submit_ticket(user, preview.ticket)
    except Exception:  # noqa: BLE001
        pass
    await env.tick(5)
    assert env.position(user) is None or env.stop_orders(user), "live position, no stop, no flatten"
    assert env.alerts("stop_missing"), "the unexpected error must raise an alert"


async def test_p01_flatten_after_stop_errors_is_verified_and_reported(env):
    user, _ = env.add_user()
    real_build = env.chain.build_and_sign

    async def build(granter, sub, orders, cancels=()):
        if any(o.kind is OrderKind.STOP for o in orders):
            raise KeyError("response field missing")
        return await real_build(granter, sub, orders, cancels)

    env.chain.build_and_sign = build  # type: ignore[method-assign]
    out = await env.enter(user)
    assert out.code is E.STOP_FAILED_FLATTENED
    assert env.intent(user)["stage"] == "flattened"
    assert env.txs(purpose="stop") == []


async def test_p01_transport_failure_placing_stop_does_not_count_as_stop_not_placed(env):
    user, _ = env.add_user()
    env.chain.fail_calls("build_and_sign", count=1000, skip=1)  # entry signs, stop cannot
    env.svc.submit_budget_s = 0.2
    await env.enter(user)
    assert env.position(user) is not None
    assert env.txs(purpose="flatten") == []  # a chain outage is waited out, not flattened
    assert env.intent(user)["stage"] == "entry_filled"


async def test_p01b_preview_rejects_a_non_positive_stop(env):
    user, _ = env.add_user("400")
    env.chain.candle_range = D("20000")  # ATR 20000 on a 60000 mark (a bad print / thin market)
    state = await env.svc.state(user)
    await rejected_with(
        env.svc.preview(user, PreviewIn(observation_id=state.observation_id, side="long", stop_atr=3.0)),
        E.INVALID_INTENT,
    )


async def test_p01c_real_backend_refuses_a_zero_trigger_as_nothing_signed():
    backend = make(FakeClient())
    with pytest.raises(ChainUnavailable):
        await backend.build_and_sign(GRANTER, SUBACCOUNT, [stop_order(trigger_price=D("0"))])
    assert backend._last_signed is None


# ---------------------------------------------------------------- H1 / C3 hung RPCs


async def test_p02_hung_gas_price_rpc_must_not_hold_the_signer_past_deadline(tmp_path: Path):
    client = FakeClient()
    never = asyncio.Event()

    async def hang():
        await never.wait()

    client.current_chain_gas_price = hang  # type: ignore[method-assign]
    chain = make(client)
    db_path = str(tmp_path / "h.sqlite")
    ctx = AppContext(settings=Settings(db_path=db_path), db=connect(db_path), chain=chain)
    ctx.db.execute(
        "INSERT INTO users (inj_address, eth_address, subaccount_id, status, token_hash, created_ms) VALUES (?,?,?,?,?,?)",
        (GRANTER, "0xe", SUBACCOUNT, "active", "h", 1),
    )
    machine = TxMachine(ctx, poll_interval_s=0.01)
    task = asyncio.create_task(
        machine.send(
            user_id=1, intent_id=None, purpose="close", granter=GRANTER, subaccount_id=SUBACCOUNT,
            orders=[market_order(reduce_only=True, margin=D(0))], deadline=time.monotonic() + 0.2,
        )
    )
    await asyncio.sleep(1.0)
    done, busy = task.done(), machine.slot_busy
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert done and not busy


async def test_h1_real_backend_times_out_the_gas_price_call():
    client = FakeClient()
    never = asyncio.Event()

    async def hang():
        await never.wait()

    client.current_chain_gas_price = hang  # type: ignore[method-assign]
    backend = make(client)
    backend._rpc_timeout_s = 0.05
    with pytest.raises(ChainUnavailable):
        await backend.build_and_sign(GRANTER, SUBACCOUNT, [market_order()])
    assert not backend._sign_lock.locked()


async def test_h1_real_backend_times_out_the_grant_query():
    client = FakeClient()
    never = asyncio.Event()

    async def hang(request, metadata=None):
        await never.wait()

    client.stubAuthz.Grants = hang  # type: ignore[method-assign]
    backend = make(client)
    backend._rpc_timeout_s = 0.05
    with pytest.raises(ChainUnavailable):
        await backend.check_grant(GRANTER, SUBACCOUNT)


async def test_p02b_hung_grant_query_must_not_stall_the_watcher(tmp_path: Path):
    client = FakeClient()
    never = asyncio.Event()

    async def hang(request, metadata=None):
        await never.wait()

    client.stubAuthz.Grants = hang  # type: ignore[method-assign]
    chain = make(client)
    db_path = str(tmp_path / "h.sqlite")
    ctx = AppContext(settings=Settings(db_path=db_path), db=connect(db_path), chain=chain)
    ctx.db.execute(
        "INSERT INTO users (inj_address, eth_address, subaccount_id, status, token_hash, created_ms) VALUES (?,?,?,?,?,?)",
        (GRANTER, "0xe", SUBACCOUNT, "active", "a", 1),
    )
    service = Service(ctx, submit_budget_s=0.5, poll_interval_s=0.01)
    watcher = Watcher(service, user_budget_s=0.2)
    task = asyncio.create_task(watcher.tick())
    await asyncio.sleep(1.5)
    done = task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert done


async def test_p02c_daily_loss_must_not_vanish_when_snapshots_stop(env):
    """No watcher snapshot after UTC midnight: reads must not re-base day start to current equity."""
    user, _ = env.add_user("100")
    yesterday = env.clock() // 86_400_000 - 1
    env.set_risk(user, "100", "100", day_id=yesterday)
    first = env.svc._risk_view(user.id, D("100"), env.clock())
    after_loss = env.svc._risk_view(user.id, D("90"), env.clock())
    assert after_loss.day_start_equity == first.day_start_equity == D("100")


async def test_h1_preview_after_midnight_still_sees_yesterdays_loss_until_a_snapshot_rolls(env):
    user, _ = env.add_user("100")
    env.set_risk(user, "120", "120", day_id=env.clock() // 86_400_000 - 1)  # 16% down, not rolled yet
    state = await env.state(user)
    assert E.DAILY_LOSS_LIMIT in state.blocked_reasons


# ---------------------------------------------------------------- M4 unbounded retries


async def test_p03_revoked_grant_must_not_spin_signing_flattens(env):
    user = await enter_protected(env)
    env.chain.set_grant(user.subaccount_id, False)
    env.chain.sub(user.subaccount_id).orders.clear()
    env.watcher.user_budget_s = 0.1
    manual(env)
    await env.tick()
    env.chain.advance_block(blocks=env.ctx.settings.stop_grace_blocks + 1)
    auto(env)
    before = len(env.chain.signed_log)
    await env.tick(3)
    assert len(env.chain.signed_log) - before <= 6, "no backoff or attempt cap while the grant is gone"


async def test_p03b_failing_close_is_bounded_across_requests(env):
    """/v1/close has no rate limit, so a repeat call must not re-arm the attempt counter."""
    user = await enter_protected(env)
    env.chain.set_grant(user.subaccount_id, False)
    before = len(env.chain.signed_log)
    for _ in range(10):
        await env.close(user)
    assert len(env.chain.signed_log) - before <= 3


async def test_m4_repeat_close_after_the_window_gets_new_attempts(env):
    user = await enter_protected(env)
    env.chain.set_book_depth(bid=D("0"))
    await env.close(user)
    assert len(env.txs(purpose="close")) == 3
    env.clock.advance(60_000)
    await env.close(user)
    assert len(env.txs(purpose="close")) == 6


async def test_m4_retry_budget_applies_to_the_watcher_but_never_blocks_a_users_close(env):
    user = await enter_protected(env)
    env.chain.set_book_depth(bid=D("0"))  # protective txs cannot fill
    env.svc.submit_budget_s = 0.2
    for _ in range(8):
        await env.svc.force_close(user, env.svc.open_intent(user.id), time.monotonic() + 0.05)
    forced = len(env.txs(purpose="close"))
    assert forced <= 6
    env.chain.set_book_depth(bid=D("1000000"))
    out = await env.close(user)  # the retry budget is spent, the user's own close still goes out
    assert out.outcome is Outcome.CLOSED
    assert env.position(user) is None


async def test_m4_bad_grant_slows_retries_further_and_recovers_when_granted_again(env):
    user = await enter_protected(env)
    env.chain.set_grant(user.subaccount_id, False)
    await env.tick()  # the watcher notices the bad grant
    assert env.svc._kv_get(f"grant_bad:{user.id}") is not None
    env.chain.sub(user.subaccount_id).orders.clear()
    for _ in range(3):
        with pytest.raises(SignerBusy):
            await env.svc.tx.send(
                user_id=user.id, intent_id=None, purpose="stop", granter=user.inj_address, subaccount_id=user.subaccount_id,
                orders=[], deadline=time.monotonic() + 0.01,
            )
    env.chain.set_grant(user.subaccount_id, True)
    env.clock.advance(GRANT_BAD_WINDOW_MS)
    await env.tick()
    assert env.svc._kv_get(f"grant_bad:{user.id}") is None


async def test_m4_retry_window_slides(env):
    user = await enter_protected(env)
    machine = env.svc.tx
    env.ctx.db.execute("UPDATE txs SET created_ms=? WHERE user_id=?", (env.clock() - RETRY_WINDOW_MS - 1, user.id))
    assert machine._retry_budget_spent(user.id) is False
    for index in range(6):
        order = OrderRequest(OrderKind.STOP, Side.SHORT, D("0.001"), D("57000"), f"s{index}", True, trigger_price=D("59000"))
        signed = await env.chain.build_and_sign(user.inj_address, user.subaccount_id, [order])
        await machine.persist(user_id=user.id, intent_id=None, purpose="stop", signed=signed)
        await machine._update_status(machine.get(machine.open_txs()[-1].id), "failed")
    assert machine._retry_budget_spent(user.id) is True
    env.clock.advance(RETRY_WINDOW_MS)
    assert machine._retry_budget_spent(user.id) is False


# ---------------------------------------------------------------- M1 M2 M3 risk state


async def test_p04_withdraw_all_then_redeposit_must_not_clear_tripped_limits(env):
    user, _ = env.add_user("100")
    await env.tick()
    env.set_risk(user, "120", "200")  # day loss 16.7%, drawdown 50%: both tripped
    state = await env.state(user)
    assert E.DAILY_LOSS_LIMIT in state.blocked_reasons and E.DRAWDOWN_LIMIT in state.blocked_reasons
    env.chain.withdraw(user.subaccount_id, D("100"))
    env.clock.advance(SNAPSHOT_INTERVAL_MS)
    await env.tick()
    env.chain.deposit(user.subaccount_id, D("100"))
    env.clock.advance(SNAPSHOT_INTERVAL_MS)
    await env.tick()
    state = await env.state(user)
    assert E.DAILY_LOSS_LIMIT in state.blocked_reasons and E.DRAWDOWN_LIMIT in state.blocked_reasons


@pytest.mark.parametrize("how", ["request_holds_user_lock", "account_read_flake"])
async def test_p05_unfinalized_realized_loss_must_not_be_rebased_as_transfer(env, how):
    user, _ = env.add_user("100")
    await env.tick()  # flat snapshot at equity 100
    await env.enter(user)
    env.chain.advance_block(mark=D("59000"))  # stop hit: ~1.6% realized loss, trade not yet finalized
    env.clock.advance(SNAPSHOT_INTERVAL_MS)
    if how == "request_holds_user_lock":
        async with env.svc.user_lock(user.id):
            await env.tick()
    else:
        env.chain.fail_calls("account", count=1)  # the old proof failed snapshot(); account is now the read
        await env.tick()
    await env.settle()  # the trade is finalized now
    risk = env.risk(user)
    assert env.trades(user)[0]["close_reason"] == "stop"
    assert D(risk["day_start_equity"]) == D("100")
    assert D(risk["peak_equity"]) == D("100")


async def test_p24_withdrawal_while_in_position_must_not_wedge_the_user(env):
    user = await enter_protected(env, deposits="100")
    await env.tick()  # non-flat snapshot
    env.chain.withdraw(user.subaccount_id, D("40"))  # takes money out while holding
    env.clock.advance(SNAPSHOT_INTERVAL_MS)
    await env.tick()
    await env.close(user)
    env.clock.advance(SNAPSHOT_INTERVAL_MS)
    await env.tick()
    # Next UTC day, flat, and the user even puts the money back.
    env.clock.advance(86_400_000)
    env.chain.deposit(user.subaccount_id, D("40"))
    await env.tick()
    env.clock.advance(SNAPSHOT_INTERVAL_MS)
    await env.tick()
    state = await env.state(user)
    assert E.DRAWDOWN_LIMIT not in state.blocked_reasons


async def test_m3_deposit_in_position_does_not_mask_the_days_loss(env):
    user = await enter_protected(env, deposits="100")
    await env.tick()
    env.chain.advance_block(mark=D("59450"))  # about -1.4 unrealized, inside the stop
    env.chain.deposit(user.subaccount_id, D("100"))
    env.clock.advance(SNAPSHOT_INTERVAL_MS)
    await env.tick()
    state = await env.state(user)
    # Without the rebase the equity jump would read as a +99% day.
    assert state.account.day_pnl_pct < 0
    assert D(env.risk(user)["day_start_equity"]) > D("190")


async def test_m3_position_pnl_is_never_a_transfer(env):
    user = await enter_protected(env, deposits="100")
    await env.tick()
    env.chain.advance_block(mark=D("60400"))
    env.clock.advance(SNAPSHOT_INTERVAL_MS)
    await env.tick()
    assert D(env.risk(user)["day_start_equity"]) == D("100")


async def test_m3_partial_close_in_position_is_not_a_transfer(env):
    user = await enter_protected(env, deposits="100")
    await env.tick()
    sub = env.chain.sub(user.subaccount_id)
    sub.position.quantity = D("0.0010")  # a partial stop fill realized a loss
    sub.deposits -= D("3")
    env.clock.advance(SNAPSHOT_INTERVAL_MS)
    await env.tick()
    assert D(env.risk(user)["day_start_equity"]) == D("100")


async def test_m2_withdraw_all_rebases_nothing_while_at_zero(env):
    user, _ = env.add_user("100")
    await env.tick()
    env.set_risk(user, "120", "200")
    env.chain.withdraw(user.subaccount_id, D("100"))
    env.clock.advance(SNAPSHOT_INTERVAL_MS)
    await env.tick()
    assert (D(env.risk(user)["day_start_equity"]), D(env.risk(user)["peak_equity"])) == (D("120"), D("200"))


async def test_db_migration_adds_the_balance_column_to_an_old_file(tmp_path):
    path = str(tmp_path / "old.sqlite")
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE equity_snapshots (user_id INTEGER NOT NULL, ts_ms INTEGER NOT NULL, equity TEXT NOT NULL, flat INTEGER NOT NULL, PRIMARY KEY (user_id, ts_ms));"
        "CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
    )
    old.close()
    conn = connect(path)
    assert "balance" in {row["name"] for row in conn.execute("PRAGMA table_info(equity_snapshots)")}
    connect(path)  # and it can run twice


# ---------------------------------------------------------------- L1 cooldown


async def test_p07_stop_out_after_top_up_must_still_cool_down(env):
    user = await enter_protected(env)
    env.chain.deposit(user.subaccount_id, D("10"))  # user tops up while the trade is under water
    env.chain.advance_block(mark=D("59000"))
    await env.settle()  # two flat reads at increasing heights, see M7
    trade = env.trades(user)[0]
    assert trade["close_reason"] == "stop"
    assert env.risk(user)["cooldown_until_window"] > env.window()


async def test_l1_take_profit_never_cools_down(env):
    user = await enter_protected(env, tp=3.0)
    env.chain.advance_block(mark=D("61000"))
    await env.settle()
    assert env.trades(user)[0]["close_reason"] == "take_profit"
    assert env.risk(user)["cooldown_until_window"] == 0


async def test_l1_user_close_at_a_loss_cools_down(env):
    user = await enter_protected(env)
    await env.close(user)
    assert env.trades(user)[0]["close_reason"] == "close"
    assert D(env.trades(user)[0]["pnl_usd"]) < 0
    assert env.risk(user)["cooldown_until_window"] > env.window()


# ---------------------------------------------------------------- H5 loss beyond the preview


def wide_stop(env):
    """2.25% of mark with 1.5 ATR, wide enough to survive the collar and the 1.5x growth rule."""
    env.chain.candle_range = D("900")


async def test_h5_default_drift_max_is_half_the_min_stop():
    assert Risk().drift_max == D("0.0025") == Risk().min_stop_pct / 2


async def test_p08_submit_at_the_old_drift_is_now_stale(env):
    """The original proof filled at +0.48% drift. The new DRIFT_MAX refuses that entry outright."""
    user, _ = env.add_user("100")
    wide_stop(env)
    preview = await env.preview(user, stop_atr=1.5)
    env.chain.advance_block(mark=D("60290"))
    await rejected_with(env.submit_ticket(user, preview.ticket), E.STALE_DECISION)
    assert env.position(user) is None


@pytest.mark.parametrize("scenario", ["clean_stop", "gap_to_worst"])
async def test_p08_loss_at_stop_must_stay_within_the_previewed_worst_loss(env, scenario):
    user, _ = env.add_user("100")
    wide_stop(env)
    preview = await env.preview(user, stop_atr=1.5)
    env.chain.advance_block(mark=D("60120"))  # +0.2% drift, inside DRIFT_MAX and the 1.5x rule
    out = await env.submit_ticket(user, preview.ticket)
    assert out.outcome is Outcome.FILLED
    stop = D(str(preview.stop_price))
    # gap_to_worst fills right at the stop's worst allowed price.
    mark = stop if scenario == "clean_stop" else stop * D("0.985") * D("1.0002")
    env.chain.advance_block(mark=mark)
    assert env.position(user) is None
    await env.settle()
    loss = D("100") - env.chain.sub(user.subaccount_id).deposits
    assert float(loss) <= preview.worst_loss_usd
    if scenario == "gap_to_worst":
        assert float(loss) > preview.worst_loss_usd * 0.95  # the stop really filled near its worst price


async def test_h5_collar_and_stop_slippage_are_in_the_preview(env):
    user, _ = env.add_user("100")
    env.chain.candle_range = D("900")
    preview = await env.preview(user, stop_atr=1.5)
    qty = D(str(preview.quantity))
    entry_worst = D("60180")
    stop_worst = D(str(preview.stop_price)) * D("0.985")
    expected = qty * (entry_worst - stop_worst) + qty * (entry_worst + stop_worst) * D("0.0005")
    assert D(str(preview.worst_loss_usd)) == pytest.approx(expected, abs=D("0.01"))


async def test_h5_default_day_budget_shrinks_a_trade_to_fit(env):
    user, _ = env.add_user("100")
    assert Risk().daily_loss == D("0.06")
    # 3.85% down is under the limit: 6.24 budget - 4 lost = 2.24 left, below the 3% per-trade budget.
    env.set_risk(user, "104", "104")
    assert (await env.preview(user)).worst_loss_usd <= 2.24
    # 3.18 left: the per-trade budget binds again, so the day no longer shrinks it.
    env.set_risk(user, "103", "103")
    assert 2.9 < (await env.preview(user)).worst_loss_usd <= 3.0


async def test_h5_size_shrinks_to_what_is_left_of_the_day(env):
    user, _ = env.add_user("100")
    use_risk(env, daily_loss=D("0.10"))
    fresh = await env.preview(user)  # a fresh day has the whole 10 left
    env.set_risk(user, "108", "108")  # 7.4% down on the day, 10.8 - 8 = 2.8 left, under the 3 budget
    shrunk = await env.preview(user)
    assert shrunk.quantity < fresh.quantity
    assert shrunk.worst_loss_usd <= 2.8


async def test_h5_no_entry_once_the_day_limit_is_reached(env):
    user, _ = env.add_user("100")
    env.set_risk(user, "107", "107")  # 6.5% down with a 6% limit
    await rejected_with(env.preview(user), E.DAILY_LOSS_LIMIT)


async def test_h5_submit_rechecks_the_budget(env):
    user, _ = env.add_user("100")
    preview = await env.preview(user)
    env.set_risk(user, "104", "104")  # under the limit, but only 2.24 left against the 2.94 worst loss
    await rejected_with(env.submit_ticket(user, preview.ticket), E.DAILY_LOSS_LIMIT)


# ---------------------------------------------------------------- L2 status regression


async def _signed_row(env, user, purpose="close"):
    order = OrderRequest(
        kind=OrderKind.MARKET, side=Side.SHORT, quantity=D("0.001"), worst_price=D("50000"),
        cid=f"cid-{purpose}", reduce_only=False, margin=D("30"),
    )
    signed = await env.chain.build_and_sign(user.inj_address, user.subaccount_id, [order])
    return await env.svc.tx.persist(user_id=user.id, intent_id=None, purpose=purpose, signed=signed)


async def test_p09_racing_rebroadcast_must_not_reopen_a_final_tx(env):
    user, _ = env.add_user()
    manual(env)
    row = await _signed_row(env, user)
    real = env.chain.broadcast
    gate = asyncio.Event()

    async def slow(tx):
        await real(tx)
        await gate.wait()

    env.chain.broadcast = slow  # type: ignore[method-assign]
    task_a = asyncio.create_task(env.svc.tx.resolve(row))
    await asyncio.sleep(0)
    env.chain.broadcast = real  # type: ignore[method-assign]
    env.chain.advance_block()
    final = await env.svc.tx.resolve(env.svc.tx.get(row.id))
    assert final.status == "ok"
    gate.set()
    await task_a
    assert env.svc.tx.get(row.id).status == "ok"


async def test_p09b_race_must_not_raise_integrity_error(env):
    user, _ = env.add_user()
    manual(env)
    row = await _signed_row(env, user)
    real = env.chain.broadcast
    gate = asyncio.Event()

    async def slow(tx):
        await real(tx)
        await gate.wait()

    env.chain.broadcast = slow  # type: ignore[method-assign]
    task_a = asyncio.create_task(env.svc.tx.resolve(row))
    await asyncio.sleep(0)
    env.chain.broadcast = real  # type: ignore[method-assign]
    env.chain.advance_block()
    await env.svc.tx.resolve(env.svc.tx.get(row.id))
    await _signed_row(env, user, purpose="other")  # the next signer flow persists its tx
    gate.set()
    results = await asyncio.gather(task_a, return_exceptions=True)
    assert not isinstance(results[0], BaseException)


# ---------------------------------------------------------------- M8 undersized stop


async def test_p11_undersized_stop_after_position_grows_must_be_noticed(env):
    user = await enter_protected(env)
    env.chain.sub(user.subaccount_id).position.quantity *= 3  # user adds from their own wallet
    manual(env)
    await env.tick()
    env.chain.advance_block(blocks=env.ctx.settings.stop_grace_blocks + 1)
    await env.tick()
    stop = env.stop_orders(user)[0]
    assert stop.quantity >= env.position(user).quantity or env.alerts("stop_missing")


async def test_m8_undersized_stop_is_replaced_in_one_tx_sized_to_the_position(env):
    user = await enter_protected(env)
    env.chain.sub(user.subaccount_id).position.quantity *= 3
    manual(env)
    await env.tick()
    env.chain.advance_block(blocks=env.ctx.settings.stop_grace_blocks + 1)
    auto(env)
    await env.tick()
    assert env.intent(user)["stage"] == "protected"
    stops = env.stop_orders(user)
    assert len(stops) == 1 and stops[0].quantity == env.position(user).quantity
    assert len(env.alerts("stop_missing")) == 1
    replacement = env.chain._signed[env.txs(purpose="stop")[-1]["tx_hash"]]
    assert len(replacement.cancels) == 1 and replacement.cancels[0].quantity < stops[0].quantity


async def test_m8_live_stop_rejects_a_short_stop(env):
    user = await enter_protected(env)
    account = await env.chain.account(user.inj_address, user.subaccount_id)
    assert live_stop(account) is not None
    grown = replace(account, position=replace(account.position, quantity=account.position.quantity * 2))
    assert live_stop(grown) is None


async def test_mutant_stop_quantity_check_short_stop_is_not_protected(env):
    user, _ = env.add_user()
    intent_id = await unresolved_entry(env, user)
    env.chain.advance_block()  # the entry fills
    await step(env, intent_id)  # stop signed and sent
    assert env.intent(user)["stage"] == "stop_pending"
    env.chain.sub(user.subaccount_id).position.quantity *= 3  # grows before the stop lands
    env.chain.advance_block()  # the stop lands, sized to the old quantity
    await step(env, intent_id)  # not enough: a fresh replacement goes out
    assert env.intent(user)["stage"] == "stop_pending"
    assert len(env.txs(purpose="stop")) == 2
    env.chain.advance_block()
    await step(env, intent_id)
    assert env.intent(user)["stage"] == "protected"
    assert env.stop_orders(user)[0].quantity == env.position(user).quantity


# ---------------------------------------------------------------- H4 silence


async def test_p12_unprotected_position_must_alert_when_txs_never_land(env):
    user, _ = env.add_user()
    env.svc.submit_budget_s = 0.3
    env.watcher.user_budget_s = 0.1
    real = env.chain.broadcast
    first = {"done": False}

    async def after_entry_nothing_lands(tx):
        if first["done"]:
            env.chain.inject(Failure.NEVER_INCLUDED, count=1)
        first["done"] = True
        await real(tx)

    env.chain.broadcast = after_entry_nothing_lands  # type: ignore[method-assign]
    await env.enter(user)
    await env.tick(5)
    assert env.position(user) is not None and not env.stop_orders(user)  # the dangerous state exists
    assert [a for a in env.alerts() if a["level"] == "critical"], "live position without a stop and no alert"


async def test_h4_stuck_stage_alerts_per_window_and_stops_once_protected(env):
    user, _ = env.add_user()
    env.chain.fail_calls("build_and_sign", count=1000, skip=1)  # the entry fills, the stop can never be signed
    env.svc.submit_budget_s = 0.2
    env.watcher.user_budget_s = 0.1
    await env.enter(user)
    assert env.intent(user)["stage"] == "entry_filled"
    manual(env)
    await env.tick()
    env.chain.advance_block(blocks=env.ctx.settings.stop_grace_blocks + 1)
    await env.tick()
    assert len(env.alerts("stop_missing")) == 1
    env.chain.fail_calls("build_and_sign", count=0)
    auto(env)
    env.next_window()
    await env.tick(2)
    assert env.intent(user)["stage"] == "protected"
    assert len(env.alerts("stop_missing")) == 1


# ---------------------------------------------------------------- M5 / C1 freshness on the real backend


async def test_p13_real_backend_as_of_must_reflect_chain_time():
    now = {"ms": 1_794_152_000_000}
    client = FakeClient()  # height and block time never change: the chain is frozen
    chain = make(client)
    chain._clock_ms = lambda: now["ms"]
    first = await chain.snapshot()
    now["ms"] += 3_600_000
    second = await chain.snapshot()
    assert first.height == second.height and first.mark_price == second.mark_price
    assert now["ms"] - second.as_of_ms >= limits.MARKET_MAX_AGE_MS


async def test_c1_frozen_oracle_is_stale_even_on_a_live_chain():
    client = FakeClient()
    chain = make(client)
    client.block_ms += 60_000  # blocks keep coming, the oracle's own timestamps do not move
    snapshot = await chain.snapshot()
    assert snapshot.as_of_ms == 1_794_152_000_000


async def test_c1_as_of_is_the_older_of_block_and_oracle():
    client = FakeClient()
    chain = make(client)
    client.block_ms -= 5_000  # the block is the older of the two here
    assert (await chain.snapshot()).as_of_ms == client.block_ms


# ---------------------------------------------------------------- C2 M7 stale reads


async def test_c2_account_height_is_the_oldest_response_height():
    client = FakeClient()
    client.response_heights = {"deposit": 1005, "position": 1003, "conditional": 1006, "market": 1004}
    account = await make(client).account(GRANTER, SUBACCOUNT)
    assert account.height == 1003


async def test_c2_account_falls_back_to_latest_height_when_a_header_is_missing():
    client = FakeClient()
    client.response_heights = {"deposit": 1005}
    assert (await make(client).account(GRANTER, SUBACCOUNT)).height == 1000


async def test_c2_height_comes_from_the_response_metadata_key():
    class Inner:
        def metadata(self):
            return ()

        async def process_response_metadata(self, grpc_call):
            pass

    class Call:
        async def initial_metadata(self):
            return grpc.aio.Metadata(("x-cosmos-block-height", "4242"))

    seen: list[int] = []
    token = _response_heights.set(seen)
    try:
        await _HeightCookies(Inner()).process_response_metadata(Call())
    finally:
        _response_heights.reset(token)
    assert seen == [4242]


async def test_mutant_entry_height_guard_lagging_read_is_not_no_fill(env):
    user, _ = env.add_user()
    intent_id = await unresolved_entry(env, user)
    env.chain.advance_block()  # included and filled at this height
    env.chain.set_read_lag(1)  # the node answering has not seen that block yet
    await step(env, intent_id)
    assert env.intent(user)["stage"] == "entry_sent"
    env.chain.set_read_lag(0)
    await step(env, intent_id)
    assert env.intent(user)["stage"] != "failed"


async def test_mutant_stop_height_guard_lagging_read_is_not_stop_missing(env):
    user, _ = env.add_user()
    intent_id = await unresolved_entry(env, user)
    env.chain.advance_block()
    await step(env, intent_id)  # stop signed and sent
    env.chain.advance_block()  # the stop lands at this height
    env.chain.set_read_lag(1)
    await step(env, intent_id)
    assert env.intent(user)["stage"] == "stop_pending"
    assert len(env.txs(purpose="stop")) == 1  # no retry from a read that predates the stop
    env.chain.set_read_lag(0)
    await step(env, intent_id)
    assert env.intent(user)["stage"] == "protected"


async def test_mutant_flatten_height_guard_lagging_read_sends_no_second_flatten(env):
    user, _ = env.add_user()
    env.chain.inject(Failure.STOP_PLACEMENT, count=2)
    intent_id = await unresolved_entry(env, user)
    for _ in range(3):  # entry fills; stop one, then stop two fail and the flatten is sent
        env.chain.advance_block()
        await step(env, intent_id)
    assert len(env.txs(purpose="flatten")) == 1
    env.chain.advance_block()  # the flatten lands at this height
    env.chain.set_read_lag(1)
    await step(env, intent_id)
    assert len(env.txs(purpose="flatten")) == 1
    assert env.intent(user)["stage"] != "flattened"  # not concluded from a stale read either
    env.chain.set_read_lag(0)
    await step(env, intent_id)
    assert env.intent(user)["stage"] == "flattened"


async def test_c2_stop_retry_with_code_97_means_already_live_not_flatten(env):
    user, _ = env.add_user()
    intent_id = await unresolved_entry(env, user)
    env.chain.advance_block()
    await step(env, intent_id)  # first stop sent
    env.chain.advance_block()  # it lands
    await step(env, intent_id)
    assert env.intent(user)["stage"] == "protected"
    # A lagging read after a 97 reply: the retry saw the cid live, so re-read instead of flattening.
    intent = env.svc.intent(intent_id)
    await env.svc._update_intent(intent_id, stage="stop_pending")
    await env.svc.rearm_stop(intent)
    row = await _duplicate_stop_row(env, user, intent)
    assert row.status == "failed" and row.code == 97
    await step(env, intent_id)
    assert env.intent(user)["stage"] == "protected"
    assert env.txs(purpose="flatten") == []


async def _duplicate_stop_row(env, user, intent):
    signed = await env.chain.build_and_sign(user.inj_address, user.subaccount_id, [env.svc._stop_order(intent, intent.quantity, D("0.01"))])
    await env.chain.broadcast(signed)
    env.chain.advance_block()
    row = await env.svc.tx.persist(user_id=user.id, intent_id=intent.id, purpose="stop", signed=signed, stage="stop_pending")
    return await env.svc.tx.resolve(row)


async def test_p16_one_stale_account_read_must_not_finalize_a_protected_position(env):
    user = await enter_protected(env)
    real = env.chain.account
    calls = {"n": 0}

    async def lagging_node(granter, sub):
        result = await real(granter, sub)
        calls["n"] += 1
        if calls["n"] == 1:
            return replace(result, position=None, orders=(), height=result.height - 20)
        return result

    env.chain.account = lagging_node  # type: ignore[method-assign]
    await env.tick()
    env.chain.account = real  # type: ignore[method-assign]
    assert env.intent(user)["stage"] == "protected"


async def test_p16_a_single_flat_read_must_not_finalize_after_a_restart(env):
    """Even with no height to compare against, one flat read is not enough."""
    user = await enter_protected(env)
    env.restart()  # forgets the heights seen so far
    real = env.chain.account

    async def lagging_node(granter, sub):
        return replace(await real(granter, sub), position=None, orders=())

    env.chain.account = lagging_node  # type: ignore[method-assign]
    await env.tick()
    env.chain.account = real  # type: ignore[method-assign]
    assert env.intent(user)["stage"] == "protected"
    await env.tick()
    assert env.intent(user)["stage"] == "protected"


async def test_m7_a_flat_read_between_two_position_reads_does_not_count(env):
    user = await enter_protected(env)
    position = env.chain.sub(user.subaccount_id).position
    env.chain.sub(user.subaccount_id).position = None
    await env.tick()  # flat read one
    env.chain.sub(user.subaccount_id).position = position
    env.chain.advance_block()
    await env.tick()  # back: the run of flat reads is broken
    env.chain.sub(user.subaccount_id).position = None
    env.chain.advance_block()
    await env.tick()  # flat again, but only the first of a new run
    assert env.intent(user)["stage"] == "protected"


async def test_m7_same_height_flat_reads_do_not_confirm(env):
    user = await enter_protected(env)
    env.chain.advance_block(mark=D("59000"))
    await env.tick(3)  # no new block between these reads
    assert env.intent(user)["stage"] == "protected"
    assert env.trades(user)[0]["closed_ms"] is None


async def test_m7_read_behind_the_last_seen_is_ignored_for_every_caller(env):
    user = await enter_protected(env)
    env.chain.advance_block(blocks=3)
    await env.svc.read_account(user)
    env.chain.set_read_lag(2)
    with pytest.raises(ChainUnavailable):
        await env.svc.read_account(user)


async def test_m7_close_uses_a_read_at_or_past_its_own_landed_tx(env):
    user = await enter_protected(env)
    out = await env.close(user)
    assert out.outcome is Outcome.CLOSED  # one read past our own close is enough


async def test_m7_close_never_acts_on_a_read_behind_its_own_tx(env):
    user = await enter_protected(env)
    env.svc.submit_budget_s = 0.2
    env.chain.advance_block(blocks=2)
    manual(env)
    close_task = asyncio.create_task(env.close(user))
    await asyncio.sleep(0.01)
    env.chain.advance_block()  # the close lands while a lagging node still shows the position
    env.chain.set_read_lag(1)
    out = await close_task
    assert out.outcome is Outcome.UNKNOWN
    assert len(env.txs(purpose="close")) == 1  # no second close from the stale read


# ---------------------------------------------------------------- C4 L5 bad market data


async def test_p17_empty_book_must_be_market_unavailable_not_a_crash(env):
    user, _ = env.add_user()
    real = env.chain.snapshot

    async def empty_book():
        snap = await real()
        return replace(snap, best_bid=D(0), best_ask=D(0))

    env.chain.snapshot = empty_book  # type: ignore[method-assign]
    await rejected_with(env.state(user), E.MARKET_UNAVAILABLE)


@pytest.mark.parametrize("side", ["bid", "ask"])
async def test_c4_one_empty_side_is_market_unavailable(env, side):
    user, _ = env.add_user()
    env.chain.set_book_depth(**{side: D("0")})
    await rejected_with(env.state(user), E.MARKET_UNAVAILABLE)


async def test_c4_empty_side_at_submit_is_market_unavailable(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.chain.set_book_depth(bid=D("0"))
    await rejected_with(env.submit_ticket(user, preview.ticket), E.MARKET_UNAVAILABLE)
    assert env.chain.signed_log == []


# ---------------------------------------------------------------- L4 stale candles


async def test_p18_stale_candles_must_block_sizing(env):
    user, _ = env.add_user()
    hour = env.clock() - env.clock() % 3_600_000
    candles = [
        Candle(hour - (i + 48) * 3_600_000, D("60000"), D("60010"), D("59990"), D("60000"))
        for i in range(70, 0, -1)
    ]
    env.chain.set_candles(candles)
    await rejected_with(env.state(user), E.MARKET_UNAVAILABLE)


async def test_l4_candle_age_boundary():
    from halo_trade import features as F

    hour = F.HOUR_MS
    now = 500_000 * hour + hour // 2
    this_hour = now - now % hour

    def series(newest_hours_ago):
        return [Candle(this_hour - i * hour, D("100"), D("101"), D("99"), D("100")) for i in range(newest_hours_ago + 60, newest_hours_ago - 1, -1)]

    F.compute(series(2), this_hour + hour // 2, D("0.0005"), D("99"), D("101"))  # closed 1.5 h ago
    with pytest.raises(F.InsufficientData):
        F.compute(series(3), this_hour + hour // 2, D("0.0005"), D("99"), D("101"))  # closed 2.5 h ago


# ---------------------------------------------------------------- H3 retry after flatten


async def test_p19_failed_flatten_must_not_abandon_the_stop(env):
    """Two stop attempts fail, then flatten cannot fill: the third stop attempt must go out.
    The bid side empties only after the entry, because an empty side is now refused up front."""
    user, _ = env.add_user()
    env.watcher.user_budget_s = 0.1
    env.svc.submit_budget_s = 0.2
    env.chain.inject(Failure.STOP_PLACEMENT, count=2)  # only two failures: the third stop succeeds
    empty_bids_after_entry(env)  # flatten (a sell) cannot fill
    preview = await env.preview(user)
    await env.submit_ticket(user, preview.ticket)
    await env.tick(5)
    assert env.position(user) is not None
    assert env.stop_orders(user), "open position, stop never retried"
    assert env.intent(user)["stage"] == "protected"


async def test_h3_stop_attempt_alternates_with_flatten_until_one_works(env):
    user, _ = env.add_user()
    env.svc.submit_budget_s = 0.2
    env.watcher.user_budget_s = 0.1
    env.chain.inject(Failure.STOP_PLACEMENT, count=1000)
    empty_bids_after_entry(env)
    await env.enter(user)
    await env.tick(3)
    stops = [r["id"] for r in env.txs(purpose="stop")]
    flattens = [r["id"] for r in env.txs(purpose="flatten")]
    assert len(stops) >= 3 and flattens
    assert stops[2] > flattens[0]  # the stop attempt came after the first failed flatten


async def test_h3_no_stop_attempt_when_the_mark_already_crossed(env):
    user, _ = env.add_user()
    env.svc.submit_budget_s = 0.2
    env.chain.inject(Failure.STOP_PLACEMENT, count=2)
    empty_bids_after_entry(env)
    original = env.chain.account

    async def crossed(granter, subaccount_id):
        account = await original(granter, subaccount_id)
        return replace(account, mark_price=D("59000")) if len(env.txs(purpose="flatten")) else account

    env.chain.account = crossed  # type: ignore[method-assign]
    await env.enter(user)
    assert len(env.txs(purpose="stop")) == 2  # the two failures, no more after the flatten


async def test_h3_rearm_of_a_protected_position_places_a_stop_before_any_flatten(env):
    user = await enter_protected(env)
    env.chain.sub(user.subaccount_id).orders.clear()
    manual(env)
    await env.tick()
    env.chain.advance_block(blocks=env.ctx.settings.stop_grace_blocks + 1)
    auto(env)
    await env.tick()
    assert env.txs(purpose="flatten") == []
    assert len(env.stop_orders(user)) == 1


# ---------------------------------------------------------------- M6 close and the watcher need only the mark


async def test_p20_snapshot_only_failure_must_not_block_close(env):
    user = await enter_protected(env)
    env.chain.fail_calls("snapshot", count=100000)  # market read fails, account read works
    env.svc.submit_budget_s = 0.2
    await env.close(user)
    await env.tick(3)
    assert env.position(user) is None


async def test_p20b_real_snapshot_survives_the_display_only_oracle_field():
    client = FakeClient()
    chain = make(client)

    class NoPrice:
        async def OraclePrice(self, request):
            return SimpleNamespace(price_pair_state=SimpleNamespace(pair_price=""))

    chain._oracle_stub = NoPrice()
    snap = await chain.snapshot()
    assert snap.mark_price > 0
    assert snap.oracle_price == snap.mark_price


async def test_m6_watcher_reconciles_with_only_the_account_read(env):
    user = await enter_protected(env)
    env.chain.fail_calls("snapshot", count=100000)
    env.chain.sub(user.subaccount_id).orders.clear()
    manual(env)
    await env.tick()
    env.chain.advance_block(blocks=env.ctx.settings.stop_grace_blocks + 1)
    await env.tick()
    assert env.alerts("stop_missing")  # the stopless check still ran


async def test_c1_entries_are_blocked_when_the_oracle_is_stale_but_close_is_not(env):
    user = await enter_protected(env)
    env.chain.set_market_as_of(0)
    state = await env.state(user)
    assert E.MARKET_UNAVAILABLE in state.blocked_reasons
    out = await env.close(user)
    assert out.outcome is Outcome.CLOSED


# ---------------------------------------------------------------- L3 rebroadcast


async def test_p21_kill_switch_must_stop_a_signed_entry_being_rebroadcast(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.chain.arm_crash("broadcast", "before")
    with pytest.raises(SimulatedCrash):
        await env.submit_ticket(user, preview.ticket)
    env.restart()
    await env.svc.set_kill_switch(True)  # operator trips the kill switch during the incident
    await env.tick(2)
    assert env.chain.broadcast_log == []
    assert env.position(user) is None, "kill switch on, but the pending entry still went out"


@pytest.mark.parametrize("status", ["paused", "revoked"])
async def test_p22_inactive_users_entry_must_not_be_rebroadcast(env, status):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.chain.arm_crash("broadcast", "before")
    with pytest.raises(SimulatedCrash):
        await env.submit_ticket(user, preview.ticket)
    env.restart()
    env.ctx.db.execute(f"UPDATE users SET status='{status}' WHERE id=?", (user.id,))
    await env.tick(2)
    assert env.chain.broadcast_log == []
    assert env.position(user) is None


async def test_l3_rebroadcast_of_stops_and_closes_is_unaffected_by_the_switch(env):
    user = await enter_protected(env)
    await env.svc.set_kill_switch(True)
    env.chain.arm_crash("broadcast", "before")
    with pytest.raises(SimulatedCrash):
        await env.close(user)
    env.restart()
    await env.tick(3)
    assert env.position(user) is None  # closing out is never blocked by the kill switch


# ---------------------------------------------------------------- C6 grantee gas


async def test_c6_low_grantee_gas_refuses_entries_and_reports_the_code(env):
    user, _ = env.add_user()
    env.chain.set_grantee_balance(D("0.01"))
    state = await env.state(user)
    assert E.GRANTEE_LOW_GAS in state.blocked_reasons
    await rejected_with(
        env.svc.preview(user, PreviewIn(observation_id=state.observation_id, side="long", stop_atr=2.0)),
        E.GRANTEE_LOW_GAS,
    )


async def test_c6_gas_read_failure_is_market_unavailable(env):
    user, _ = env.add_user()
    env.chain.fail_calls("grantee_balance_inj", count=2)
    await rejected_with(env.state(user), E.MARKET_UNAVAILABLE)


async def test_c6_submit_rechecks_gas(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.chain.set_grantee_balance(D("0.01"))
    await rejected_with(env.submit_ticket(user, preview.ticket), E.GRANTEE_LOW_GAS)
    assert env.chain.signed_log == []


async def test_c6_low_gas_alerts_and_never_blocks_close(env):
    user = await enter_protected(env)
    env.chain.set_grantee_balance(D("0.01"))
    await env.tick()
    assert len(env.alerts("grantee_low_gas")) == 1
    await env.tick()
    assert len(env.alerts("grantee_low_gas")) == 1  # deduped and throttled
    out = await env.close(user)
    assert out.outcome is Outcome.CLOSED
    assert env.position(user) is None


async def test_c6_real_backend_reads_the_fee_denom_balance():
    client = FakeClient()
    client.inj_amount = str(25 * 10**16)
    assert await make(client).grantee_balance_inj() == D("0.25")


async def test_c5_deposit_gas_is_above_the_measured_use():
    assert load_script("testnet_setup").GAS_DEPOSIT_TX == 350_000


# ---------------------------------------------------------------- C7 min notional at worst price


async def test_c7_stop_must_meet_min_notional_at_its_worst_price(env):
    user, _ = env.add_user()
    env.chain.market_info = replace(env.chain.market_info, min_notional=D("100"))  # notional 102, stop worst 99.47
    await rejected_with(env.preview(user), E.SIZE_TOO_SMALL)


# ---------------------------------------------------------------- C9 classify by cid


def _resting(cid, kind, side=Side.SHORT):
    return RestingOrder("h", cid, kind, side, D("0.0025"), D("59000"), D("57000"), True)


async def test_c9_stop_recognised_by_cid_when_mark_inference_says_take(env):
    user = await enter_protected(env)
    intent = env.svc.open_intent(user.id)
    account = await env.chain.account(user.inj_address, user.subaccount_id)
    flipped = replace(account, orders=(_resting(make_cid(user.id, intent.window_id, "stop"), OrderKind.TAKE),))
    assert live_stop(flipped, intent) is not None
    assert live_stop(flipped) is None


async def test_c9_take_is_not_mistaken_for_a_stop_when_inference_says_stop(env):
    user = await enter_protected(env)
    intent = env.svc.open_intent(user.id)
    account = await env.chain.account(user.inj_address, user.subaccount_id)
    flipped = replace(account, orders=(_resting(make_cid(user.id, intent.window_id, "take"), OrderKind.STOP),))
    assert live_stop(flipped, intent) is None
    assert live_stop(flipped) is not None


async def test_c9_order_with_an_unknown_cid_keeps_the_inferred_kind(env):
    user = await enter_protected(env)
    intent = env.svc.open_intent(user.id)
    account = await env.chain.account(user.inj_address, user.subaccount_id)
    other = replace(account, orders=(_resting("someone-elses", OrderKind.STOP),))
    assert live_stop(other, intent) is not None


# ---------------------------------------------------------------- C10 lookup


@pytest.mark.parametrize("response", [{}, {"code": 0}, {"code": 0, "height": ""}])
async def test_c10_empty_tx_response_is_not_success(response):
    client = FakeClient()
    backend = make(client)
    tx = await backend.build_and_sign(GRANTER, SUBACCOUNT, [market_order()])
    client.tx_response = response
    with pytest.raises(ChainUnavailable):
        await backend.lookup(tx)


# ---------------------------------------------------------------- surviving mutants


async def test_mutant_open_intent_blocks_a_new_entry_with_no_position_on_chain(env):
    user, _ = env.add_user()
    env.chain.inject(Failure.NEVER_INCLUDED)
    manual(env)
    env.svc.submit_budget_s = 0.2
    await env.enter(user)
    assert env.position(user) is None and env.intent(user)["stage"] == "entry_sent"
    env.next_window()
    env.chain.advance_block()
    await rejected_with(env.preview(user), E.POSITION_OPEN)


async def test_mutant_no_cancel_of_leftovers_while_a_tx_is_open(env):
    user, _ = env.add_user()
    env.chain.add_resting_order(
        user.subaccount_id,
        RestingOrder("hash1", "cid1", OrderKind.STOP, Side.SHORT, D("0.01"), D("50000"), D("48000"), False),
    )
    other_tx = OrderRequest(OrderKind.STOP, Side.SHORT, D("0.001"), D("57000"), "cid-other", True, trigger_price=D("59000"))
    signed = await env.chain.build_and_sign(user.inj_address, user.subaccount_id, [other_tx])
    await env.svc.tx.persist(user_id=user.id, intent_id=None, purpose="stop", signed=signed)
    env.chain.fail_calls("lookup", count=1)  # resolve_all_open sees an outage; the tx stays open this tick
    await env.tick()
    assert env.txs(purpose="cancel") == []
    assert len(env.orders(user)) == 1


# ---------------------------------------------------------------- testnet candles


class _FakeCandleClient:
    def __init__(self):
        self.calls = 0

    async def fetch_chain_derivative_markets(self, status=None, market_ids=None, with_mid_price_and_tob=None):
        self.calls += 1
        return {"markets": [{"market": {"ticker": "BTC/USDC PERP", "marketId": "0xmainnet"}}]}


def _candle_backend(**kw):
    return InjectiveChain(
        network="testnet", grantee_private_key="11" * 32, market_ticker="BTC/USDC PERP", tx_timeout_blocks=40,
        collar=D("0.005"), client=FakeClient(), clock_ms=lambda: 1_794_152_000_000, **kw,
    )


async def test_candles_default_to_the_trading_network():
    backend = make(FakeClient())
    assert backend._chart_base_url.startswith("https://k8s.testnet.chart")
    assert await backend._chart_market_id("0xown") == "0xown"


async def test_testnet_can_read_candles_from_mainnet_by_ticker(monkeypatch):
    client = _FakeCandleClient()
    backend = _candle_backend(candle_network="mainnet", candle_client=client)
    assert backend._chart_base_url.startswith("https://k8s.global.mainnet.chart")
    assert await backend._chart_market_id("0xown") == "0xmainnet"
    assert await backend._chart_market_id("0xown") == "0xmainnet"
    assert client.calls == 1  # looked up once, cached after
    assert backend.is_mainnet is False  # signing stays on testnet


async def test_candle_network_missing_ticker_is_unavailable():
    class NoMarkets(_FakeCandleClient):
        async def fetch_chain_derivative_markets(self, status=None, market_ids=None, with_mid_price_and_tob=None):
            return {"markets": []}

    backend = _candle_backend(candle_network="mainnet", candle_client=NoMarkets())
    with pytest.raises(ChainUnavailable):
        await backend._chart_market_id("0xown")


def test_candle_network_cannot_be_testnet_for_mainnet_trading():
    with pytest.raises(ValueError):
        InjectiveChain(
            network="mainnet", grantee_private_key="11" * 32, market_ticker="BTC/USDC PERP", tx_timeout_blocks=40,
            collar=D("0.005"), client=FakeClient(), candle_network="testnet",
        )


async def test_candle_request_uses_the_mainnet_market_id_and_stays_under_a_deadline(monkeypatch):
    seen = {}

    class FakeHttp:
        def __init__(self, timeout, follow_redirects):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params):
            seen["url"], seen["params"] = url, params
            never = asyncio.Event()
            await never.wait()

    monkeypatch.setattr("halo_trade.chain.injective.httpx.AsyncClient", FakeHttp)
    backend = _candle_backend(candle_network="mainnet", candle_client=_FakeCandleClient())
    backend._rpc_timeout_s = 0.05
    with pytest.raises(ChainUnavailable):
        await backend.candles_1h(5)
    assert seen["params"]["marketId"] == "0xmainnet"
    assert seen["url"].startswith("https://k8s.global.mainnet.chart")


# ---------------------------------------------------------------- defaults


def test_tx_timeout_default_is_twenty_blocks():
    assert Settings().tx_timeout_blocks == 20


def test_order_refused_counts_as_nothing_signed_and_a_value_error():
    from halo_trade.chain.base import OrderRefused

    assert issubclass(OrderRefused, ChainUnavailable) and issubclass(OrderRefused, ValueError)


def test_grantee_low_gas_is_additive_with_a_status():
    assert limits.STATUS_BY_CODE[E.GRANTEE_LOW_GAS] == 503


# ---------------------------------------------------------------- H1 hung calls inside send, H2 refused orders


async def test_p01_order_the_backend_refuses_counts_as_stop_not_placed(env):
    from halo_trade.chain.base import OrderRefused

    user, _ = env.add_user()
    real_build = env.chain.build_and_sign

    async def build(granter, sub, orders, cancels=()):
        if any(o.kind is OrderKind.STOP for o in orders):
            raise OrderRefused("stop and take orders need a positive trigger price")
        return await real_build(granter, sub, orders, cancels)

    env.chain.build_and_sign = build  # type: ignore[method-assign]
    out = await env.enter(user)
    assert out.code is E.STOP_FAILED_FLATTENED
    assert env.position(user) is None


def _send(env, user, budget=0.1):
    return env.svc.tx.send(
        user_id=user.id, intent_id=None, purpose="stop", granter=user.inj_address, subaccount_id=user.subaccount_id,
        orders=[OrderRequest(OrderKind.MARKET, Side.LONG, D("0.001"), D("70000"), "cid-h1", False, margin=D("30"))],
        deadline=time.monotonic() + budget,
    )


def _hang_forever():
    never = asyncio.Event()

    async def hang(tx):
        await never.wait()

    return hang


async def test_h1_hung_broadcast_leaves_the_tx_signed_and_frees_the_signer(env):
    user, _ = env.add_user()
    manual(env)
    env.chain.broadcast = _hang_forever()  # type: ignore[method-assign]
    row = await asyncio.wait_for(_send(env, user), 1.0)
    assert row.status == "signed"  # resolve() re-sends the same bytes later
    assert not env.svc.tx.slot_busy


async def test_h1_hung_lookup_does_not_hold_the_signer_past_the_deadline(env):
    user, _ = env.add_user()
    manual(env)
    env.chain.lookup = _hang_forever()  # type: ignore[method-assign]
    row = await asyncio.wait_for(_send(env, user), 1.0)
    assert row.is_open
    assert not env.svc.tx.slot_busy


async def test_h1_hung_lookup_of_an_earlier_tx_is_signer_busy_within_the_deadline(env):
    user, _ = env.add_user()
    manual(env)
    await _send(env, user, budget=0.02)  # open and unmined
    env.chain.lookup = _hang_forever()  # type: ignore[method-assign]
    with pytest.raises(SignerBusy):
        await asyncio.wait_for(_send(env, user), 1.0)
    assert not env.svc.tx.slot_busy


async def test_c2_code_97_with_no_stop_visible_yet_rereads_instead_of_retrying(env):
    user, _ = env.add_user()
    intent_id = await unresolved_entry(env, user)
    env.chain.advance_block()
    await step(env, intent_id)  # first stop sent
    env.chain.advance_block()  # it lands
    await step(env, intent_id)
    intent = env.svc.intent(intent_id)
    await env.svc.rearm_stop(intent)
    await _duplicate_stop_row(env, user, intent)
    real = env.chain.account

    async def no_stop_yet(granter, sub):
        return replace(await real(granter, sub), orders=())

    env.chain.account = no_stop_yet  # type: ignore[method-assign]
    await step(env, intent_id)
    assert len(env.txs(purpose="stop")) == 2  # the first stop and the duplicate, nothing new
    assert env.txs(purpose="flatten") == []
    env.chain.advance_block()  # a later block still without it: now it is really gone
    await step(env, intent_id)
    assert len(env.txs(purpose="stop")) == 3


async def test_h3_stop_lost_after_recovering_through_the_retry_is_replaced_not_flattened(env):
    user, _ = env.add_user()
    env.svc.submit_budget_s = 1.5
    env.chain.inject(Failure.STOP_PLACEMENT, count=2)
    empty_bids_after_entry(env)
    preview = await env.preview(user)
    await env.submit_ticket(user, preview.ticket)
    assert env.intent(user)["stage"] == "protected"
    flattens = len(env.txs(purpose="flatten"))
    assert flattens == 1
    env.chain.sub(user.subaccount_id).orders.clear()
    manual(env)
    await env.tick()
    env.chain.advance_block(blocks=env.ctx.settings.stop_grace_blocks + 1)
    auto(env)
    env.clock.advance(RETRY_WINDOW_MS)
    await env.tick()
    assert env.intent(user)["stage"] == "protected"
    assert len(env.stop_orders(user)) == 1
    assert len(env.txs(purpose="flatten")) == flattens


async def test_h2_stop_errors_do_not_carry_over_into_a_later_rearm(env):
    user = await enter_protected(env)
    intent = env.svc.open_intent(user.id)
    await env.svc._kv_set(env.svc._stop_error_key(intent), "2")
    await env.svc.rearm_stop(intent)
    assert env.svc._stop_errors(intent) == 0


# ---------------------------------------------------------------- M3 operator reset


def _ops(env, *args):
    import os
    import subprocess
    import sys

    script = Path(__file__).resolve().parent.parent / "scripts" / "ops.py"
    return subprocess.run([sys.executable, str(script), *args], env={**os.environ, "HALO_DB_PATH": env.db_path}, capture_output=True, text=True)


async def test_m3_reset_risk_unwedges_a_user_and_keeps_the_cooldown(env):
    user, _ = env.add_user()
    await env.tick()  # snapshot at 100
    env.set_risk(user, "150", "300", cooldown=env.window() + 3)
    result = _ops(env, "reset-risk", str(user.id))
    assert result.returncode == 0, result.stderr
    assert (D(env.risk(user)["day_start_equity"]), D(env.risk(user)["peak_equity"])) == (D("100"), D("100"))
    assert env.risk(user)["cooldown_until_window"] == env.window() + 3


async def test_m3_reset_risk_without_a_snapshot_changes_nothing_and_fails(env):
    user, _ = env.add_user()
    env.set_risk(user, "150", "300")
    assert _ops(env, "reset-risk", str(user.id)).returncode == 1
    assert D(env.risk(user)["peak_equity"]) == D("300")


async def test_m3_reset_risk_needs_a_user_id(env):
    assert _ops(env, "reset-risk").returncode != 0
