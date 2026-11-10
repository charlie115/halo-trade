"""Every sizing rule on its own, plus one worked example."""
from dataclasses import replace
from decimal import Decimal as D
from itertools import product

from halo_trade import sizing
from halo_trade.chain.base import Side
from halo_trade.chain.mock import DEFAULT_MARKET as M
from halo_trade.config import Risk

TICK = M.price_tick
QTICK = M.quantity_tick


def test_risk_usd_is_equity_times_risk_per_trade():
    assert sizing.risk_usd(D("100"), D("0.015")) == D("1.5")


def test_stop_distance_uses_atr_when_larger_than_min():
    assert sizing.stop_distance(D("60000"), D("300"), D("2"), D("0.005")) == D("600")


def test_stop_distance_uses_min_pct_when_atr_smaller():
    assert sizing.stop_distance(D("60000"), D("10"), D("1.5"), D("0.005")) == D("300")


def test_stop_price_long_rounds_up_so_distance_does_not_grow():
    # mark 100.005 - distance 1.002 = 99.003 -> rounds UP to 99.01 (distance shrinks)
    stop = sizing.stop_price(Side.LONG, D("100.005"), D("1.002"), D("0.01"))
    assert stop == D("99.01")
    assert D("100.005") - stop <= D("1.002")


def test_stop_price_short_rounds_down_so_distance_does_not_grow():
    # mark 100.005 + distance 1.002 = 101.007 -> rounds DOWN to 101.00
    stop = sizing.stop_price(Side.SHORT, D("100.005"), D("1.002"), D("0.01"))
    assert stop == D("101.00")
    assert stop - D("100.005") <= D("1.002")


def test_raw_quantity_is_risk_over_distance():
    assert sizing.raw_quantity(D("1.5"), D("600")) == D("0.0025")


def test_quantity_capped_by_leverage():
    # risk would allow 1.0 but equity 100 * 2 * 0.98 / 60000 = 0.00326...
    capped = sizing.cap_quantity(D("1"), D("100"), D("2"), D("0.98"), D("60000"))
    assert capped == D("100") * 2 * D("0.98") / D("60000")


def test_quantity_not_capped_when_below_cap():
    assert sizing.cap_quantity(D("0.001"), D("100"), D("2"), D("0.98"), D("60000")) == D("0.001")


def test_quantity_rounds_down_to_tick():
    assert sizing.round_quantity(D("0.00259"), D("0.0001")) == D("0.0025")


def test_notional():
    assert sizing.notional(D("0.0025"), D("60000")) == D("150")


def test_margin_needed_is_notional_over_max_leverage():
    assert sizing.margin_needed(D("150"), D("2")) == D("75")


def test_take_profit_none_when_not_requested():
    assert sizing.take_profit_price(Side.LONG, D("100"), D("3"), None, TICK) is None


def test_take_profit_long_rounds_toward_mark():
    tp = sizing.take_profit_price(Side.LONG, D("100.005"), D("3"), D("2"), D("0.01"))
    assert tp == D("106.00")  # 106.005 down to 106.00, target comes closer


def test_take_profit_short_rounds_toward_mark():
    tp = sizing.take_profit_price(Side.SHORT, D("100.005"), D("3"), D("2"), D("0.01"))
    assert tp == D("94.01")  # 94.005 up to 94.01


def test_worst_loss_formula():
    assert sizing.worst_loss(D("0.0025"), D("60300"), D("57618"), D("0.0005")) == D("0.0025") * D("2682") + D("0.0025") * D("117918") * D("0.0005")
    assert sizing.worst_loss(D("0.0025"), D("59700"), D("62418"), D("0.0005")) == D("0.0025") * D("2718") + D("0.0025") * D("122118") * D("0.0005")


def test_entry_worst_price_long_stays_inside_collar():
    worst = sizing.entry_worst_price(Side.LONG, D("100.003"), D("0.005"), D("0.01"))
    assert worst == D("100.50")  # 100.503 rounds DOWN, never above mark * (1 + collar)
    assert worst <= D("100.003") * D("1.005")


def test_entry_worst_price_short_stays_inside_collar():
    worst = sizing.entry_worst_price(Side.SHORT, D("100.003"), D("0.005"), D("0.01"))
    assert worst == D("99.51")  # 99.502985 rounds UP, never below mark * (1 - collar)
    assert worst >= D("100.003") * D("0.995")


def test_stop_worst_price_for_sell_stop_rounds_up():
    # 99 * 0.97 = 96.03; a non-tick value must round toward the trigger
    assert sizing.stop_worst_price(Side.SHORT, D("99.005"), D("0.03"), D("0.01")) == D("96.04")


def test_stop_worst_price_for_buy_stop_rounds_down():
    assert sizing.stop_worst_price(Side.LONG, D("99.005"), D("0.03"), D("0.01")) == D("101.97")


def test_default_policy_numbers():
    risk = Risk()
    assert risk.risk_per_trade == D("0.03")
    assert risk.collar == D("0.003")
    assert risk.stop_worst_pct == D("0.015")
    assert risk.drift_max == D("0.0025")
    assert risk.daily_loss == D("0.06")


def test_compute_worked_example_long():
    risk = Risk()
    result = sizing.compute(Side.LONG, D("100"), D("60000"), D("300"), D("2"), None, M, risk)
    assert result.risk_usd == D("3")
    assert result.stop_distance == D("600")
    assert result.stop_price == D("59400")
    # Entry at the 0.3% collar 60180, stop at its 1.5% worst 58509, the taker fee on both legs.
    assert result.entry_worst_price == D("60180")
    assert result.stop_worst_price == D("58509")
    per_unit = (D("60180") - D("58509")) + (D("60180") + D("58509")) * M.taker_fee
    assert per_unit == D("1730.3445")
    # 3 / 1730.3445 = 0.001733..., down to the 0.0001 tick.
    assert result.quantity == D("0.0017")
    assert result.notional == D("102.0000")
    assert result.margin_needed == D("51.0000")
    assert result.take_profit_price is None
    assert result.worst_loss == D("0.0017") * per_unit == D("2.94158565")
    assert result.worst_loss <= result.risk_usd


def test_compute_short_stop_above_mark():
    risk = Risk()
    result = sizing.compute(Side.SHORT, D("100"), D("60000"), D("300"), D("2"), D("3"), M, risk)
    assert result.stop_price == D("60600")
    assert result.take_profit_price == D("59100")


def test_compute_leverage_cap_binds_on_tight_stop():
    # ATR tiny, stop = min pct = 300, stop_worst 58804.50: 1434.99225 of worst loss per unit.
    # At the default 3% that buys 0.0020 (1.2x), under the cap of 100*2*.98/60000 = 0.003266...
    assert sizing.compute(Side.LONG, D("100"), D("60000"), D("1"), D("1.5"), None, M, Risk()).quantity == D("0.0020")
    # A 6% budget would buy 0.00418, so now the cap is what decides.
    result = sizing.compute(Side.LONG, D("100"), D("60000"), D("1"), D("1.5"), None, M, Risk(risk_per_trade=D("0.06")))
    uncapped = result.risk_usd / sizing.worst_loss(D(1), result.entry_worst_price, result.stop_worst_price, M.taker_fee)
    assert uncapped > D("0.0041")
    assert result.quantity == D("0.0032")
    assert result.margin_needed <= D("100")
    assert result.worst_loss < result.risk_usd  # the cap can only shrink the worst case


def test_worst_loss_never_exceeds_the_budget_on_a_grid_at_default_config():
    risk = Risk()
    coarse = replace(M, price_tick=D("0.5"), quantity_tick=D("0.001"))
    fee, sized = M.taker_fee, 0
    grid = product(
        (M, coarse),
        (Side.LONG, Side.SHORT),
        (D("1.5"), D("2"), D("3")),
        (D("9876.54"), D("30000"), D("60000"), D("60000.37"), D("123456.78")),
        (D("0.0005"), D("0.002"), D("0.005"), D("0.01"), D("0.02"), D("0.05")),  # ATR as a share of the mark
        (D("20"), D("50"), D("100"), D("200"), D("400")),
    )
    for market, side, stop_atr, mark, atr_pct, equity in grid:
        case = (market.quantity_tick, side, stop_atr, mark, atr_pct, equity)
        result = sizing.compute(side, equity, mark, mark * atr_pct, stop_atr, None, market, risk)
        budget = equity * D("0.03")
        entry, stop_fill = result.entry_worst_price, result.stop_worst_price
        # Both worst prices stay inside their own limits, and the loss is recomputed by hand here.
        assert abs(entry - mark) <= mark * D("0.003"), case
        assert abs(stop_fill - result.stop_price) <= result.stop_price * D("0.015"), case
        per_unit = abs(entry - stop_fill) + (entry + stop_fill) * fee
        assert result.worst_loss == result.quantity * per_unit, case
        assert result.worst_loss <= budget, case
        if result.quantity == 0:
            continue
        sized += 1
        # The budget is really being spent: one more tick breaks it, unless the leverage cap stopped it.
        next_size = result.quantity + market.quantity_tick
        assert next_size * per_unit > budget or next_size * mark > equity * D("2") * D("0.98"), case
    assert sized > 500  # the grid is not passing on zero quantities


def test_compute_tiny_equity_rounds_quantity_to_zero():
    risk = Risk()
    result = sizing.compute(Side.LONG, D("0.5"), D("60000"), D("300"), D("2"), None, M, risk)
    assert result.quantity == 0


def test_implied_exit_price_round_trips_long():
    entry, exit_px, qty, fee = D("60000"), D("59400"), D("0.0025"), D("0.0005")
    pnl = (exit_px - entry) * qty - fee * qty * (entry + exit_px)
    assert abs(sizing.implied_exit_price(Side.LONG, entry, qty, pnl, fee) - exit_px) < D("0.0001")


def test_implied_exit_price_round_trips_short():
    entry, exit_px, qty, fee = D("60000"), D("60600"), D("0.0025"), D("0.0005")
    pnl = (entry - exit_px) * qty - fee * qty * (entry + exit_px)
    assert abs(sizing.implied_exit_price(Side.SHORT, entry, qty, pnl, fee) - exit_px) < D("0.0001")


def test_day_budget_caps_the_worst_case():
    full = sizing.compute(Side.LONG, D("100"), D("60000"), D("600"), D("2"), None, M, Risk())
    capped = sizing.compute(Side.LONG, D("100"), D("60000"), D("600"), D("2"), None, M, Risk(), D("1.5"))
    assert capped.quantity < full.quantity
    assert capped.worst_loss <= D("1.5")
    assert sizing.compute(Side.LONG, D("100"), D("60000"), D("600"), D("2"), None, M, Risk(), D("-1")).quantity == 0


def test_a_larger_day_budget_changes_nothing():
    full = sizing.compute(Side.LONG, D("100"), D("60000"), D("600"), D("2"), None, M, Risk())
    assert sizing.compute(Side.LONG, D("100"), D("60000"), D("600"), D("2"), None, M, Risk(), D("6")).quantity == full.quantity


def test_close_band_is_wider_than_the_stop_band():
    risk = Risk()
    assert risk.close_worst_pct > risk.stop_worst_pct
    assert sizing.close_worst_price(Side.SHORT, D("60000"), risk.close_worst_pct, M.price_tick) < sizing.stop_worst_price(Side.SHORT, D("60000"), risk.stop_worst_pct, M.price_tick)
