"""Entry flow: every check proven at integration level, submit re-checks, tickets, replays."""
import asyncio
from dataclasses import replace
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from halo_trade.chain.base import OrderKind, Side
from halo_trade.chain.mock import Failure
from halo_trade.contracts import STOP_ATR_MENU, ErrorCode as E, Outcome, PreviewIn, SubmitIn
from halo_trade.limits import ApiError
from halo_trade.txmachine import RETRY_WINDOW_MS


def empty_bids_after_entry(env) -> None:
    """Empty the bid side the moment the stop is being signed, once the entry has filled."""
    real = env.chain.build_and_sign
    emptied = {"done": False}

    async def build(granter, sub, orders, cancels=()):
        if any(o.kind is OrderKind.STOP for o in orders) and not emptied["done"]:
            emptied["done"] = True
            env.chain.set_book_depth(bid=D("0"))
        return await real(granter, sub, orders, cancels)

    env.chain.build_and_sign = build  # type: ignore[method-assign]


def use_risk(env, **changes):
    env.ctx.settings = replace(env.ctx.settings, risk=replace(env.ctx.settings.risk, **changes))


async def rejected_with(awaitable, code):
    with pytest.raises(ApiError) as caught:
        await awaitable
    assert caught.value.out.code is code, caught.value.out
    assert caught.value.out.outcome is Outcome.REJECTED
    return caught.value


# ---------------------------------------------------------------------- happy path


async def test_entry_then_stop_in_separate_txs(env):
    user, _ = env.add_user()
    out = await env.enter(user)
    assert out.outcome is Outcome.FILLED
    assert out.stop_live is True
    assert env.signed_orders() == [[OrderKind.MARKET], [OrderKind.STOP]]
    intent = env.intent(user)
    assert intent["stage"] == "protected"
    assert [r["purpose"] for r in env.txs()] == ["entry", "stop"]
    assert out.tx_hashes == [r["tx_hash"] for r in env.txs()]


async def test_stop_is_on_chain_reduce_only_at_correct_price_and_size(env):
    user, _ = env.add_user()
    await env.enter(user)
    position = env.position(user)
    stop = env.stop_orders(user)[0]
    assert stop.reduce_only is True
    assert stop.side is Side.SHORT
    assert stop.quantity == position.quantity == D("0.0017")
    assert stop.trigger_price == D("59400")
    assert stop.worst_price == D("59400") * D("0.985") == D("58509")


async def test_take_profit_placed_after_stop_in_its_own_tx(env):
    user, _ = env.add_user()
    out = await env.enter(user, tp=3.0)
    assert env.signed_orders() == [[OrderKind.MARKET], [OrderKind.STOP], [OrderKind.TAKE]]
    assert out.take_profit_price == 60900.0
    take = [o for o in env.orders(user) if o.kind is OrderKind.TAKE][0]
    assert take.trigger_price == D("60900")
    assert take.reduce_only is True


async def test_entry_margin_is_notional_over_max_leverage(env):
    user, _ = env.add_user()
    await env.enter(user)
    assert env.position(user).margin == D("0.0017") * D("60000") / D("2")


async def test_short_entry_stop_above_mark(env):
    user, _ = env.add_user()
    out = await env.enter(user, side="short")
    assert out.outcome is Outcome.FILLED
    stop = env.stop_orders(user)[0]
    assert stop.side is Side.LONG
    assert stop.trigger_price == D("60600")


async def test_preview_quantity_and_prices_server_computed(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    assert preview.quantity == 0.0017
    assert preview.stop_price == 59400.0
    assert preview.mark_price == 60000.0
    # Entry at the collar 60180, stop filled at its worst 58509, both fees.
    assert preview.worst_loss_usd == pytest.approx(0.0017 * (60180 - 58509) + 0.0017 * (60180 + 58509) * 0.0005)
    assert preview.worst_loss_usd <= 3.0  # the 3% budget on 100 of equity
    assert preview.expires_ms == env.clock() + 30_000


@pytest.mark.parametrize("deposits", ["50", "100", "200"])
@pytest.mark.parametrize("atr_pct", ["0.002", "0.005", "0.01", "0.015", "0.02"])
@pytest.mark.parametrize("stop_atr", STOP_ATR_MENU)
@pytest.mark.parametrize("side", ["long", "short"])
async def test_default_policy_every_menu_stop_is_tradable(env, deposits, atr_pct, stop_atr, side):
    """A flat account at default config, at any normal ATR, with no drift between preview and submit."""
    user, _ = env.add_user(deposits)
    atr = D("60000") * D(atr_pct)
    env.chain.candle_range = atr
    preview = await env.preview(user, side=side, stop_atr=stop_atr)  # SIZE_TOO_SMALL or DAILY_LOSS_LIMIT raise here
    distance = max(D(str(stop_atr)) * atr, D("300"))
    assert abs(D(str(preview.stop_price)) - D("60000")) == distance  # the scenario really has this ATR
    assert preview.quantity > 0
    assert D(str(preview.worst_loss_usd)) <= D(deposits) * D("0.03")
    out = await env.submit_ticket(user, preview.ticket)  # STALE_DECISION would raise here
    assert out.outcome is Outcome.FILLED
    assert out.stop_live is True


# ---------------------------------------------------------------------- entry checks at preview


async def test_kill_switch_blocks_preview(env):
    user, _ = env.add_user()
    state = await env.svc.state(user)
    await env.svc.set_kill_switch(True)
    await rejected_with(env.svc.preview(user, PreviewIn(observation_id=state.observation_id, side="long", stop_atr=2.0)), E.PAUSED)


async def test_paused_user_blocked_but_state_works(env):
    user, _ = env.add_user(status="paused")
    state = await env.svc.state(user)
    assert E.PAUSED in state.blocked_reasons
    assert state.can_open is False
    await rejected_with(env.preview(user), E.PAUSED)


async def test_grant_missing_rejected(env):
    user, _ = env.add_user(granted=False)
    await rejected_with(env.preview(user), E.GRANT_MISSING)


async def test_stale_market_data_blocks_preview_and_reported_in_state(env):
    user, _ = env.add_user()
    state = await env.svc.state(user)
    env.chain.auto_mine = False
    env.chain.freeze_market_data()
    env.clock.advance(31_000)
    fresh_state = await env.svc.state(user)
    assert E.MARKET_UNAVAILABLE in fresh_state.blocked_reasons
    await rejected_with(env.svc.preview(user, PreviewIn(observation_id=state.observation_id, side="long", stop_atr=2.0)), E.MARKET_UNAVAILABLE)


async def test_state_when_chain_unreachable_is_503_not_a_guess(env):
    user, _ = env.add_user()
    env.chain.go_down()
    with pytest.raises(ApiError) as caught:
        await env.svc.state(user)
    assert caught.value.out.code is E.MARKET_UNAVAILABLE
    assert caught.value.status == 503


async def test_observation_unknown_rejected(env):
    user, _ = env.add_user()
    await rejected_with(env.svc.preview(user, PreviewIn(observation_id="no-such-observation", side="long", stop_atr=2.0)), E.STALE_OBSERVATION)


async def test_observation_of_other_user_rejected(env):
    a, _ = env.add_user()
    b, _ = env.add_user()
    state_a = await env.svc.state(a)
    await rejected_with(env.svc.preview(b, PreviewIn(observation_id=state_a.observation_id, side="long", stop_atr=2.0)), E.STALE_OBSERVATION)


async def test_observation_from_previous_window_rejected(env):
    user, _ = env.add_user()
    state = await env.svc.state(user)
    env.next_window()
    await rejected_with(env.svc.preview(user, PreviewIn(observation_id=state.observation_id, side="long", stop_atr=2.0)), E.STALE_OBSERVATION)


async def test_observation_older_than_120s_rejected(env):
    user, _ = env.add_user()
    state = await env.svc.state(user)
    env.clock.advance(120_000)
    await rejected_with(env.svc.preview(user, PreviewIn(observation_id=state.observation_id, side="long", stop_atr=2.0)), E.STALE_OBSERVATION)


def test_menu_values_enforced_by_contract():
    with pytest.raises(ValidationError):
        PreviewIn(observation_id="abcdefgh", side="long", stop_atr=2.5)
    with pytest.raises(ValidationError):
        PreviewIn(observation_id="abcdefgh", side="long", stop_atr=2.0, take_profit_atr=9)
    with pytest.raises(ValidationError):
        PreviewIn(observation_id="abcdefgh", side="sideways", stop_atr=2.0)


async def test_position_open_rejected(env):
    user, _ = env.add_user()
    await env.enter(user)
    env.next_window()
    await rejected_with(env.preview(user), E.POSITION_OPEN)


async def test_unresolved_intent_counts_as_position_open(env):
    user, _ = env.add_user()
    env.chain.auto_mine = False
    await env.enter(user)  # entry stays unresolved at the end of the budget
    assert env.intent(user)["stage"] == "entry_sent"
    env.next_window()
    env.chain.advance_block()  # market data is fresh again; the entry is still unresolved
    env.chain.inject(Failure.NEVER_INCLUDED)
    await rejected_with(env.preview(user), E.POSITION_OPEN)


async def test_window_used_after_any_intent_this_window(env):
    user, _ = env.add_user()
    await env.enter(user)
    await env.close(user)
    env.chain.auto_mine = True
    await rejected_with(env.preview(user), E.WINDOW_USED)


async def test_cooldown_rejected(env):
    user, _ = env.add_user()
    env.set_risk(user, "100", "100", cooldown=env.window() + 2)
    await rejected_with(env.preview(user), E.COOLDOWN)


async def test_daily_loss_limit_rejected(env):
    user, _ = env.add_user()
    env.set_risk(user, "110", "110")
    await rejected_with(env.preview(user), E.DAILY_LOSS_LIMIT)


async def test_drawdown_limit_rejected(env):
    user, _ = env.add_user()
    env.set_risk(user, "100", "150")
    await rejected_with(env.preview(user), E.DRAWDOWN_LIMIT)


async def test_over_equity_cap_rejected(env):
    user, _ = env.add_user(deposits="500")
    await rejected_with(env.preview(user), E.OVER_CAP)


async def test_take_profit_too_close_rejected(env):
    user, _ = env.add_user()
    env.chain.candle_range = D("20")  # 2 ATR = 40 USD = 6.7 bps, below 3 x cost
    await rejected_with(env.preview(user, tp=2.0), E.TP_TOO_CLOSE)


async def test_size_too_small_rejected(env):
    user, _ = env.add_user(deposits="0.5")
    await rejected_with(env.preview(user), E.SIZE_TOO_SMALL)


async def test_balance_too_low_rejected(env):
    user, _ = env.add_user()
    env.chain.set_available(user.subaccount_id, D("1"))
    await rejected_with(env.preview(user), E.BALANCE_TOO_LOW)


async def test_book_too_thin_rejected(env):
    user, _ = env.add_user()
    env.chain.set_book_depth(ask=D("10"))
    await rejected_with(env.preview(user), E.BOOK_TOO_THIN)


async def test_book_too_thin_checks_the_exit_side_too(env):
    user, _ = env.add_user()
    env.chain.set_book_depth(ask=D("10"))
    # A short enters on the deep bids but has to buy back from the thin asks.
    await rejected_with(env.preview(user, side="short"), E.BOOK_TOO_THIN)


async def test_candles_unavailable_is_market_unavailable(env):
    user, _ = env.add_user()
    state = await env.svc.state(user)
    env.chain.fail_calls("candles_1h", count=2)
    await rejected_with(env.svc.preview(user, PreviewIn(observation_id=state.observation_id, side="long", stop_atr=2.0)), E.MARKET_UNAVAILABLE)


# ---------------------------------------------------------------------- submit re-checks


async def test_submit_rejects_when_kill_switch_set_after_preview(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    await env.svc.set_kill_switch(True)
    await rejected_with(env.submit_ticket(user, preview.ticket), E.PAUSED)
    assert env.chain.signed_log == []


async def test_submit_rejects_after_grant_lost(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.chain.set_grant(user.subaccount_id, False)
    await rejected_with(env.submit_ticket(user, preview.ticket), E.GRANT_MISSING)


async def test_submit_rejects_expired_ticket(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.clock.advance(30_000)
    await rejected_with(env.submit_ticket(user, preview.ticket), E.TICKET_EXPIRED)
    assert env.chain.signed_log == []


async def test_submit_rejects_price_drift(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.chain.advance_block(mark=D("60400"))  # 0.67% > DRIFT_MAX 0.5%
    await rejected_with(env.submit_ticket(user, preview.ticket), E.STALE_DECISION)
    assert env.chain.signed_log == []


async def test_submit_rejects_drift_past_new_default(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.chain.advance_block(mark=D("59840"))  # -0.267%, past the 0.25% default
    await rejected_with(env.submit_ticket(user, preview.ticket), E.STALE_DECISION)


async def test_submit_within_drift_max_executes(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.chain.advance_block(mark=D("59900"))  # -0.17%, moving away from the stop's entry risk
    out = await env.submit_ticket(user, preview.ticket)
    assert out.outcome is Outcome.FILLED


# At the default DRIFT_MAX of 0.25% risk growth cannot bind: even the tightest stop needs 0.4% of
# drift to grow 1.5x. These relax the drift cap so the risk-growth guard is the one that answers.
RELAXED_DRIFT = D("0.01")


async def test_submit_rejects_when_risk_outgrew_the_ticket_even_inside_drift_max(env):
    use_risk(env, drift_max=RELAXED_DRIFT)
    user, _ = env.add_user()
    preview = await env.preview(user)
    # Stop 59400, worst entry at the ticket 60180: 780 of risk per unit, 1.5x allows 1170.
    # +0.67% is inside the cap, but worst entry 60400 * 1.003 = 60581.20 is 1181.20 from the stop.
    env.chain.advance_block(mark=D("60400"))
    caught = await rejected_with(env.submit_ticket(user, preview.ticket), E.STALE_DECISION)
    assert caught.out.limit == "max_risk_growth=1.5"
    assert env.chain.signed_log == []


async def test_risk_growth_is_measured_from_the_worst_entry_at_the_ticket(env):
    use_risk(env, drift_max=RELAXED_DRIFT)
    user, _ = env.add_user()
    preview = await env.preview(user)
    # Worst entry 60380 * 1.003 = 60561.14 is 1161.14 from the stop. That is inside 1.5 x 780,
    # and would be refused if the growth were still measured from the ticket mark (1.5 x 600).
    env.chain.advance_block(mark=D("60380"))
    out = await env.submit_ticket(user, preview.ticket)
    assert out.outcome is Outcome.FILLED


async def test_entry_risk_check_is_side_aware(env):
    use_risk(env, drift_max=RELAXED_DRIFT)
    short_user, _ = env.add_user()
    long_user, _ = env.add_user()
    short_ticket = (await env.preview(short_user, side="short")).ticket
    long_ticket = (await env.preview(long_user, side="long")).ticket
    # Short: stop 60600, worst entry at the ticket 59820, 780 of risk, 1.5x allows 1170.
    # A short fills lower as the mark falls: 59600 * 0.997 = 59421.20 is 1178.80 from the stop.
    env.chain.advance_block(mark=D("59600"))
    caught = await rejected_with(env.submit_ticket(short_user, short_ticket), E.STALE_DECISION)
    assert caught.out.limit == "max_risk_growth=1.5"
    # The same move only shrinks a long's risk.
    assert (await env.submit_ticket(long_user, long_ticket)).outcome is Outcome.FILLED


async def test_submit_rejects_when_book_thinned(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.chain.set_book_depth(ask=D("10"))
    await rejected_with(env.submit_ticket(user, preview.ticket), E.BOOK_TOO_THIN)


async def test_submit_rejects_when_balance_dropped(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.chain.set_available(user.subaccount_id, D("1"))
    await rejected_with(env.submit_ticket(user, preview.ticket), E.BALANCE_TOO_LOW)


async def test_expired_ticket_is_reported_before_stale_market(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.chain.auto_mine = False
    env.chain.freeze_market_data()
    env.clock.advance(30_500)
    await rejected_with(env.submit_ticket(user, preview.ticket), E.TICKET_EXPIRED)


async def test_submit_market_staleness_ordered_after_ticket_validity(env):
    user, _ = env.add_user()
    state = await env.svc.state(user)
    preview = await env.svc.preview(user, PreviewIn(observation_id=state.observation_id, side="long", stop_atr=2.0))
    env.chain.freeze_market_data()
    env.chain._frozen_as_of_ms = env.clock() - 40_000
    await rejected_with(env.submit_ticket(user, preview.ticket), E.MARKET_UNAVAILABLE)


async def test_submit_chain_down_rejected_and_nothing_signed(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.chain.go_down()
    await rejected_with(env.submit_ticket(user, preview.ticket), E.MARKET_UNAVAILABLE)
    env.chain.come_back()
    assert env.chain.signed_log == []
    assert env.intent(user)["stage"] == "rejected"


async def test_submit_rejects_observation_older_than_120s_at_submit(env):
    user, _ = env.add_user()
    state = await env.svc.state(user)
    env.clock.advance(100_000)
    preview = await env.svc.preview(user, PreviewIn(observation_id=state.observation_id, side="long", stop_atr=2.0))
    env.clock.advance(20_000)  # ticket still valid, observation now over 120 s old
    await rejected_with(env.submit_ticket(user, preview.ticket), E.STALE_OBSERVATION)


async def test_submit_rejects_when_cooldown_began_after_preview(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.set_risk(user, "100", "100", cooldown=env.window() + 2)
    await rejected_with(env.submit_ticket(user, preview.ticket), E.COOLDOWN)


async def test_submit_rejects_at_daily_loss_after_preview(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.set_risk(user, "120", "120")
    await rejected_with(env.submit_ticket(user, preview.ticket), E.DAILY_LOSS_LIMIT)


async def test_submit_rejects_when_position_appeared(env):
    user, _ = env.add_user()
    first = await env.preview(user)
    second = await env.preview(user, side="short")
    await env.submit_ticket(user, first.ticket)
    # POSITION_OPEN precedes WINDOW_USED in the spec order.
    await rejected_with(env.submit_ticket(user, second.ticket), E.POSITION_OPEN)


# ---------------------------------------------------------------------- tickets and replays


async def test_ticket_of_another_user_not_found(env):
    a, _ = env.add_user()
    b, _ = env.add_user()
    preview = await env.preview(a)
    await rejected_with(env.svc.submit(b, SubmitIn(ticket=preview.ticket)), E.TICKET_NOT_FOUND)
    assert env.chain.signed_log == []


async def test_unknown_ticket_not_found(env):
    user, _ = env.add_user()
    await rejected_with(env.svc.submit(user, SubmitIn(ticket="no-such-ticket-id")), E.TICKET_NOT_FOUND)


async def test_replay_returns_stored_result_without_chain_calls(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    first = await env.submit_ticket(user, preview.ticket)
    signed = len(env.chain.signed_log)
    broadcasts = len(env.chain.broadcast_log)
    second = await env.submit_ticket(user, preview.ticket)
    assert second == first
    assert len(env.chain.signed_log) == signed
    assert len(env.chain.broadcast_log) == broadcasts


async def test_double_submit_concurrently_executes_once(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    first, second = await asyncio.gather(env.submit_ticket(user, preview.ticket), env.submit_ticket(user, preview.ticket))
    assert first.tx_hashes == second.tx_hashes
    assert len([r for r in env.txs() if r["purpose"] == "entry"]) == 1
    assert len(env.chain.signed_log) == 2  # one entry, one stop


async def test_replay_of_rejected_ticket_returns_same_rejection(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.chain.advance_block(mark=D("60400"))
    first = await rejected_with(env.submit_ticket(user, preview.ticket), E.STALE_DECISION)
    second = await rejected_with(env.submit_ticket(user, preview.ticket), E.STALE_DECISION)
    assert first.out == second.out


async def test_rejected_submit_burns_the_window(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.chain.advance_block(mark=D("60400"))
    await rejected_with(env.submit_ticket(user, preview.ticket), E.STALE_DECISION)
    env.chain.advance_block(mark=D("60000"))
    await rejected_with(env.preview(user), E.WINDOW_USED)


async def test_one_entry_intent_per_window_second_ticket_rejected(env):
    user, _ = env.add_user()
    first = await env.preview(user)
    second = await env.preview(user)
    await env.submit_ticket(user, first.ticket)
    await env.close(user)
    env.chain.auto_mine = True
    await rejected_with(env.submit_ticket(user, second.ticket), E.WINDOW_USED)


async def test_ticket_used_when_rejection_could_not_be_stored(env):
    user, _ = env.add_user()
    first = await env.preview(user)
    second = await env.preview(user)
    await env.submit_ticket(user, first.ticket)
    await env.close(user)
    env.chain.auto_mine = True
    await rejected_with(env.submit_ticket(user, second.ticket), E.WINDOW_USED)
    await rejected_with(env.submit_ticket(user, second.ticket), E.TICKET_USED)


async def test_stale_ticket_cannot_replay_into_next_window(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.clock.set(env.clock() + 5_000)  # still within 30 s, window unchanged
    env.next_window()
    with pytest.raises(ApiError) as caught:
        await env.submit_ticket(user, preview.ticket)
    assert caught.value.out.code in (E.TICKET_EXPIRED, E.STALE_OBSERVATION)
    assert env.chain.signed_log == []


# ---------------------------------------------------------------------- execution outcomes


async def test_partial_fill_uses_actual_quantity(env):
    user, _ = env.add_user()
    env.chain.inject(Failure.PARTIAL_FILL, ratio=D("0.6"))
    out = await env.enter(user)
    assert out.outcome is Outcome.FILLED
    # 60% of the 0.0017 asked for, down to the 0.0001 tick.
    assert env.position(user).quantity == D("0.0010")
    intent = env.intent(user)
    assert D(intent["quantity"]) == D("0.0010")
    assert env.stop_orders(user)[0].quantity == D("0.0010")
    assert D(env.trades(user)[0]["quantity"]) == D("0.0010")


async def test_entry_tx_failed_on_chain_marks_failed_no_position(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.chain.set_grant(user.subaccount_id, False)
    env.chain.check_grant = _always_ok  # type: ignore[method-assign]  # reach the chain with the grant lost
    out = await env.submit_ticket(user, preview.ticket)
    assert out.outcome is Outcome.FAILED
    assert out.code is E.CHAIN_REJECTED
    assert env.position(user) is None
    assert env.intent(user)["stage"] == "failed"


async def _always_ok(granter, subaccount_id):
    from halo_trade.chain.base import GrantStatus

    return GrantStatus(ok=True)


async def test_entry_included_but_no_fill_marks_failed(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.chain.set_book_depth(ask=D("1000"))  # depth check passes at submit
    orig = env.chain._execute_market
    env.chain._execute_market = lambda sub, order, all_or_nothing=False: False  # type: ignore[method-assign]
    out = await env.submit_ticket(user, preview.ticket)
    env.chain._execute_market = orig  # type: ignore[method-assign]
    assert out.outcome is Outcome.FAILED
    assert out.code is E.CHAIN_REJECTED
    assert len(env.stop_orders(user)) == 0
    assert env.intent(user)["stage"] == "failed"


async def test_entry_expired_marks_failed_and_never_rebuilt(env):
    user, _ = env.add_user()
    env.chain.inject(Failure.NEVER_INCLUDED)
    preview = await env.preview(user)
    out = await env.submit_ticket(user, preview.ticket)
    assert out.outcome is Outcome.FAILED
    assert out.code is E.CHAIN_REJECTED
    assert len(env.txs(purpose="entry")) == 1
    assert len(env.chain.signed_log) == 1
    assert env.position(user) is None


async def test_stop_failure_once_retries_with_fresh_tx(env):
    user, _ = env.add_user()
    env.chain.inject(Failure.STOP_PLACEMENT)
    out = await env.enter(user)
    assert out.outcome is Outcome.FILLED
    assert env.intent(user)["stage"] == "protected"
    stops = env.txs(purpose="stop")
    assert [r["status"] for r in stops] == ["failed", "ok"]
    assert stops[0]["tx_hash"] != stops[1]["tx_hash"]


async def test_stop_failing_twice_flattens_and_verifies(env):
    user, _ = env.add_user()
    env.chain.inject(Failure.STOP_PLACEMENT, count=2)
    out = await env.enter(user)
    assert out.outcome is Outcome.FAILED
    assert out.code is E.STOP_FAILED_FLATTENED
    assert out.stop_live is False
    assert env.position(user) is None
    intent = env.intent(user)
    assert intent["stage"] == "flattened"
    assert intent["error_code"] == E.STOP_FAILED_FLATTENED.value
    assert [r["status"] for r in env.txs(purpose="stop")] == ["failed", "failed"]
    assert env.txs(purpose="flatten")[0]["status"] == "ok"
    assert env.trades(user)[0]["close_reason"] == "flatten"
    assert len(env.alerts("flatten")) == 1


async def test_flatten_is_not_reported_until_verified_flat(env):
    user, _ = env.add_user()
    env.chain.inject(Failure.STOP_PLACEMENT, count=1000)  # a re-armed stop must keep failing here
    env.svc.submit_budget_s = 0.2
    empty_bids_after_entry(env)  # the flatten's market sell fills nothing
    out = await env.enter(user)
    assert out.outcome is Outcome.UNKNOWN
    assert env.position(user) is not None
    assert env.intent(user)["stage"] != "flattened"
    env.chain.set_book_depth(bid=D("1000000"))
    env.svc.submit_budget_s = 1.5
    env.clock.advance(RETRY_WINDOW_MS)  # the failing attempts used up this minute's retry budget
    await env.tick()
    assert env.position(user) is None
    assert env.intent(user)["stage"] == "flattened"


async def test_stop_trigger_already_crossed_flattens_without_placing(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    original = env.chain.account

    async def crossed_account(granter, subaccount_id):
        account = await original(granter, subaccount_id)
        # Report a mark below the stop only once the entry has filled.
        if account.position is not None:
            from dataclasses import replace

            return replace(account, mark_price=D("59000"))
        return account

    env.chain.account = crossed_account  # type: ignore[method-assign]
    out = await env.submit_ticket(user, preview.ticket)
    assert out.code is E.STOP_FAILED_FLATTENED
    assert env.txs(purpose="stop") == []
    assert env.position(user) is None


async def test_no_position_after_entry_ok_is_never_a_silent_fill(env):
    user, _ = env.add_user()
    out = await env.enter(user)
    assert out.quantity == float(env.position(user).quantity)
    assert out.entry_price == float(env.position(user).entry_price)


async def test_every_call_writes_a_decision_row(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    await env.submit_ticket(user, preview.ticket)
    await env.close(user)
    kinds = [r["kind"] for r in env.ctx.db.execute("SELECT kind FROM decisions ORDER BY id")]
    assert kinds == ["state", "preview", "submit", "close"]


async def test_rejected_preview_also_writes_decision(env):
    user, _ = env.add_user()
    await env.svc.set_kill_switch(True)
    state = await env.svc.state(user)
    with pytest.raises(ApiError):
        await env.svc.preview(user, PreviewIn(observation_id=state.observation_id, side="long", stop_atr=2.0))
    assert env.ctx.db.execute("SELECT COUNT(*) FROM decisions WHERE kind='preview'").fetchone()[0] == 1


async def test_budget_exhausted_returns_unknown_and_watcher_finishes(env):
    user, _ = env.add_user()
    env.chain.auto_mine = False
    out = await env.enter(user)  # entry cannot resolve without blocks
    assert out.outcome is Outcome.UNKNOWN
    assert out.code is E.OUTCOME_UNKNOWN
    assert out.check_state_after_s is not None and out.check_state_after_s > 0
    env.chain.auto_mine = True
    await env.tick(2)
    assert env.intent(user)["stage"] == "protected"
    assert env.position(user) is not None
    assert len(env.stop_orders(user)) == 1
    # The ticket replay now shows the finished result.
    ticket = env.intent(user)["ticket_id"]
    assert (await env.submit_ticket(user, ticket)).outcome is Outcome.FILLED


async def test_submit_never_blocks_over_budget(env):
    user, _ = env.add_user()
    env.chain.auto_mine = False
    env.svc.submit_budget_s = 0.15
    started = asyncio.get_event_loop().time()
    out = await env.enter(user)
    assert out.outcome is Outcome.UNKNOWN
    assert asyncio.get_event_loop().time() - started < 2.0


async def test_two_different_tickets_same_window_race_executes_exactly_one(env):
    user, _ = env.add_user()
    first = await env.preview(user)
    second = await env.preview(user)
    results = await asyncio.gather(env.submit_ticket(user, first.ticket), env.submit_ticket(user, second.ticket), return_exceptions=True)
    outcomes = sorted(type(r).__name__ for r in results)
    assert outcomes == ["ApiError", "SubmitOut"]
    assert len(env.txs(purpose="entry")) == 1
    assert env.ctx.db.execute("SELECT COUNT(*) FROM intents WHERE user_id=?", (user.id,)).fetchone()[0] == 1


async def test_unique_user_window_constraint_exists_in_database(env):
    import sqlite3

    user, _ = env.add_user()
    window, now = env.window(), env.clock()
    sql = (
        "INSERT INTO intents (user_id, window_id, ticket_id, side, quantity, stop_price, stage, created_ms, updated_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?)"
    )
    env.ctx.db.execute(sql, (user.id, window, "t1", "long", "1", "1", "entry_pending", now, now))
    with pytest.raises(sqlite3.IntegrityError):
        env.ctx.db.execute(sql, (user.id, window, "t2", "long", "1", "1", "entry_pending", now, now))


async def test_serialised_signing_slot_across_users(env):
    a, _ = env.add_user()
    b, _ = env.add_user()
    outs = await asyncio.gather(env.enter(a), env.enter(b))
    assert [o.outcome for o in outs] == [Outcome.FILLED, Outcome.FILLED]
    # Each signer slot use was sequential: no two open rows ever existed (unique index would throw).
    assert len(env.chain.signed_log) == 4
