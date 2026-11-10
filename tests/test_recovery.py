"""Crash and restart at each intent stage: the process dies, a new one resumes from the DB."""
import pytest

from halo_trade.chain.mock import SimulatedCrash
from halo_trade.contracts import ErrorCode as E, Outcome


async def crash_during_submit(env, user, method, when="before", skip=0, **enter_kwargs):
    preview = await env.preview(user, **enter_kwargs)
    env.chain.arm_crash(method, when, skip=skip)
    with pytest.raises(SimulatedCrash):
        await env.submit_ticket(user, preview.ticket)
    env.restart()


async def test_crash_before_signing_entry_never_executes(env):
    user, _ = env.add_user()
    await crash_during_submit(env, user, "build_and_sign")
    assert env.txs() == []
    await env.tick()
    assert env.intent(user)["stage"] == "failed"
    assert env.chain.broadcast_log == []
    assert env.position(user) is None


async def test_crash_after_sign_before_persist_leaves_nothing_broadcast(env):
    user, _ = env.add_user()
    await crash_during_submit(env, user, "build_and_sign", when="after")
    assert env.txs() == []
    await env.tick()
    assert env.intent(user)["stage"] == "failed"
    assert env.chain.broadcast_log == []
    assert env.position(user) is None


async def test_entry_sent_unknown_same_window_resumes_with_same_bytes(env):
    user, _ = env.add_user()
    await crash_during_submit(env, user, "broadcast", when="before")
    row = env.txs()[0]
    assert row["status"] == "signed"
    assert env.intent(user)["stage"] == "entry_sent"
    await env.tick(2)
    assert env.intent(user)["stage"] == "protected"
    assert len(env.chain.signed_log) == 2  # entry + stop
    assert env.txs(purpose="entry")[0]["status"] == "ok"
    assert env.position(user) is not None
    assert len(env.stop_orders(user)) == 1


async def test_entry_sent_after_broadcast_crash_resolves_by_lookup(env):
    user, _ = env.add_user()
    await crash_during_submit(env, user, "broadcast", when="after")
    assert env.txs()[0]["status"] == "signed"  # we never saw broadcast return
    await env.tick(2)
    assert env.intent(user)["stage"] == "protected"
    assert len(env.txs(purpose="entry")) == 1
    entry_hash = env.txs(purpose="entry")[0]["tx_hash"]
    assert env.chain.broadcast_log.count(entry_hash) == 1  # included on the first send, never re-sent


async def test_restart_never_rebroadcasts_a_closed_windows_entry(env):
    user, _ = env.add_user()
    await crash_during_submit(env, user, "broadcast", when="before")
    entry_hash = env.txs()[0]["tx_hash"]
    env.next_window()
    env.chain.auto_mine = False
    await env.tick(3)
    assert env.chain.broadcast_log == []
    assert entry_hash not in env.chain.included
    assert env.intent(user)["stage"] == "entry_sent"
    # Only resolution by timeout_height finishes it, and it never executed.
    env.chain.advance_block(blocks=env.chain.tx_timeout_blocks + 1)
    env.chain.auto_mine = True
    await env.tick(2)
    assert env.intent(user)["stage"] == "failed"
    assert env.position(user) is None
    assert env.chain.broadcast_log == []


async def test_restart_with_entry_included_before_crash_but_window_closed_still_finishes(env):
    user, _ = env.add_user()
    await crash_during_submit(env, user, "broadcast", when="after")
    env.next_window()
    await env.tick(3)
    # The tx was already sent before the crash: it executed, so it must be protected.
    assert env.intent(user)["stage"] == "protected"
    assert len(env.stop_orders(user)) == 1


async def test_entry_filled_before_stop_placed_resumes_and_places_stop(env):
    user, _ = env.add_user()
    # skip=0 on build_and_sign would crash the entry; the entry is the first sign, so skip it.
    await crash_during_submit(env, user, "build_and_sign", skip=1)
    assert env.intent(user)["stage"] == "entry_filled"
    assert env.txs(purpose="stop") == []
    await env.tick(2)
    assert env.intent(user)["stage"] == "protected"
    assert len(env.stop_orders(user)) == 1
    assert len(env.txs(purpose="entry")) == 1


async def test_stop_sent_unknown_resumes_with_same_bytes(env):
    user, _ = env.add_user()
    # broadcast skip=1: the entry broadcast passes, the stop broadcast crashes before sending.
    await crash_during_submit(env, user, "broadcast", skip=1)
    assert env.intent(user)["stage"] == "stop_pending"
    stop = env.txs(purpose="stop")[0]
    assert stop["status"] == "signed"
    await env.tick(2)
    assert env.intent(user)["stage"] == "protected"
    assert len(env.txs(purpose="stop")) == 1  # the stop was not rebuilt
    assert len(env.stop_orders(user)) == 1


async def test_stop_sent_crash_after_broadcast_resolves(env):
    user, _ = env.add_user()
    await crash_during_submit(env, user, "broadcast", when="after", skip=1)
    assert env.intent(user)["stage"] == "stop_pending"
    await env.tick(2)
    assert env.intent(user)["stage"] == "protected"
    assert len(env.txs(purpose="stop")) == 1
    assert len(env.stop_orders(user)) == 1


async def test_crash_after_flatten_broadcast_still_verifies(env):
    from halo_trade.chain.mock import Failure

    user, _ = env.add_user()
    env.chain.inject(Failure.STOP_PLACEMENT, count=2)
    # Broadcasts: entry, stop, stop retry. The flatten is the fourth; crash after sending it.
    await crash_during_submit(env, user, "broadcast", when="after", skip=3)
    assert env.txs(purpose="flatten")[0]["status"] == "signed"
    await env.tick(2)
    assert env.intent(user)["stage"] == "flattened"
    assert env.position(user) is None
    assert len(env.txs(purpose="flatten")) == 1


async def test_restart_finishes_a_submit_that_returned_unknown(env):
    user, _ = env.add_user()
    env.chain.auto_mine = False
    out = await env.enter(user)
    assert out.outcome is Outcome.UNKNOWN
    env.restart()
    env.chain.auto_mine = True
    await env.tick(2)
    assert env.intent(user)["stage"] == "protected"


async def test_ticket_replay_after_restart_returns_stored_result(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    first = await env.submit_ticket(user, preview.ticket)
    env.restart()
    second = await env.submit_ticket(user, preview.ticket)
    assert second == first
    assert len(env.chain.signed_log) == 2


async def test_chain_down_through_whole_entry_then_recovers_without_double_execution(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.chain.auto_mine = False
    env.chain.fail_calls("broadcast", count=1000)
    out = await env.submit_ticket(user, preview.ticket)
    assert out.outcome is Outcome.UNKNOWN
    assert env.intent(user)["stage"] == "entry_sent"
    assert env.chain.signed_log and len(env.chain.signed_log) == 1
    env.chain.fail_calls("broadcast", count=0)
    env.chain.auto_mine = True
    await env.tick(3)
    assert env.intent(user)["stage"] == "protected"
    assert len(env.txs(purpose="entry")) == 1


@pytest.mark.parametrize(
    "method,skip,stage",
    [
        ("lookup", 0, "entry_sent"),
        ("account", 3, "entry_sent"),  # state, preview, submit each read the account first
    ],
)
async def test_chain_outage_after_entry_sent_recovers(env, method, skip, stage):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.chain.fail_calls(method, count=1000, skip=0 if method == "lookup" else 1)
    env.chain.auto_mine = False
    out = await env.submit_ticket(user, preview.ticket)
    assert out.outcome is Outcome.UNKNOWN
    assert env.intent(user)["stage"] == stage
    env.chain.fail_calls(method, count=0)
    env.chain.auto_mine = True
    await env.tick(3)
    assert env.intent(user)["stage"] == "protected"


async def test_chain_outage_while_signing_stop_is_resumed(env):
    user, _ = env.add_user()
    env.chain.fail_calls("build_and_sign", count=1000, skip=1)  # entry signs, stop cannot
    env.svc.submit_budget_s = 0.2
    preview = await env.preview(user)
    out = await env.submit_ticket(user, preview.ticket)
    assert out.outcome is Outcome.UNKNOWN
    assert env.intent(user)["stage"] == "entry_filled"
    env.svc.submit_budget_s = 1.5
    env.chain.fail_calls("build_and_sign", count=0)
    await env.tick(2)
    assert env.intent(user)["stage"] == "protected"


async def test_chain_outage_while_broadcasting_stop_is_resumed(env):
    user, _ = env.add_user()
    env.chain.tx_timeout_blocks = 1_000_000  # polling mines blocks; do not let the stop expire
    env.chain.fail_calls("broadcast", count=1000, skip=1)
    env.svc.submit_budget_s = 0.2
    preview = await env.preview(user)
    out = await env.submit_ticket(user, preview.ticket)
    assert out.outcome is Outcome.UNKNOWN
    assert env.intent(user)["stage"] == "stop_pending"
    env.chain.fail_calls("broadcast", count=0)
    env.svc.submit_budget_s = 1.5
    await env.tick(2)
    assert env.intent(user)["stage"] == "protected"
    assert len(env.txs(purpose="stop")) == 1


async def test_chain_down_at_signing_of_entry_definitively_fails(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.chain.fail_calls("build_and_sign", count=1000)
    out = await env.submit_ticket(user, preview.ticket)
    assert out.outcome is Outcome.FAILED
    assert out.code is E.MARKET_UNAVAILABLE
    assert env.txs() == []
    assert env.intent(user)["stage"] == "failed"
    env.chain.fail_calls("build_and_sign", count=0)
    await env.tick(2)
    assert env.chain.signed_log == []
