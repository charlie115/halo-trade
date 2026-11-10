"""The presets' rules in code, the pack the model always gets, and what the server does with them."""

from __future__ import annotations

import asyncio
import json
from typing import Optional

import httpx
import pytest

from halo_trade import limits, onboarding
from halo_trade.contracts import AccountOut, ErrorCode, FeaturesOut, MarketOut, PositionOut, StateOut
from halo_trade.strategies import PRESET_EXITS, PRESETS, evaluate

from tests.conftest import rising_candles
from tests.test_onboarding_api import Bench


CHECK_IDS = {"trend", "momentum_1h", "momentum_4h", "higher_timeframe", "stretched", "volatility", "event_risk", "fomc", "funding"}
CLOSED_VALUES = {"up", "down", "flat", "none", "elevated", "high"}


def a_state(*, trend="up", ret_1h=0.3, ret_4h=0.8, ret_24h=1.0, atr_pct=0.4, volatility=1.0, side: Optional[str] = None) -> StateOut:
    features = FeaturesOut(
        atr_1h=240, atr_1h_pct=atr_pct, ema20_1h=60100, ema50_1h=59900, trend=trend, ret_1h_pct=ret_1h, ret_4h_pct=ret_4h, ret_24h_pct=ret_24h,
        volatility_ratio=volatility, round_trip_cost_bps=2.0,
    )
    position = None if side is None else PositionOut(side=side, quantity=0.1, entry_price=60000, unrealized_pnl_usd=1.0, stop_price=59000, take_profit_price=None, windows_held=1)
    return StateOut(
        server_time_ms=1, window_id=1, next_window_ms=2, observation_id="obs-00000", market=MarketOut(ticker="BTC/USDC PERP", mark_price=60000, oracle_price=60000, best_bid=59999, best_ask=60001, spread_bps=0.3, funding_rate_bps=0.5),
        features=features, account=AccountOut(equity_usd=100, available_usd=100, day_pnl_pct=0, drawdown_pct=0), position=position, can_open=position is None, blocked_reasons=[],
    )


def a_view(*, event_risk="none", fomc=48.0, funding=1.0) -> dict:
    return {"event_risk": event_risk, "fomc_in_hours": fomc, "funding_avg_bps": funding}


def a_pack(trend4h: Optional[str] = "up") -> dict:
    return {"frames": {"4h": {"trend": trend4h, "rsi_14": 55.0, "range_position": 0.7}}, "book": {"spread_bps": 0.3, "imbalance": 0.1}}


def check_of(setup, check_id: str) -> dict:
    return next(check for check in setup.checks if check["id"] == check_id)


def with_core(state: StateOut, **changed) -> StateOut:
    return state.model_copy(update={"features": state.features.model_copy(update=changed)})


# ---------------------------------------------------------------- steady
def test_steady_long_needs_the_trend_and_both_returns():
    setup = evaluate("steady", a_state(), a_view(), a_pack())
    assert (setup.side, setup.ready, setup.stop_atr, setup.take_profit_atr, setup.exit_signal) == ("long", True, 3.0, 4.0, None)
    assert [check["id"] for check in setup.checks] == ["trend", "momentum_1h", "momentum_4h", "higher_timeframe", "event_risk", "fomc", "funding"]


def test_steady_short_is_the_mirror():
    setup = evaluate("steady", a_state(trend="down", ret_1h=-0.3, ret_4h=-0.8), a_view(), a_pack("down"))
    assert (setup.side, setup.ready, setup.stop_atr, setup.take_profit_atr) == ("short", True, 3.0, 4.0)


@pytest.mark.parametrize("changes", [{"trend": "flat"}, {"ret_1h": 0.0}, {"ret_1h": -0.1}, {"ret_4h": -0.2}, {"trend": "down"}])
def test_steady_holds_when_a_signal_disagrees(changes):
    setup = evaluate("steady", a_state(**changes), a_view(), a_pack())
    assert (setup.side, setup.ready, setup.stop_atr, setup.take_profit_atr) == (None, False, None, None)


def test_a_flat_trend_gives_a_directionless_check_null_not_false():
    setup = evaluate("steady", a_state(trend="flat"), a_view(), a_pack())
    assert check_of(setup, "trend")["ok"] is False
    assert check_of(setup, "momentum_1h")["ok"] is None and check_of(setup, "momentum_1h")["value"] == 0.3
    assert check_of(setup, "higher_timeframe")["ok"] is None and check_of(setup, "funding")["ok"] is None


@pytest.mark.parametrize(
    "check_id, view, pack",
    [
        ("event_risk", a_view(event_risk="high"), a_pack()),
        ("fomc", a_view(fomc=5.5), a_pack()),
        ("funding", a_view(funding=6.0), a_pack()),
        ("higher_timeframe", a_view(), a_pack("down")),
    ],
    ids=["event_risk", "fomc", "crowded_funding", "4h_against"],
)
def test_each_steady_filter_blocks_a_long(check_id, view, pack):
    setup = evaluate("steady", a_state(), view, pack)
    assert setup.side == "long" and setup.ready is False  # the candidate stays visible, blocked
    assert check_of(setup, check_id)["ok"] is False
    assert all(check["ok"] is not False for check in setup.checks if check["id"] != check_id)


def test_steady_short_is_blocked_by_funding_crowded_the_other_way():
    setup = evaluate("steady", a_state(trend="down", ret_1h=-0.3, ret_4h=-0.8), a_view(funding=-6.0), a_pack("down"))
    assert setup.side == "short" and setup.ready is False
    assert check_of(setup, "funding")["ok"] is False


def test_funding_exactly_at_the_limit_does_not_block():
    assert evaluate("steady", a_state(), a_view(funding=5.0), a_pack()).ready is True


def test_a_fomc_at_exactly_six_hours_does_not_block():
    assert evaluate("steady", a_state(), a_view(fomc=6.0), a_pack()).ready is True


def test_steady_is_not_stopped_by_a_flat_higher_timeframe():
    setup = evaluate("steady", a_state(), a_view(), a_pack("flat"))
    assert setup.ready is True and check_of(setup, "higher_timeframe")["ok"] is True


def test_steady_exit_signal_follows_the_trend_against_the_position():
    assert evaluate("steady", a_state(trend="down", ret_1h=-0.1, ret_4h=-0.2, side="long"), a_view(), a_pack("down")).exit_signal is True
    assert evaluate("steady", a_state(trend="up", side="long"), a_view(), a_pack("up")).exit_signal is False
    assert evaluate("steady", a_state(trend="flat", side="long"), a_view(), a_pack()).exit_signal is False
    assert evaluate("steady", a_state(trend="up", side="short"), a_view(), a_pack()).exit_signal is True


def test_a_position_does_not_change_what_the_setup_says():
    setup = evaluate("steady", a_state(side="long"), a_view(), a_pack())
    assert setup.side == "long" and setup.ready is True  # `can_open` and the server limits govern the entry


# ---------------------------------------------------------------- trend
def test_trend_side_follows_the_trend_with_stop_two():
    setup = evaluate("trend", a_state(ret_4h=-0.1), a_view(), a_pack())
    assert (setup.side, setup.ready, setup.stop_atr, setup.take_profit_atr) == ("long", True, 2.0, 3.0)
    assert evaluate("trend", a_state(trend="down", ret_4h=0.1), a_view(), a_pack("down")).side == "short"
    assert evaluate("trend", a_state(trend="flat"), a_view(), a_pack()).side is None


def test_trend_take_profit_is_four_only_with_the_4h_return_and_uncrowded_funding():
    assert evaluate("trend", a_state(ret_4h=0.8), a_view(funding=1.0), a_pack()).take_profit_atr == 4.0
    assert evaluate("trend", a_state(ret_4h=-0.1), a_view(funding=1.0), a_pack()).take_profit_atr == 3.0
    assert evaluate("trend", a_state(ret_4h=0.8), a_view(funding=6.0), a_pack()).take_profit_atr == 3.0
    assert evaluate("trend", a_state(trend="down", ret_4h=-0.8), a_view(funding=-6.0), a_pack("down")).take_profit_atr == 3.0
    assert evaluate("trend", a_state(trend="down", ret_4h=-0.8), a_view(funding=6.0), a_pack("down")).take_profit_atr == 4.0


def test_trend_with_unknown_funding_takes_the_longer_target():
    assert evaluate("trend", a_state(ret_4h=0.8), a_view(funding=None), a_pack()).take_profit_atr == 4.0


def test_crowded_funding_does_not_block_a_trend_trade():
    setup = evaluate("trend", a_state(), a_view(funding=9.0), a_pack())
    assert setup.ready is True and setup.side == "long"
    assert all(check["id"] != "funding" for check in setup.checks)


@pytest.mark.parametrize(
    "check_id, view, pack",
    [("event_risk", a_view(event_risk="high"), a_pack()), ("fomc", a_view(fomc=2.0), a_pack()), ("higher_timeframe", a_view(), a_pack("down"))],
    ids=["event_risk", "fomc", "4h_against"],
)
def test_each_trend_filter_blocks(check_id, view, pack):
    setup = evaluate("trend", a_state(), view, pack)
    assert setup.side == "long" and setup.ready is False
    assert check_of(setup, check_id)["ok"] is False


def test_trend_exit_signal():
    assert evaluate("trend", a_state(trend="down", side="long"), a_view(), a_pack("down")).exit_signal is True
    assert evaluate("trend", a_state(trend="up", side="long"), a_view(), a_pack()).exit_signal is False
    assert evaluate("trend", a_state(), a_view(), a_pack()).exit_signal is None


# ---------------------------------------------------------------- reversal
STRETCHED_UP = {"ret_24h": 4.0, "atr_pct": 0.5, "volatility": 1.5, "trend": "up"}
STRETCHED_DOWN = {"ret_24h": -4.0, "atr_pct": 0.5, "volatility": 1.5, "trend": "down"}


def test_reversal_shorts_a_stretched_rise_and_buys_a_stretched_fall():
    short = evaluate("reversal", a_state(**STRETCHED_UP), a_view(), a_pack())
    assert (short.side, short.ready, short.stop_atr, short.take_profit_atr) == ("short", True, 2.0, 2.0)
    long = evaluate("reversal", a_state(**STRETCHED_DOWN), a_view(), a_pack())
    assert (long.side, long.ready, long.stop_atr, long.take_profit_atr) == ("long", True, 2.0, 2.0)
    assert [check["id"] for check in short.checks] == ["stretched", "volatility", "event_risk", "funding"]
    assert check_of(short, "stretched")["value"] == 8.0


@pytest.mark.parametrize(
    "changes",
    [{"ret_24h": 2.0}, {"volatility": 1.2}, {"ret_24h": -2.9, "atr_pct": 0.5}],
    ids=["not_stretched", "not_elevated", "fall_not_stretched"],
)
def test_reversal_needs_both_stretched_and_elevated(changes):
    setup = evaluate("reversal", a_state(**{**STRETCHED_UP, **changes}), a_view(), a_pack())
    assert (setup.side, setup.ready, setup.stop_atr, setup.take_profit_atr) == (None, False, None, None)


def test_reversal_stretched_at_exactly_six_atr_counts():
    assert evaluate("reversal", a_state(ret_24h=3.0, atr_pct=0.5, volatility=1.5), a_view(), a_pack()).ready is True


def test_reversal_event_risk_blocks():
    setup = evaluate("reversal", a_state(**STRETCHED_UP), a_view(event_risk="high"), a_pack())
    assert setup.side == "short" and setup.ready is False


def test_reversal_is_blocked_by_funding_crowded_against_the_fade():
    long = evaluate("reversal", a_state(**STRETCHED_DOWN), a_view(funding=6.0), a_pack())
    assert long.side == "long" and long.ready is False and check_of(long, "funding")["ok"] is False
    short = evaluate("reversal", a_state(**STRETCHED_UP), a_view(funding=-6.0), a_pack())
    assert short.side == "short" and short.ready is False
    # Funding crowded the way of the fade is not what blocks it.
    assert evaluate("reversal", a_state(**STRETCHED_DOWN), a_view(funding=-6.0), a_pack()).ready is True


def test_reversal_ignores_fomc_and_the_4h_trend():
    assert evaluate("reversal", a_state(**STRETCHED_UP), a_view(fomc=1.0), a_pack("up")).ready is True
    setup = evaluate("reversal", a_state(**STRETCHED_DOWN), a_view(fomc=1.0), a_pack("down"))
    assert setup.ready is True and all(check["id"] not in ("fomc", "higher_timeframe") for check in setup.checks)


def test_reversal_exit_signal_fires_only_when_the_original_move_resumes():
    assert evaluate("reversal", a_state(ret_1h=-0.3, side="long"), a_view(), a_pack()).exit_signal is True
    assert evaluate("reversal", a_state(ret_1h=0.3, side="long"), a_view(), a_pack()).exit_signal is False
    assert evaluate("reversal", a_state(ret_1h=0.3, side="short"), a_view(), a_pack()).exit_signal is True
    assert evaluate("reversal", a_state(ret_1h=-0.3, side="short"), a_view(), a_pack()).exit_signal is False
    assert evaluate("reversal", a_state(ret_1h=-0.3), a_view(), a_pack()).exit_signal is None


# ---------------------------------------------------------------- null and missing
def test_null_context_gives_null_checks_and_blocks_nothing():
    view = {"event_risk": None, "fomc_in_hours": None, "funding_avg_bps": None}
    setup = evaluate("steady", a_state(), view, a_pack(None))
    assert setup.ready is True
    for check_id in ("event_risk", "fomc", "funding", "higher_timeframe"):
        assert check_of(setup, check_id)["ok"] is None and check_of(setup, check_id)["value"] is None
    reversal = evaluate("reversal", a_state(**STRETCHED_UP), {}, a_pack(None))
    assert reversal.ready is True and check_of(reversal, "event_risk")["ok"] is None and check_of(reversal, "funding")["ok"] is None


def test_a_missing_pack_reads_as_null_context():
    setup = evaluate("trend", a_state(), a_view(), None)
    assert setup.ready is True and check_of(setup, "higher_timeframe")["ok"] is None


@pytest.mark.parametrize("field, value", [("ret_1h_pct", None), ("ret_1h_pct", float("nan")), ("ret_4h_pct", float("inf")), ("trend", None), ("trend", "sideways")])
def test_a_missing_core_signal_means_no_setup(field, value):
    setup = evaluate("steady", with_core(a_state(), **{field: value}), a_view(), a_pack())
    assert (setup.side, setup.ready, setup.stop_atr, setup.take_profit_atr) == (None, False, None, None)


@pytest.mark.parametrize("field, value", [("ret_24h_pct", None), ("atr_1h_pct", None), ("atr_1h_pct", 0.0), ("volatility_ratio", float("nan"))])
def test_reversal_with_a_missing_core_signal_has_no_setup(field, value):
    setup = evaluate("reversal", with_core(a_state(**STRETCHED_UP), **{field: value}), a_view(), a_pack())
    assert setup.side is None and setup.ready is False


# ---------------------------------------------------------------- the shape
@pytest.mark.parametrize("preset", list(PRESETS))
@pytest.mark.parametrize("changes", [{}, {"trend": "down", "ret_1h": -0.3, "ret_4h": -0.8}, {"trend": "flat"}, {"side": "long"}])
def test_every_check_uses_the_closed_list(preset, changes):
    setup = evaluate(preset, a_state(**changes), a_view(), a_pack())
    for check in setup.checks:
        assert set(check) == {"id", "ok", "value"}
        assert check["id"] in CHECK_IDS
        assert check["ok"] in (True, False, None)
        assert check["value"] is None or isinstance(check["value"], (int, float)) or check["value"] in CLOSED_VALUES
    json.dumps(setup.as_json())  # the payload and the snapshot are JSON


def test_the_setup_is_frozen():
    setup = evaluate("steady", a_state(), a_view(), a_pack())
    with pytest.raises(Exception):
        setup.ready = False  # type: ignore[misc]


@pytest.mark.parametrize(
    "preset, state_kwargs",
    [("steady", {}), ("trend", {}), ("reversal", STRETCHED_UP)],
)
def test_the_code_uses_the_exits_the_text_gives(preset, state_kwargs):
    setup = evaluate(preset, a_state(**state_kwargs), a_view(), a_pack())
    assert setup.stop_atr == PRESET_EXITS[preset]["stop_atr"]
    assert setup.take_profit_atr in PRESET_EXITS[preset]["take_profit_atr"]


def test_the_text_says_what_the_code_does_for_the_new_filter():
    assert "frames.4h.trend" in PRESETS["steady"].instructions and "frames.4h.trend" in PRESETS["trend"].instructions
    assert "frames.4h.trend" not in PRESETS["reversal"].instructions


def test_the_new_code_is_a_real_code_with_a_status():
    assert ErrorCode.SETUP_NOT_MET.value == "SETUP_NOT_MET"
    assert limits.STATUS_BY_CODE[ErrorCode.SETUP_NOT_MET] == 409


# ---------------------------------------------------------------- the owner's page
@pytest.fixture
def b() -> Bench:
    return Bench()


async def test_stats_are_all_null_before_any_run(b):
    wallet, session = await b.onboarded()
    assert (await b.agent(wallet, session)).json()["stats"] == dict.fromkeys(onboarding.STAT_KEYS)


async def test_stats_with_no_trades_have_real_zero_counts_and_null_rates(b):
    wallet, session = await b.started()
    stats = (await b.agent(wallet, session)).json()["stats"]
    assert (stats["trades"], stats["wins"], stats["losses"]) == (0, 0, 0)
    assert stats["win_rate"] is None and stats["avg_r"] is None and stats["avg_hold_ms"] is None and stats["max_drawdown_pct"] is None


async def test_stats_cover_the_newest_run_only(b):
    wallet, session = await b.started()
    uid = b.user(wallet)["id"]
    # An old trade, before this run started, must not count.
    b.ctx.db.execute("INSERT INTO intents(id,user_id,window_id,ticket_id,side,quantity,stop_price,stage,created_ms,updated_ms) VALUES(1,?,1,'tk','long','1','59000','closed',1,1)", (uid,))
    b.ctx.db.execute("INSERT INTO trades(user_id,intent_id,side,quantity,entry_price,pnl_usd,opened_ms,closed_ms) VALUES(?,1,'long','1','60000','5000',1,2)", (uid,))
    started = b.ctx.db.execute("SELECT started_ms FROM strategy_runs WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()["started_ms"]
    for index, pnl, stop in ((2, "1000", "59000"), (3, "-500", "59000"), (4, None, "59000"), (5, "2000", "59000")):
        b.ctx.db.execute(
            "INSERT INTO intents(id,user_id,window_id,ticket_id,side,quantity,stop_price,stage,created_ms,updated_ms) VALUES(?,?,?,?,?,?,?,'closed',1,1)",
            (index, uid, index, f"t{index}", "long", "1", stop),
        )
        b.ctx.db.execute(
            "INSERT INTO trades(user_id,intent_id,side,quantity,entry_price,pnl_usd,close_reason,opened_ms,closed_ms) VALUES(?,?,'long','1','60000',?,'stop',?,?)",
            (uid, index, pnl, started + index * 1000, started + index * 1000 + 600_000),
        )
    b.clock.set(started + 20 * 60_000)  # past the last close, within the session's life
    stats = (await b.agent(wallet, session)).json()["stats"]
    assert (stats["trades"], stats["wins"], stats["losses"]) == (4, 2, 1)
    assert stats["win_rate"] == round(2 / 3, 3)
    assert (stats["avg_r"], stats["best_r"], stats["worst_r"]) == (round(2.5 / 3, 2), 2.0, -0.5)  # the old trade's 5R is not here
    assert stats["avg_hold_ms"] == 600_000
    assert stats["max_drawdown_pct"] is None  # too little history to say


def stop_the_run(b, uid: int, at_ms: int) -> None:
    """Ends the newest run, so its equity history is what the page reports, not a live read of the mock."""
    b.ctx.db.execute("UPDATE strategy_runs SET status='stopped', stop_reason='user', stopped_ms=? WHERE user_id=?", (at_ms, uid))


async def test_max_drawdown_is_net_of_transfers(b):
    wallet, session = await b.started()
    uid = b.user(wallet)["id"]
    started = b.ctx.db.execute("SELECT started_ms FROM strategy_runs WHERE user_id=?", (uid,)).fetchone()["started_ms"]
    minute = 60_000
    # Flat, 200 is deposited, then an open position takes the account from 350 down to 280: 20%.
    rows = [(150, 1, 150)] * 20 + [(350, 1, 350)] * 20 + [(280, 0, 350)] * 20
    for index, (equity, flat, balance) in enumerate(rows):
        b.ctx.db.execute("INSERT INTO equity_snapshots(user_id,ts_ms,equity,flat,balance) VALUES(?,?,?,?,?)", (uid, started + (index + 1) * 5 * minute, str(equity), flat, str(balance)))
    stop_the_run(b, uid, started + 301 * minute)
    b.clock.set(started + 301 * minute)
    session = await b.session(wallet)  # the first one has expired by now
    stats = (await b.agent(wallet, session)).json()["stats"]
    assert stats["max_drawdown_pct"] == 20.0


async def test_a_withdrawal_is_not_a_drawdown(b):
    wallet, session = await b.started()
    uid = b.user(wallet)["id"]
    started = b.ctx.db.execute("SELECT started_ms FROM strategy_runs WHERE user_id=?", (uid,)).fetchone()["started_ms"]
    minute = 60_000
    for index, equity in enumerate([150] * 30 + [50] * 30):
        b.ctx.db.execute("INSERT INTO equity_snapshots(user_id,ts_ms,equity,flat,balance) VALUES(?,?,?,1,?)", (uid, started + (index + 1) * 5 * minute, str(equity), str(equity)))
    stop_the_run(b, uid, started + 301 * minute)
    b.clock.set(started + 301 * minute)
    session = await b.session(wallet)
    assert (await b.agent(wallet, session)).json()["stats"]["max_drawdown_pct"] == 0.0


async def test_additive_fields_do_not_disturb_the_rest_of_the_view(b):
    wallet, session = await b.started()
    body = (await b.agent(wallet, session)).json()
    for key in ("ok", "status", "halted_by_operator", "preset", "next_run_ms", "now_ms", "last_checked_ms", "explorer_tx", "run", "position", "daily_loss", "performance", "decisions", "trades", "market", "telegram", "stats", "memory"):
        assert key in body, key


# ---------------------------------------------------------------- public market data
def set_hours(b, hours: int) -> None:
    b.chain.set_candles(rising_candles(b.clock(), hours=hours, ahead=0))


async def test_candles_shape_oldest_first_with_the_open_bar(b):
    set_hours(b, 300)
    response = await b.client.get("/onboard/candles")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["period_ms"] == 3_600_000 and isinstance(body["as_of_ms"], int) and isinstance(body["mark"], str)
    candles = body["candles"]
    assert len(candles) == 168
    assert all(len(candle) == 5 and isinstance(candle[0], int) and all(isinstance(price, str) for price in candle[1:]) for candle in candles)
    assert [candle[0] for candle in candles] == sorted(candle[0] for candle in candles)
    # The bar still open is the current hour's: it has no close yet to speak of, but it is here.
    assert candles[-1][0] == b.clock() - b.clock() % 3_600_000


async def test_candles_are_served_from_cache_for_about_thirty_seconds(b):
    set_hours(b, 200)
    calls = []
    real = b.chain.live_candles_1h

    async def counted(limit):
        calls.append(limit)
        return await real(limit)

    b.chain.live_candles_1h = counted  # type: ignore[method-assign]
    first = (await b.client.get("/onboard/candles")).json()
    second = (await b.client.get("/onboard/candles")).json()
    assert len(calls) == 1 and first == second
    b.clock.advance(29_000)
    await b.client.get("/onboard/candles")
    assert len(calls) == 1
    b.clock.advance(2_000)
    await b.client.get("/onboard/candles")
    assert len(calls) == 2


async def test_concurrent_candle_polls_are_one_chain_read(b):
    set_hours(b, 200)
    calls = []
    real = b.chain.live_candles_1h

    async def counted(limit):
        calls.append(limit)
        await asyncio.sleep(0.01)
        return await real(limit)

    b.chain.live_candles_1h = counted  # type: ignore[method-assign]
    bodies = await asyncio.gather(*[b.client.get("/onboard/candles") for _ in range(20)])
    assert all(body.status_code == 200 for body in bodies)
    assert len(calls) == 1


async def test_candles_when_the_chain_is_down_are_empty_never_a_500(b):
    b.chain.go_down()
    response = await b.client.get("/onboard/candles")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["candles"] == [] and body["mark"] is None and isinstance(body["as_of_ms"], int)


async def test_a_candle_read_failing_alone_leaves_the_mark(b):
    set_hours(b, 200)
    b.chain.fail_calls("live_candles_1h", count=1)
    body = (await b.client.get("/onboard/candles")).json()
    assert body["candles"] == [] and body["mark"] is not None


async def test_candles_are_rate_limited_per_client_with_the_usual_error_shape(b):
    set_hours(b, 200)
    for _ in range(onboarding.CANDLES_PER_MIN):
        assert (await b.client.get("/onboard/candles")).status_code == 200
    blocked = await b.client.get("/onboard/candles")
    assert blocked.status_code == 429 and blocked.json()["code"] == "RATE_LIMITED" and blocked.json()["ok"] is False


async def test_demo_serves_what_the_root_page_serves_with_the_same_headers(b):
    root = await b.client.get("/")
    demo = await b.client.get("/demo")
    assert demo.status_code == root.status_code
    assert demo.content == root.content
    for name in ("content-security-policy", "x-content-type-options", "referrer-policy"):
        assert demo.headers.get(name) == root.headers.get(name)


async def test_the_injective_backend_keeps_the_open_bar_only_on_the_live_read(monkeypatch):
    from tests.test_injective_unit import make

    now_ms = 1_794_152_000_000
    hour = 3_600_000
    bar = now_ms - now_ms % hour

    class Http:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params):
            times = [(bar - hour) // 1000, bar // 1000]
            return httpx.Response(200, json={"t": times, "o": [1, 2], "h": [1, 2], "l": [1, 2], "c": [1, 2]}, request=httpx.Request("GET", url))

    monkeypatch.setattr("halo_trade.chain.injective.httpx.AsyncClient", Http)
    backend = make(now_ms=now_ms)
    assert [candle.open_ms for candle in await backend.candles_1h(5)] == [bar - hour]
    assert [candle.open_ms for candle in await backend.live_candles_1h(5)] == [bar - hour, bar]
