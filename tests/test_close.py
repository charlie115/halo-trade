"""POST /v1/close is never blocked and verifies the position is gone."""
from decimal import Decimal as D

import pytest

from halo_trade.chain.base import Side
from halo_trade.chain.mock import Failure
from halo_trade.contracts import ErrorCode as E, Outcome
from halo_trade.limits import ApiError


async def test_close_position_full_flow(env):
    user, _ = env.add_user()
    await env.enter(user, tp=3.0)
    out = await env.close(user, reason="done")
    assert out.outcome is Outcome.CLOSED
    assert env.position(user) is None
    assert env.intent(user)["stage"] == "closed"
    assert env.txs(purpose="close")[0]["status"] == "ok"
    # The chain removes the reduce-only stop and take-profit with the position.
    assert env.orders(user) == []
    trade = env.trades(user)[0]
    assert trade["close_reason"] == "close"
    assert trade["exit_price"] is not None and trade["pnl_usd"] is not None and trade["closed_ms"] is not None


async def test_close_is_reduce_only_and_sized_to_actual_position(env):
    user, _ = env.add_user()
    await env.enter(user)
    await env.close(user)
    entry = env.chain._signed[env.txs(purpose="close")[0]["tx_hash"]]
    order = entry.orders[0]
    assert order.reduce_only is True
    assert order.side is Side.SHORT
    assert order.quantity == D("0.0017")


async def test_close_keeps_stop_in_a_separate_message_path(env):
    user, _ = env.add_user()
    await env.enter(user)
    await env.close(user)
    entry = env.chain._signed[env.txs(purpose="close")[0]["tx_hash"]]
    assert len(entry.orders) == 1 and len(entry.cancels) == 0  # a failed close leaves the stop


async def test_close_with_no_position_is_rejected(env):
    user, _ = env.add_user()
    with pytest.raises(ApiError) as caught:
        await env.close(user)
    assert caught.value.out.code is E.NO_POSITION
    assert caught.value.out.outcome is Outcome.REJECTED


def close_decisions(env):
    return env.ctx.db.execute("SELECT payload_json FROM decisions WHERE kind='close'").fetchall()


async def test_repeated_close_with_no_position_writes_no_decision(env):
    user, _ = env.add_user()
    for _ in range(5):
        with pytest.raises(ApiError) as caught:
            await env.close(user)
        assert caught.value.out.code is E.NO_POSITION
    assert close_decisions(env) == []


async def test_close_that_acted_still_writes_a_decision(env):
    user, _ = env.add_user()
    await env.enter(user)
    await env.close(user)
    assert len(close_decisions(env)) == 1
    with pytest.raises(ApiError):
        await env.close(user)  # flat now, so this one leaves no row
    assert len(close_decisions(env)) == 1


async def test_failed_close_still_writes_a_decision(env):
    user, _ = env.add_user()
    await env.enter(user)
    env.chain.set_book_depth(bid=D("0"))
    out = await env.close(user)
    assert out.outcome is Outcome.FAILED
    assert len(close_decisions(env)) == 1


@pytest.mark.parametrize("limit", ["kill_switch", "paused", "cooldown", "daily_loss", "drawdown", "grant"])
async def test_close_never_blocked_by_any_limit(env, limit):
    user, _ = env.add_user()
    await env.enter(user)
    if limit == "kill_switch":
        await env.svc.set_kill_switch(True)
    elif limit == "paused":
        env.ctx.db.execute("UPDATE users SET status='paused' WHERE id=?", (user.id,))
    elif limit == "cooldown":
        env.set_risk(user, "100", "100", cooldown=env.window() + 10)
    elif limit == "daily_loss":
        env.set_risk(user, "200", "200")
    elif limit == "drawdown":
        env.set_risk(user, "100", "1000")
    elif limit == "grant":
        env.chain.check_grant = _grant_says_no  # type: ignore[method-assign]
    out = await env.close(user)
    assert out.outcome is Outcome.CLOSED
    assert env.position(user) is None


async def _grant_says_no(granter, subaccount_id):
    from halo_trade.chain.base import GrantStatus

    return GrantStatus(ok=False, missing=("x",))


async def test_close_failed_then_position_remains_and_stop_stays_live(env):
    user, _ = env.add_user()
    await env.enter(user)
    env.chain.set_book_depth(bid=D("0"))
    out = await env.close(user)
    assert out.outcome is Outcome.FAILED
    assert env.position(user) is not None
    assert len(env.stop_orders(user)) == 1  # protection survived the failed close
    assert env.intent(user)["stage"] == "protected"


async def test_close_retry_budget_limits_attempts(env):
    user, _ = env.add_user()
    await env.enter(user)
    env.chain.set_book_depth(bid=D("0"))
    await env.close(user)
    assert len(env.txs(purpose="close")) == 3  # attempts capped, never a tight infinite loop


async def test_close_partial_fill_retries_until_flat(env):
    user, _ = env.add_user()
    await env.enter(user)
    env.chain.set_book_depth(bid=D("60"))  # 0.0010 of the 0.0017 position per attempt
    out = await env.close(user)
    assert out.outcome is Outcome.CLOSED
    assert env.position(user) is None
    assert len(env.txs(purpose="close")) >= 2


async def test_close_chain_down_returns_unknown_and_watcher_finishes(env):
    user, _ = env.add_user()
    await env.enter(user)
    env.chain.go_down()
    out = await env.close(user)
    assert out.outcome is Outcome.UNKNOWN
    assert out.code is E.OUTCOME_UNKNOWN
    assert env.position(user) is not None
    env.chain.come_back()
    await env.tick(2)
    assert env.position(user) is None
    assert env.intent(user)["stage"] == "closed"


async def test_close_during_unresolved_entry_returns_unknown_then_completes(env):
    user, _ = env.add_user()
    env.chain.auto_mine = False
    await env.enter(user)  # entry unresolved
    assert env.intent(user)["stage"] == "entry_sent"
    out = await env.close(user)
    assert out.outcome is Outcome.UNKNOWN
    env.chain.auto_mine = True
    await env.tick(3)
    assert env.position(user) is None
    assert env.intent(user)["stage"] == "closed"


async def test_close_after_stop_filled_reports_no_position(env):
    user, _ = env.add_user()
    await env.enter(user)
    env.chain.advance_block(mark=D("59000"))
    await env.tick()  # the first flat read
    env.chain.advance_block()
    with pytest.raises(ApiError) as caught:
        await env.close(user)  # a second one at a higher height ends the trade
    assert caught.value.out.code is E.NO_POSITION
    intent = env.intent(user)
    assert intent["stage"] == "closed"
    assert env.trades(user)[0]["close_reason"] == "stop"


async def test_close_failure_injection_transport_after_inclusion(env):
    user, _ = env.add_user()
    await env.enter(user)
    env.chain.inject(Failure.TRANSPORT_AFTER_INCLUSION)
    out = await env.close(user)
    assert out.outcome is Outcome.CLOSED
    assert env.position(user) is None
    assert len(env.txs(purpose="close")) == 1


async def test_stale_close_request_is_not_applied_to_a_new_position(env):
    import json

    user, _ = env.add_user()
    await env.svc._kv_set(env.svc._close_key(user.id), json.dumps({"ms": env.clock(), "attempts": 0}))
    out = await env.enter(user)
    assert out.outcome is Outcome.FILLED
    assert env.svc.close_requested(user.id) is False
    await env.tick(2)
    assert env.position(user) is not None
    assert env.txs(purpose="close") == []
