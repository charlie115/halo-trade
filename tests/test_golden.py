"""Golden run: open, protect, stop hit, cooldown, and re-entry after the cooldown."""
from decimal import Decimal as D

import pytest

from halo_trade.chain.base import OrderKind, Side
from halo_trade.contracts import ErrorCode as E, Outcome
from halo_trade.limits import ApiError


async def test_golden_open_protect_stop_hit_cooldown_reentry(env):
    user, _ = env.add_user()

    # 1. Observe and open.
    state = await env.svc.state(user)
    assert state.can_open is True
    assert state.blocked_reasons == []
    preview = await env.preview(user, side="long", stop_atr=2.0, tp=None)
    assert preview.quantity == 0.0017

    out = await env.submit_ticket(user, preview.ticket)
    assert out.outcome is Outcome.FILLED
    assert out.stop_live is True

    # 2. Protected: the on-chain stop is reduce-only and sized to the position.
    position = env.position(user)
    assert position.side is Side.LONG
    stop = env.stop_orders(user)[0]
    assert (stop.kind, stop.side, stop.reduce_only, stop.quantity) == (OrderKind.STOP, Side.SHORT, True, position.quantity)
    assert env.intent(user)["stage"] == "protected"
    open_state = await env.svc.state(user)
    assert open_state.position is not None
    assert open_state.position.stop_price == 59400.0
    assert E.POSITION_OPEN in open_state.blocked_reasons
    await env.tick()
    assert env.intent(user)["stage"] == "protected"

    # 3. The mark trades through the stop; the chain triggers and fills it.
    env.chain.advance_block(mark=D("59300"))
    assert env.position(user) is None
    await env.settle()
    intent = env.intent(user)
    trade = env.trades(user)[0]
    assert intent["stage"] == "closed"
    assert trade["close_reason"] == "stop"
    assert D(trade["pnl_usd"]) < 0
    assert D(trade["exit_price"]) == pytest.approx(D("59294.07"), abs=D("1"))
    stop_window = env.window()
    assert env.risk(user)["cooldown_until_window"] == stop_window + 3

    # 4. Cooldown: the next two windows are blocked even though the agent would like to trade.
    env.next_window()
    env.chain.advance_block()
    state = await env.svc.state(user)
    assert E.COOLDOWN in state.blocked_reasons
    with pytest.raises(ApiError) as caught:
        await env.preview(user)
    assert caught.value.out.code is E.COOLDOWN

    env.next_window()
    env.chain.advance_block()
    with pytest.raises(ApiError):
        await env.preview(user)

    # 5. After the cooldown a fresh entry works from a fresh window.
    env.next_window()
    env.chain.advance_block()
    state = await env.svc.state(user)
    assert state.can_open is True
    # The first stop cost about 1.3 of a 6 budget, so a 3% worst case still fits what is left.
    second = await env.preview(user, side="short", stop_atr=2.0)
    assert second.stop_price > 59300
    out2 = await env.submit_ticket(user, second.ticket)
    assert out2.outcome is Outcome.FILLED
    assert env.intent(user)["stage"] == "protected"

    # 6. A manual exit is logged and the replay of the first ticket is unchanged.
    closed = await env.close(user, reason="done")
    assert closed.outcome is Outcome.CLOSED
    assert env.position(user) is None
    assert env.intent(user)["stage"] == "closed"
    assert (await env.submit_ticket(user, preview.ticket)).outcome is Outcome.FILLED

    # 7. Decisions were recorded throughout and no tx was left unresolved.
    assert env.ctx.db.execute("SELECT COUNT(*) FROM txs WHERE status IN ('signed','sent')").fetchone()[0] == 0
    kinds = {r["kind"] for r in env.ctx.db.execute("SELECT kind FROM decisions")}
    assert kinds == {"state", "preview", "submit", "close"}
