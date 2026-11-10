"""Everything in the spec's Watcher section."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from halo_trade.chain.base import OrderKind, RestingOrder, Side
from halo_trade.chain.mock import Failure
from halo_trade.alerts import HEARTBEAT_KEY, check_watcher_heartbeat
from halo_trade.config import WINDOW_MS
from halo_trade.contracts import ErrorCode as E, Outcome
from halo_trade.limits import ApiError
from halo_trade.txmachine import RETRY_WINDOW_MS
from halo_trade.watcher import PURGE_AGE_MS, PURGE_INTERVAL_MS, SNAPSHOT_INTERVAL_MS


def manual(env):
    env.chain.auto_mine = False


def auto(env):
    env.chain.auto_mine = True


async def enter_protected(env, tp=None):
    user, token = env.add_user()
    out = await env.enter(user, tp=tp)
    assert out.outcome is Outcome.FILLED
    return user


# ---------------------------------------------------------------------- stop lifecycle


async def test_stop_hit_writes_trade_and_cooldown(env):
    user = await enter_protected(env)
    env.chain.advance_block(mark=D("59000"))
    assert env.position(user) is None
    await env.settle()
    intent = env.intent(user)
    assert intent["stage"] == "closed"
    assert env.trades(user)[0]["close_reason"] == "stop"
    assert env.trades(user)[0]["pnl_usd"] is not None
    assert env.risk(user)["cooldown_until_window"] == env.window() + 3


async def test_take_profit_hit_has_no_cooldown(env):
    user = await enter_protected(env, tp=3.0)
    env.chain.advance_block(mark=D("61000"))
    await env.settle()
    assert env.intent(user)["stage"] == "closed"
    assert env.trades(user)[0]["close_reason"] == "take_profit"
    assert env.risk(user)["cooldown_until_window"] == 0


async def test_external_close_at_a_profit_has_no_cooldown(env):
    user = await enter_protected(env)
    # A position closed outside this service, at a mid price: no stop or target matched.
    env.chain.deposit(user.subaccount_id, D("0.5"))  # the fee is paid back: this exit made money
    sub = env.chain.sub(user.subaccount_id)
    sub.position = None
    sub.orders = []
    await env.settle()
    assert env.intent(user)["stage"] == "closed"
    assert env.trades(user)[0]["close_reason"] == "external"
    assert env.risk(user)["cooldown_until_window"] == 0


async def test_external_close_at_a_loss_cools_down(env):
    user = await enter_protected(env)
    sub = env.chain.sub(user.subaccount_id)
    sub.position = None
    sub.orders = []
    await env.settle()
    assert env.trades(user)[0]["close_reason"] == "external"
    assert D(env.trades(user)[0]["pnl_usd"]) < 0
    assert env.risk(user)["cooldown_until_window"] == env.window() + 3


async def test_stale_stop_order_with_no_position_is_cancelled(env):
    user, _ = env.add_user()
    env.chain.add_resting_order(
        user.subaccount_id,
        RestingOrder("hash1", "cid1", OrderKind.STOP, Side.SHORT, D("0.01"), D("50000"), D("48000"), False),
    )
    await env.tick()
    assert env.orders(user) == []
    assert len(env.txs(purpose="cancel")) == 1


async def test_position_with_live_stop_left_alone(env):
    user = await enter_protected(env)
    txs_before = len(env.txs())
    await env.tick()
    assert len(env.txs()) == txs_before
    assert env.intent(user)["stage"] == "protected"


async def test_tp_placed_once_by_watcher_if_absent(env):
    user = await enter_protected(env)
    # Simulate a TP requested but not placed: place it through the normal path by faking the field.
    env.ctx.db.execute("UPDATE intents SET take_profit_price='60900' WHERE user_id=?", (user.id,))
    await env.tick()
    assert [r["purpose"] for r in env.txs()].count("take") == 1
    await env.tick()
    assert [r["purpose"] for r in env.txs()].count("take") == 1
    assert env.txs(purpose="take")[0]["status"] == "ok"


# ---------------------------------------------------------------------- missing stop


async def test_missing_stop_within_grace_does_nothing(env):
    user = await enter_protected(env)
    env.chain.sub(user.subaccount_id).orders.clear()
    manual(env)
    await env.tick()
    env.chain.advance_block(blocks=env.ctx.settings.stop_grace_blocks)
    await env.tick()
    assert len(env.txs(purpose="stop")) == 1  # no replacement yet
    assert env.alerts("stop_missing") == []


async def test_missing_stop_past_grace_is_replaced(env):
    user = await enter_protected(env)
    env.chain.sub(user.subaccount_id).orders.clear()
    manual(env)
    await env.tick()
    env.chain.advance_block(blocks=env.ctx.settings.stop_grace_blocks + 1)
    auto(env)
    await env.tick()
    assert env.intent(user)["stage"] == "protected"
    assert len(env.stop_orders(user)) == 1
    assert len(env.alerts("stop_missing")) == 1


async def test_missing_stop_that_cannot_be_placed_flattens(env):
    user = await enter_protected(env)
    env.chain.sub(user.subaccount_id).orders.clear()
    manual(env)
    await env.tick()
    env.chain.advance_block(blocks=env.ctx.settings.stop_grace_blocks + 1)
    auto(env)
    env.chain.inject(Failure.STOP_PLACEMENT, count=2)
    await env.tick()
    assert env.intent(user)["stage"] == "flattened"
    assert env.position(user) is None
    assert env.trades(user)[0]["close_reason"] == "flatten"
    assert len(env.alerts("flatten")) == 1


async def test_orphan_position_without_intent_is_flattened_after_grace(env):
    user, _ = env.add_user()
    # A position the service did not open: no intent, no stop.
    env.chain.register_account(user.inj_address, "0xsub-other", D("10"))
    from halo_trade.chain.mock import _Position

    env.chain.sub(user.subaccount_id).position = _Position(Side.LONG, D("0.001"), D("60000"), D("30"))
    manual(env)
    await env.tick()
    assert env.position(user) is not None
    env.chain.advance_block(blocks=env.ctx.settings.stop_grace_blocks + 1)
    auto(env)
    await env.tick()
    assert env.position(user) is None
    assert env.txs(purpose="flatten")[0]["status"] == "ok"
    assert len(env.alerts("stop_missing")) == 1


# ---------------------------------------------------------------------- trigger failure


async def test_thin_book_trigger_failure_within_grace_waits(env):
    user = await enter_protected(env)
    manual(env)
    env.chain.set_book_depth(bid=D("1"))
    env.chain.advance_block(mark=D("59000"))
    assert env.position(user) is not None  # stop triggered, fill failed
    await env.tick()
    env.chain.advance_block(blocks=env.ctx.settings.stop_grace_blocks - 1)
    await env.tick()
    assert env.txs(purpose="close") == []
    assert env.position(user) is not None


async def test_thin_book_trigger_failure_past_grace_closes_reduce_only(env):
    user = await enter_protected(env)
    manual(env)
    env.chain.set_book_depth(bid=D("1"))
    env.chain.advance_block(mark=D("59000"))
    assert env.position(user) is not None
    await env.tick()
    env.chain.advance_block(blocks=env.ctx.settings.stop_grace_blocks + 1, mark=D("58800"))
    env.chain.set_book_depth(bid=D("1000000"))
    auto(env)
    await env.tick(2)
    assert env.position(user) is None
    intent = env.intent(user)
    assert intent["stage"] == "closed"
    trade = env.trades(user)[0]
    assert trade["close_reason"] == "stop_trigger_failed"
    assert env.risk(user)["cooldown_until_window"] == env.window() + 3
    close = env.chain._signed[env.txs(purpose="close")[0]["tx_hash"]]
    assert close.orders[0].reduce_only is True
    assert len(env.alerts("stop_missing")) == 1


async def test_trigger_failure_close_keeps_retrying_while_thin(env):
    user = await enter_protected(env)
    manual(env)
    env.chain.set_book_depth(bid=D("1"))
    env.chain.advance_block(mark=D("59000"))
    await env.tick()
    env.chain.advance_block(blocks=env.ctx.settings.stop_grace_blocks + 1)
    auto(env)
    await env.tick()
    assert env.position(user) is not None  # still thin: nothing filled
    assert len(env.txs(purpose="close")) >= 1
    env.chain.set_book_depth(bid=D("1000000"))
    env.clock.advance(RETRY_WINDOW_MS)  # the retries inside the thin window used up this minute
    await env.tick()
    assert env.position(user) is None
    assert env.intent(user)["stage"] == "closed"


async def test_injected_stop_trigger_failure_is_closed_by_watcher(env):
    user = await enter_protected(env)
    manual(env)
    env.chain.inject(Failure.STOP_TRIGGER)
    env.chain.advance_block(mark=D("59000"))
    assert env.position(user) is not None
    assert env.chain.triggered[-1][1] is False
    await env.tick()
    env.chain.advance_block(blocks=env.ctx.settings.stop_grace_blocks + 1)
    auto(env)
    await env.tick(2)
    assert env.position(user) is None
    assert env.trades(user)[0]["close_reason"] == "stop_trigger_failed"


async def test_mark_recovering_inside_grace_clears_breach_and_stop_is_replaced(env):
    user = await enter_protected(env)
    manual(env)
    env.chain.set_book_depth(bid=D("1"))
    env.chain.advance_block(mark=D("59000"))
    await env.tick()
    env.chain.advance_block(mark=D("60000"))
    await env.tick()  # breach cleared; the stop is gone so the stopless clock starts
    env.chain.set_book_depth(bid=D("1000000"))
    env.chain.advance_block(blocks=env.ctx.settings.stop_grace_blocks + 1)
    auto(env)
    await env.tick()
    assert env.position(user) is not None
    assert env.txs(purpose="close") == []
    assert env.intent(user)["stage"] == "protected"
    assert len(env.stop_orders(user)) == 1


# ---------------------------------------------------------------------- equity and transfers


async def test_equity_snapshot_written_and_throttled(env):
    user, _ = env.add_user()
    await env.tick()
    await env.tick()
    assert env.ctx.db.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0] == 1
    env.clock.advance(SNAPSHOT_INTERVAL_MS)
    await env.tick()
    assert env.ctx.db.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0] == 2


async def test_deposit_while_flat_rebases_risk_state(env):
    user, _ = env.add_user()
    await env.tick()
    env.clock.advance(SNAPSHOT_INTERVAL_MS)
    env.chain.deposit(user.subaccount_id, D("50"))
    await env.tick()
    risk = env.risk(user)
    assert D(risk["day_start_equity"]) == D("150")
    assert D(risk["peak_equity"]) == D("150")
    state = await env.svc.state(user)
    assert state.can_open is True


async def test_withdrawal_while_flat_rebases_risk_state(env):
    user, _ = env.add_user()
    await env.tick()
    env.clock.advance(SNAPSHOT_INTERVAL_MS)
    env.chain.withdraw(user.subaccount_id, D("40"))
    await env.tick()
    risk = env.risk(user)
    assert D(risk["day_start_equity"]) == D("60")
    assert D(risk["peak_equity"]) == D("60")
    assert E.DAILY_LOSS_LIMIT not in (await env.svc.state(user)).blocked_reasons


async def test_deposit_cannot_clear_a_tripped_daily_loss_limit(env):
    user, _ = env.add_user()
    env.set_risk(user, "110", "110")
    await env.tick()
    env.clock.advance(SNAPSHOT_INTERVAL_MS)
    env.chain.deposit(user.subaccount_id, D("50"))
    await env.tick()
    assert E.DAILY_LOSS_LIMIT in (await env.svc.state(user)).blocked_reasons


async def test_small_equity_move_is_not_a_transfer(env):
    user, _ = env.add_user()
    await env.tick()
    env.clock.advance(SNAPSHOT_INTERVAL_MS)
    env.chain.deposit(user.subaccount_id, D("0.5"))
    await env.tick()
    assert D(env.risk(user)["day_start_equity"]) == D("100")


async def test_equity_jump_while_in_position_is_not_a_transfer(env):
    user = await enter_protected(env)
    await env.tick()
    env.chain.advance_block(mark=D("61000"))
    env.clock.advance(SNAPSHOT_INTERVAL_MS)
    await env.tick()
    assert D(env.risk(user)["day_start_equity"]) == D("100")


async def test_realized_loss_between_flat_snapshots_is_not_a_transfer(env):
    user, _ = env.add_user()
    await env.tick()  # flat snapshot at equity 100
    await env.enter(user)
    env.chain.advance_block(mark=D("59000"))  # stop hit: about a 1.6% realized loss
    await env.tick()
    env.clock.advance(SNAPSHOT_INTERVAL_MS)
    await env.tick()
    risk = env.risk(user)
    assert D(risk["day_start_equity"]) == D("100")
    assert D(risk["peak_equity"]) == D("100")


async def test_snapshot_does_not_overwrite_a_cooldown_set_during_its_chain_read(env):
    user, _ = env.add_user()
    await env.tick()
    env.clock.advance(SNAPSHOT_INTERVAL_MS)
    original = env.chain.account

    async def read_then_cooldown(granter, sub):
        result = await original(granter, sub)
        env.ctx.db.execute("UPDATE user_risk_state SET cooldown_until_window=? WHERE user_id=?", (env.window() + 9, user.id))
        return result

    env.chain.account = read_then_cooldown  # type: ignore[method-assign]
    env.watcher._tick_ms, env.watcher._tick_accounts = env.clock(), {}  # as a new tick would
    await env.watcher._snapshot_equity(user)
    env.chain.account = original  # type: ignore[method-assign]
    assert env.risk(user)["cooldown_until_window"] == env.window() + 9


async def test_day_rollover_rebases_day_start_but_not_peak(env):
    user, _ = env.add_user()
    env.set_risk(user, "120", "120", day_id=env.clock() // 86_400_000 - 1)
    await env.tick()
    risk = env.risk(user)
    assert D(risk["day_start_equity"]) == D("100")
    assert D(risk["peak_equity"]) == D("120")


async def test_peak_tracks_new_highs(env):
    user, _ = env.add_user()
    await env.tick()
    env.chain.deposit(user.subaccount_id, D("0.5"))  # below the transfer threshold, counts as gain
    env.clock.advance(SNAPSHOT_INTERVAL_MS)
    await env.tick()
    assert D(env.risk(user)["peak_equity"]) == D("100.5")


# ---------------------------------------------------------------------- alerts


async def test_unknown_tx_older_than_two_minutes_alerts_once(env):
    user, _ = env.add_user()
    manual(env)
    await env.enter(user)
    env.chain.advance_block()
    env.chain.inject(Failure.NEVER_INCLUDED)
    env.clock.advance(121_000)
    env.chain.fail_calls("lookup", count=1000)
    await env.tick()
    await env.tick()
    assert len(env.alerts("tx_unknown")) == 1


async def test_flatten_alert_is_deduplicated(env):
    user, _ = env.add_user()
    env.chain.inject(Failure.STOP_PLACEMENT, count=2)
    await env.enter(user)
    await env.tick()
    assert len(env.alerts("flatten")) == 1


async def test_grant_lost_alert(env):
    user = await enter_protected(env)
    env.chain.set_grant(user.subaccount_id, False, ("x",))
    await env.tick()
    assert len(env.alerts("grant_lost")) == 1
    await env.tick()
    assert len(env.alerts("grant_lost")) == 1


async def test_stale_heartbeat_detected_and_alerted(env):
    env.add_user()
    await env.tick()
    env.clock.advance(90_000)
    assert await check_watcher_heartbeat(env.ctx) is True
    assert len(env.alerts("watcher_stale")) == 1


async def test_fresh_heartbeat_not_alerted(env):
    env.add_user()
    await env.tick()
    assert await check_watcher_heartbeat(env.ctx) is False
    assert env.alerts("watcher_stale") == []


async def test_tick_writes_heartbeat_row(env):
    env.add_user()
    await env.tick()
    row = env.ctx.db.execute("SELECT value FROM system_state WHERE key=?", (HEARTBEAT_KEY,)).fetchone()
    assert int(row["value"]) == env.clock()


async def test_tick_survives_chain_outage_and_keeps_heartbeating(env):
    user = await enter_protected(env)
    env.chain.go_down()
    await env.tick()
    row = env.ctx.db.execute("SELECT value FROM system_state WHERE key=?", (HEARTBEAT_KEY,)).fetchone()
    assert row is not None
    assert env.position(user) is not None  # nothing invented during the outage
    env.chain.come_back()


async def test_one_users_chain_error_does_not_block_others(env):
    a = await enter_protected(env)
    env.next_window()
    env.chain.advance_block()
    b, _ = env.add_user()
    out = await env.enter(b)
    assert out.outcome is Outcome.FILLED
    env.chain.fail_calls("account", count=1)
    await env.tick()
    assert env.position(a) is not None and env.position(b) is not None


async def test_paused_and_revoked_users_still_watched_when_holding_intents(env):
    user = await enter_protected(env)
    env.ctx.db.execute("UPDATE users SET status='revoked' WHERE id=?", (user.id,))
    env.chain.advance_block(mark=D("59000"))
    await env.settle()
    assert env.intent(user)["stage"] == "closed"


async def test_revoked_user_with_no_intent_not_watched(env):
    user, _ = env.add_user(status="revoked")
    await env.tick()
    assert env.ctx.db.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0] == 0


async def test_watcher_skips_user_while_request_holds_lock(env):
    user = await enter_protected(env)
    env.chain.sub(user.subaccount_id).orders.clear()  # would normally start the stopless clock
    txs_before = len(env.txs())
    async with env.svc.user_lock(user.id):
        await env.tick()
    assert env.svc._kv_get(f"stopless:{user.id}") is None
    assert len(env.txs()) == txs_before


async def test_entry_pending_after_crash_is_failed_not_retried(env):
    from halo_trade.txmachine import transaction

    user, _ = env.add_user()
    now = env.clock()
    async with env.ctx.db_lock:
        with transaction(env.ctx.db) as conn:
            conn.execute(
                "INSERT INTO intents (user_id, window_id, ticket_id, side, quantity, stop_price, stage, created_ms, updated_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                (user.id, env.window(), "ticket-x", "long", "0.001", "59000", "entry_pending", now, now),
            )
    await env.tick()
    assert env.intent(user)["stage"] == "failed"
    assert env.chain.signed_log == []


async def test_entry_landing_after_its_window_closed_is_still_protected(env):
    user, _ = env.add_user()
    manual(env)
    await env.enter(user)  # broadcast accepted, in the mempool, unresolved
    env.next_window()
    env.chain.advance_block(blocks=env.chain.tx_timeout_blocks + 1)  # included in the first block
    auto(env)
    await env.tick(2)
    # It landed, so it must be protected, not abandoned because its window closed.
    assert env.intent(user)["stage"] == "protected"
    assert len(env.stop_orders(user)) == 1
    assert len(env.txs(purpose="entry")) == 1


async def test_position_after_resolved_failed_entry_is_treated_as_orphan_and_flattened(env):
    # An intent marked failed whose position nevertheless exists can never be left unprotected.
    user, _ = env.add_user()
    await env.enter(user)
    env.ctx.db.execute("UPDATE intents SET stage='failed' WHERE user_id=?", (user.id,))
    env.chain.sub(user.subaccount_id).orders.clear()
    manual(env)
    await env.tick()
    env.chain.advance_block(blocks=env.ctx.settings.stop_grace_blocks + 1)
    auto(env)
    await env.tick()
    assert env.position(user) is None


async def test_cooldown_window_arithmetic(env):
    user = await enter_protected(env)
    stop_window = env.window()
    env.chain.advance_block(mark=D("59000"))
    await env.settle()
    assert env.risk(user)["cooldown_until_window"] == stop_window + 1 + env.ctx.settings.risk.cooldown_windows


async def test_watcher_never_starts_an_entry(env):
    user, _ = env.add_user()
    await env.preview(user)  # a ticket exists but nothing was submitted
    await env.tick(3)
    assert env.chain.signed_log == []
    assert env.intents(user) == []


# ---------------------------------------------------------------------- retention


def _insert_observation(env, user, row_id, age_ms):
    created = env.clock() - age_ms
    env.ctx.db.execute(
        "INSERT INTO observations (id, user_id, window_id, mark, as_of_ms, created_ms) VALUES (?,?,?,?,?,?)",
        (row_id, user.id, created // WINDOW_MS, "60000", created, created),
    )


def _insert_ticket(env, user, row_id, age_ms, used=False, expired=True):
    created, now = env.clock() - age_ms, env.clock()
    env.ctx.db.execute(
        "INSERT INTO tickets (id, user_id, window_id, observation_id, side, quantity, mark, stop_price, stop_atr, expires_ms, used_ms, created_ms) "
        "VALUES (?,?,?,'obs','long','0.0017','60000','59400','2',?,?,?)",
        (row_id, user.id, created // WINDOW_MS, now - 1 if expired else now + 60_000, created if used else None, created),
    )


def _ids(env, table):
    return {r["id"] for r in env.ctx.db.execute(f"SELECT id FROM {table}")}


async def test_purge_drops_only_old_observations_and_old_spent_tickets(env):
    user, _ = env.add_user()
    old = PURGE_AGE_MS + 1
    _insert_observation(env, user, "obs_old", old)
    _insert_observation(env, user, "obs_exactly_7d", PURGE_AGE_MS)
    _insert_observation(env, user, "obs_new", 3_600_000)
    _insert_ticket(env, user, "old_used", old, used=True, expired=False)
    _insert_ticket(env, user, "old_expired", old)
    _insert_ticket(env, user, "old_live", old, expired=False)  # neither used nor expired
    _insert_ticket(env, user, "used_exactly_7d", PURGE_AGE_MS, used=True)
    _insert_ticket(env, user, "new_used", 3_600_000, used=True)
    _insert_ticket(env, user, "new_expired", 3_600_000)
    await env.tick()
    assert _ids(env, "observations") == {"obs_exactly_7d", "obs_new"}
    assert _ids(env, "tickets") == {"old_live", "used_exactly_7d", "new_used", "new_expired"}


async def test_purge_runs_at_most_once_an_hour_even_across_a_restart(env):
    user, _ = env.add_user()
    await env.tick()  # this one purges and starts the hour
    _insert_observation(env, user, "o1", PURGE_AGE_MS + 1)
    _insert_ticket(env, user, "t1", PURGE_AGE_MS + 1, used=True)
    env.clock.advance(PURGE_INTERVAL_MS - 1)
    env.restart()
    await env.tick()
    assert _ids(env, "observations") == {"o1"} and _ids(env, "tickets") == {"t1"}
    env.clock.advance(1)
    await env.tick()
    assert _ids(env, "observations") == set() and _ids(env, "tickets") == set()


async def test_purge_leaves_a_protected_position_and_its_history_alone(env):
    user = await enter_protected(env)
    assert _ids(env, "observations") and _ids(env, "tickets")
    env.clock.advance(PURGE_AGE_MS + PURGE_INTERVAL_MS)
    env.chain.advance_block()
    await env.tick()
    assert _ids(env, "observations") == set() and _ids(env, "tickets") == set()
    assert env.intent(user)["stage"] == "protected"
    assert env.position(user) is not None and len(env.stop_orders(user)) == 1
    assert env.ctx.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] > 0
