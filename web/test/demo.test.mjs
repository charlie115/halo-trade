// The example account is built around the clock. Nothing it shows may be in the future, at any
// minute of the hour: the status line once read "Checked 18:45, next 18:09".
import assert from "node:assert/strict";
import { test } from "node:test";

import { demoData } from "../src/demo.js";

const MINUTE = 60_000;
const BASE = Date.UTC(2026, 10, 10, 9, 0, 0);
const moments = [];
for (let minute = 0; minute < 60; minute += 1) moments.push(BASE + minute * MINUTE + 17_000);
moments.push(BASE, BASE + 59 * MINUTE + 59_999, Date.UTC(2027, 0, 1, 0, 0, 1), Date.UTC(2026, 11, 31, 23, 59, 59));

test("the newest check is behind the clock and the next one is ahead of it", () => {
  for (const now of moments) {
    const { agent, config } = demoData(now);
    const label = new Date(now).toISOString();
    assert.ok(agent.last_checked_ms < now, `checked after now at ${label}`);
    assert.ok(agent.next_run_ms > now, `next check not ahead at ${label}`);
    assert.equal(agent.next_run_ms - agent.last_checked_ms, config.plan.decision_interval_s * 1000, label);
    const newest = Math.max(...agent.decisions.map((row) => row.created_ms));
    assert.equal(agent.last_checked_ms, newest, `the first decision is not the newest at ${label}`);
    assert.equal(agent.decisions[0].created_ms, newest, label);
  }
});

test("no time in the example account is in the future", () => {
  for (const now of moments) {
    const { agent, candles } = demoData(now);
    const label = new Date(now).toISOString();
    const times = [
      ...agent.decisions.map((row) => row.created_ms),
      ...agent.trades.flatMap((trade) => [trade.opened_ms, trade.closed_ms]),
      ...agent.performance.points.map(([time]) => time),
      ...agent.market.headlines.map((row) => row.published_ms),
      ...candles.candles.map(([time]) => time),
      agent.position.opened_ms,
      agent.performance.since_ms,
    ];
    for (const time of times) {
      assert.ok(Number.isFinite(time), label);
      assert.ok(time <= now, `${new Date(time).toISOString()} is after ${label}`);
    }
    for (const trade of agent.trades) assert.ok(trade.opened_ms < trade.closed_ms, label);
  }
});
