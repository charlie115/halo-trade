// The chat's pure parts: what a proposal changes, whether it is still current, and the scripted
// answers of the example account. No browser and no server here.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { demoAnswer, demoApply, settingsLine } from "../src/chat-demo.js";
import { proposalRows, stillCurrent } from "../src/chat.js";
import { demoData } from "../src/demo.js";

const NOW = Date.UTC(2026, 10, 10, 9, 20, 17);
const HANGUL = /[가-힣]/;
const REFUSAL = /cannot open or close a trade from chat/;
const FALLBACK = /This example answers a few things/;

/** The example account as the page hands it to the scripted answers: `{agent, spec, name}`. */
function example() {
  const { agent, config } = demoData(NOW);
  const preset = config.presets[0];
  return { agent, spec: preset.tuning, name: preset.name };
}

const rowsOf = (proposal, context) => proposalRows(proposal, () => context.spec, (id) => (id === context.agent.preset ? context.name : "Steady"));

test("a proposal lists only the settings it changes, as before and after", () => {
  const context = example();
  const { reply, proposal } = demoAnswer("Make my settings safer.", context);
  assert.match(reply, /It is ready to apply in the card below\.$/);
  assert.deepEqual(rowsOf(proposal, context), [
    { label: "Crowded funding", from: "Off", to: "Past 5 bps" },
    { label: "Risk per trade", from: "1%", to: "0.5%" },
    { label: "Leverage", from: "Up to 2×", to: "Up to 1×" },
  ]);
  // What the card offers to apply is the whole set of settings, and it starts from the current one.
  assert.equal(proposal.summary, settingsLine(context.spec, proposal.tuning));
  assert.equal(proposal.from.tuning, context.agent.tuning);
  assert.equal(proposal.preset, context.agent.preset);
});

test("a change of strategy gets a row of its own", () => {
  const context = example();
  const { proposal } = demoAnswer("long only", context);
  const moved = { ...proposal, preset: "steady" };
  const rows = rowsOf(moved, context);
  assert.deepEqual(rows[0], { label: "Strategy", from: "Trend", to: "Steady" });
  assert.deepEqual(rows[1], { label: "Direction", from: "Long and short", to: "Long only" });
});

test("a proposal the page cannot lay out has no rows, so the card shows the signed line instead", () => {
  const context = example();
  const { proposal } = demoAnswer("long only", context);
  assert.equal(proposalRows(proposal, () => null, () => ""), null);
  assert.equal(proposalRows({ ...proposal, tuning: proposal.from.tuning }, () => context.spec, () => context.name), null);
  assert.equal(proposalRows({}, () => context.spec, () => context.name), null);
});

test("a proposal is current only while the settings it started from are in force", () => {
  const context = example();
  const { proposal } = demoAnswer("long only", context);
  const { agent } = context;
  assert.equal(stillCurrent(proposal, { preset: agent.preset, tuning: agent.tuning }), true);
  assert.equal(stillCurrent(proposal, { preset: agent.preset, tuning: structuredClone(agent.tuning) }), true);
  assert.equal(stillCurrent(proposal, { preset: agent.preset, tuning: proposal.tuning }), false);
  assert.equal(stillCurrent(proposal, { preset: "steady", tuning: agent.tuning }), false);
  assert.equal(stillCurrent(proposal, null), true);
});

test("an order typed into the chat is refused, and a question about an order is answered", () => {
  const context = example();
  for (const text of ["Buy BTC now", "sell everything", "close my position", "Stop the agent", "withdraw my funds", "지금 매수해"]) {
    const { reply, proposal } = demoAnswer(text, context);
    assert.equal(proposal, null, text);
    if (HANGUL.test(text)) assert.match(reply, /거래를 열거나 닫을 수 없고/, text);
    else assert.match(reply, REFUSAL, text);
  }
  const asked = demoAnswer("Why did you close the last trade?", context);
  assert.doesNotMatch(asked.reply, REFUSAL);
  assert.doesNotMatch(asked.reply, FALLBACK);
});

test("every question the page offers as a chip has an answer of its own", () => {
  const html = readFileSync(new URL("../src/index.html", import.meta.url), "utf8");
  const chips = [...new Set([...html.matchAll(/data-ask="([^"]+)"/g)].map((match) => match[1]))];
  assert.ok(chips.length >= 4, "the chips were not found in the page");
  for (const text of chips) {
    const { reply } = demoAnswer(text, example());
    assert.ok(reply.length > 0, text);
    assert.doesNotMatch(reply, FALLBACK, text);
    assert.doesNotMatch(reply, REFUSAL, text);
  }
});

test("a visitor who writes Korean is answered in Korean", () => {
  const context = example();
  const status = demoAnswer("지금 상태 어때?", context);
  assert.equal(status.proposal, null);
  assert.match(status.reply, HANGUL);
  const safer = demoAnswer("더 안전하게 해줘", context);
  assert.match(safer.reply, HANGUL);
  assert.deepEqual(rowsOf(safer.proposal, context).map((row) => row.label), ["Crowded funding", "Risk per trade", "Leverage"]);
});

test("something the example cannot answer says what it can, and proposes nothing", () => {
  const { reply, proposal } = demoAnswer("Tell me a joke", example());
  assert.equal(proposal, null);
  assert.match(reply, FALLBACK);
});

test("applying a proposal changes the example account, and asking again finds nothing to change", () => {
  const context = example();
  const { agent, spec } = context;
  const before = agent.tuning;
  const { proposal } = demoAnswer("long only", context);
  assert.equal(agent.tuning, before, "an answer alone changed the settings");

  demoApply(agent, proposal, spec);
  assert.equal(agent.tuning.direction, "long");
  assert.equal(agent.custom, true);
  assert.equal(agent.settings, proposal.summary);
  assert.equal(Number(agent.daily_loss.limit_usd), Number(agent.performance.start_equity) * Number(agent.tuning.risk.daily_loss));

  const again = demoAnswer("long only", context);
  assert.equal(again.proposal, null);
  assert.equal(again.reply, "Those are already the agent's settings.");

  // Back to the strategy as designed is not an edit.
  const back = demoAnswer("trade both ways", context);
  demoApply(agent, back.proposal, spec);
  assert.equal(agent.custom, false);
});
