// The settings editor works on plain objects. What it builds has to be the object the server stores
// for the same choices, or the signature made over one would not match the other.
import assert from "node:assert/strict";
import { test } from "node:test";

import { applyChoice, edited, everyLabel, fieldsFor, sanitize, stable } from "../src/tune.js";

// What /onboard/config sends for each preset at the operator's usual limits.
const ACCOUNT = { interval_s: 900, risk: { per_trade: "0.03", leverage: "2", daily_loss: "0.06" } };
const MENUS = {
  direction: ["both", "long", "short"],
  stop_atr: [1.5, 2, 3],
  take_profit_atr: [2, 3, 4],
  interval_s: [900, 1800, 3600, 14400],
  risk: { per_trade: ["0.005", "0.01", "0.02", "0.03"], leverage: ["1", "2"], daily_loss: ["0.02", "0.03", "0.04", "0.06"] },
};
const FILTERS = { fomc: { toggle: true, hours: [3, 6, 12, 24] }, funding: { toggle: true, bps: [2, 5, 10] } };
const SPECS = {
  steady: {
    defaults: {
      v: 1, direction: "both", stop_atr: 3, take_profit_atr: 4,
      checks: { higher_timeframe: { on: true }, fomc: { on: true, hours: 6 }, funding: { on: true, bps: 5 } },
      ...ACCOUNT,
    },
    options: { ...MENUS, extend_target: false, checks: { higher_timeframe: { toggle: true }, ...FILTERS } },
  },
  trend: {
    defaults: {
      v: 1, direction: "both", stop_atr: 2, take_profit_atr: 3, extend_target: true,
      checks: { higher_timeframe: { on: true }, fomc: { on: true, hours: 6 }, funding: { on: false, bps: 5 } },
      ...ACCOUNT,
    },
    options: { ...MENUS, extend_target: true, checks: { higher_timeframe: { toggle: true }, ...FILTERS } },
  },
  reversal: {
    defaults: {
      v: 1, direction: "both", stop_atr: 2, take_profit_atr: 2,
      checks: { stretched: { multiple: 6 }, volatility: { above: 1.2 }, fomc: { on: false, hours: 6 }, funding: { on: true, bps: 5 } },
      ...ACCOUNT,
    },
    options: {
      ...MENUS,
      extend_target: false,
      checks: { stretched: { toggle: false, multiple: [4, 6, 8] }, volatility: { toggle: false, above: [1, 1.2, 1.5] }, ...FILTERS },
    },
  },
};
const fresh = (id) => sanitize(SPECS[id], null);
const field = (id, tuning, name) => fieldsFor(SPECS[id], tuning).find((row) => row.id === name);
const pick = (id, tuning, ...choices) => choices.reduce((now, [name, value]) => applyChoice(SPECS[id], now, name, value), tuning);

test("the editor opens on the preset as designed, with nothing marked changed", () => {
  for (const id of Object.keys(SPECS)) {
    const tuning = fresh(id);
    assert.deepEqual(tuning, SPECS[id].defaults, id);
    assert.equal(edited(SPECS[id], tuning), false, id);
    const rows = fieldsFor(SPECS[id], tuning);
    assert.ok(rows.every((row) => row.changed === false), id);
    for (const row of rows) {
      assert.ok(row.options.some((option) => option.value === row.value), `${id} ${row.id} shows a value that is not one of its choices`);
    }
  }
});

test("each preset offers its own rows, in reading order", () => {
  const ids = (id) => fieldsFor(SPECS[id], fresh(id)).map((row) => row.id);
  const account = ["interval_s", "risk.per_trade", "risk.daily_loss", "risk.leverage"];
  assert.deepEqual(ids("steady"), ["direction", "stop_atr", "take_profit_atr", "checks.higher_timeframe", "checks.fomc", "checks.funding", ...account]);
  assert.deepEqual(ids("trend"), ["direction", "stop_atr", "take_profit_atr", "extend_target", "checks.higher_timeframe", "checks.fomc", "checks.funding", ...account]);
  assert.deepEqual(ids("reversal"), ["direction", "checks.stretched", "checks.volatility", "stop_atr", "take_profit_atr", "checks.fomc", "checks.funding", ...account]);
  // Reversal's two entry conditions cannot be switched off, only moved.
  assert.deepEqual(field("reversal", fresh("reversal"), "checks.stretched").options.map((option) => option.value), ["4", "6", "8"]);
  assert.equal(field("reversal", fresh("reversal"), "checks.stretched").group, "trade");
  assert.equal(field("reversal", fresh("reversal"), "checks.fomc").group, "skip");
});

test("one choice changes one value and marks that row", () => {
  const tuning = pick("steady", fresh("steady"), ["direction", "long"], ["stop_atr", "1.5"], ["checks.fomc", "24"], ["interval_s", "3600"], ["risk.leverage", "1"]);
  assert.deepEqual(tuning, {
    ...SPECS.steady.defaults,
    direction: "long",
    stop_atr: 1.5,
    checks: { higher_timeframe: { on: true }, fomc: { on: true, hours: 24 }, funding: { on: true, bps: 5 } },
    interval_s: 3600,
    risk: { per_trade: "0.03", leverage: "1", daily_loss: "0.06" },
  });
  assert.equal(edited(SPECS.steady, tuning), true);
  const changed = fieldsFor(SPECS.steady, tuning).filter((row) => row.changed).map((row) => row.id);
  assert.deepEqual(changed, ["direction", "stop_atr", "checks.fomc", "interval_s", "risk.leverage"]);
  assert.deepEqual(fresh("steady"), SPECS.steady.defaults, "the defaults were changed in place");
});

test("choosing the usual value again leaves nothing edited", () => {
  const tuning = pick("steady", fresh("steady"), ["direction", "short"], ["checks.funding", "off"], ["direction", "both"], ["checks.funding", "5"]);
  assert.equal(edited(SPECS.steady, tuning), false);
});

test("a value that is not on the menu changes nothing", () => {
  for (const [name, value] of [
    ["direction", "sideways"], ["stop_atr", "9"], ["take_profit_atr", "2.5"], ["interval_s", "60"], ["checks.fomc", "1"],
    ["checks.funding", "on"], ["checks.higher_timeframe", "6"], ["checks.stretched", "off"], ["extend_target", "on"],
    ["risk.per_trade", "0.5"], ["risk.leverage", "3"], ["risk.nothing", "1"], ["checks.nothing", "off"], ["nothing", "1"],
  ]) {
    for (const id of Object.keys(SPECS)) {
      assert.deepEqual(applyChoice(SPECS[id], fresh(id), name, value), SPECS[id].defaults, `${id} ${name}=${value}`);
    }
  }
});

test("a filter that is switched off keeps its usual number", () => {
  const moved = pick("steady", fresh("steady"), ["checks.funding", "10"]);
  assert.deepEqual(moved.checks.funding, { on: true, bps: 10 });
  const off = pick("steady", moved, ["checks.funding", "off"]);
  assert.deepEqual(off.checks.funding, { on: false, bps: 5 });
  assert.equal(field("steady", off, "checks.funding").value, "off");
  // Trend starts with the funding filter off; choosing a number switches it on.
  const trend = pick("trend", fresh("trend"), ["checks.funding", "2"]);
  assert.deepEqual(trend.checks.funding, { on: true, bps: 2 });
  assert.deepEqual(pick("trend", trend, ["checks.funding", "off"]), SPECS.trend.defaults);
  const plain = pick("steady", fresh("steady"), ["checks.higher_timeframe", "off"]);
  assert.deepEqual(plain.checks.higher_timeframe, { on: false });
  assert.deepEqual(pick("steady", plain, ["checks.higher_timeframe", "on"]), SPECS.steady.defaults);
});

test("the longer target needs a step above the take-profit", () => {
  const usual = field("trend", fresh("trend"), "extend_target");
  assert.equal(usual.value, "on");
  assert.equal(Boolean(usual.fixed), false);
  assert.deepEqual(usual.options.map((option) => option.label), ["Off", "4× if 4h agrees"]);
  assert.equal(field("trend", pick("trend", fresh("trend"), ["take_profit_atr", "2"]), "extend_target").options[1].label, "3× if 4h agrees");

  const top = pick("trend", fresh("trend"), ["take_profit_atr", "4"]);
  assert.equal(top.extend_target, false);
  const row = field("trend", top, "extend_target");
  assert.equal(row.value, "off");
  assert.equal(row.fixed, true);
  assert.deepEqual(row.options.map((option) => option.value), ["off"]);
  assert.equal(pick("trend", top, ["extend_target", "on"]).extend_target, false);

  assert.equal(field("steady", fresh("steady"), "extend_target"), undefined);
  assert.equal("extend_target" in pick("steady", fresh("steady"), ["extend_target", "on"]), false);
});

test("the daily limit never sits below the risk of one trade", () => {
  const tight = pick("steady", fresh("steady"), ["risk.per_trade", "0.005"], ["risk.daily_loss", "0.02"]);
  assert.deepEqual(tight.risk, { per_trade: "0.005", leverage: "2", daily_loss: "0.02" });
  assert.deepEqual(field("steady", tight, "risk.daily_loss").options.filter((option) => option.disabled), []);
  // Raising the risk of one trade pulls the day's limit up to the first choice that covers it.
  const raised = pick("steady", tight, ["risk.per_trade", "0.03"]);
  assert.deepEqual(raised.risk, { per_trade: "0.03", leverage: "2", daily_loss: "0.03" });
  assert.deepEqual(field("steady", raised, "risk.daily_loss").options.filter((option) => option.disabled).map((option) => option.value), ["0.02"]);
  assert.equal(pick("steady", raised, ["risk.daily_loss", "0.02"]).risk.daily_loss, "0.03");
});

test("labels say what each choice does", () => {
  const rows = Object.fromEntries(fieldsFor(SPECS.reversal, fresh("reversal")).map((row) => [row.id, row]));
  const labels = (name) => rows[name].options.map((option) => option.label);
  assert.deepEqual(labels("direction"), ["Long and short", "Long only", "Short only"]);
  assert.deepEqual(labels("stop_atr"), ["1.5× hourly range", "2× hourly range", "3× hourly range"]);
  assert.deepEqual(labels("checks.volatility"), ["1×", "1.2×", "1.5×"]);
  assert.deepEqual(labels("checks.fomc"), ["Off", "Within 3 hours", "Within 6 hours", "Within 12 hours", "Within 24 hours"]);
  assert.deepEqual(labels("checks.funding"), ["Off", "Past 2 bps", "Past 5 bps", "Past 10 bps"]);
  assert.deepEqual(labels("interval_s"), ["Every 15 minutes", "Every 30 minutes", "Every hour", "Every 4 hours"]);
  assert.deepEqual(labels("risk.per_trade"), ["0.5%", "1%", "2%", "3%"]);
  assert.deepEqual(labels("risk.daily_loss"), ["2%", "3%", "4%", "6%"]);
  assert.deepEqual(labels("risk.leverage"), ["Up to 1×", "Up to 2×"]);
  assert.equal(everyLabel(45), "Every 45 seconds");
  assert.equal(everyLabel(60), "Every minute");
});

test("stored settings come back into the editor, and anything unknown becomes the usual value", () => {
  const stored = pick("trend", fresh("trend"), ["direction", "short"], ["take_profit_atr", "2"], ["checks.fomc", "off"], ["checks.funding", "10"], ["risk.per_trade", "0.01"], ["risk.daily_loss", "0.02"]);
  assert.deepEqual(sanitize(SPECS.trend, stored), stored);
  assert.deepEqual(sanitize(SPECS.trend, JSON.parse(JSON.stringify(stored))), stored);

  const odd = { ...stored, stop_atr: 7, direction: "up", interval_s: 5, checks: { fomc: { on: true, hours: 48 }, funding: "yes" }, risk: { per_trade: "0.5", leverage: 2 } };
  const read = sanitize(SPECS.trend, odd);
  assert.deepEqual(read, { ...SPECS.trend.defaults, take_profit_atr: 2 });
  for (const junk of [null, undefined, "steady", 4, [], {}, { checks: null }, { risk: [] }]) {
    assert.deepEqual(sanitize(SPECS.steady, junk), SPECS.steady.defaults);
  }
  // Settings stored for one preset do not leak rows the other does not have.
  assert.equal("extend_target" in sanitize(SPECS.steady, stored), false);
});

test("one set of choices is one string, whatever order it was built in", () => {
  const forward = pick("steady", fresh("steady"), ["direction", "long"], ["risk.leverage", "1"]);
  const backward = pick("steady", fresh("steady"), ["risk.leverage", "1"], ["direction", "long"]);
  assert.equal(stable(forward), stable(backward));
  assert.equal(stable({ b: 1, a: [2, { d: 3, c: 4 }] }), '{"a":[2,{"c":4,"d":3}],"b":1}');
  assert.notEqual(stable(forward), stable(fresh("steady")));
  assert.equal(stable(undefined), undefined);
});
