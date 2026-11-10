"""A wallet's own settings for a preset (a "tuning"): the menus a wallet may pick from, the checks on
what it sends, and the one line of the signed message that states the result.

The strategy part (direction, exits, filters) is read by `strategies.py`, which builds the model's
text and the server check from the same object. The account part (how often to decide, and risk
limits that can only be tighter than the operator's) is read by the decision engine and the risk checks.

Every value comes off a short menu, so there is no free text and nothing a wallet can type reaches
the model. A stored object is always complete and canonical: one set of choices, one JSON string,
one signed line.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from .config import Risk, Settings
from .contracts import STOP_ATR_MENU, TAKE_PROFIT_ATR_MENU
from .strategies import DEFAULT_TUNING, extended_target

VERSION = 1
DIRECTIONS = ("both", "long", "short")
INTERVAL_MENU_S = (900, 1800, 3600, 14400)
RISK_PER_TRADE_MENU = (Decimal("0.005"), Decimal("0.01"), Decimal("0.02"), Decimal("0.03"))
DAILY_LOSS_MENU = (Decimal("0.02"), Decimal("0.03"), Decimal("0.04"), Decimal("0.06"))
# The one number each filter takes, and what it may be.
PARAMS: dict[str, tuple[str, tuple[float, ...]]] = {
    "fomc": ("hours", (3.0, 6.0, 12.0, 24.0)),
    "funding": ("bps", (2.0, 5.0, 10.0)),
    "stretched": ("multiple", (4.0, 6.0, 8.0)),
    "volatility": ("above", (1.0, 1.2, 1.5)),
}
_RISK_KEYS = ("per_trade", "leverage", "daily_loss")
_MAX_NUMBER_CHARS = 16


class TuningError(ValueError):
    """What the wallet sent is not on the menus. The message names our field, never the wallet's input."""


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _decimal(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value)
    if len(text) > _MAX_NUMBER_CHARS:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None


def _pick(value: Any, menu: tuple[float, ...]) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return next((step for step in menu if step == value), None)


def intervals(settings: Settings) -> list[int]:
    """How often a wallet may ask for a decision: the operator's interval, or slower."""
    floor = settings.decision_interval_s
    return sorted({floor} | {step for step in INTERVAL_MENU_S if step > floor})


def _risk_menus(risk: Risk) -> dict[str, list[Decimal]]:
    """Each list ends at the operator's own value, so a wallet can tighten a limit and never loosen it."""
    leverage = {Decimal(step) for step in range(1, int(risk.max_leverage) + 1)} | {risk.max_leverage}
    return {
        "per_trade": sorted({step for step in RISK_PER_TRADE_MENU if step < risk.risk_per_trade} | {risk.risk_per_trade}),
        "leverage": sorted(leverage),
        "daily_loss": sorted({step for step in DAILY_LOSS_MENU if step < risk.daily_loss} | {risk.daily_loss}),
    }


def options(preset_id: str, settings: Settings) -> dict[str, Any]:
    """What the page offers for this preset."""
    base = DEFAULT_TUNING[preset_id]
    checks: dict[str, Any] = {}
    for name, spec in base["checks"].items():
        entry: dict[str, Any] = {"toggle": "on" in spec}
        if name in PARAMS:
            key, menu = PARAMS[name]
            entry[key] = list(menu)
        checks[name] = entry
    return {
        "direction": list(DIRECTIONS),
        "stop_atr": list(STOP_ATR_MENU),
        "take_profit_atr": list(TAKE_PROFIT_ATR_MENU),
        "extend_target": "extend_target" in base,
        "checks": checks,
        "interval_s": intervals(settings),
        "risk": {key: [_plain(step) for step in menu] for key, menu in _risk_menus(settings.risk).items()},
    }


def defaults(preset_id: str, settings: Settings) -> dict[str, Any]:
    """The preset as designed, at the operator's interval and limits."""
    risk = settings.risk
    return {
        "v": VERSION,
        **copy.deepcopy(DEFAULT_TUNING[preset_id]),
        "interval_s": settings.decision_interval_s,
        "risk": {"per_trade": _plain(risk.risk_per_trade), "leverage": _plain(risk.max_leverage), "daily_loss": _plain(risk.daily_loss)},
    }


def _build(preset_id: str, raw: Any, settings: Settings, strict: bool) -> dict[str, Any]:
    out = defaults(preset_id, settings)
    if raw is None:
        return out

    def bad(field: str) -> None:
        if strict:
            raise TuningError(f"{field} is not one of the choices")

    if not isinstance(raw, dict):
        bad("tuning")
        return out
    if any(key not in out for key in raw):
        bad("tuning")
    if raw.get("v", VERSION) != VERSION:
        bad("v")

    # The strategy part: fixed menus, the same for every operator.
    if "direction" in raw:
        if isinstance(raw["direction"], str) and raw["direction"] in DIRECTIONS:
            out["direction"] = raw["direction"]
        else:
            bad("direction")
    for key, menu in (("stop_atr", STOP_ATR_MENU), ("take_profit_atr", TAKE_PROFIT_ATR_MENU)):
        if key in raw:
            picked = _pick(raw[key], menu)
            if picked is None:
                bad(key)
            else:
                out[key] = picked
    if "extend_target" in raw and "extend_target" in out:
        if isinstance(raw["extend_target"], bool):
            out["extend_target"] = raw["extend_target"]
        else:
            bad("extend_target")
    given = raw.get("checks", {})
    if not isinstance(given, dict):
        bad("checks")
        given = {}
    for name, spec in given.items():
        target = out["checks"].get(name)
        if target is None or not isinstance(spec, dict):
            bad("checks")
            continue
        for key, value in spec.items():
            if key == "on" and "on" in target:
                if isinstance(value, bool):
                    target["on"] = value
                else:
                    bad(f"checks.{name}.on")
            elif name in PARAMS and key == PARAMS[name][0]:
                picked = _pick(value, PARAMS[name][1])
                if picked is None:
                    bad(f"checks.{name}.{key}")
                else:
                    target[key] = picked
            else:
                bad(f"checks.{name}")

    # The account part: read against the operator's settings as they are now. A stored value the
    # operator has since moved past is pulled back inside, never dropped.
    if "interval_s" in raw:
        value, allowed = raw["interval_s"], intervals(settings)
        whole = isinstance(value, int) and not isinstance(value, bool)
        if strict:
            if not whole or value not in allowed:
                bad("interval_s")
            out["interval_s"] = value
        elif whole and value > 0:
            out["interval_s"] = min(max(value, allowed[0]), allowed[-1])
    chosen = raw.get("risk", {})
    if not isinstance(chosen, dict):
        bad("risk")
        chosen = {}
    menus = _risk_menus(settings.risk)
    for key, value in chosen.items():
        if key not in menus:
            bad("risk")
            continue
        amount = _decimal(value)
        if strict:
            if amount is None or amount not in menus[key]:
                bad(f"risk.{key}")
            out["risk"][key] = _plain(amount)
        elif amount is not None and amount > 0:
            out["risk"][key] = _plain(min(amount, menus[key][-1]))
    if Decimal(out["risk"]["daily_loss"]) < Decimal(out["risk"]["per_trade"]):
        bad("risk.daily_loss")  # one trade could then lose more than the day allows

    # One set of choices, one object: a number that nothing reads goes back to its default.
    if out.get("extend_target") and extended_target(out) is None:
        out["extend_target"] = False
    for name, spec in out["checks"].items():
        if "on" not in spec or name not in PARAMS:
            continue
        read = spec["on"] or (name == "funding" and out.get("extend_target"))
        if not read:
            key = PARAMS[name][0]
            spec[key] = DEFAULT_TUNING[preset_id]["checks"][name][key]
    return out


def normalize(preset_id: str, raw: Any, settings: Settings) -> dict[str, Any]:
    """What a wallet sent, checked against the menus and completed. Raises TuningError on anything else."""
    return _build(preset_id, raw, settings, strict=True)


def resolve(preset_id: str, stored: Optional[str], settings: Settings) -> dict[str, Any]:
    """The settings in force for a stored row. Never raises: no row, or one this version cannot read,
    means the preset's defaults, and the operator's current interval and limits always bound the rest."""
    if not stored:
        return defaults(preset_id, settings)
    try:
        raw = json.loads(stored)
    except (TypeError, ValueError):
        return defaults(preset_id, settings)
    return _build(preset_id, raw, settings, strict=False)


def dumps(tuning: dict[str, Any]) -> str:
    return json.dumps(tuning, sort_keys=True, separators=(",", ":"))


def digest(preset_id: str, tuning: dict[str, Any]) -> str:
    """A short id for "this preset with these settings", kept on every decision so runs can be compared."""
    return hashlib.sha256(dumps({"preset": preset_id, "tuning": tuning}).encode()).hexdigest()[:16]


def is_default(preset_id: str, tuning: dict[str, Any], settings: Settings) -> bool:
    return tuning == defaults(preset_id, settings)


def risk_of(tuning: dict[str, Any], operator: Risk) -> Risk:
    """The operator's limits with this wallet's tighter choices. `min` again, so nothing stored can loosen one."""
    chosen = tuning["risk"]
    return replace(
        operator,
        risk_per_trade=min(Decimal(chosen["per_trade"]), operator.risk_per_trade),
        max_leverage=min(Decimal(chosen["leverage"]), operator.max_leverage),
        daily_loss=min(Decimal(chosen["daily_loss"]), operator.daily_loss),
    )


# ---------------------------------------------------------------------- the signed line
def _any_of(items: list[str]) -> str:
    return " or ".join(items) if len(items) <= 2 else ", ".join(items[:-1]) + ", or " + items[-1]


def _every(seconds: int) -> str:
    for unit, size in (("hour", 3600), ("minute", 60), ("second", 1)):
        if seconds % size == 0:
            count = seconds // size
            return unit if count == 1 else f"{count} {unit}s"
    return f"{seconds} seconds"


def _percent(fraction: str) -> str:
    return _plain(Decimal(fraction) * 100)


def summary(preset_id: str, tuning: dict[str, Any]) -> str:
    """Every choice in one line of plain words, for the message the wallet signs. Letters, digits and
    simple punctuation only: the sign-in message format allows nothing else in this line."""
    checks = tuning["checks"]
    parts = [{"both": "long and short", "long": "long only", "short": "short only"}[tuning["direction"]]]
    if "stretched" in checks:
        parts.append(
            f"entry when the 24-hour move is at least {checks['stretched']['multiple']:g}x the hourly range "
            f"and volatility is above {checks['volatility']['above']:g}"
        )
    exits = f"stop {tuning['stop_atr']:g}x and target {tuning['take_profit_atr']:g}x the hourly range"
    further = extended_target(tuning)
    if further is not None:
        exits += f", target {further:g}x when the 4-hour move agrees"
        if not checks["funding"]["on"]:
            exits += f" and funding is not crowded past {checks['funding']['bps']:g} bps"
    parts.append(exits)
    filters = []
    if checks["fomc"]["on"]:
        filters.append(f"within {checks['fomc']['hours']:g} hours of a Fed decision")
    if checks["funding"]["on"]:
        filters.append(f"when funding is crowded past {checks['funding']['bps']:g} bps")
    if checks.get("higher_timeframe", {}).get("on"):
        filters.append("against the 4-hour trend")
    parts.append("no entries " + _any_of(filters) if filters else "no optional filters")
    parts.append(f"a check every {_every(tuning['interval_s'])}")
    risk = tuning["risk"]
    parts.append(
        f"risk per trade {_percent(risk['per_trade'])} percent, daily loss limit {_percent(risk['daily_loss'])} percent, "
        f"leverage up to {risk['leverage']}x"
    )
    return "; ".join(parts)


# ---------------------------------------------------------------------- the editor's rows, for the chat lane
# The same rows, ids and values as the page's editor (`web/src/tune.js`), so a change proposed in
# chat names a row the owner can also see and set by hand.
_DIRECTION_LABELS = {"both": "Long and short", "long": "Long only", "short": "Short only"}
_CHECK_ROWS: dict[str, tuple[str, Optional[str]]] = {
    "stretched": ("24h move at least", "{:g}x hourly range"),
    "volatility": ("Volatility above", "{:g}x"),
    "fomc": ("Near a Fed decision", "Within {:g} hours"),
    "funding": ("Crowded funding", "Past {:g} bps"),
    "higher_timeframe": ("Against the 4h trend", None),
}
_RISK_ROWS = (("per_trade", "Risk per trade"), ("daily_loss", "Daily loss limit"), ("leverage", "Leverage"))


def _choice(value: str, label: str) -> dict[str, str]:
    return {"value": value, "label": label}


def _check_row(tuning: dict[str, Any], name: str) -> dict[str, Any]:
    label, step_label = _CHECK_ROWS[name]
    entry = tuning["checks"][name]
    toggle = "on" in entry
    choices = [_choice("off", "Off")] if toggle else []
    if name in PARAMS:
        key, menu = PARAMS[name]
        choices += [_choice(f"{step:g}", step_label.format(step)) for step in menu]  # type: ignore[union-attr]
        value = "off" if toggle and not entry["on"] else f"{entry[key]:g}"
    else:
        choices.append(_choice("on", "On"))
        value = "on" if entry["on"] else "off"
    return {"field": f"checks.{name}", "label": label, "value": value, "choices": choices}


def rows(preset_id: str, tuning: dict[str, Any], settings: Settings) -> list[dict[str, Any]]:
    """Every setting of this preset as one row: its id, its label, its value now and what it may be."""
    names = [name for name in tuning["checks"] if name in _CHECK_ROWS]
    out: list[dict[str, Any]] = [
        {"field": "direction", "label": "Direction", "value": tuning["direction"], "choices": [_choice(step, _DIRECTION_LABELS[step]) for step in DIRECTIONS]},
        *[_check_row(tuning, name) for name in names if "on" not in tuning["checks"][name]],
        {"field": "stop_atr", "label": "Stop distance", "value": f"{tuning['stop_atr']:g}", "choices": [_choice(f"{step:g}", f"{step:g}x hourly range") for step in STOP_ATR_MENU]},
        {"field": "take_profit_atr", "label": "Take-profit", "value": f"{tuning['take_profit_atr']:g}", "choices": [_choice(f"{step:g}", f"{step:g}x hourly range") for step in TAKE_PROFIT_ATR_MENU]},
    ]
    if "extend_target" in tuning:
        further = next((step for step in TAKE_PROFIT_ATR_MENU if step > tuning["take_profit_atr"]), None)
        choices = [_choice("off", "Off")] + ([] if further is None else [_choice("on", f"{further:g}x if 4h agrees")])
        out.append({"field": "extend_target", "label": "Longer target", "value": "on" if extended_target(tuning) is not None else "off", "choices": choices})
    out += [_check_row(tuning, name) for name in names if "on" in tuning["checks"][name]]
    out.append({"field": "interval_s", "label": "Checks", "value": str(tuning["interval_s"]), "choices": [_choice(str(step), f"Every {_every(step)}") for step in intervals(settings)]})
    menus = _risk_menus(settings.risk)
    for key, label in _RISK_ROWS:
        if key == "leverage":
            choices = [_choice(_plain(step), f"Up to {_plain(step)}x") for step in menus[key]]
        else:
            choices = [_choice(_plain(step), f"{_plain(step * 100)}%") for step in menus[key]]
        out.append({"field": f"risk.{key}", "label": label, "value": tuning["risk"][key], "choices": choices})
    return out


def _choice_value(row: dict[str, Any], value: Any) -> Optional[str]:
    """The row's own value for what was asked: `2.0`, `"2"` and the label "2x hourly range" are one choice."""
    if isinstance(value, bool):
        text = "on" if value else "off"
    elif isinstance(value, (int, float)):
        text = f"{value:g}"
    elif isinstance(value, str) and len(value) <= 64:
        text = value.strip()
    else:
        return None
    number = _decimal(text)
    if number is not None:
        text = _plain(number)
    for choice in row["choices"]:
        if text == choice["value"] or text.casefold() == choice["label"].casefold():
            return choice["value"]
    return None


def _set(tuning: dict[str, Any], field: str, value: str) -> None:
    if field == "direction":
        tuning["direction"] = value
    elif field in ("stop_atr", "take_profit_atr"):
        tuning[field] = float(value)
    elif field == "interval_s":
        tuning["interval_s"] = int(value)
    elif field == "extend_target":
        tuning["extend_target"] = value == "on"
    elif field.startswith("checks."):
        name = field[len("checks."):]
        entry = tuning["checks"][name]
        if value in ("on", "off"):
            entry["on"] = value == "on"
        else:
            entry[PARAMS[name][0]] = float(value)
            if "on" in entry:
                entry["on"] = True
    else:
        tuning["risk"][field[len("risk."):]] = value


def apply(preset_id: str, tuning: dict[str, Any], changes: list[tuple[str, Any]], settings: Settings) -> dict[str, Any]:
    """`tuning` with each (row id, value) applied in order, then checked like anything a wallet sends.
    Raises TuningError for a row this preset does not have or a value that is not on that row's menu."""
    out = copy.deepcopy(tuning)
    touched: set[str] = set()
    for field, value in changes:
        row = next((row for row in rows(preset_id, out, settings) if row["field"] == field), None)
        if row is None:
            raise TuningError("field is not a setting of this strategy")  # the field came from outside: never echoed
        picked = _choice_value(row, value)
        if picked is None:
            raise TuningError(f"{field} is not one of the choices")
        _set(out, field, picked)
        touched.add(field)
    # One trade may never risk more than the day allows: a higher risk per trade lifts the day's
    # limit to the next step that covers it, as the page's editor does.
    risk = out["risk"]
    if Decimal(risk["daily_loss"]) < Decimal(risk["per_trade"]) and "risk.daily_loss" not in touched:
        enough = [step for step in _risk_menus(settings.risk)["daily_loss"] if step >= Decimal(risk["per_trade"])]
        if enough:
            risk["daily_loss"] = _plain(enough[0])
    return normalize(preset_id, out, settings)


def switched(preset_id: str, tuning: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Another preset as designed, keeping what is the owner's own across presets: direction, how often and the limits."""
    out = defaults(preset_id, settings)
    out["direction"] = tuning["direction"]
    out["interval_s"] = tuning["interval_s"]
    out["risk"] = dict(tuning["risk"])
    return normalize(preset_id, out, settings)


def difference(before_id: str, before: dict[str, Any], after_id: str, after: dict[str, Any], settings: Settings) -> list[dict[str, Any]]:
    """The rows that differ between two sets of choices, each with its label and both values in words."""

    def worded(row: Optional[dict[str, Any]]) -> Optional[str]:
        if row is None:
            return None
        return next((choice["label"] for choice in row["choices"] if choice["value"] == row["value"]), row["value"])

    old = {row["field"]: row for row in rows(before_id, before, settings)}
    out = []
    for row in rows(after_id, after, settings):
        was = old.get(row["field"])
        if was is None or was["value"] != row["value"]:
            out.append({"field": row["field"], "label": row["label"], "from": worded(was), "to": worded(row)})
    return out
