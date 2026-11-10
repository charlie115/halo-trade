// A wallet's own settings for a strategy: what the editor offers and how one change is applied.
// Every choice comes off the server's menus (`tuning.options` in /onboard/config), and the server
// checks the result again before it writes the message the wallet signs. No DOM here.

const DIRECTIONS = { both: "Long and short", long: "Long only", short: "Short only" };
const range = (step) => `${step}× hourly range`;
const percent = (fraction) => `${Number((Number(fraction) * 100).toFixed(4))}%`;

// One row per filter the rules know. `key` is the one number the filter takes, if it takes one.
const CHECKS = {
  stretched: { label: "24h move at least", key: "multiple", option: range },
  volatility: { label: "Volatility above", key: "above", option: (step) => `${step}×` },
  fomc: { label: "Near a Fed decision", key: "hours", option: (step) => `Within ${step} hours` },
  funding: { label: "Crowded funding", key: "bps", option: (step) => `Past ${step} bps` },
  higher_timeframe: { label: "Against the 4h trend" },
};

export const GROUPS = { trade: "Trade", skip: "Skip entries", limits: "Limits" };

export function everyLabel(seconds) {
  if (seconds % 3600 === 0) return seconds === 3600 ? "Every hour" : `Every ${seconds / 3600} hours`;
  if (seconds % 60 === 0) return seconds === 60 ? "Every minute" : `Every ${seconds / 60} minutes`;
  return `Every ${seconds} seconds`;
}

/** One string per set of choices, whatever order the keys arrived in. */
export function stable(value) {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stable(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

const clone = (value) => JSON.parse(JSON.stringify(value));
const list = (value) => (Array.isArray(value) ? value : []);

/** The take-profit one menu step above the chosen one, or null at the top of the menu. */
function nextTarget(spec, tuning) {
  const further = list(spec.options.take_profit_atr).filter((step) => step > tuning.take_profit_atr);
  return further.length ? Math.min(...further) : null;
}

function checkValue(spec, tuning, name) {
  const { key } = CHECKS[name];
  const entry = tuning.checks[name];
  if (spec.options.checks[name].toggle && !entry.on) return "off";
  return key ? String(entry[key]) : "on";
}

function checkField(spec, tuning, name) {
  const known = CHECKS[name];
  const offered = spec.options.checks[name];
  const options = [];
  if (offered.toggle) options.push({ value: "off", label: "Off" });
  if (known.key) list(offered[known.key]).forEach((step) => options.push({ value: String(step), label: known.option(step) }));
  else options.push({ value: "on", label: "On" });
  return { id: `checks.${name}`, group: offered.toggle ? "skip" : "trade", label: known.label, value: checkValue(spec, tuning, name), options };
}

/** The editor's rows for this preset, in reading order: each a label, a value and the choices. */
export function fieldsFor(spec, tuning) {
  const { options } = spec;
  const checks = Object.keys(options.checks || {}).filter((name) => CHECKS[name] && tuning.checks[name]);
  const fields = [
    { id: "direction", group: "trade", label: "Direction", value: tuning.direction, options: list(options.direction).map((value) => ({ value, label: DIRECTIONS[value] || value })) },
    ...checks.filter((name) => !options.checks[name].toggle).map((name) => checkField(spec, tuning, name)),
    { id: "stop_atr", group: "trade", label: "Stop distance", value: String(tuning.stop_atr), options: list(options.stop_atr).map((step) => ({ value: String(step), label: range(step) })) },
    { id: "take_profit_atr", group: "trade", label: "Take-profit", value: String(tuning.take_profit_atr), options: list(options.take_profit_atr).map((step) => ({ value: String(step), label: range(step) })) },
  ];
  if (options.extend_target) {
    const further = nextTarget(spec, tuning);
    fields.push({
      id: "extend_target",
      group: "trade",
      label: "Longer target",
      value: tuning.extend_target && further !== null ? "on" : "off",
      fixed: further === null, // nothing above the chosen take-profit to extend to
      options: [
        { value: "off", label: "Off" },
        ...(further === null ? [] : [{ value: "on", label: `${further}× if 4h agrees` }]),
      ],
    });
  }
  fields.push(...checks.filter((name) => options.checks[name].toggle).map((name) => checkField(spec, tuning, name)));
  const risk = options.risk || {};
  fields.push(
    { id: "interval_s", group: "limits", label: "Checks", value: String(tuning.interval_s), options: list(options.interval_s).map((step) => ({ value: String(step), label: everyLabel(step) })) },
    { id: "risk.per_trade", group: "limits", label: "Risk per trade", value: tuning.risk.per_trade, options: list(risk.per_trade).map((step) => ({ value: step, label: percent(step) })) },
    {
      id: "risk.daily_loss",
      group: "limits",
      label: "Daily loss limit",
      value: tuning.risk.daily_loss,
      // One trade may never risk more than the day allows.
      options: list(risk.daily_loss).map((step) => ({ value: step, label: percent(step), disabled: Number(step) < Number(tuning.risk.per_trade) })),
    },
    { id: "risk.leverage", group: "limits", label: "Leverage", value: tuning.risk.leverage, options: list(risk.leverage).map((step) => ({ value: step, label: `Up to ${step}×` })) },
  );
  const usual = spec.defaults;
  return fields.map((field) => ({ ...field, changed: field.value !== valueOf(spec, usual, field.id) }));
}

function valueOf(spec, tuning, id) {
  if (id === "direction") return tuning.direction;
  if (id === "stop_atr" || id === "take_profit_atr" || id === "interval_s") return String(tuning[id]);
  if (id === "extend_target") return tuning.extend_target && nextTarget(spec, tuning) !== null ? "on" : "off";
  if (id.startsWith("checks.")) return checkValue(spec, tuning, id.slice(7));
  return tuning.risk[id.slice(5)];
}

const numberIn = (menu, text) => list(menu).find((step) => String(step) === text);

/** The form the server stores, and one object per state of the editor: a filter that is off keeps its usual number. */
function settle(spec, tuning) {
  if (tuning.extend_target && nextTarget(spec, tuning) === null) tuning.extend_target = false;
  for (const [name, entry] of Object.entries(tuning.checks)) {
    const key = CHECKS[name] && CHECKS[name].key;
    if (key && "on" in entry && !entry.on) entry[key] = spec.defaults.checks[name][key];
  }
  const { risk } = tuning;
  if (Number(risk.daily_loss) < Number(risk.per_trade)) {
    const enough = list(spec.options.risk.daily_loss).filter((step) => Number(step) >= Number(risk.per_trade));
    if (enough.length) risk.daily_loss = enough.reduce((low, step) => (Number(step) < Number(low) ? step : low));
  }
  return tuning;
}

/** `tuning` with one row set to `value`. A value that is not on that row's menu changes nothing. */
export function applyChoice(spec, tuning, id, value) {
  const next = clone(tuning);
  const { options } = spec;
  if (id === "direction") {
    if (list(options.direction).includes(value)) next.direction = value;
  } else if (id === "stop_atr" || id === "take_profit_atr" || id === "interval_s") {
    const step = numberIn(options[id], value);
    if (step !== undefined) next[id] = step;
  } else if (id === "extend_target") {
    if (options.extend_target && (value === "on" || value === "off")) next.extend_target = value === "on";
  } else if (id.startsWith("checks.")) {
    const name = id.slice(7);
    const offered = (options.checks || {})[name];
    const entry = next.checks[name];
    if (offered && entry && CHECKS[name]) {
      const { key } = CHECKS[name];
      const step = key ? numberIn(offered[key], value) : undefined;
      if (value === "off" && offered.toggle) entry.on = false;
      else if (step !== undefined) {
        entry[key] = step;
        if (offered.toggle) entry.on = true;
      } else if (value === "on" && offered.toggle && !key) entry.on = true;
    }
  } else if (id.startsWith("risk.")) {
    const key = id.slice(5);
    if (list((options.risk || {})[key]).includes(value)) next.risk[key] = value;
  }
  return settle(spec, next);
}

/** A stored set of choices read back into the editor. Anything the menus no longer offer becomes the default. */
export function sanitize(spec, stored) {
  let tuning = clone(spec.defaults);
  if (!stored || typeof stored !== "object") return tuning;
  for (const { id } of fieldsFor(spec, tuning)) {
    let wanted;
    try {
      wanted = valueOf(spec, stored, id);
    } catch {
      continue; // a stored object of another shape
    }
    if (typeof wanted === "string") tuning = applyChoice(spec, tuning, id, wanted);
  }
  return tuning;
}

export function edited(spec, tuning) {
  return stable(tuning) !== stable(spec.defaults);
}
