"""The watcher running many wallets at once, and who it reads how often."""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import replace
from types import SimpleNamespace

from halo_trade.watcher import Watcher

FLAT_S = 60


def flat_every_minute(env) -> None:
    env.ctx.settings = replace(env.ctx.settings, watch_flat_interval_s=FLAT_S)


def track(env, method: str, delay: float = 0.0) -> SimpleNamespace:
    state = SimpleNamespace(now=0, peak=0, calls=Counter())
    original = getattr(env.chain, method)

    async def tracked(*args, **kwargs):
        state.now += 1
        state.peak = max(state.peak, state.now)
        state.calls[args[0] if args else method] += 1
        await asyncio.sleep(delay)
        state.now -= 1
        return await original(*args, **kwargs)

    setattr(env.chain, method, tracked)
    return state


def wallets(env, count: int, **kwargs):
    return [env.add_user(**kwargs)[0] for _ in range(count)]


def snapshots(env, user) -> int:
    return env.ctx.db.execute("SELECT COUNT(*) FROM equity_snapshots WHERE user_id=?", (user.id,)).fetchone()[0]


# ---------------------------------------------------------------- concurrency
async def test_the_watcher_reads_up_to_its_limit_at_once(env):
    env.ctx.settings = replace(env.ctx.settings, watch_concurrency=4)
    wallets(env, 12)
    reads = track(env, "account", delay=0.01)
    await env.tick()
    assert reads.peak == 4


async def test_a_hung_wallet_does_not_hold_up_the_others(env):
    env.ctx.settings = replace(env.ctx.settings, watch_concurrency=3)
    hung, *rest = wallets(env, 7)
    original = env.chain.account

    async def account(granter, subaccount):
        if granter == hung.inj_address:
            await asyncio.sleep(30)
        return await original(granter, subaccount)

    env.chain.account = account
    await asyncio.wait_for(Watcher(env.svc, user_budget_s=0.02).tick(), 3)
    assert all(snapshots(env, user) == 1 for user in rest)
    assert snapshots(env, hung) == 0


async def test_a_failing_wallet_does_not_stop_the_others(env):
    bad, *rest = wallets(env, 5)
    original = env.chain.account

    async def account(granter, subaccount):
        if granter == bad.inj_address:
            raise RuntimeError("boom")
        return await original(granter, subaccount)

    env.chain.account = account
    await env.tick()
    assert all(snapshots(env, user) == 1 for user in rest)


async def test_the_limit_starts_when_a_wallet_gets_its_slot_not_while_it_queues(env):
    env.ctx.settings = replace(env.ctx.settings, watch_concurrency=1)
    users = wallets(env, 4)
    track(env, "account", delay=0.02)
    await asyncio.wait_for(Watcher(env.svc, user_budget_s=0.04).tick(), 5)  # 4 x 0.02 is past one budget in the queue
    assert all(snapshots(env, user) == 1 for user in users)


async def test_a_wallet_busy_elsewhere_is_left_for_the_next_tick(env):
    user, = wallets(env, 1)
    reads = track(env, "account")
    async with env.svc.user_lock(user.id):
        await env.watcher._check_user(user)
    assert reads.calls[user.inj_address] == 0


async def test_signing_stays_one_at_a_time_however_many_wallets_need_a_stop(env):
    env.ctx.settings = replace(env.ctx.settings, watch_concurrency=20)
    users = wallets(env, 4)
    for user in users:
        await env.enter(user)
        env.next_window()
    for user in users:
        env.chain.sub(user.subaccount_id).orders.clear()
    await env.tick()
    env.chain.advance_block(blocks=env.ctx.settings.stop_grace_blocks + 1)
    signing = track(env, "build_and_sign", delay=0.005)
    for _ in range(12):
        await env.tick()
        env.chain.advance_block()
        if all(env.stop_orders(user) for user in users):
            break
    assert all(env.stop_orders(user) for user in users)
    assert signing.peak == 1


async def test_a_long_tick_keeps_the_heartbeat_fresh(env, monkeypatch):
    from halo_trade import watcher as watcher_module
    from halo_trade.alerts import HEARTBEAT_KEY

    monkeypatch.setattr(watcher_module, "BEAT_DURING_TICK_S", 0.0)
    env.ctx.settings = replace(env.ctx.settings, watch_concurrency=1)
    user, other = wallets(env, 2)
    beats = []
    original = env.chain.account

    async def account(granter, subaccount):
        beats.append(env.ctx.db.execute("SELECT value FROM system_state WHERE key=?", (HEARTBEAT_KEY,)).fetchone()["value"])
        env.clock.advance(1_000)
        return await original(granter, subaccount)

    env.chain.account = account
    await env.tick()
    assert len(set(beats)) == 2  # the second wallet's read saw a heartbeat newer than the first's


# ---------------------------------------------------------------- who is read how often
async def test_flat_wallets_are_read_once_a_minute(env):
    flat_every_minute(env)
    user, = wallets(env, 1)
    reads = track(env, "account")
    await env.tick()
    assert reads.calls[user.inj_address] == 1  # always once at the start
    for _ in range(FLAT_S // 5):  # over the next minute
        env.clock.advance(5_000)
        await env.tick()
    # Its second read lands on its own place in the minute, then one minute after that.
    assert reads.calls[user.inj_address] == 2


async def test_flat_wallets_started_together_are_spread_over_the_minute(env):
    flat_every_minute(env)
    users = wallets(env, 30)
    await env.tick()
    next_reads = sorted(env.watcher._next_flat_poll_ms[user.id] for user in users)
    assert len(set(next_reads)) > 20
    assert next_reads[-1] - next_reads[0] > 40_000


async def test_a_wallet_with_a_position_is_read_every_tick(env):
    flat_every_minute(env)
    user, = wallets(env, 1)
    await env.enter(user)
    reads = track(env, "account")
    for _ in range(4):
        env.clock.advance(5_000)
        await env.tick()
    assert reads.calls[user.inj_address] == 4


async def test_a_paused_wallet_with_nothing_open_is_in_the_slow_group(env):
    flat_every_minute(env)
    user, = wallets(env, 1, status="paused")
    reads = track(env, "account")
    await env.tick()
    env.clock.advance(5_000)
    await env.tick()
    assert reads.calls[user.inj_address] == 1


async def test_a_paused_wallet_with_a_position_is_in_the_fast_group(env):
    flat_every_minute(env)
    user, = wallets(env, 1)
    await env.enter(user)
    env.ctx.db.execute("UPDATE users SET status='paused' WHERE id=?", (user.id,))
    reads = track(env, "account")
    env.clock.advance(5_000)
    await env.tick()
    assert reads.calls[user.inj_address] == 1


async def test_placing_an_order_moves_a_wallet_to_the_fast_group_at_once(env):
    flat_every_minute(env)
    user, = wallets(env, 1)
    await env.tick()
    reads = track(env, "account")
    env.clock.advance(5_000)
    await env.tick()
    assert reads.calls[user.inj_address] == 0
    env.ctx.db.execute(
        "INSERT INTO intents (user_id, window_id, ticket_id, side, quantity, stop_price, stage, created_ms, updated_ms) VALUES (?,?,'t','long','1','1','entry_pending',1,1)",
        (user.id, env.window()),
    )
    await env.tick()
    assert reads.calls[user.inj_address] == 1


async def test_starting_a_wallet_moves_it_to_the_fast_group_at_once(env):
    flat_every_minute(env)
    user, = wallets(env, 1)
    await env.tick()
    reads = track(env, "account")
    env.clock.advance(5_000)
    await env.tick()
    assert reads.calls[user.inj_address] == 0
    env.ctx.db.execute("INSERT INTO strategy_runs(user_id,preset,status,started_ms) VALUES(?,'trend','active',?)", (user.id, env.clock()))
    await env.tick()
    assert reads.calls[user.inj_address] == 1


async def test_a_started_wallet_drops_to_the_slow_group_after_a_minute(env):
    flat_every_minute(env)
    user, = wallets(env, 1)
    await env.tick()
    env.ctx.db.execute("INSERT INTO strategy_runs(user_id,preset,status,started_ms) VALUES(?,'trend','active',?)", (user.id, env.clock()))
    env.clock.advance(FLAT_S * 1000 + 1)
    await env.tick()  # its own slow turn
    reads = track(env, "account")
    env.clock.advance(5_000)
    await env.tick()
    assert reads.calls[user.inj_address] == 0


async def test_a_close_in_progress_moves_a_wallet_to_the_fast_group(env):
    flat_every_minute(env)
    user, = wallets(env, 1)
    await env.tick()
    await env.svc._arm_close(user.id, env.clock())
    reads = track(env, "account")
    env.clock.advance(5_000)
    await env.tick()
    assert reads.calls[user.inj_address] >= 1


async def test_a_tx_still_open_moves_a_wallet_to_the_fast_group(env):
    flat_every_minute(env)
    user, = wallets(env, 1)
    await env.tick()
    env.ctx.db.execute(
        "INSERT INTO txs (user_id, purpose, signer, sequence, timeout_height, tx_hash, tx_bytes, status, created_ms, updated_ms) VALUES (?,'cancel','s',1,9,'h',x'00','sent',1,1)",
        (user.id,),
    )
    assert user.id in env.watcher._fast_users()


async def test_a_position_the_last_read_showed_keeps_a_wallet_in_the_fast_group(env):
    flat_every_minute(env)
    user, = wallets(env, 1)
    await env.tick()
    env.watcher._holding.add(user.id)
    reads = track(env, "account")
    env.clock.advance(5_000)
    await env.tick()
    assert reads.calls[user.inj_address] == 1


async def test_a_wallet_back_to_flat_leaves_the_fast_group(env):
    flat_every_minute(env)
    user, = wallets(env, 1)
    await env.enter(user)
    await env.tick()
    await env.close(user)
    env.clock.advance(5_000)
    await env.tick()  # sees it flat
    assert user.id not in env.watcher._holding
    reads = track(env, "account")
    env.clock.advance(5_000)
    await env.tick()
    assert reads.calls[user.inj_address] == 0


async def test_a_flat_read_serves_the_snapshot_and_the_grant_check(env):
    flat_every_minute(env)
    user, = wallets(env, 1)
    await env.tick()
    assert snapshots(env, user) == 1
    env.clock.advance(FLAT_S * 1000 + 1)
    reads = track(env, "account")
    grants = track(env, "check_grant")
    await env.tick()
    assert reads.calls[user.inj_address] == 1  # one read did for all three
    assert grants.calls[user.inj_address] == 1
    assert snapshots(env, user) == 2


async def test_a_fast_wallet_still_snapshots_and_checks_its_grant_about_once_a_minute(env):
    flat_every_minute(env)
    user, = wallets(env, 1)
    await env.enter(user)
    await env.tick()
    before = snapshots(env, user)
    grants = track(env, "check_grant")
    for _ in range(5):
        env.clock.advance(5_000)
        await env.tick()
    assert snapshots(env, user) == before and grants.calls[user.inj_address] == 0
    env.clock.advance(40_000)
    await env.tick()
    assert snapshots(env, user) == before + 1 and grants.calls[user.inj_address] == 1


async def test_a_wallet_whose_read_failed_is_not_retried_until_its_next_turn(env):
    flat_every_minute(env)
    user, = wallets(env, 1)
    env.chain.fail_calls("account", count=1)
    await env.tick()
    reads = track(env, "account")
    env.clock.advance(5_000)
    await env.tick()
    assert reads.calls[user.inj_address] <= 1


async def test_the_first_tick_reads_every_wallet(env):
    flat_every_minute(env)
    users = wallets(env, 5)
    reads = track(env, "account")
    await env.tick()
    assert all(reads.calls[user.inj_address] == 1 for user in users)


async def test_zero_means_every_wallet_every_tick(env):
    user, = wallets(env, 1)
    reads = track(env, "account")
    await env.tick()
    await env.tick()
    assert reads.calls[user.inj_address] == 2
