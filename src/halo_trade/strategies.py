"""Preset strategies. A preset is two halves that must say the same thing: `instructions`, which the
LLM reads, and `evaluate`, which checks those rules in server code so neither can drift from the
other. Both are built from one settings object (a "tuning"), so a wallet that changes a number
changes the text and the check together. Size, leverage and every limit stay in server code.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .contracts import STOP_ATR_MENU, TAKE_PROFIT_ATR_MENU, StateOut


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    summary: str  # one line for the page
    instructions: str  # what the LLM reads


# The part of a wallet's settings the rules read. The defaults are the preset as designed. `tuning.py`
# holds the menus a wallet may pick from, and the account settings (interval, risk) that sit beside these.
DEFAULT_TUNING: dict[str, dict[str, Any]] = {
    "steady": {
        "direction": "both",
        "stop_atr": 3.0,
        "take_profit_atr": 4.0,
        "checks": {"higher_timeframe": {"on": True}, "fomc": {"on": True, "hours": 6.0}, "funding": {"on": True, "bps": 5.0}},
    },
    "trend": {
        "direction": "both",
        "stop_atr": 2.0,
        "take_profit_atr": 3.0,
        "extend_target": True,  # one menu step further when the 4h move agrees and funding is not crowded
        "checks": {"higher_timeframe": {"on": True}, "fomc": {"on": True, "hours": 6.0}, "funding": {"on": False, "bps": 5.0}},
    },
    "reversal": {
        "direction": "both",
        "stop_atr": 2.0,
        "take_profit_atr": 2.0,
        "checks": {"stretched": {"multiple": 6.0}, "volatility": {"above": 1.2}, "fomc": {"on": False, "hours": 6.0}, "funding": {"on": True, "bps": 5.0}},
    },
}


def with_defaults(preset_id: str, tuning: Optional[dict[str, Any]]) -> dict[str, Any]:
    """`tuning` laid over the preset's defaults, so a partial object (a backtest sweep, an old row) still works."""
    base = DEFAULT_TUNING[preset_id]
    if not tuning:
        return base
    merged = {key: tuning.get(key, value) for key, value in base.items() if key != "checks"}
    given = tuning.get("checks") or {}
    merged["checks"] = {name: {**spec, **(given.get(name) or {})} for name, spec in base["checks"].items()}
    return merged


def extended_target(tuning: dict[str, Any]) -> Optional[float]:
    """The further take-profit Trend may use, or None when the wallet turned that off or there is no step above."""
    if not tuning.get("extend_target"):
        return None
    return next((step for step in TAKE_PROFIT_ATR_MENU if step > tuning["take_profit_atr"]), None)


# ---------------------------------------------------------------------- the text half
def _any_of(items: list[str]) -> str:
    return " or ".join(items) if len(items) <= 2 else ", ".join(items[:-1]) + ", or " + items[-1]


def _blockers(checks: dict[str, Any]) -> list[str]:
    """What keeps a trend-following preset out, in the order the checks run."""
    items = ["desk.event_risk is high"]
    if checks["fomc"]["on"]:
        items.append(f"desk.fomc_in_hours is below {checks['fomc']['hours']:g}")
    if checks["funding"]["on"]:
        bps = checks["funding"]["bps"]
        items.append(f"funding is crowded your way (desk.funding_avg_bps above {bps:g} for a long, below -{bps:g} for a short)")
    if checks["higher_timeframe"]["on"]:
        items.append("frames.4h.trend points against the trade")
    return items


def _steady_text(t: dict[str, Any]) -> str:
    lines = ["Strategy: Steady. You enter rarely and hold most of the time.", "Enter only when every signal agrees:"]
    if t["direction"] != "short":
        lines.append("- Long: features.trend is up, and ret_1h_pct and ret_4h_pct are both above 0.")
    if t["direction"] != "long":
        lines.append("- Short: features.trend is down, and ret_1h_pct and ret_4h_pct are both below 0.")
    lines.append("If any signal disagrees or features.trend is flat, hold.")
    if t["direction"] != "both":
        other = "short" if t["direction"] == "long" else "long"
        lines.append(f"This wallet trades {t['direction']} only: when the signals point {other}, hold.")
    widest = ", the widest on the menu," if t["stop_atr"] == max(STOP_ATR_MENU) else ""
    lines.append(f"On entry use stop_atr {t['stop_atr']:g}{widest} and take_profit_atr {t['take_profit_atr']:g}.")
    lines.append("With a position open, hold unless features.trend now points against it. Then close.")
    lines.append(f"Context can only keep you out, never put you in. When the signals agree but {_any_of(_blockers(t['checks']))}, hold.")
    lines.append("Memory is thin evidence: mention a losing streak in similar cases in your reason, but the rules above still decide.")
    return "\n".join(lines)


_TREND_ENTRY = {
    "both": "Go long when features.trend is up and short when it is down. When it is flat, hold.",
    "long": "Go long when features.trend is up. When it is flat or down, hold: this wallet trades long only.",
    "short": "Go short when features.trend is down. When it is flat or up, hold: this wallet trades short only.",
}


def _trend_text(t: dict[str, Any]) -> str:
    checks, target, further = t["checks"], t["take_profit_atr"], extended_target(t)
    use = f"Use take_profit_atr {target:g}."
    if further is not None:
        use = f"Use take_profit_atr {target:g}, or {further:g} when ret_4h_pct points the same way as the trend."
    context = f"Context can only keep you out, never put you in. When features.trend gives an entry but {_any_of(_blockers(checks))}, hold."
    if further is not None and not checks["funding"]["on"]:
        bps = checks["funding"]["bps"]
        context += f" When funding is crowded your way (desk.funding_avg_bps above {bps:g} for a long, below -{bps:g} for a short), use take_profit_atr {target:g}, not {further:g}."
    lines = [
        "Strategy: Trend. You follow the 1h trend.",
        _TREND_ENTRY[t["direction"]],
        f"On entry use stop_atr {t['stop_atr']:g}. {use}",
        "With a position open, hold while features.trend still points its way. Close when it points against it.",
        context,
        "Memory is thin evidence: mention similar losing cases in your reason, but the rules above still decide.",
    ]
    return "\n".join(lines)


_REVERSAL_ENTRY = {
    "both": "When both are true, go short after a rise or long after a fall. Otherwise hold.",
    "long": "When both are true after a fall, go long. Otherwise hold: this wallet trades long only.",
    "short": "When both are true after a rise, go short. Otherwise hold: this wallet trades short only.",
}


def _reversal_text(t: dict[str, Any]) -> str:
    checks = t["checks"]
    nearest = ", the nearest take-profit on the menu" if t["take_profit_atr"] == min(TAKE_PROFIT_ATR_MENU) else ""
    context = "Context can only keep you out, never put you in. When both conditions hold but desk.event_risk is high, hold: news-driven moves can keep going."
    if checks["fomc"]["on"]:
        context += f" Hold too when desk.fomc_in_hours is below {checks['fomc']['hours']:g}."
    if checks["funding"]["on"]:
        bps = checks["funding"]["bps"]
        context += f" Crowded funding against the fade (desk.funding_avg_bps above {bps:g} when you would go long, below -{bps:g} when you would go short) means more can unwind, so hold too."
    lines = [
        "Strategy: Reversal. You trade against stretched 24h moves when volatility is elevated.",
        f"A move is stretched when the size of ret_24h_pct is at least {checks['stretched']['multiple']:g} times atr_1h_pct.",
        f"Volatility is elevated when volatility_ratio is above {checks['volatility']['above']:g}.",
        _REVERSAL_ENTRY[t["direction"]],
        f"On entry use stop_atr {t['stop_atr']:g} and take_profit_atr {t['take_profit_atr']:g}{nearest}.",
        "With a position open, hold and let the stop or take-profit end it. Close only when ret_1h_pct shows the original move resuming.",
        context,
        "Memory is thin evidence and never overrides these rules.",
    ]
    return "\n".join(lines)


_TEXT: dict[str, Callable[[dict[str, Any]], str]] = {"steady": _steady_text, "trend": _trend_text, "reversal": _reversal_text}


def instructions(preset_id: str, tuning: Optional[dict[str, Any]] = None) -> str:
    """What the model reads for this preset with these settings. Built from the same object `evaluate` reads."""
    return _TEXT[preset_id](with_defaults(preset_id, tuning))


PRESETS: dict[str, Preset] = {
    preset.id: preset
    for preset in (
        Preset("steady", "Steady", "Trades only when trend and momentum agree, with the widest stop. About one trade every few days.", instructions("steady")),
        Preset("trend", "Trend", "Follows the hourly trend when the 4-hour trend does not disagree. About one trade a day.", instructions("trend")),
        Preset("reversal", "Reversal", "Bets against unusually large 24-hour moves, with a close target. Trades rarely.", instructions("reversal")),
    )
}

def _exits(preset_id: str) -> dict[str, Any]:
    tuning = DEFAULT_TUNING[preset_id]
    further = extended_target(tuning)
    return {"stop_atr": tuning["stop_atr"], "take_profit_atr": [tuning["take_profit_atr"]] + ([] if further is None else [further])}


# What each preset's default instructions tell the model to use. A test holds this to those instructions.
PRESET_EXITS: dict[str, dict[str, Any]] = {preset_id: _exits(preset_id) for preset_id in DEFAULT_TUNING}

_TRENDS = ("up", "down", "flat")
_EVENT_RISKS = ("none", "elevated", "high")
_AGAINST = {"long": "down", "short": "up"}  # the trend that runs against a side
_SIDE_OF_TREND = {"up": "long", "down": "short"}


@dataclass(frozen=True)
class Setup:
    """What a preset's rules say about this moment. `exit_signal` is only meaningful with a position open."""

    side: Optional[str]
    ready: bool
    stop_atr: Optional[float]
    take_profit_atr: Optional[float]
    checks: tuple[dict[str, Any], ...]  # {"id", "ok" (true, false, or null when unknown), "value"}
    exit_signal: Optional[bool]

    def as_json(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "ready": self.ready,
            "stop_atr": self.stop_atr,
            "take_profit_atr": self.take_profit_atr,
            "checks": [dict(check) for check in self.checks],
            "exit_signal": self.exit_signal,
        }


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _closed(value: Any, allowed: tuple[str, ...]) -> Optional[str]:
    return value if value in allowed else None


def _short(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(value, digits)


def _check(check_id: str, ok: Optional[bool], value: Any) -> dict[str, Any]:
    return {"id": check_id, "ok": ok, "value": value}


def _agrees(direction: Optional[str], move: Optional[float]) -> Optional[bool]:
    """Whether a return points the way of the trade. Unknown without a direction or a return."""
    if direction is None or move is None:
        return None
    return move > 0 if direction == "long" else move < 0


def _crowded_your_way(direction: Optional[str], funding: Optional[float], bps: float) -> Optional[bool]:
    if direction is None or funding is None:
        return None
    return funding > bps if direction == "long" else funding < -bps


# ---------------------------------------------------------------------- the checks the presets share
def _trend_check(trend: Optional[str]) -> dict[str, Any]:
    return _check("trend", None if trend is None else trend != "flat", trend)


def _event_risk_check(view: dict[str, Any]) -> dict[str, Any]:
    risk = _closed(view.get("event_risk"), _EVENT_RISKS)
    return _check("event_risk", None if risk is None else risk != "high", risk)


def _fomc_check(view: dict[str, Any], block_hours: float) -> dict[str, Any]:
    hours = _number(view.get("fomc_in_hours"))
    return _check("fomc", None if hours is None else hours >= block_hours, _short(hours, 1))


def _funding_check(direction: Optional[str], view: dict[str, Any], bps: float) -> dict[str, Any]:
    funding = _number(view.get("funding_avg_bps"))
    crowded = _crowded_your_way(direction, funding, bps)
    return _check("funding", None if crowded is None else not crowded, _short(funding, 2))


def _higher_timeframe_check(direction: Optional[str], pack: Optional[dict[str, Any]]) -> dict[str, Any]:
    frame = ((pack or {}).get("frames") or {}).get("4h") or {}
    trend = _closed(frame.get("trend"), _TRENDS)
    ok = None if trend is None or direction is None else trend != _AGAINST[direction]
    return _check("higher_timeframe", ok, trend)


def _context_checks(tuning: dict[str, Any], direction: Optional[str], view: dict[str, Any]) -> list[dict[str, Any]]:
    """Event risk always, then the Fed and funding filters the wallet left on."""
    spec = tuning["checks"]
    checks = [_event_risk_check(view)]
    if spec["fomc"]["on"]:
        checks.append(_fomc_check(view, spec["fomc"]["hours"]))
    if spec["funding"]["on"]:
        checks.append(_funding_check(direction, view, spec["funding"]["bps"]))
    return checks


# ---------------------------------------------------------------------- the rules
Candidate = tuple[Optional[str], Optional[float], Optional[float], list[dict[str, Any]]]


def _steady(state: StateOut, view: dict[str, Any], pack: Optional[dict[str, Any]], tuning: dict[str, Any]) -> Candidate:
    features = state.features
    trend = _closed(features.trend, _TRENDS)
    direction = _SIDE_OF_TREND.get(trend) if trend else None
    ret_1h, ret_4h = _number(features.ret_1h_pct), _number(features.ret_4h_pct)
    agrees_1h, agrees_4h = _agrees(direction, ret_1h), _agrees(direction, ret_4h)
    side = direction if agrees_1h is True and agrees_4h is True else None
    checks = [_trend_check(trend), _check("momentum_1h", agrees_1h, _short(ret_1h, 3)), _check("momentum_4h", agrees_4h, _short(ret_4h, 3))]
    if tuning["checks"]["higher_timeframe"]["on"]:
        checks.append(_higher_timeframe_check(direction, pack))
    return side, tuning["stop_atr"], tuning["take_profit_atr"], checks + _context_checks(tuning, direction, view)


def _trend(state: StateOut, view: dict[str, Any], pack: Optional[dict[str, Any]], tuning: dict[str, Any]) -> Candidate:
    features = state.features
    trend = _closed(features.trend, _TRENDS)
    direction = _SIDE_OF_TREND.get(trend) if trend else None
    take_profit, further = tuning["take_profit_atr"], extended_target(tuning)
    crowded = _crowded_your_way(direction, _number(view.get("funding_avg_bps")), tuning["checks"]["funding"]["bps"])
    # Unknown funding is not crowded funding: the text keeps the nearer target only when funding is crowded your way.
    if further is not None and _agrees(direction, _number(features.ret_4h_pct)) is True and crowded is not True:
        take_profit = further
    checks = [_trend_check(trend)]
    if tuning["checks"]["higher_timeframe"]["on"]:
        checks.append(_higher_timeframe_check(direction, pack))
    return direction, tuning["stop_atr"], take_profit, checks + _context_checks(tuning, direction, view)


def _reversal(state: StateOut, view: dict[str, Any], _pack: Optional[dict[str, Any]], tuning: dict[str, Any]) -> Candidate:
    features = state.features
    spec = tuning["checks"]
    ret_24h, atr_pct, volatility = _number(features.ret_24h_pct), _number(features.atr_1h_pct), _number(features.volatility_ratio)
    known = ret_24h is not None and atr_pct is not None and atr_pct > 0
    stretched = None if not known else abs(ret_24h) >= spec["stretched"]["multiple"] * atr_pct
    multiple = None if not known else abs(ret_24h) / atr_pct
    elevated = None if volatility is None else volatility > spec["volatility"]["above"]
    direction = None if ret_24h is None or ret_24h == 0 else ("short" if ret_24h > 0 else "long")
    side = direction if stretched is True and elevated is True else None
    checks = [_check("stretched", stretched, _short(multiple, 2)), _check("volatility", elevated, _short(volatility, 2))]
    return side, tuning["stop_atr"], tuning["take_profit_atr"], checks + _context_checks(tuning, direction, view)


def _exit_when_trend_turns(state: StateOut) -> Optional[bool]:
    trend = _closed(state.features.trend, _TRENDS)
    return None if trend is None or state.position is None else trend == _AGAINST[state.position.side]


def _exit_when_move_resumes(state: StateOut) -> Optional[bool]:
    move = _number(state.features.ret_1h_pct)
    if move is None or state.position is None:
        return None
    return move < 0 if state.position.side == "long" else move > 0  # the fade's original move, carrying on


@dataclass(frozen=True)
class _Rules:
    candidate: Callable[[StateOut, dict[str, Any], Optional[dict[str, Any]], dict[str, Any]], Candidate]
    exit_signal: Callable[[StateOut], Optional[bool]]


_RULES: dict[str, _Rules] = {
    "steady": _Rules(_steady, _exit_when_trend_turns),
    "trend": _Rules(_trend, _exit_when_trend_turns),
    "reversal": _Rules(_reversal, _exit_when_move_resumes),
}


def evaluate(preset_id: str, state: StateOut, view: dict[str, Any], pack: Optional[dict[str, Any]], tuning: Optional[dict[str, Any]] = None) -> Setup:
    """The preset's rules, with this wallet's settings, against the numbers the model is about to see.
    A context value that is null gives `ok: null` and blocks nothing; a missing core signal means no setup.
    Exits do not read the settings: a position opened under older settings is still closed by the preset's rule."""
    rules = _RULES[preset_id]
    tuning = with_defaults(preset_id, tuning)
    side, stop_atr, take_profit_atr, checks = rules.candidate(state, view, pack, tuning)
    if tuning["direction"] != "both":  # the rule's side stays visible; the wallet's choice is one more check
        checks.append(_check("direction", None if side is None else side == tuning["direction"], tuning["direction"]))
    ready = side is not None and all(check["ok"] is not False for check in checks)
    return Setup(side, ready, None if side is None else stop_atr, None if side is None else take_profit_atr, tuple(checks), rules.exit_signal(state))
