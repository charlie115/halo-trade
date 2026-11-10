"""Each entry check rejects on its own, and the spec order holds."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from halo_trade import limits as L
from halo_trade.contracts import ErrorCode as E

NOW = 1_800_000_060_000
WINDOW = NOW // 900_000


def base(**over) -> L.EntryInputs:
    fields = dict(
        now_ms=NOW,
        window_id=WINDOW,
        user_id=1,
        kill_switch=False,
        user_status="active",
        grant_ok=True,
        gas_ok=True,
        market_as_of_ms=NOW - 1_000,
        observation=L.ObservationView(user_id=1, window_id=WINDOW, created_ms=NOW - 1_000),
        side="long",
        stop_atr=D("2"),
        take_profit_atr=D("3"),
        has_position=False,
        window_used=False,
        cooldown_until_window=0,
        equity=D("100"),
        day_start_equity=D("100"),
        peak_equity=D("100"),
        daily_loss_limit=D("0.06"),
        max_drawdown=D("0.30"),
        max_equity_usd=D("400"),
        take_profit_bps=D("150"),
        round_trip_cost_bps=D("12"),
        min_tp_cost_mult=D("3"),
        quantity=D("0.0025"),
        notional=D("150"),
        min_notional=D("5"),
        margin_needed=D("75"),
        available=D("100"),
        book_depth_usd=D("10000"),
        exit_depth_usd=D("10000"),
        stop_price=D("59000"),
        stop_worst_notional=D("140"),
        worst_loss=D("0"),
    )
    fields.update(over)
    return L.EntryInputs(**fields)


def test_baseline_passes():
    assert L.first_rejection(base()) is None


# Inputs that make exactly one check fail, keyed by the code it must produce.
SINGLE_FAILURES = [
    (L.check_kill_switch, E.PAUSED, dict(kill_switch=True)),
    (L.check_user_status, E.PAUSED, dict(user_status="paused")),
    (L.check_user_status, E.UNAUTHORIZED, dict(user_status="revoked")),
    (L.check_grant, E.GRANT_MISSING, dict(grant_ok=False)),
    (L.check_grantee_gas, E.GRANTEE_LOW_GAS, dict(gas_ok=False)),
    (L.check_market_fresh, E.MARKET_UNAVAILABLE, dict(market_as_of_ms=None)),
    (L.check_market_fresh, E.MARKET_UNAVAILABLE, dict(market_as_of_ms=NOW - 30_000)),
    (L.check_observation, E.STALE_OBSERVATION, dict(observation=None)),
    (L.check_observation, E.STALE_OBSERVATION, dict(observation=L.ObservationView(2, WINDOW, NOW - 1000))),
    (L.check_observation, E.STALE_OBSERVATION, dict(observation=L.ObservationView(1, WINDOW - 1, NOW - 1000))),
    (L.check_observation, E.STALE_OBSERVATION, dict(observation=L.ObservationView(1, WINDOW, NOW - 120_000))),
    (L.check_intent_menu, E.INVALID_INTENT, dict(side="sideways")),
    (L.check_intent_menu, E.INVALID_INTENT, dict(stop_atr=D("2.5"))),
    (L.check_intent_menu, E.INVALID_INTENT, dict(take_profit_atr=D("9"))),
    (L.check_stop_price, E.INVALID_INTENT, dict(stop_price=D("0"))),
    (L.check_stop_price, E.INVALID_INTENT, dict(stop_price=D("-5"))),
    (L.check_no_position, E.POSITION_OPEN, dict(has_position=True)),
    (L.check_window_unused, E.WINDOW_USED, dict(window_used=True)),
    (L.check_cooldown, E.COOLDOWN, dict(cooldown_until_window=WINDOW + 1)),
    (L.check_daily_loss, E.DAILY_LOSS_LIMIT, dict(equity=D("94"), day_start_equity=D("100"), peak_equity=D("100"))),
    (L.check_daily_loss, E.DAILY_LOSS_LIMIT, dict(worst_loss=D("6.01"))),  # would not fit today's 6
    (L.check_daily_loss, E.DAILY_LOSS_LIMIT, dict(equity=D("97"), worst_loss=D("3.01"))),  # 3 already gone
    (L.check_drawdown, E.DRAWDOWN_LIMIT, dict(equity=D("70"), day_start_equity=D("70"), peak_equity=D("100"))),
    (L.check_over_cap, E.OVER_CAP, dict(equity=D("401"), day_start_equity=D("401"), peak_equity=D("401"))),
    (L.check_tp_distance, E.TP_TOO_CLOSE, dict(take_profit_bps=D("35.9"))),
    (L.check_size, E.SIZE_TOO_SMALL, dict(quantity=D("0"), notional=D("0"), margin_needed=D("0"))),
    (L.check_size, E.SIZE_TOO_SMALL, dict(notional=D("4.99"), margin_needed=D("2"))),
    (L.check_size, E.SIZE_TOO_SMALL, dict(stop_worst_notional=D("4.99"))),
    (L.check_balance, E.BALANCE_TOO_LOW, dict(margin_needed=D("100.01"))),
    (L.check_depth, E.BOOK_TOO_THIN, dict(book_depth_usd=D("149.99"))),
    (L.check_depth, E.BOOK_TOO_THIN, dict(exit_depth_usd=D("149.99"))),
]


@pytest.mark.parametrize("check,code,over", SINGLE_FAILURES)
def test_check_rejects_and_only_this_check(check, code, over):
    inputs = base(**over)
    rejection = check(inputs)
    assert rejection is not None and rejection.code is code
    assert L.first_rejection(inputs).code is code


@pytest.mark.parametrize("check,_code,over", SINGLE_FAILURES)
def test_check_passes_on_baseline(check, _code, over):
    assert check(base()) is None


def test_boundaries_just_inside_limits_pass():
    assert L.check_market_fresh(base(market_as_of_ms=NOW - 29_999)) is None
    assert L.check_observation(base(observation=L.ObservationView(1, WINDOW, NOW - 119_999))) is None
    assert L.check_daily_loss(base(equity=D("94.01"))) is None
    assert L.check_daily_loss(base(worst_loss=D("6"))) is None  # exactly what is left
    assert L.check_drawdown(base(equity=D("70.01"), day_start_equity=D("70.01"))) is None
    assert L.check_over_cap(base(equity=D("400"), day_start_equity=D("400"), peak_equity=D("400"))) is None
    assert L.check_tp_distance(base(take_profit_bps=D("36"))) is None
    assert L.check_size(base(notional=D("5"), quantity=D("0.001"), margin_needed=D("2.5"))) is None
    assert L.check_balance(base(margin_needed=D("100"))) is None
    assert L.check_depth(base(book_depth_usd=D("150"))) is None
    assert L.check_depth(base(exit_depth_usd=D("150"))) is None
    assert L.check_size(base(stop_worst_notional=D("5"))) is None


def test_tp_check_skipped_without_take_profit():
    assert L.check_tp_distance(base(take_profit_bps=None, take_profit_atr=None)) is None


def test_cooldown_blocks_until_exclusive_window():
    assert L.check_cooldown(base(cooldown_until_window=WINDOW)) is None
    assert L.check_cooldown(base(cooldown_until_window=WINDOW + 1)).code is E.COOLDOWN


def test_daily_loss_ignored_when_day_start_zero():
    assert L.check_daily_loss(base(day_start_equity=D("0"), equity=D("0"), peak_equity=D("0"), notional=D("0"), margin_needed=D("0"), available=D("0"))) is None


def test_spec_order_is_exact():
    """Disable the failures one at a time from the front; codes must appear in spec order."""
    all_bad = dict(
        kill_switch=True,
        user_status="paused",
        grant_ok=False,
        gas_ok=False,
        market_as_of_ms=None,
        observation=None,
        stop_atr=D("9"),
        stop_price=D("0"),
        has_position=True,
        window_used=True,
        cooldown_until_window=WINDOW + 1,
        equity=D("450"),
        day_start_equity=D("500"),
        peak_equity=D("900"),
        take_profit_bps=D("1"),
        quantity=D("0"),
        notional=D("0"),
    )
    expected_order = [
        (E.PAUSED, {"kill_switch": False}),
        (E.PAUSED, {"user_status": "active"}),
        (E.GRANT_MISSING, {"grant_ok": True}),
        (E.GRANTEE_LOW_GAS, {"gas_ok": True}),
        (E.MARKET_UNAVAILABLE, {"market_as_of_ms": NOW - 1}),
        (E.STALE_OBSERVATION, {"observation": L.ObservationView(1, WINDOW, NOW - 1)}),
        (E.INVALID_INTENT, {"stop_atr": D("2")}),
        (E.INVALID_INTENT, {"stop_price": D("59000")}),
        (E.POSITION_OPEN, {"has_position": False}),
        (E.WINDOW_USED, {"window_used": False}),
        (E.COOLDOWN, {"cooldown_until_window": 0}),
        (E.DAILY_LOSS_LIMIT, {"day_start_equity": D("450")}),
        (E.DRAWDOWN_LIMIT, {"peak_equity": D("450")}),
        (E.OVER_CAP, {"equity": D("100"), "day_start_equity": D("100"), "peak_equity": D("100")}),
        (E.TP_TOO_CLOSE, {"take_profit_bps": D("150")}),
        (E.SIZE_TOO_SMALL, {"quantity": D("1"), "notional": D("5000")}),
        (E.BALANCE_TOO_LOW, {"margin_needed": D("5"), "available": D("100")}),
        (E.BOOK_TOO_THIN, {"book_depth_usd": D("100000")}),
    ]
    state = dict(all_bad)
    state["margin_needed"] = D("5000")
    state["available"] = D("1")
    state["book_depth_usd"] = D("1")
    for code, fix in expected_order:
        rejection = L.first_rejection(base(**state))
        assert rejection is not None and rejection.code is code, (code, rejection)
        state.update(fix)
    assert L.first_rejection(base(**state)) is None


def test_entry_checks_tuple_matches_spec_order():
    assert L.ENTRY_CHECKS == (
        L.check_kill_switch, L.check_user_status, L.check_grant, L.check_grantee_gas, L.check_market_fresh,
        L.check_observation, L.check_intent_menu, L.check_stop_price, L.check_no_position, L.check_window_unused,
        L.check_cooldown, L.check_daily_loss, L.check_drawdown, L.check_over_cap,
        L.check_tp_distance, L.check_size, L.check_balance, L.check_depth,
    )


def test_state_blocked_reasons_lists_all_without_intent_checks():
    inputs = base(kill_switch=True, has_position=True, grant_ok=False, observation=None, stop_atr=D("9"))
    assert L.blocked_reasons(inputs) == [E.PAUSED, E.GRANT_MISSING, E.POSITION_OPEN]


def test_ticket_valid():
    assert L.check_ticket_valid(NOW, NOW + 1, None) is None
    assert L.check_ticket_valid(NOW, NOW, None).code is E.TICKET_EXPIRED
    assert L.check_ticket_valid(NOW, NOW - 1, None).code is E.TICKET_EXPIRED
    assert L.check_ticket_valid(NOW, NOW + 1, NOW - 5).code is E.TICKET_USED


def test_drift_boundary():
    assert L.check_drift(D("60300"), D("60000"), D("0.005")) is None  # exactly DRIFT_MAX allowed
    assert L.check_drift(D("60301"), D("60000"), D("0.005")).code is E.STALE_DECISION
    assert L.check_drift(D("59699"), D("60000"), D("0.005")).code is E.STALE_DECISION


def test_risk_growth_boundary_and_sign():
    # Ticket stop distance 300 at 1.5x allows 450 of risk from the worst entry.
    assert L.check_risk_growth("long", D("60150"), D("60000"), D("59700"), D("1.5")) is None
    assert L.check_risk_growth("long", D("60150.01"), D("60000"), D("59700"), D("1.5")).code is E.STALE_DECISION
    assert L.check_risk_growth("short", D("59850"), D("60000"), D("60300"), D("1.5")) is None
    assert L.check_risk_growth("short", D("59849.99"), D("60000"), D("60300"), D("1.5")).code is E.STALE_DECISION


def test_take_profit_bps():
    assert L.take_profit_bps(D("100"), D("101")) == D("100")
    assert L.take_profit_bps(D("100"), None) is None


# ---------------------------------------------------------------------- transfers / risk state


def state(day_start="100", peak="100", cooldown=0, day=10) -> L.RiskState:
    return L.RiskState(day, D(day_start), D(peak), cooldown)


def test_transfer_requires_matching_snapshots_and_no_trade():
    assert L.is_transfer(D("100"), True, D("150"), True, False) is True
    assert L.is_transfer(D("100"), False, D("150"), False, False) is True  # both in a position, on balance
    assert L.is_transfer(D("100"), False, D("150"), True, False) is False  # was in a position
    assert L.is_transfer(D("100"), True, D("150"), False, False) is False  # is in a position
    assert L.is_transfer(D("100"), True, D("150"), True, True) is False  # a trade closed between
    assert L.is_transfer(None, None, D("150"), True, False) is False


def test_rebase_to_zero_keeps_baselines_and_redeposit_scales_from_last_nonzero():
    s = state("120", "200")
    assert L.rebased(s, D("100"), D("0")) == s
    restored = L.advance_risk_state(s, D("50"), 10, prev_equity=D("0"), prev_flat=True, flat=True, rebase_from=D("100"))
    assert (restored.day_start_equity, restored.peak_equity) == (D("60"), D("100"))


def test_first_ever_funding_sets_the_baselines():
    zero = state("0", "0")
    funded = L.advance_risk_state(zero, D("80"), 10, prev_equity=D("0"), prev_flat=True, flat=True)
    assert (funded.day_start_equity, funded.peak_equity) == (D("80"), D("80"))


def test_day_start_reset_by_a_zero_balance_rollover_is_rebuilt_on_redeposit():
    s = state("0", "200")
    restored = L.advance_risk_state(s, D("50"), 10, prev_equity=D("0"), prev_flat=True, flat=True, rebase_from=D("100"))
    assert (restored.day_start_equity, restored.peak_equity) == (D("50"), D("100"))


def test_transfer_threshold_is_one_percent():
    assert L.is_transfer(D("100"), True, D("101"), True, False) is False  # exactly 1%: not a jump
    assert L.is_transfer(D("100"), True, D("101.01"), True, False) is True
    assert L.is_transfer(D("100"), True, D("98.99"), True, False) is True


def test_rebase_deposit_preserves_percentages():
    s = state("100", "120")
    rebased = L.rebased(s, D("94"), D("188"))  # doubled by a deposit while 6% down on the day
    assert rebased.day_start_equity == D("200")
    assert rebased.peak_equity == D("240")
    # day pnl: 188/200 = -6% (preserved), drawdown 188/240 (preserved ratio)
    assert (rebased.day_start_equity - D("188")) / rebased.day_start_equity == D("0.06")


def test_rebase_withdrawal_preserves_percentages():
    s = state("100", "100")
    rebased = L.rebased(s, D("100"), D("50"))
    assert rebased.day_start_equity == D("50")
    assert rebased.peak_equity == D("50")


def test_rebase_after_loss_cannot_clear_daily_loss():
    s = state("100", "100")
    # 6% down, deposit raises equity 94 -> 94 + 1.00 (1.06% jump)
    new = L.advance_risk_state(s, D("95"), 10, prev_equity=D("94"), prev_flat=True, flat=True)
    assert (new.day_start_equity - D("95")) / new.day_start_equity >= D("0.06")


def test_advance_creates_state_when_missing():
    assert L.advance_risk_state(None, D("100"), 5) == L.RiskState(5, D("100"), D("100"))


def test_advance_rolls_day_and_keeps_peak():
    s = state("100", "120", day=9)
    new = L.advance_risk_state(s, D("90"), 10)
    assert new.day_id == 10
    assert new.day_start_equity == D("90")
    assert new.peak_equity == D("120")


def test_advance_raises_peak():
    new = L.advance_risk_state(state("100", "100"), D("110"), 10, flat=False)
    assert new.peak_equity == D("110")


def test_advance_classifies_on_balance_not_equity():
    new = L.advance_risk_state(state("100", "100"), D("54"), 10, prev_equity=D("100"), prev_flat=False, flat=False, balance=D("60"))
    assert (new.day_start_equity, new.peak_equity) == (D("60"), D("60"))
    unchanged = L.advance_risk_state(state("100", "100"), D("80"), 10, prev_equity=D("100"), prev_flat=False, flat=False, balance=D("100"))
    assert unchanged.day_start_equity == D("100")


def test_advance_does_not_treat_position_pnl_as_transfer():
    new = L.advance_risk_state(state("100", "100"), D("80"), 10, prev_equity=D("100"), prev_flat=True, flat=False)
    assert new.day_start_equity == D("100")


def test_advance_realized_loss_between_flat_snapshots_is_not_a_transfer():
    new = L.advance_risk_state(state("100", "100"), D("90"), 10, prev_equity=D("100"), prev_flat=True, flat=True, trade_closed_between=True)
    assert new.day_start_equity == D("100")
    assert new.peak_equity == D("100")


def test_rejections_do_not_mutate_inputs():
    original = base()
    replace(original, kill_switch=True)
    assert original.kill_switch is False
