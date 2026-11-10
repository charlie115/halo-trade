// The example account behind /demo. Everything here is made up and the page says so.
// Built around the current time, so the chart always ends now. Loaded only on that page.

import { settingsLine } from "./chat-demo.js";

const HOUR = 3_600_000;
const CHECK_EVERY = 15 * 60_000;
// The newest check is always this far behind the clock, so nothing on the page is in the future.
const CHECKED_AGO = 6 * 60_000;
const ATR = 0.004;
const RISK_USD = 2;

// The Trend strategy's settings and the menus they are chosen from, in the shape the server sends.
// The example's chat proposes changes from these menus, the same way a real agent does.
const TUNING = {
  defaults: {
    v: 1,
    direction: "both",
    stop_atr: 2,
    take_profit_atr: 3,
    extend_target: true,
    checks: { higher_timeframe: { on: true }, fomc: { on: true, hours: 6 }, funding: { on: false, bps: 5 } },
    interval_s: 900,
    risk: { per_trade: "0.01", leverage: "2", daily_loss: "0.06" },
  },
  options: {
    direction: ["both", "long", "short"],
    stop_atr: [1.5, 2, 3],
    take_profit_atr: [2, 3, 4],
    extend_target: true,
    checks: { higher_timeframe: { toggle: true }, fomc: { toggle: true, hours: [3, 6, 12, 24] }, funding: { toggle: true, bps: [2, 5, 10] } },
    interval_s: [900, 1800, 3600, 14400],
    risk: { per_trade: ["0.005", "0.01", "0.02", "0.03"], leverage: ["1", "2"], daily_loss: ["0.02", "0.03", "0.04", "0.06"] },
  },
};

// One repeatable path: a climb, a slide, a base and a second climb.
const DRIFT = [
  [40, 0.0009],
  [32, -0.0011],
  [36, 0.0002],
  [30, 0.0013],
  [14, -0.0006],
  [20, 0.0008],
];

function random(seed) {
  let value = seed;
  return () => {
    value = (value + 0x6d2b79f5) | 0;
    let t = Math.imul(value ^ (value >>> 15), 1 | value);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function buildCandles(now) {
  const next = random(20261109);
  const current = now - (now % HOUR);
  const total = DRIFT.reduce((sum, [hours]) => sum + hours, 0);
  const candles = [];
  let price = 70840;
  let index = 0;
  DRIFT.forEach(([hours, drift]) => {
    for (let hour = 0; hour < hours; hour += 1) {
      const open = price;
      const close = open * (1 + drift + (next() - 0.5) * 0.0052);
      const reach = open * (0.0007 + next() * 0.0022);
      const high = Math.max(open, close) + reach;
      const low = Math.min(open, close) - open * (0.0007 + next() * 0.0022);
      candles.push([current - (total - 1 - index) * HOUR, open, high, low, close]);
      price = close;
      index += 1;
    }
  });
  return candles;
}

const round = (value) => Math.round(value * 10) / 10;
const checks = (rows) => rows.map(([id, ok, value]) => ({ id, ok, value }));

const MARKET_THEN = { bias: "bullish", event_risk: "none", funding_avg_bps: 0.8, fear_greed: 62 };

function seen(candle, extra = {}) {
  return { mark: round(candle[4]), trend: "up", atr_pct: 0.4, ret_1h_pct: 0.21, ret_4h_pct: 0.58, ret_24h_pct: 1.34, volatility: 1.06, funding_bps: 0.6, ...extra };
}

// Walks the path from the entry until the stop or the target is touched, or the agent cuts it.
function playOut(candles, plan) {
  const { ago, side, cutAgo = null } = plan;
  const first = candles.length - 1 - ago;
  const entry = candles[first][4];
  const sign = side === "long" ? 1 : -1;
  const stop = entry * (1 - sign * ATR * 2);
  const take = entry * (1 + sign * ATR * 3);
  for (let index = first + 1; index < candles.length; index += 1) {
    const [, , high, low, close] = candles[index];
    const closedAgo = candles.length - 1 - index;
    const stopped = side === "long" ? low <= stop : high >= stop;
    const reached = side === "long" ? high >= take : low <= take;
    if (stopped) return { entry, stop, take, exit: stop, closedAgo, reason: "stop" };
    if (reached) return { entry, stop, take, exit: take, closedAgo, reason: "take_profit" };
    if (cutAgo !== null && closedAgo <= cutAgo) return { entry, stop, take, exit: close, closedAgo, reason: "close" };
  }
  return null;
}

const ENTRY_REASONS = {
  long: "The 1h and 4h trends agree and funding is not crowded.",
  short: "The 1h trend has turned down and the 4h agrees.",
};
const WRONG_IF = { long: "An hourly close back under the 20 EMA.", short: "An hourly close back above the 20 EMA." };

export function demoData(now) {
  const candles = buildCandles(now);
  const hoursAgo = (hours) => candles[candles.length - 1 - hours];
  const at = (hours, minutes = 45) => hoursAgo(hours)[0] + minutes * 60_000;
  const lastClose = candles[candles.length - 1][4];

  // Newest first. Hours ago; what happens next comes from the path above.
  const plan = [
    { ago: 33, side: "short" },
    { ago: 40, side: "short" },
    { ago: 54, side: "long" },
    { ago: 66, side: "long" },
    { ago: 76, side: "long", cutAgo: 72 },
    { ago: 92, side: "long" },
    { ago: 110, side: "short" },
    { ago: 130, side: "long" },
    { ago: 142, side: "long" },
  ];
  const played = plan.map((row) => ({ ...row, ...playOut(candles, row) }));
  const trades = played.map((row) => {
    const distance = Math.abs(row.entry - row.stop);
    const move = row.side === "long" ? row.exit - row.entry : row.entry - row.exit;
    const size = RISK_USD / distance;
    return {
      side: row.side,
      quantity: size.toFixed(4),
      entry_price: round(row.entry).toFixed(1),
      exit_price: round(row.exit).toFixed(1),
      pnl_usd: (move * size).toFixed(2),
      close_reason: row.reason,
      closed_by: row.reason === "close" ? "agent" : null,
      opened_ms: at(row.ago),
      closed_ms: at(row.closedAgo, 25),
      tx_hash: null,
    };
  });

  const start = 200;
  const realised = trades.reduce((sum, trade) => sum + Number(trade.pnl_usd), 0);

  // The open position: long, a few hours in.
  const entry = round(hoursAgo(5)[4]);
  const stop = round(entry * (1 - ATR * 2));
  const take = round(entry * (1 + ATR * 3));
  const quantity = RISK_USD / (entry - stop);
  const unrealized = (lastClose - entry) * quantity;
  const equity = start + realised + unrealized;

  let running = start;
  const points = [[candles[candles.length - 170][0], start]];
  [...trades].reverse().forEach((trade) => {
    running += Number(trade.pnl_usd);
    points.push([trade.closed_ms, running]);
  });
  points.push([now - 5 * HOUR, running], [now, equity]);

  const lined = (side) => checks([
    ["trend", true, side === "long" ? "up" : "down"],
    ["higher_timeframe", true, side === "long" ? "up" : "down"],
    ["event_risk", true, "none"],
    ["fomc", true, 142],
    ["funding", true, side === "long" ? 0.8 : -0.3],
  ]);
  const setupFor = (side, ready = true) => ({ side, ready, checks: lined(side), stop_atr: 2, take_profit_atr: 3, exit_signal: false });
  const held = (hours, reason, trend = "up", created = at(hours)) => ({
    created_ms: created,
    outcome: "hold",
    side: null,
    reason,
    code: null,
    trigger: "schedule",
    snapshot: { seen: seen(hoursAgo(hours), { trend }), chose: { action: "hold" }, server: {}, market: MARKET_THEN, setup: setupFor("long", false) },
  });
  const opened = (trade, row) => ({
    created_ms: trade.opened_ms,
    outcome: "entered",
    side: trade.side,
    reason: ENTRY_REASONS[trade.side],
    code: null,
    trigger: "schedule",
    snapshot: {
      seen: seen(hoursAgo(row.ago), { trend: trade.side === "long" ? "up" : "down" }),
      chose: { action: "enter", side: trade.side, stop_atr: 2, take_profit_atr: 3 },
      server: { quantity: trade.quantity, entry_price: trade.entry_price, stop_price: round(row.stop).toFixed(1), take_profit_price: round(row.take).toFixed(1), worst_loss_usd: RISK_USD.toFixed(2), tx_hashes: [] },
      market: MARKET_THEN,
      looked: ["candles_summary", "derivatives", "similar_trades"],
      recalled: [{ side: trade.side, r: 1.5, matched: ["side", "trend"], age_days: 2.4 }],
      counter: WRONG_IF[trade.side],
      setup: setupFor(trade.side),
    },
  });

  const pastEntries = played.flatMap((row, index) => {
    const rows = [];
    if (row.reason === "close") {
      rows.push({
        created_ms: trades[index].closed_ms,
        outcome: "closed",
        side: row.side,
        reason: "The 1h trend turned against the position, so it was closed.",
        code: null,
        trigger: "price_move",
        snapshot: { seen: seen(hoursAgo(row.closedAgo), { trend: "down" }), chose: { action: "close" }, server: {}, market: { ...MARKET_THEN, bias: "mixed" } },
      });
    }
    rows.push(opened(trades[index], row));
    return rows;
  });

  const openRow = {
    created_ms: at(5),
    outcome: "entered",
    side: "long",
    reason: ENTRY_REASONS.long,
    code: null,
    trigger: "schedule",
    snapshot: {
      seen: seen(hoursAgo(5)),
      chose: { action: "enter", side: "long", stop_atr: 2, take_profit_atr: 3 },
      server: { quantity: quantity.toFixed(4), entry_price: entry.toFixed(1), stop_price: stop.toFixed(1), take_profit_price: take.toFixed(1), worst_loss_usd: RISK_USD.toFixed(2), tx_hashes: [] },
      market: MARKET_THEN,
      looked: ["candles_summary", "derivatives", "similar_trades"],
      recalled: [
        { side: "long", r: 1.5, matched: ["side", "trend"], age_days: 2.4 },
        { side: "long", r: -0.34, matched: ["side", "trend", "funding"], age_days: 1.7 },
      ],
      counter: WRONG_IF.long,
      setup: setupFor("long"),
    },
  };

  const refused = {
    created_ms: at(9, 30),
    outcome: "rejected",
    side: "short",
    reason: "Momentum has turned down over the last hour.",
    code: "SETUP_NOT_MET",
    trigger: "price_move",
    snapshot: {
      seen: seen(hoursAgo(9), { trend: "flat", ret_1h_pct: -0.44 }),
      chose: { action: "enter", side: "short", stop_atr: 2, take_profit_atr: 3 },
      server: {},
      market: { ...MARKET_THEN, bias: "mixed" },
      setup: { side: null, ready: false, checks: checks([["trend", false, "flat"], ["higher_timeframe", null, null], ["event_risk", true, "none"], ["fomc", true, 146], ["funding", true, 0.4]]), stop_atr: null, take_profit_atr: null, exit_signal: null },
    },
  };

  const decisions = [
    held(0, "The 1h trend still points up and the stop has room.", "up", now - CHECKED_AGO),
    held(1, "Trend is intact. Funding is not crowded."),
    held(2, "Nothing has changed the case for this long."),
    held(3, "Holding. The pullback stayed above the entry."),
    openRow,
    refused,
    held(10, "The hourly trend has not turned yet.", "flat"),
    held(11, "The trend is flat, so there is no trade to take.", "flat"),
    held(13, "Flat trend and a thin book. Waiting.", "flat"),
    ...pastEntries,
  ];

  const wins = trades.filter((trade) => Number(trade.pnl_usd) > 0).length;

  return {
    eth: "0x7a3f00000000000000000000000000000000c41d",
    candles: { ok: true, period_ms: HOUR, candles: candles.map(([t, o, h, l, c]) => [t, o.toFixed(1), h.toFixed(1), l.toFixed(1), c.toFixed(1)]), mark: lastClose.toFixed(1), as_of_ms: now },
    balance: start + realised,
    available: (start + realised - 20).toFixed(2),
    config: {
      network: "testnet",
      market: { ticker: "BTC/USDC PERP", market_id: "", quote_denom: "" },
      min_deposit_usd: "50",
      max_deposit_usd: "200",
      public_host: "",
      presets: [{ id: "trend", name: "Trend", summary: "", tuning: TUNING }],
      plan: { decision_interval_s: CHECK_EVERY / 1000, risk_per_trade: "0.01", daily_loss: "0.06" },
      agent_available: true,
    },
    checked: { grantee: "", marketId: "", ttlSeconds: 2_592_000, grants: [] },
    agent: {
      status: "active",
      halted_by_operator: false,
      preset: "trend",
      tuning: structuredClone(TUNING.defaults),
      custom: false,
      settings: settingsLine(TUNING, TUNING.defaults),
      next_run_ms: now - CHECKED_AGO + CHECK_EVERY,
      now_ms: now,
      last_checked_ms: decisions[0].created_ms,
      explorer_tx: "",
      position: {
        side: "long",
        quantity: quantity.toFixed(4),
        entry_price: entry,
        unrealized_pnl_usd: unrealized,
        stop_price: stop,
        take_profit_price: take,
        loss_if_stopped_usd: String(RISK_USD),
        stop_tx_hash: null,
        opened_ms: at(5),
        unrealized_r: unrealized / RISK_USD,
      },
      position_unavailable: false,
      daily_loss: { used_usd: "0.00", limit_usd: "12.00" },
      performance: {
        active: true,
        since_ms: points[0][0],
        until_ms: null,
        start_equity: String(start),
        equity: equity.toFixed(2),
        net_usd: (equity - start).toFixed(2),
        transfers_usd: "0",
        transfers: [],
        trades: trades.length,
        history_too_short: false,
        points,
        hold_usd: ((start * lastClose) / candles[candles.length - 170][4] - start).toFixed(2),
      },
      decisions,
      trades,
      stats: { trades: trades.length, wins, losses: trades.length - wins, win_rate: wins / trades.length, avg_r: null, best_r: null, worst_r: null, avg_hold_ms: null, max_drawdown_pct: null },
      memory: {
        cases: trades.length,
        lessons: [
          { lesson: "Longs with the 4h trend behind them reached the target more often than those against it.", side: "long", trend: "up", r: 1.5 },
          { lesson: "A short into a flat market was stopped quickly. Wait for the base to break first.", side: "short", trend: "down", r: -1 },
          { lesson: "Closing when the 1h trend turned cut the loss to a fraction of the planned risk.", side: "long", trend: "up", r: -0.34 },
        ],
        playbook: [
          { side: "long", trend: "up", trades: 6, win_rate: 0.5, avg_r: 0.36 },
          { side: "short", trend: "down", trades: 3, win_rate: 0.67, avg_r: 0.67 },
        ],
      },
      market: {
        ...MARKET_THEN,
        themes: ["etf_flows", "macro_rates"],
        summary: "Fund inflows held up while traders wait for the next rate decision.",
        headlines: [
          { title: "Example: spot funds extend their run of weekly inflows", link: "", source: "Example", published_ms: now - 42 * 60_000 },
          { title: "Example: traders trim leverage ahead of the rate decision", link: "", source: "Example", published_ms: now - 2.4 * HOUR },
          { title: "Example: options desks price a quieter week", link: "", source: "Example", published_ms: now - 5 * HOUR },
        ],
      },
      telegram: { available: false, linked: false, display: null },
    },
  };
}
