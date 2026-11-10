import { Network, getNetworkEndpoints } from "@injectivelabs/networks";
import { api, getCandles, getConfig, ApiError } from "./api.js";
import { drawLadder, drawPriceChart, formatPrice, shape } from "./chart.js";
import {
  GAS_DEPOSIT,
  GAS_GRANT,
  GAS_REVOKE,
  GAS_WITHDRAW,
  assembleTx,
  buildDepositMsg,
  buildGrantMsgs,
  buildRevokeMsgs,
  buildTypedData,
  buildWithdrawMsg,
  feeFor,
  fromBaseUnits,
  grantExpiryUnix,
  toBaseUnits,
} from "./tx.js";
import { GuardError, assertChallengeMatches, identityFor, verifyServerConfig } from "./guard.js";
import { bindThemeToggle } from "./theme-toggle.js";
import { createChat } from "./chat.js";
import { GROUPS, applyChoice, edited, everyLabel, fieldsFor, sanitize, stable } from "./tune.js";
import { WalletError, connectWallet, ensureEvmChain, getProvider, personalSign, shortAddress, signTypedData } from "./wallet.js";
import { broadcast, fetchAccount, fetchBankBalance, fetchBaseFee, fetchLatestHeight } from "./lcd.js";

const SESSION_KEY = "halo-trade-session";
const TIMEOUT_BLOCKS = 600n;
const STATUS_POLLS = 15;
const STATUS_POLL_MS = 2000;
const AGENT_POLL_MS = 20000;
const DEMO_REPLY_MS = 450; // the example answers from the page; the pause is the only thing that is acted
const CANDLES_POLL_MS = 60000;
const DEMO_PATH = "/demo";
const ENTRY_MATCH_MS = 20 * 60 * 1000; // a trade and the decision that opened it
const USDC_DECIMALS = 6;
const NOT_READ = "Could not read";
const DELAYED_AFTER_MS = 5 * 60 * 1000; // past this, "due" would hide an agent that has stuck
const DECISION_ROWS = 10;
const OUTCOME_LABELS = {
  hold: "Held",
  entered: "Opened",
  closed: "Closed",
  rejected: "Refused",
  skipped: "Skipped",
  no_decision: "Could not decide",
  unknown: "Result unknown",
};
// Every code a decision can carry, in plain words. The code itself stays one click away.
const CODE_LINES = {
  GRANT_MISSING: "Trading access is missing or has expired.",
  GRANTEE_LOW_GAS: "Our trading key is low on gas.",
  MARKET_UNAVAILABLE: "Market data could not be read.",
  STALE_OBSERVATION: "Market data was out of date.",
  STALE_DECISION: "The price moved before the order could go out.",
  INVALID_INTENT: "The order was not valid.",
  POSITION_OPEN: "A position was already open.",
  NO_POSITION: "There was no position to close.",
  WINDOW_USED: "Already traded in this 15 minutes.",
  COOLDOWN: "Waiting after a recent loss.",
  DAILY_LOSS_LIMIT: "Today's loss limit was reached.",
  DRAWDOWN_LIMIT: "The overall loss limit was reached.",
  OVER_CAP: "The balance is above the trading limit.",
  TP_TOO_CLOSE: "The take-profit was too close to cover costs.",
  SIZE_TOO_SMALL: "The order would be too small.",
  BALANCE_TOO_LOW: "The balance is too low to trade.",
  BOOK_TOO_THIN: "Not enough buyers and sellers in the market.",
  TICKET_EXPIRED: "The order expired before it went out.",
  TICKET_USED: "That order had already been sent.",
  TICKET_NOT_FOUND: "The order could not be found.",
  RATE_LIMITED: "Too many requests at that moment.",
  CHAIN_REJECTED: "The chain refused the order.",
  STOP_FAILED_FLATTENED: "The stop could not be placed, so the position was closed.",
  OUTCOME_UNKNOWN: "The chain has not confirmed it yet.",
  UNAUTHORIZED: "This wallet's access was not accepted.",
  PAUSED: "The agent was stopped during this check.",
  INTERNAL: "Something went wrong on our server.",
  BAD_PRESET: "The chosen strategy was not available.",
  RUN_CHANGED: "The agent was restarted during this check.",
  CYCLE_TIMEOUT: "This check took too long.",
  LLM_NOT_CONFIGURED: "The AI model is not set up.",
  LLM_BUDGET: "Today's AI usage limit was reached.",
  LLM_MALFORMED: "The AI answer was not usable.",
  LLM_TIMEOUT: "The AI model took too long to answer.",
  LLM_ERROR: "The AI model could not answer.",
  SETUP_NOT_MET: "The strategy's rules were not met, so nothing was placed.",
};
// What the agent looked up, how the market read, and why a past trade came to mind, in plain words.
const TOOL_LABELS = {
  candles_summary: "Price charts",
  derivatives: "Funding and open interest",
  orderbook: "Order book",
  market_brief: "Market read",
  event_calendar: "Event calendar",
  position_detail: "Open position",
  similar_trades: "Similar past trades",
  my_lessons: "Past lessons",
  my_performance: "Past results",
};
const BIAS_LABELS = { bullish: "Leaning up", bearish: "Leaning down", mixed: "Mixed", neutral: "Neutral" };
const EVENT_RISK_LABELS = { none: "Calm", elevated: "Elevated", high: "High" };
const THEME_LABELS = {
  etf_flows: "ETF flows",
  regulation: "Regulation",
  macro_rates: "Interest rates",
  security_incident: "Security incident",
  exchange_issue: "Exchange issue",
  stablecoin: "Stablecoins",
  institutional: "Institutions",
  geopolitics: "Geopolitics",
  other: "Other",
};
const WAKE_LABELS = { price_move: "Woken early by a price move", near_stop: "Woken early, price near the stop" };
const TREND_LABELS = { up: "Up", down: "Down", flat: "Flat" };
const percentValue = (value) => signedPct(value);
const CHECK_LABELS = {
  trend: { label: "1h trend", value: (value) => TREND_LABELS[value] },
  higher_timeframe: { label: "4h trend", value: (value) => TREND_LABELS[value] },
  momentum_1h: { label: "1h move", value: percentValue },
  momentum_4h: { label: "4h move", value: percentValue },
  stretched: { label: "24h move stretched", value: (value) => `${Number(value).toFixed(1)}×` },
  volatility: { label: "Volatility", value: (value) => `${Number(value).toFixed(2)}×` },
  event_risk: { label: "News risk", value: (value) => EVENT_RISK_LABELS[value] },
  fomc: { label: "Next Fed decision", value: (value) => `in ${Math.round(Number(value))} h` },
  funding: { label: "Funding", value: (value) => bps(value) },
  direction: { label: "Direction", value: (value) => (value === "short" ? "Short only" : "Long only") },
};
const MATCH_LABELS = { side: "side", trend: "trend", volatility: "volatility", funding: "funding", bias: "market read", preset: "strategy" };
const FEED_HOSTS = ["cointelegraph.com", "decrypt.co", "www.theblock.co", "www.federalreserve.gov"];
const OUTCOME_LINES = {
  no_decision: "The agent could not reach a decision.",
  rejected: "The order was refused.",
  skipped: "This check was skipped.",
  unknown: "The order's result is not confirmed yet.",
};

const $ = (id) => document.getElementById(id);

const state = {
  config: null,
  checked: null, // what the guard let through from config; the only source for anything signed
  rest: null,
  eth: null,
  inj: null,
  sub: null,
  session: null,
  sessionExpires: 0,
  verified: false,
  userStatus: null,
  balance: 0,
  grantOk: false,
  grantExpiresMs: null,
  busy: false,
  revoked: false,
  chainRevokeOffer: false,
  agent: null, // the last /onboard/agent body that was read successfully
  agentReadFailed: false,
  agentReadMs: 0,
  agentTicket: 0, // only the newest read may write
  equityRead: false,
  sessionLost: false, // the panel was showing when the session ended
  closing: false, // the close confirmation is open
  closeKey: "", // which position that confirmation is about
  telegramLink: null, // { url, expiresMs } from the last link request
  decisionsKey: "",
  marketKey: "",
  revokeAsk: false, // the "position stays open" question is showing, before any signature
  revokedPosition: false, // after revoke: true, null (could not be read), "closed", "flat" or false
  revokedCloseAsk: false,
  grantRemoved: false, // the on-chain grant is gone, so our server can no longer close anything
  setupOpen: null, // the Setup row, once the viewer has chosen. Null: open only if a step needs them
  decisionRows: DECISION_ROWS,
  statusKey: "",
  tradesKey: "",
  chartKey: "",
  available: null, // what /onboard/status says can be withdrawn, as sent
  withdrawOpen: false,
  demo: false,
  candles: null, // the last /onboard/candles body that was read
  candlesReadMs: 0,
  chartRange: 72,
  priceChartKey: "",
  selectedKey: null, // the decision whose halo is showing on the chart
  tuning: {}, // this wallet's own settings by preset id, once it has changed or stored any
  tuneKey: "", // what the settings editor last drew
  settingsKey: "",
  demoChat: null, // the example's scripted answers, loaded with the example
};
let pollTimer = null;
let chartView = null;
let chat = null;

const COPY = {
  NO_WALLET: "No wallet found. On a phone, open this page in the MetaMask app browser. On a computer, install the MetaMask extension and reload.",
  NO_ACCOUNT: "MetaMask returned no account. Unlock MetaMask and try again.",
  CONNECT_REJECTED: "You closed the MetaMask connection request. Select Connect MetaMask to try again.",
  SIGN_REJECTED: "You rejected the signature in MetaMask. Nothing was sent. Try again when you are ready.",
  SWITCH_REJECTED: "MetaMask must be on the network this signature uses. Accept the network switch and try again.",
  WALLET_ERROR: "MetaMask returned an error. Reload the page and try again.",
  NO_CHAIN_ACCOUNT: "This wallet has no Injective account yet. Send a little INJ and your USDC to your Injective address first.",
  LCD_ERROR: "Could not reach the Injective network. Check your connection and try again.",
  TX_FAILED: "The transaction failed on chain. Nothing moved except the network fee, if any.",
  NONCE_INVALID: "That request expired. Try again.",
  BAD_SIGNATURE: "That signature does not match this wallet. Connect the right wallet and try again.",
  SESSION_REQUIRED: "Your signed session ended. Sign the ownership message in step 1 again.",
  BALANCE_EMPTY: "Your trading subaccount is empty. Deposit USDC in step 2 first.",
  OVER_MAX_DEPOSIT: "The trading subaccount holds more than the per-wallet limit. Withdraw the excess to continue.",
  BELOW_MIN_DEPOSIT: "The trading subaccount holds less than the minimum deposit. Add USDC in step 2 to continue.",
  GRANT_MISSING: "The trading grant is not active on chain yet. Sign it in step 3, then try again.",
  USERS_FULL: "All wallet slots are full. Check back later.",
  USER_NOT_FOUND: "No account exists for this wallet yet. Finish the four steps first.",
  BAD_STATE: "That action does not apply to this wallet right now. Reload and check its status.",
  CHAIN_UNAVAILABLE: "The server could not reach Injective. Try again in a minute.",
  NETWORK_DISABLED: "Onboarding is switched off on this deployment.",
  GRANTEE_UNAVAILABLE: "The server has no trading key configured yet. Try again later.",
  AGENT_UNAVAILABLE: "The agent is not available on this server yet.",
  BAD_PRESET: "That strategy is not available. Reload and choose again.",
  BAD_TUNING: "Those settings are not available. Reload and choose again.",
  OVER_CAP: "This wallet holds more than the agent may trade with. Withdraw the excess to continue.",
  NO_POSITION: "There is no open position.",
  TELEGRAM_UNAVAILABLE: "Telegram is not available on this server.",
  TELEGRAM_ALREADY_LINKED: "This wallet already has a Telegram chat. Disconnect it first.",
  SERVER_ERROR: "The server could not do that. Try again in a minute.",
  CLOSE_SIGN_IN: "Your session ended. Press Close position to sign in again.",
  WITHDRAW_OVER: "That is more than is available to withdraw.",
  BAD_LINK: "The server sent a link this page will not open.",
  TOO_MANY_PENDING: "Too many sign-in requests are pending. Try again in a few minutes.",
  RATE_LIMITED: "Too many requests from your connection. Wait a minute and try again.",
  INVALID_REQUEST: "The server refused that request. Reload the page and try again.",
  NETWORK: "Could not reach the server. Check your connection and try again.",
  AMOUNT_INVALID: "Enter a USDC amount like 50 or 50.5.",
  AMOUNT_ZERO: "Enter an amount above zero.",
  AMOUNT_OVER: "That would put the trading subaccount above the per-wallet limit.",
  AMOUNT_BELOW_MIN: "That would leave the trading subaccount below the minimum deposit.",
  CONFIG_REFUSED: "This page refused the server's settings because they do not match what was built into it. Nothing was signed. Do not continue, and tell the operator.",
  IDENTITY_MISMATCH: "The server answered with a different address than your wallet's. Nothing was signed. Do not continue, and tell the operator.",
  CHALLENGE_REFUSED: "The server sent a sign-in request that does not match this site. Nothing was signed. Do not continue, and tell the operator.",
  CHAT_FAILED: "The message could not be sent. Try again.",
};

function message(error, fallback) {
  if (error instanceof WalletError || error instanceof ApiError || error instanceof GuardError) return COPY[error.code] || fallback;
  return fallback;
}

function actionMessage(error) {
  return message(error, error instanceof ApiError ? COPY.SERVER_ERROR : COPY.WALLET_ERROR);
}

function bindText(name, value) {
  document.querySelectorAll(`[data-bind="${name}"]`).forEach((node) => {
    node.textContent = value;
  });
}

function stepEl(name) {
  return $(`step-${name}`);
}

function setStep(name, stepState, status) {
  const el = stepEl(name);
  el.dataset.state = stepState;
  el.querySelector("[data-status]").textContent = status;
  const toggle = el.querySelector("[data-toggle]");
  if (toggle) {
    toggle.hidden = stepState !== "done";
    if (stepState !== "done") el.classList.remove("open");
    toggle.textContent = el.classList.contains("open") ? "Hide" : toggle.dataset.label;
  }
}

function showError(scope, text) {
  const el = (typeof scope === "string" ? stepEl(scope) : scope).querySelector("[data-error]");
  if (!text) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.textContent = text;
}

function showNote(name, text) {
  const el = stepEl(name).querySelector("[data-note]");
  el.hidden = !text;
  el.textContent = text || "";
}

function saveSession() {
  try {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify({ eth: state.eth, session: state.session, expires: state.sessionExpires }));
  } catch {
    // Storage may be unavailable; the user just re-signs after a reload.
  }
}

function clearSession() {
  state.session = null;
  state.verified = false;
  try {
    sessionStorage.removeItem(SESSION_KEY);
  } catch {
    // Same as above.
  }
}

function formatUsdc(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "0 USDC";
  return `${number.toLocaleString("en-US", { maximumFractionDigits: 2 })} USDC`;
}

function formatDate(ms) {
  return new Date(ms).toISOString().slice(0, 10);
}

function balanceOk() {
  const min = Number(state.config.min_deposit_usd);
  const max = Number(state.config.max_deposit_usd);
  return state.balance >= min && state.balance <= max;
}

function setBusy(busy) {
  state.busy = busy;
  document.querySelectorAll("button, input[name='preset']").forEach((control) => {
    control.disabled = busy;
  });
  lockTune(busy);
  syncStart();
  if (chat) chat.sync(); // after every button was switched back on, the chat sets its own again
}

function chosenPreset() {
  const input = document.querySelector("input[name='preset']:checked");
  return input ? input.value : null;
}

/** What the server offers for a preset's settings, or null where it sends none (the example account). */
function tuningSpec(id = chosenPreset()) {
  const preset = id === null ? null : (state.config.presets || []).find((entry) => entry.id === id);
  const spec = preset && preset.tuning;
  const whole = spec && spec.defaults && spec.options && spec.defaults.checks && spec.options.checks && spec.defaults.risk && spec.options.risk;
  return whole ? spec : null;
}

/** The settings the chosen preset would start with: this wallet's own, or the preset as designed. */
function chosenTuning() {
  const spec = tuningSpec();
  if (!spec) return undefined;
  return state.tuning[chosenPreset()] || spec.defaults;
}

function syncStart() {
  $("start-btn").disabled = state.busy || !state.config || !state.config.agent_available || !chosenPreset();
  if (state.config) {
    renderTune();
    renderPlan();
  }
}

function walletExists() {
  return state.userStatus === "active" || state.userStatus === "paused";
}

// Without a read, only `active` proves the wallet started. A wallet still `paused` may never have.
function hasStarted() {
  return state.agent ? Boolean(state.agent.preset) : state.userStatus === "active";
}

function isRunning() {
  return state.userStatus === "active" && hasStarted();
}

function presetName(id) {
  const preset = (state.config.presets || []).find((entry) => entry.id === id);
  return preset ? preset.name : id || "";
}

function setText(el, text) {
  if (el.textContent !== text) el.textContent = text;
}

function showFact(id, visible) {
  const value = $(id);
  value.hidden = !visible;
  value.previousElementSibling.hidden = !visible;
}

function setFact(id, text) {
  showFact(id, text !== null);
  if (text !== null) setText($(id), text);
}

function clockTime(ms) {
  return new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

function decisionTime(ms) {
  const when = new Date(ms);
  if (when.toDateString() === new Date().toDateString()) return clockTime(ms);
  return `${when.toLocaleDateString("en-GB", { day: "numeric", month: "short" })} ${clockTime(ms)}`;
}

function amount(value) {
  return Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function money(value) {
  return `${amount(value)} USDC`;
}

function signedMoney(value) {
  const number = Number(value);
  return `${number < 0 ? "\u2212" : "+"}${money(Math.abs(number))}`;
}

function price(value) {
  return Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}

function positionLine(position) {
  const side = position.side === "short" ? "Short" : "Long";
  return `${side} ${quantity(position.quantity)} ${asset()} at ${price(position.entry_price)}`;
}

function quantity(value) {
  return Number(value).toLocaleString("en-US", { maximumFractionDigits: 6 });
}

// A value the server does not have is null, and Number(null) is 0: never let that through.
function known(value) {
  return value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
}

function node(tag, className, text) {
  const made = document.createElement(tag);
  if (className) made.className = className;
  if (text !== undefined) made.textContent = text; // Text only, never markup.
  return made;
}

function explorerUrl(hash) {
  const base = state.agent && state.agent.explorer_tx;
  if (!base || typeof hash !== "string" || !/^[0-9A-Za-z]+$/.test(hash)) return null;
  try {
    const url = new URL(base + hash);
    return url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

function statusParts() {
  const { agent } = state;
  if (state.sessionLost) return state.userStatus === "paused" ? [{ text: "Stopped." }] : [];
  if (agent === null) return [];
  const running = isRunning();
  if (agent.halted_by_operator === true) {
    return [{ text: running ? "Trading is paused by the operator." : "Stopped. Trading is paused by the operator." }];
  }
  const side = agent.position ? agent.position.side : null;
  if (!running) {
    const left = agent.position_unavailable ? "The position could not be read." : side ? `Your ${side} is still open.` : "No position open.";
    return [{ text: `Stopped. ${left}` }];
  }
  const doing = { text: agent.position_unavailable ? "The position could not be read." : side ? `Holding ${side}.` : "Watching the market." };
  const next = agent.next_run_ms;
  const late = next !== null && next !== undefined ? agent.now_ms - next : null;
  const delayed = late !== null && late > DELAYED_AFTER_MS;
  const when = late === null ? "" : late >= 0 ? "due now" : clockTime(next);
  if (agent.last_checked_ms === null || agent.last_checked_ms === undefined) {
    if (delayed) return [doing, { text: `Delayed, first check was due ${clockTime(next)}.`, alarm: true }];
    return when ? [doing, { text: `First check ${when}.` }] : [doing];
  }
  // A check that failed is not a check: it never reads as "Checked".
  const failed = agent.decisions[0] && agent.decisions[0].outcome === "no_decision";
  const past = `${failed ? "Could not check at" : "Checked"} ${clockTime(agent.last_checked_ms)}`;
  if (delayed) return [doing, { text: `${past}.`, alarm: failed }, { text: `Delayed, next was due ${clockTime(next)}.`, alarm: true }];
  return [doing, { text: when ? `${past}, next ${when}.` : `${past}.`, alarm: failed }];
}

function renderStatus() {
  const parts = statusParts();
  const status = $("agent-status");
  $("headline").dataset.tone = parts.some((part) => part.alarm) ? "alarm" : isRunning() ? "running" : "idle";
  status.hidden = parts.length === 0;
  const key = JSON.stringify(parts);
  if (state.statusKey === key) return; // Rewritten only on a change, so a screen reader hears only that.
  state.statusKey = key;
  status.replaceChildren(...parts.flatMap((part, index) => {
    const span = node("span", part.alarm ? "alarm" : "", part.text);
    return index === 0 ? [span] : [" ", span];
  }));
}

function groupDecisions(decisions) {
  const rows = [];
  for (const decision of decisions) {
    const last = rows[rows.length - 1];
    if (decision.outcome === "hold" && last && last.items[0].outcome === "hold") last.items.push(decision);
    else rows.push({ items: [decision] });
  }
  return rows;
}

function decisionLines(decision) {
  const plain = CODE_LINES[decision.code] || OUTCOME_LINES[decision.outcome];
  const lines = [];
  if (plain) lines.push(node("p", "decision-reason", plain));
  if (decision.reason) lines.push(node("p", "decision-reason model", decision.reason));
  if (WAKE_LABELS[decision.trigger]) lines.push(node("p", "decision-wake", WAKE_LABELS[decision.trigger]));
  return lines;
}

function timeNode(ms, label = decisionTime(ms)) {
  const time = node("time", "", label);
  time.dateTime = new Date(ms).toISOString();
  return time;
}

function decisionHead(label, side, time) {
  const head = node("p", "decision-head");
  head.append(node("span", "decision-label", label));
  if (side) head.append(node("span", "decision-side", side));
  head.append(time);
  return head;
}

function disclosure(key, summaryNodes, bodyNodes) {
  const details = node("details");
  details.dataset.key = key;
  const summary = node("summary");
  summary.append(...summaryNodes);
  details.append(summary, ...bodyNodes);
  return details;
}

function rowKey(row) {
  const oldest = row.items[row.items.length - 1]; // A run of holds grows at its newest end.
  return `${oldest.outcome}:${oldest.created_ms}`;
}

function decisionRow(row) {
  const first = row.items[0];
  const item = node("li", "decision");
  item.dataset.outcome = first.outcome;
  if (first.side) item.dataset.side = first.side;
  const label = OUTCOME_LABELS[first.outcome] || "Decision";
  if (row.items.length > 1) {
    const oldest = row.items[row.items.length - 1];
    const range = `${decisionTime(oldest.created_ms)} to ${decisionTime(first.created_ms)}`;
    const held = node("ol", "decision-held");
    held.append(...row.items.map((decision) => {
      const entry = node("li");
      const line = [timeNode(decision.created_ms), node("p", "", decision.reason || "No reason given.")];
      if (decision.snapshot) entry.append(disclosure(`held:${decision.created_ms}`, line, [why(decision)]));
      else entry.append(...line);
      return entry;
    }));
    item.append(disclosure(rowKey(row), [decisionHead(`${label} ×${row.items.length}`, first.side, timeNode(first.created_ms, range)), ...decisionLines(first)], [held]));
    return item;
  }
  const summary = [decisionHead(label, first.side, timeNode(first.created_ms)), ...decisionLines(first)];
  if (first.code || first.snapshot) item.append(disclosure(rowKey(row), summary, [why(first)]));
  else item.append(...summary);
  return item;
}

function renderDecisions(all) {
  const decisions = all.filter((decision) => decision.code !== "NO_CHANGE"); // "Last checked" already covers these.
  const list = $("decisions-list");
  const rows = groupDecisions(decisions);
  const shown = Math.min(state.decisionRows, rows.length);
  $("decisions-empty").hidden = decisions.length > 0;
  $("decisions-more").hidden = shown >= rows.length;
  const key = JSON.stringify([decisions, shown]);
  if (state.decisionsKey === key) return; // Rebuilt only on a change, so a poll keeps any selection.
  state.decisionsKey = key;
  const open = new Set([...list.querySelectorAll("details[open]")].map((details) => details.dataset.key));
  list.replaceChildren(...rows.slice(0, shown).map(decisionRow));
  list.querySelectorAll("details").forEach((details) => {
    details.open = open.has(details.dataset.key);
  });
}

function closeReasonLine(trade) {
  switch (trade.close_reason) {
    case "stop": return "Stop-loss";
    case "take_profit": return "Take-profit";
    case "stop_trigger_failed": return "Stop-loss, closed by the server";
    case "flatten": return "Closed by the server, stop could not be placed";
    case "external": return "Closed outside this site";
    case "close":
      if (trade.closed_by === "owner") return "Closed by you";
      return trade.closed_by === "agent" ? "Closed by the agent" : "Closed";
    default: return "Closed";
  }
}

function duration(ms) {
  const minutes = Math.round(Math.max(0, ms) / 60000);
  if (minutes < 1) return "under 1m";
  const hours = Math.floor(minutes / 60);
  if (hours === 0) return `${minutes}m`;
  return minutes % 60 === 0 ? `${hours}h` : `${hours}h ${minutes % 60}m`;
}

function tradeRow(trade) {
  const item = node("li", "trade");
  item.dataset.key = tradeKey(trade);
  const side = trade.side === "long" ? "Long" : trade.side === "short" ? "Short" : trade.side;
  const hasResult = known(trade.pnl_usd);
  // Worked out from the change in equity, so a deposit during the trade moves it: never exact.
  const result = node("span", `trade-result${hasResult ? (Number(trade.pnl_usd) < 0 ? " loss" : " gain") : ""}`, hasResult ? `≈ ${signedMoney(trade.pnl_usd)}` : "Result unavailable");
  const prices = known(trade.entry_price) ? `${price(trade.entry_price)} to ${known(trade.exit_price) ? `≈\u00a0${price(trade.exit_price)}` : "exit unavailable"}` : "";
  const parts = [closeReasonLine(trade), duration(trade.closed_ms - trade.opened_ms), prices].filter(Boolean);
  const meta = node("p", "trade-meta", parts.join(" · "));
  const url = explorerUrl(trade.tx_hash);
  if (url) {
    const link = node("a", "", "Explorer");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    meta.append(" · ", link);
  }
  item.append(timeNode(trade.closed_ms), node("span", "trade-side", known(trade.quantity) ? `${side} ${quantity(trade.quantity)} ${asset()}` : side), meta, result);
  return item;
}

function renderTrades(trades) {
  $("trades-empty").hidden = trades.length > 0;
  const key = JSON.stringify(trades);
  if (state.tradesKey === key) return;
  state.tradesKey = key;
  $("trades-list").replaceChildren(...trades.map(tradeRow));
}

function renderSetup(dashboard) {
  const needsYou = state.verified && ["connect", "deposit", "grant"].some((name) => ["active", "blocked"].includes(stepEl(name).dataset.state));
  const open = !dashboard || (state.setupOpen === null ? needsYou : state.setupOpen);
  $("layout").classList.toggle("dashboard", dashboard);
  $("setup-toggle").hidden = !dashboard;
  $("setup-toggle").setAttribute("aria-expanded", String(open));
  setText($("setup-toggle-state"), open ? "Hide" : "Show");
  $("steps").hidden = !open;
}

function toggleSetup() {
  state.setupOpen = $("setup-toggle").getAttribute("aria-expanded") !== "true";
  render();
}

// The strategy choice and the caution live in step 4, so a restart goes through them too.
function openStart() {
  state.setupOpen = true;
  render();
  (document.querySelector("input[name='preset']:checked") || document.querySelector("input[name='preset']")).focus();
}

const STOP_WORDS = { 1.5: "Tight", 2: "Medium", 3: "Wide" };

function renderPlan() {
  const { config } = state;
  const plan = config.plan || {};
  const preset = (config.presets || []).find((entry) => entry.id === chosenPreset());
  // With a preset chosen these are the settings it would start with. Before that, the server's own.
  const tuning = chosenTuning();
  const every = tuning ? tuning.interval_s : plan.decision_interval_s;
  const stop = tuning ? tuning.stop_atr : preset && preset.stop_atr;
  const risk = tuning ? tuning.risk : { per_trade: plan.risk_per_trade, daily_loss: plan.daily_loss };
  const ofBalance = (rate) => (state.equityRead && known(rate) ? `about ${money(state.balance * Number(rate))}` : "Unavailable");
  setFact("plan-market", config.market.ticker);
  setFact("plan-checks", known(every) ? everyLabel(Number(every)) : "Unavailable");
  setFact("plan-stop", known(stop) ? `${STOP_WORDS[stop] || "Set"}, ${stop}× the hourly range` : null);
  setFact("plan-loss", ofBalance(risk.per_trade));
  setFact("plan-limit", ofBalance(risk.daily_loss));
  setFact("plan-deposit", `${config.min_deposit_usd} to ${config.max_deposit_usd} USDC`);
}

// The settings editor: one short menu per value. `tune.js` decides the rows; this draws them.
const tuneId = (id) => `tune-${id.replace(".", "-")}`;

function choiceLocked() {
  const first = document.querySelector("input[name='preset']");
  return Boolean(first && first.disabled);
}

function lockTune(locked) {
  document.querySelectorAll("#tune-fields select").forEach((select) => {
    select.disabled = locked || select.dataset.fixed === "true";
  });
  $("tune-reset").disabled = locked;
}

function tuneRow(field, locked) {
  const row = node("div", "tune-row");
  row.dataset.changed = String(field.changed);
  const label = node("label", "", field.label);
  label.htmlFor = tuneId(field.id);
  const select = document.createElement("select");
  select.id = tuneId(field.id);
  select.dataset.field = field.id;
  if (field.fixed) select.dataset.fixed = "true";
  field.options.forEach((choice) => {
    const option = node("option", "", choice.label);
    option.value = choice.value;
    option.disabled = Boolean(choice.disabled);
    select.append(option);
  });
  select.value = field.value;
  select.disabled = locked || Boolean(field.fixed);
  row.append(label, select);
  return row;
}

function renderTune() {
  const spec = tuningSpec();
  $("tune").hidden = !spec;
  if (!spec) {
    state.tuneKey = "";
    return;
  }
  const tuning = chosenTuning();
  const changed = edited(spec, tuning);
  $("tune-mark").hidden = !changed;
  $("tune-reset").hidden = !changed;
  const key = `${chosenPreset()}:${stable(tuning)}`;
  if (key === state.tuneKey) return; // a redraw would close an open menu
  state.tuneKey = key;
  const locked = choiceLocked();
  const groups = new Map();
  for (const field of fieldsFor(spec, tuning)) {
    if (!groups.has(field.group)) {
      const group = node("div", "tune-group");
      const title = node("p", "tune-group-title", GROUPS[field.group]);
      title.id = `tune-group-${field.group}`;
      group.setAttribute("role", "group");
      group.setAttribute("aria-labelledby", title.id);
      group.append(title);
      groups.set(field.group, group);
    }
    groups.get(field.group).append(tuneRow(field, locked));
  }
  $("tune-fields").replaceChildren(...groups.values());
}

function onTune(event) {
  const select = event.target.closest("select[data-field]");
  const spec = tuningSpec();
  if (!select || !spec) return;
  const id = select.dataset.field;
  state.tuning[chosenPreset()] = applyChoice(spec, chosenTuning(), id, select.value);
  state.tuneKey = ""; // draw again even when nothing changed, so the menu shows what is in force
  syncStart();
  const again = document.getElementById(tuneId(id));
  if (again) again.focus();
}

function resetTune() {
  const spec = tuningSpec();
  if (!spec) return;
  state.tuning[chosenPreset()] = sanitize(spec, null);
  syncStart();
  $("tune").querySelector("summary").focus();
}

// A wallet that last ran with its own settings finds them in the editor again.
function seedTuning() {
  const { agent } = state;
  if (!agent || !agent.preset || !agent.tuning || Object.hasOwn(state.tuning, agent.preset)) return;
  const spec = tuningSpec(agent.preset);
  if (spec) state.tuning[agent.preset] = sanitize(spec, agent.tuning);
}

/** "Trend", or "Trend, edited" when the wallet runs it with its own settings. */
function strategyLabel(agent) {
  return agent.custom ? `${presetName(agent.preset)}, edited` : presetName(agent.preset);
}

// The line the wallet signed, one choice per row.
function renderSettings(line) {
  const text = typeof line === "string" ? line : "";
  $("agent-settings").hidden = !text;
  if (text === state.settingsKey) return;
  state.settingsKey = text;
  const rows = text.split("; ").filter(Boolean).map((part) => node("li", "", part[0].toUpperCase() + part.slice(1)));
  $("agent-settings-list").replaceChildren(...rows);
}

function signedPct(value) {
  const number = Number(value);
  return `${number < 0 ? "−" : "+"}${Math.abs(number).toFixed(2)}%`;
}

function asset() {
  return state.config.market.ticker.split("/")[0];
}

function seenParts(seen) {
  const parts = [];
  if (known(seen.mark)) parts.push(`Mark ${price(seen.mark)}`);
  if (seen.trend) parts.push(`Trend ${seen.trend}`);
  if (known(seen.atr_pct)) parts.push(`Hourly range ${Number(seen.atr_pct).toFixed(2)}%`);
  if (known(seen.ret_1h_pct)) parts.push(`1h ${signedPct(seen.ret_1h_pct)}`);
  if (known(seen.ret_4h_pct)) parts.push(`4h ${signedPct(seen.ret_4h_pct)}`);
  if (known(seen.ret_24h_pct)) parts.push(`24h ${signedPct(seen.ret_24h_pct)}`);
  if (known(seen.volatility)) parts.push(`Volatility ${Number(seen.volatility).toFixed(2)}×`);
  if (known(seen.funding_bps)) parts.push(`Funding ${Number(seen.funding_bps).toFixed(2)} bps`);
  return parts;
}

function chips(parts) {
  const row = node("span", "chips");
  parts.forEach((part, index) => {
    if (index) row.append(" ");
    row.append(node("span", "chip", part));
  });
  return row;
}

function checkRow(check) {
  const known_ = CHECK_LABELS[check.id];
  if (!known_) return null;
  const item = node("li", "check");
  item.dataset.ok = check.ok === null || check.ok === undefined ? "unknown" : String(check.ok);
  item.append(node("span", "check-name", known_.label));
  const value = check.value === null || check.value === undefined ? null : known_.value(check.value);
  item.append(node("span", "check-value", value || (check.ok === null || check.ok === undefined ? "Unknown" : "")));
  return item;
}

function checkList(setup) {
  const list = node("ul", "checks");
  list.append(...(setup.checks || []).map(checkRow).filter(Boolean));
  return list;
}

function choseLine(chose) {
  if (chose.action === "hold") return "Hold";
  if (chose.action === "close") return "Close the position";
  const side = chose.side === "short" ? "Short" : "Long";
  const target = known(chose.take_profit_atr) ? `take-profit ${chose.take_profit_atr}×` : "no take-profit";
  return `${side} · stop ${chose.stop_atr}× · ${target}`;
}

function serverLine(server) {
  const parts = [];
  if (known(server.quantity)) parts.push(`Size ${quantity(server.quantity)} ${asset()}`);
  if (known(server.entry_price)) parts.push(`Filled at ${price(server.entry_price)}`);
  if (known(server.stop_price)) parts.push(`Stop-loss ${price(server.stop_price)}`);
  if (known(server.take_profit_price)) parts.push(`Take-profit ${price(server.take_profit_price)}`);
  if (known(server.worst_loss_usd)) parts.push(`Worst case about ${signedMoney(-Number(server.worst_loss_usd))}`);
  if (server.exit_rule) parts.push("Closed by the strategy's exit rule");
  return parts.join(" · ");
}

function bps(value) {
  return `${Number(value) < 0 ? "\u2212" : "+"}${Math.abs(Number(value)).toFixed(2)} bps`;
}

function marketParts(market) {
  const parts = [];
  if (BIAS_LABELS[market.bias]) parts.push(BIAS_LABELS[market.bias]);
  if (EVENT_RISK_LABELS[market.event_risk]) parts.push(`Event risk ${EVENT_RISK_LABELS[market.event_risk].toLowerCase()}`);
  if (known(market.funding_avg_bps)) parts.push(`Funding ${bps(market.funding_avg_bps)}`);
  if (known(market.fear_greed)) parts.push(`Fear and Greed ${Number(market.fear_greed)}`);
  return parts;
}

function recalledLine(recalled) {
  const row = node("span", "decision-recall");
  const r = Number(recalled.r);
  const side = recalled.side === "short" ? "Short" : "Long";
  row.append(`${side}, `);
  row.append(node("span", r < 0 ? "loss" : "", `${r < 0 ? "\u2212" : "+"}${Math.abs(r).toFixed(2)}R`));
  const reasons = (recalled.matched || []).map((key) => MATCH_LABELS[key]).filter(Boolean);
  if (reasons.length) row.append(`. Same ${reasons.join(", ")}`);
  return row;
}

function why(decision) {
  const { snapshot: snap } = decision;
  const facts = node("dl", "facts decision-why");
  const add = (label, ...content) => {
    const value = node("dd");
    value.append(...content);
    facts.append(node("dt", "", label), value);
  };
  if (snap && snap.setup && (snap.setup.checks || []).length) add("Rules", checkList(snap.setup));
  if (snap && snap.counter) add("Wrong if", snap.counter);
  if (decision.reason) add("Stated reason", decision.reason);
  add("Model saw", snap && snap.seen ? chips(seenParts(snap.seen)) : "Not recorded");
  const looked = (snap && snap.looked ? snap.looked : []).map((tool) => TOOL_LABELS[tool]).filter(Boolean);
  if (looked.length) add("Looked at", chips(looked));
  const market = snap && snap.market ? marketParts(snap.market) : [];
  if (market.length) add("Market then", chips(market));
  if (snap && snap.recalled && snap.recalled.length) add("Recalled", ...snap.recalled.map(recalledLine));
  if (snap && snap.chose) add("Model chose", choseLine(snap.chose));
  const server = snap && snap.server ? snap.server : {};
  const set = serverLine(server);
  if (set) add("Server set", set);
  if (server.limit) add("Limit", node("code", "", server.limit));
  const links = (server.tx_hashes || []).map(explorerUrl).filter(Boolean).map((url) => {
    const link = node("a", "", "Explorer");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  });
  if (links.length) add("On chain", ...links);
  if (decision.code) add("Code", node("code", "", decision.code));
  return facts;
}

function renderChart(perf) {
  const holder = $("equity-chart");
  const caption = $("equity-caption");
  const key = JSON.stringify([perf.points, perf.transfers, perf.history_too_short]);
  if (state.chartKey === key) return;
  state.chartKey = key;
  caption.hidden = false;
  if (perf.history_too_short || perf.points.length < 2) {
    holder.replaceChildren();
    caption.textContent = "Too early to draw a line.";
    return;
  }
  const width = 320;
  const height = 72;
  const inset = 4;
  const values = perf.points.map((point) => point[1]);
  const start = Number(perf.start_equity);
  // At least 1% either side of the start, so a few cents of noise cannot look like a cliff.
  const low = Math.min(...values, start * 0.99);
  const high = Math.max(...values, start * 1.01);
  const firstMs = perf.points[0][0];
  const span = perf.points[perf.points.length - 1][0] - firstMs;
  const x = (ms) => (((ms - firstMs) / span) * width).toFixed(1);
  const y = (value) => (height - inset - ((value - low) / (high - low)) * (height - 2 * inset)).toFixed(1);
  const svg = shape("svg", {
    class: "equity-svg",
    viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: "none",
    role: "img",
    "aria-label": `Account value from ${amount(values[0])} to ${amount(values[values.length - 1])} USDC`,
  });
  perf.transfers.forEach((transfer) => {
    svg.append(shape("line", {
      class: "equity-transfer", x1: x(transfer.ts_ms), x2: x(transfer.ts_ms), y1: 0, y2: height, "vector-effect": "non-scaling-stroke",
    }));
  });
  const line = perf.points.map(([ms, value]) => `${x(ms)},${y(value)}`).join(" ");
  const area = `${x(perf.points[0][0])},${height} ${line} ${x(perf.points[perf.points.length - 1][0])},${height}`;
  svg.append(shape("polygon", { class: "equity-area", points: area }));
  svg.append(shape("polyline", { class: "equity-line", points: line, "vector-effect": "non-scaling-stroke" }));
  holder.replaceChildren(svg);
  const range = `${amount(Math.min(...values))} to ${amount(Math.max(...values))} USDC`;
  caption.textContent = perf.transfers.length ? `${range} · Marks are deposits and withdrawals` : range;
}

function shortDate(ms) {
  return new Date(ms).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

function renderResult(perf) {
  $("result").hidden = !perf;
  if (!perf) return;
  const number = $("result-number");
  const moved = perf.transfers.length > 0;
  const hasNet = known(perf.net_usd);
  // Transfers are worked out from balance changes, so with any in the run the figure is close, not exact.
  setText(number, hasNet ? `${moved ? "≈ " : ""}${signedMoney(perf.net_usd)}` : "Result unavailable");
  number.classList.toggle("loss", hasNet && Number(perf.net_usd) < 0);
  number.classList.toggle("gain", hasNet && Number(perf.net_usd) > 0);
  const period = perf.active ? `since ${shortDate(perf.since_ms)}` : `${shortDate(perf.since_ms)} to ${shortDate(perf.until_ms)}`;
  setText($("result-since"), `${period} · ${perf.trades} trade${perf.trades === 1 ? "" : "s"}`);
  const value = known(perf.equity) ? `${money(perf.equity)} ${perf.active ? "now" : "at stop"}` : "Account value unavailable";
  setText($("result-note"), `${value} · net of fees and funding`);
  renderChart(perf);
  const hold = $("result-hold");
  hold.hidden = !known(perf.hold_usd);
  if (!hold.hidden) setText(hold, `Holding ${asset()} instead: ≈ ${signedMoney(perf.hold_usd)}`);
}

function tradeKey(trade) {
  return `trade:${trade.opened_ms}`;
}

function decisionNear(decisions, outcome, ms, within) {
  return decisions.find((decision) => decision.outcome === outcome && Math.abs(decision.created_ms - ms) <= within) || null;
}

const moveWord = (decision) => OUTCOME_LABELS[decision.outcome] || "Decision";

function chartMarks(agent) {
  const marks = [];
  const trips = [];
  const decisions = agent.decisions.filter((decision) => decision.code !== "NO_CHANGE");
  const usedEntries = new Set();
  for (const trade of agent.trades) {
    if (!known(trade.entry_price) || !known(trade.exit_price)) continue;
    const entry = { ms: trade.opened_ms, price: Number(trade.entry_price) };
    const exit = { ms: trade.closed_ms, price: Number(trade.exit_price) };
    const win = known(trade.pnl_usd) ? Number(trade.pnl_usd) >= 0 : null;
    trips.push({ entry, exit, win });
    const opened = decisionNear(decisions, "entered", trade.opened_ms, ENTRY_MATCH_MS);
    if (opened) usedEntries.add(opened.created_ms);
    const side = trade.side === "short" ? "short" : "long";
    marks.push({ ...entry, kind: side, key: opened ? `entered:${opened.created_ms}` : tradeKey(trade), label: `Opened ${side} at ${clockDay(trade.opened_ms)}` });
    const closed = decisionNear(decisions, "closed", trade.closed_ms, ENTRY_MATCH_MS);
    marks.push({ ...exit, kind: win === null ? "exit" : win ? "win" : "loss", key: closed ? `closed:${closed.created_ms}` : tradeKey(trade), label: `${closeReasonLine(trade)} at ${clockDay(trade.closed_ms)}` });
  }
  const checks = [];
  for (const decision of decisions) {
    const seen = decision.snapshot && decision.snapshot.seen;
    if (decision.outcome === "hold") checks.push(decision.created_ms);
    if (!seen || !known(seen.mark) || usedEntries.has(decision.created_ms)) continue;
    const at = { ms: decision.created_ms, price: Number(seen.mark) };
    if (decision.outcome === "entered") {
      const filled = decision.snapshot.server && known(decision.snapshot.server.entry_price) ? Number(decision.snapshot.server.entry_price) : at.price;
      marks.push({ ...at, price: filled, kind: decision.side === "short" ? "short" : "long", key: `entered:${decision.created_ms}`, label: `Opened ${decision.side} at ${clockDay(decision.created_ms)}` });
    } else if (decision.outcome === "rejected" || decision.outcome === "unknown") {
      marks.push({ ...at, kind: "refused", key: `${decision.outcome}:${decision.created_ms}`, label: `${moveWord(decision)} at ${clockDay(decision.created_ms)}` });
    }
  }
  return { marks, trips, checks };
}

function clockDay(ms) {
  return decisionTime(ms);
}

function readCandles() {
  const body = state.candles;
  if (!body || !Array.isArray(body.candles)) return [];
  return body.candles
    .map((row) => row.map(Number))
    .filter((row) => row.length === 5 && row.every(Number.isFinite));
}

function chartPosition(position, opened) {
  if (!position) return null;
  return {
    entry: Number(position.entry_price),
    stop: known(position.stop_price) ? Number(position.stop_price) : null,
    take: known(position.take_profit_price) ? Number(position.take_profit_price) : null,
    openedMs: opened,
  };
}

function setQuote(candles, mark) {
  const last = mark !== null ? mark : candles[candles.length - 1][4];
  setText($("chart-price"), price(last));
  const dayAgo = candles[Math.max(0, candles.length - 25)][4];
  const change = ((last - dayAgo) / dayAgo) * 100;
  const label = $("chart-change");
  setText(label, `${signedPct(change)} 24h`);
  label.classList.toggle("loss", change < 0);
  label.classList.toggle("gain", change > 0);
}

function showCandle(candle) {
  const row = $("chart-ohlc");
  const overPrice = candle !== null;
  row.hidden = !overPrice;
  $("chart-price").hidden = overPrice;
  $("chart-change").hidden = overPrice;
  if (!overPrice) return;
  const [open, o, h, l, c] = candle;
  setText(row, `${decisionTime(open)}   O ${formatPrice(o)}  H ${formatPrice(h)}  L ${formatPrice(l)}  C ${formatPrice(c)}`);
}

function renderPriceChart(agent) {
  const holder = $("price-chart");
  const candles = readCandles();
  const empty = candles.length < 2;
  $("chart-empty").hidden = !empty;
  $("chart-source").hidden = empty || !state.candles || state.candles.source !== "binance";
  holder.hidden = empty;
  document.querySelectorAll(".segmented button").forEach((button) => {
    button.setAttribute("aria-pressed", String(Number(button.dataset.range) === state.chartRange));
  });
  if (empty) {
    state.priceChartKey = "";
    chartView = null;
    return;
  }
  const mark = state.candles && known(state.candles.mark) ? Number(state.candles.mark) : null;
  setQuote(candles, mark);
  const { marks, trips, checks } = chartMarks(agent);
  const position = chartPosition(agent.position, agent.position ? agent.position.opened_ms || null : null);
  const key = JSON.stringify([candles[candles.length - 1], candles.length, marks, trips, checks, position, mark, state.chartRange, holder.clientWidth, holder.clientHeight]);
  if (key === state.priceChartKey && chartView) return;
  state.priceChartKey = key;
  chartView = drawPriceChart(holder, {
    candles,
    periodMs: Number(state.candles.period_ms) || 3_600_000,
    rangeHours: state.chartRange,
    trades: trips,
    marks,
    checks,
    position,
    last: mark !== null ? mark : candles[candles.length - 1][4],
    selected: state.selectedKey,
    onSelect: selectMove,
    onHover: showCandle,
  });
}

function markSelected() {
  document.querySelectorAll(".decision.is-selected, .trade.is-selected").forEach((row) => row.classList.remove("is-selected"));
  if (state.selectedKey === null) return;
  const selector = `[data-key="${CSS.escape(state.selectedKey)}"]`;
  const row = $("decisions-list").querySelector(`details${selector}`);
  if (row) row.closest(".decision").classList.add("is-selected");
  const trade = $("trades-list").querySelector(`.trade${selector}`);
  if (trade) trade.classList.add("is-selected");
}

function revealRow(row) {
  const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  row.scrollIntoView({ block: "nearest", behavior: calm ? "auto" : "smooth" });
}

// From the chart: read that move. From the list, `chartView.select` alone moves the halo.
function selectMove(key) {
  state.selectedKey = key;
  if (chartView) chartView.select(key);
  const agent = state.agent;
  const rows = groupDecisions(agent.decisions.filter((decision) => decision.code !== "NO_CHANGE"));
  const index = rows.findIndex((row) => rowKey(row) === key);
  if (index >= state.decisionRows) {
    state.decisionRows = index + 1;
    renderDecisions(agent.decisions);
  }
  const selector = `[data-key="${CSS.escape(key)}"]`;
  const details = $("decisions-list").querySelector(`details${selector}`);
  markSelected();
  if (details) {
    details.open = true;
    revealRow(details.closest(".decision"));
    return;
  }
  const trade = $("trades-list").querySelector(`.trade${selector}`);
  if (trade) revealRow(trade);
}

function onFeedClick(event) {
  const summary = event.target.closest("summary");
  if (!summary) return;
  const details = summary.parentElement;
  if (details.dataset.key.startsWith("held:")) return;
  state.selectedKey = details.open ? null : details.dataset.key; // the click runs before it toggles
  if (chartView) chartView.select(state.selectedKey);
  markSelected();
}

function setRange(hours) {
  state.chartRange = hours;
  render();
}

function renderLimitMeter(daily) {
  const holder = $("limit-meter");
  if (!daily || !known(daily.limit_usd) || Number(daily.limit_usd) <= 0) {
    holder.replaceChildren();
    return;
  }
  const used = Math.max(0, Math.min(1, Number(daily.used_usd) / Number(daily.limit_usd)));
  const svg = shape("svg", { class: "meter", viewBox: "0 0 100 4", preserveAspectRatio: "none", "aria-hidden": "true" });
  svg.append(shape("rect", { class: "meter-track", width: 100, height: 4 }));
  svg.append(shape("rect", { class: `meter-used${used >= 0.75 ? " meter-near" : ""}`, width: (used * 100).toFixed(1), height: 4 }));
  holder.replaceChildren(svg);
}

function signedR(value) {
  const number = Number(value);
  return `${number < 0 ? "\u2212" : "+"}${Math.abs(number).toFixed(2)}R`;
}

function renderKpis(agent) {
  const { stats } = agent;
  const rated = stats && known(stats.win_rate) && stats.trades > 0;
  setFact("agent-winrate", rated ? `${Math.round(Number(stats.win_rate) * 100)}%` : null);
  renderLimitMeter(agent.daily_loss);
}

function positionMark(position) {
  const entry = Number(position.entry_price);
  const size = Number(position.quantity);
  if (!known(position.unrealized_pnl_usd) || !size) return entry;
  const moved = Number(position.unrealized_pnl_usd) / size;
  return position.side === "short" ? entry - moved : entry + moved;
}

function renderWaiting(agent, position) {
  const box = $("waiting");
  const latest = agent.decisions.find((decision) => decision.code !== "NO_CHANGE");
  const setup = !position && !agent.position_unavailable && isRunning() && latest && latest.snapshot ? latest.snapshot.setup : null;
  box.hidden = !setup;
  if (!setup) return;
  const failing = (setup.checks || []).filter((check) => check.ok === false).length;
  setText($("waiting-title"), setup.ready ? "A setup is ready. The AI decides if it is worth taking." : failing ? "Waiting for the rules to line up" : "Waiting for a setup");
  $("waiting-list").replaceChildren(...(setup.checks || []).map(checkRow).filter(Boolean));
}

function exitRuleLine(agent, position) {
  const latest = agent.decisions.find((decision) => decision.snapshot && decision.snapshot.setup);
  const signal = position && latest ? latest.snapshot.setup.exit_signal : null;
  if (signal === null || signal === undefined) return null;
  if (!signal) return "Not reached";
  return isRunning() && !agent.halted_by_operator ? "Reached. It closes at the next check." : "Reached";
}

function renderPosition(agent) {
  const { position } = agent;
  setFact("agent-exit", exitRuleLine(agent, position));
  const ladder = $("position-ladder");
  const age = $("position-age");
  const opened = position && known(position.opened_ms) ? Number(position.opened_ms) : null;
  setText(age, opened !== null ? `${duration(agent.now_ms - opened)} in` : "");
  ladder.hidden = !position || !known(position.stop_price);
  if (!ladder.hidden) {
    drawLadder(ladder, {
      stop: Number(position.stop_price),
      entry: Number(position.entry_price),
      take: known(position.take_profit_price) ? Number(position.take_profit_price) : null,
      mark: positionMark(position),
      label: known(position.unrealized_r) ? signedR(position.unrealized_r) : "",
    });
  } else {
    ladder.replaceChildren();
  }
  renderWaiting(agent, position);
}

const TREND_PHRASES = { up: "an uptrend", down: "a downtrend", flat: "a flat market" };

function lessonRow(entry) {
  const item = node("li", "lesson");
  item.append(node("p", "", entry.lesson));
  if (known(entry.r)) item.append(node("span", `lesson-r${Number(entry.r) < 0 ? " loss" : " gain"}`, signedR(entry.r)));
  return item;
}

function playbookRow(row) {
  const item = node("li");
  const side = row.side === "short" ? "Short" : "Long";
  item.append(node("span", "play-name", `${side} in ${TREND_PHRASES[row.trend] || "any market"}`));
  item.append(node("span", "play-count", `${row.trades} trade${row.trades === 1 ? "" : "s"}`));
  item.append(node("span", "play-win", `${Math.round(Number(row.win_rate) * 100)}%`));
  item.append(node("span", `play-r${Number(row.avg_r) < 0 ? " loss" : " gain"}`, signedR(row.avg_r)));
  return item;
}

function renderMemory(agent) {
  const memory = agent.memory || { cases: 0, lessons: [], playbook: [] };
  const lessons = memory.lessons || [];
  const playbook = memory.playbook || [];
  setText($("memory-count"), memory.cases ? `${memory.cases} trade${memory.cases === 1 ? "" : "s"} reviewed` : "");
  $("memory-empty").hidden = lessons.length > 0 || playbook.length > 0;
  $("memory-lessons").hidden = lessons.length === 0;
  $("memory-playbook").hidden = playbook.length === 0;
  $("memory-note").hidden = lessons.length === 0 && playbook.length === 0;
  $("memory-lessons").replaceChildren(...lessons.slice(0, 3).map(lessonRow));
  $("memory-playbook").replaceChildren(...playbook.slice(0, 4).map(playbookRow));
}

function drawDashboard() {
  if (state.agent && !$("agent").hidden && !state.sessionLost) {
    renderPriceChart(state.agent);
    renderPosition(state.agent);
  }
}

function positionOpenKnown() {
  return Boolean(state.agent && state.agent.position) || state.revokedPosition === true;
}

// Reads no more than what the page already has. The withdrawal itself never touches our server.
function feedLink(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" && FEED_HOSTS.includes(parsed.hostname) ? parsed.href : null;
  } catch {
    return null;
  }
}

function ago(ms, now) {
  const minutes = Math.max(0, Math.round((now - ms) / 60000));
  return minutes < 60 ? `${minutes} min ago` : `${Math.round(minutes / 60)} h ago`;
}

function headlineRow(headline, now) {
  const item = node("li");
  const url = feedLink(headline.link);
  if (url) {
    const link = node("a", "", headline.title);
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    item.append(link);
  } else {
    item.append(node("span", "", headline.title));
  }
  item.append(node("span", "market-source", ` ${headline.source} · ${ago(headline.published_ms, now)}`));
  return item;
}

function renderMarket() {
  const panel = $("market");
  const agent = state.agent;
  const market = agent && !state.sessionLost && !state.revoked && hasStarted() ? agent.market : null;
  panel.hidden = !market;
  if (!market) {
    state.marketKey = "";
    return;
  }
  const key = JSON.stringify(market);
  if (key === state.marketKey) return; // Rebuilt only on a change, so a poll keeps any selection.
  state.marketKey = key;
  setFact("market-read", BIAS_LABELS[market.bias] || null);
  setFact("market-risk", EVENT_RISK_LABELS[market.event_risk] || null);
  const themes = (market.themes || []).map((theme) => THEME_LABELS[theme]).filter(Boolean);
  setFact("market-themes", themes.length ? themes.join(" · ") : null);
  setFact("market-funding", known(market.funding_avg_bps) ? bps(market.funding_avg_bps) : null);
  showFact("market-fear", known(market.fear_greed));
  setText($("market-fear-value"), known(market.fear_greed) ? String(market.fear_greed) : "");
  const summary = $("market-summary");
  summary.hidden = !market.summary;
  setText(summary, market.summary || "");
  const headlines = market.headlines || [];
  $("market-news").replaceChildren(...headlines.slice(0, 5).map((headline) => headlineRow(headline, agent.now_ms)));
  $("market-news").hidden = headlines.length === 0;
}

function renderFunds() {
  const connected = Boolean(state.eth);
  const read = state.equityRead && known(state.available);
  const show = connected && (read ? Number(state.available) > 0 : state.revoked || walletExists());
  $("funds").hidden = !show;
  if (!show) return;
  setText($("funds-available"), read ? `${money(state.available)} available` : "Available amount unavailable");
  $("withdraw-open").hidden = state.withdrawOpen;
  $("withdraw-form").hidden = !state.withdrawOpen;
  $("withdraw-warning").hidden = !positionOpenKnown();
  $("withdraw-max").hidden = !read;
}

function renderAgent() {
  const panel = $("agent");
  const { agent } = state;
  const lost = state.sessionLost;
  const show = walletExists() && !state.revoked && (lost || (hasStarted() && (agent !== null || state.agentReadFailed)));
  panel.hidden = !show;
  $("headline").hidden = !show;
  renderSetup(show);
  if (!show) {
    $("agent-telegram").hidden = true;
    return;
  }

  const running = isRunning();
  renderStatus();
  $("agent-signin").hidden = !lost;

  const note = $("agent-note");
  note.hidden = !state.agentReadFailed;
  if (state.agentReadFailed) {
    setText(note, agent ? `Could not refresh. Last read ${clockTime(state.agentReadMs)}.` : "Could not read your agent. Trying again shortly.");
  }

  const shown = !lost && agent !== null;
  renderResult(shown ? agent.performance : null);
  $("agent-facts").hidden = !shown;
  $("agent-decisions").hidden = !shown;
  $("agent-trades").hidden = !shown;
  $("chart-pane").hidden = !shown;
  $("position-pane").hidden = !shown;
  $("memory").hidden = !shown;
  $("stop-btn").hidden = !running;
  $("stop-note").hidden = !running;
  $("start-again-btn").hidden = !shown || running;
  renderSettings(shown ? agent.settings : null);

  let position = null;
  if (shown) {
    position = agent.position;
    const noStop = Boolean(position) && position.stop_price === null;
    setFact("agent-preset", strategyLabel(agent));
    // While running, /onboard/agent reads the chain live; /onboard/status can be 10 s behind it.
    const live = agent.performance && agent.performance.active && known(agent.performance.equity) ? agent.performance.equity : null;
    setFact("agent-equity", live !== null ? money(live) : state.equityRead ? money(state.balance) : NOT_READ);
    setFact("agent-position", agent.position_unavailable ? "Could not be read" : position ? positionLine(position) : "No open position");
    setFact("agent-pnl", position ? signedMoney(position.unrealized_pnl_usd) : null);
    const drift = position ? Number(position.unrealized_pnl_usd) : 0;
    $("agent-pnl").classList.toggle("gain", drift > 0);
    $("agent-pnl").classList.toggle("loss", drift < 0);
    showFact("agent-stop", Boolean(position));
    if (position) {
      setText($("agent-stop-price"), noStop ? "None on chain" : price(position.stop_price));
      const proof = noStop ? null : explorerUrl(position.stop_tx_hash);
      $("agent-stop-link").hidden = proof === null;
      if (proof) $("agent-stop-link").href = proof;
    }
    $("agent-stop").classList.toggle("alarm", noStop);
    setFact("agent-loss", position && known(position.loss_if_stopped_usd) ? `about ${signedMoney(-Number(position.loss_if_stopped_usd))}` : null);
    setFact("agent-take", position ? (position.take_profit_price === null ? "None" : price(position.take_profit_price)) : null);
    setFact("agent-limit", agent.daily_loss ? `${amount(agent.daily_loss.used_usd)} of ${money(agent.daily_loss.limit_usd)}` : "Could not be read");
    renderDecisions(agent.decisions);
    renderTrades(agent.trades);
    renderKpis(agent);
    renderMemory(agent);
    markSelected();
    renderPriceChart(agent);
    renderPosition(agent);
  }

  // A confirmation is about one position: if that one is gone, so is the question.
  const key = position ? `${position.side}:${position.quantity}:${position.entry_price}` : "";
  if (key !== state.closeKey) {
    state.closeKey = key;
    state.closing = false;
  }
  $("close-btn").hidden = !position || state.closing;
  $("close-confirm").hidden = !position || !state.closing;
  $("agent-actions").hidden = state.closing || (!running && !shown);

  const telegram = shown ? agent.telegram : null;
  const offered = Boolean(telegram && telegram.available);
  $("agent-telegram").hidden = !offered;
  if (offered) {
    const link = state.telegramLink;
    const live = !telegram.linked && link !== null && link.expiresMs > Date.now();
    $("telegram-state").hidden = !telegram.linked;
    if (telegram.linked) setText($("telegram-state"), telegram.display ? `Connected as ${telegram.display}` : "Connected");
    $("telegram-disconnect").hidden = !telegram.linked;
    $("telegram-connect").hidden = telegram.linked || live;
    $("telegram-open").hidden = !live;
    $("telegram-hint").hidden = !live;
  }
}

function render() {
  const { config } = state;
  if (!config) return;
  const existing = state.userStatus === "active" || state.userStatus === "paused";
  const connected = Boolean(state.eth);

  // Step 1
  $("connect-info").hidden = !connected;
  $("connect-btn").hidden = connected;
  $("prove-btn").hidden = !connected || state.verified;
  if (state.verified) {
    setStep("connect", "done", `Verified ${shortAddress(state.eth)}`);
  } else if (connected) {
    setStep("connect", "active", `Connected ${shortAddress(state.eth)}. Sign the message to continue.`);
  } else {
    setStep("connect", "active", "Not connected");
  }

  // Step 2
  const fundedEnough = existing || balanceOk();
  if (!state.verified) {
    setStep("deposit", "pending", "Waiting for step 1");
  } else if (state.balance > Number(config.max_deposit_usd) && !existing) {
    setStep("deposit", "blocked", `Above the ${config.max_deposit_usd} USDC limit. Withdraw the excess to continue.`);
  } else if (fundedEnough) {
    setStep("deposit", "done", `${formatUsdc(state.balance)} deposited`);
  } else if (state.balance > 0) {
    setStep("deposit", "active", `Below the ${config.min_deposit_usd} USDC minimum`);
  } else {
    setStep("deposit", "active", "Trading subaccount is empty");
  }

  // Step 3
  if (!state.verified || (!fundedEnough && !existing)) {
    setStep("grant", "pending", state.verified ? "Waiting for your deposit" : "Waiting for step 1");
  } else if (state.grantOk) {
    const until = state.grantExpiresMs ? `, expires ${formatDate(state.grantExpiresMs)}` : "";
    setStep("grant", "done", `Active${until}`);
  } else {
    setStep("grant", "active", existing ? "Grant expired or missing. Sign again to renew." : "Not signed yet");
  }

  // Step 4
  const ready = state.verified && (existing || (fundedEnough && state.grantOk));
  $("agent-unavailable").hidden = Boolean(config.agent_available);
  $("start-caution").hidden = !config.agent_available;
  if (state.verified && isRunning()) {
    setStep("strategy", "done", state.agent ? strategyLabel(state.agent) : "");
  } else if (ready) {
    setStep("strategy", "active", "");
  } else {
    setStep("strategy", "pending", "Waiting for the steps above");
  }
  if (state.agent && state.agent.preset && !chosenPreset()) {
    const previous = document.querySelector(`input[name='preset'][value="${CSS.escape(state.agent.preset)}"]`);
    if (previous) previous.checked = true;
  }
  seedTuning();
  syncStart();

  renderAgent();
  renderMarket();
  renderFunds();
  $("wallet-chip").hidden = !connected;
  if (connected) setText($("wallet-chip"), shortAddress(state.eth));

  // Access
  $("manage").hidden = !(existing || state.revoked || state.chainRevokeOffer);
  const manageNote = state.revoked
    ? "Access is revoked for this wallet."
    : existing
      ? ""
      : "Our server cannot confirm this wallet right now. You can still remove the trading grant on chain.";
  $("manage-state").hidden = !manageNote;
  $("manage-state").textContent = manageNote;
  $("revoke-btn").hidden = !existing || state.revoked || state.revokeAsk;
  $("revoke-confirm").hidden = !existing || state.revoked || !state.revokeAsk;
  renderRevokedPosition();
  if (state.demo) lockDemo();
  if (chat) chat.sync();
}

const REVOKED_POSITION_NOTE = {
  open: "Your position is still open. Close it before removing the grant: after that our server cannot.",
  openNoGrant: "Your position is still open with its on-chain stop. Our server can no longer close it.",
  unread: "We could not check for an open position. If you have one, close it before removing the grant.",
  unreadNoGrant: "We could not check for an open position. If you have one, our server can no longer close it.",
  closed: "Your position is closed.",
  flat: "There is no open position.",
};

function renderRevokedPosition() {
  const known = state.revokedPosition;
  const noGrant = state.grantRemoved ? "NoGrant" : "";
  const key = known === true ? `open${noGrant}` : known === null ? `unread${noGrant}` : known;
  const closable = state.revoked && (known === true || known === null) && !state.grantRemoved;
  const note = $("revoke-position");
  note.hidden = !state.revoked || known === false;
  if (!note.hidden) setText(note, REVOKED_POSITION_NOTE[key]);
  $("revoked-close-btn").hidden = !closable || state.revokedCloseAsk;
  $("revoked-close-confirm").hidden = !closable || !state.revokedCloseAsk;
  $("revoke-chain-btn").hidden = state.grantRemoved;
  $("revoke-once").hidden = state.grantRemoved;
  // While something can still be closed, removing the grant is not the one step left.
  if (state.revoked) setText($("revoke-once"), closable ? "The agent has stopped." : "The agent has stopped. One step is left: remove the grant on chain.");
}

async function refreshStatus() {
  if (!state.session) return;
  const status = await api.status(state.eth, state.session);
  state.userStatus = status.user_status;
  state.balance = Number(status.balance_usd);
  state.grantOk = status.grant_ok;
  state.grantExpiresMs = status.grant_expires_ms;
  state.available = status.available_usd;
  state.equityRead = true;
  $("sub-usdc").textContent = formatUsdc(status.balance_usd);
  render();
}

async function refreshWalletBalance() {
  if (!state.inj) return;
  try {
    const amount = await fetchBankBalance(state.rest, state.inj, state.config.market.quote_denom);
    $("wallet-usdc").textContent = formatUsdc(fromBaseUnits(amount, USDC_DECIMALS));
  } catch {
    $("wallet-usdc").textContent = "Could not read";
  }
}

function isSessionEnd(error) {
  return error instanceof ApiError && error.code === "SESSION_REQUIRED";
}

function sessionEnded() {
  state.sessionLost = !$("agent").hidden;
  clearSession();
  clearPoll();
  if (chat) chat.reset();
  Object.assign(state, { agent: null, agentReadFailed: false, equityRead: false, available: null, closing: false, telegramLink: null });
  render();
}

// `readStatus` is off only when the caller has just read /onboard/status itself.
async function refreshAgent({ readStatus = true } = {}) {
  if (!state.session) return;
  const { eth, session } = state;
  state.agentTicket += 1;
  const ticket = state.agentTicket;
  const current = () => ticket === state.agentTicket && state.session === session;
  if (readStatus) {
    try {
      await refreshStatus();
      state.equityRead = true;
    } catch (error) {
      if (!current()) return;
      if (isSessionEnd(error)) {
        sessionEnded();
        return;
      }
      state.equityRead = false; // Shown as unread, never as a zero balance.
    }
  } else {
    state.equityRead = true;
  }
  if (!current() || !walletExists()) return;
  let agent;
  try {
    agent = await api.agent(eth, session);
  } catch (error) {
    if (!current()) return;
    if (isSessionEnd(error)) {
      sessionEnded();
      return;
    }
    state.agentReadFailed = true;
    render();
    return;
  }
  if (!current()) return;
  state.agent = agent;
  state.agentReadFailed = false;
  state.agentReadMs = Date.now();
  state.userStatus = agent.status;
  render();
  refreshCandles();
}

// Market data, so it can fail without saying anything about the agent. The last good read stays.
async function refreshCandles() {
  if (state.demo || Date.now() - state.candlesReadMs < CANDLES_POLL_MS) return;
  state.candlesReadMs = Date.now();
  try {
    const body = await getCandles();
    if (!state.candles || body.candles.length) state.candles = body; // an empty answer keeps the last good chart
  } catch {
    return;
  }
  if (state.agent) render();
}

function clearPoll() {
  clearTimeout(pollTimer);
  pollTimer = null;
}

function schedulePoll() {
  clearPoll();
  if (!state.session || !walletExists() || document.visibilityState !== "visible") return;
  pollTimer = setTimeout(pollAgent, AGENT_POLL_MS);
}

async function pollAgent() {
  clearPoll();
  await refreshAgent();
  schedulePoll();
}

function adoptAddress(eth) {
  const identity = identityFor(eth);
  state.eth = eth;
  state.identity = identity;
  state.inj = identity.inj;
  state.sub = identity.sub;
  bindText("eth", eth);
  bindText("inj", state.inj);
  bindText("sub", state.sub);
  render();
}

async function challengeFor(action, preset, tuning) {
  const info = await api.start(state.eth, action, preset, tuning);
  assertChallengeMatches(info, state.identity, window.location.host, action);
  return info;
}

async function onConnect() {
  showError("connect", "");
  setBusy(true);
  try {
    const eth = await connectWallet();
    adoptAddress(eth);
  } catch (error) {
    showError("connect", message(error, COPY.WALLET_ERROR));
    if (error instanceof WalletError && error.code === "NO_WALLET") $("no-wallet").hidden = false;
  } finally {
    setBusy(false);
  }
}

async function signIn() {
  const info = await challengeFor("onboard");
  const signature = await personalSign(state.eth, info.message);
  const proof = await api.verify(state.eth, signature);
  Object.assign(state, {
    session: proof.session, sessionExpires: proof.expires_ms, verified: true, userStatus: proof.user_status,
    chainRevokeOffer: false, sessionLost: false,
  });
  saveSession();
}

// A returning wallet that revoked with a position open can still close it, until the grant is gone.
async function readRevokedPosition() {
  if (state.userStatus !== "revoked" || !state.session || state.revoked) return;
  let agent;
  try {
    agent = await api.agent(state.eth, state.session);
  } catch (error) {
    if (isSessionEnd(error)) return;
    agent = { position: null, position_unavailable: true };
  }
  if (!agent.position && !agent.position_unavailable) return;
  state.revoked = true;
  state.revokedPosition = agent.position ? true : null;
  state.grantRemoved = !state.grantOk;
  showChainRevoke(true);
  render();
}

async function onProve() {
  showError("connect", "");
  setBusy(true);
  try {
    await signIn();
    showError($("agent"), "");
    if (!state.revoked) $("revoke-followup").hidden = true;
    await refreshStatus();
    await refreshWalletBalance();
    await refreshAgent({ readStatus: false });
    await readRevokedPosition();
    schedulePoll();
  } catch (error) {
    showError(state.sessionLost ? $("agent") : "connect", message(error, COPY.WALLET_ERROR));
    // The server may be the problem, not the wallet. A user who cannot sign in can still end the grant.
    if ((error instanceof ApiError && error.code !== "BAD_SIGNATURE") || error instanceof GuardError) {
      state.chainRevokeOffer = true;
      render();
      showChainRevoke(false);
    }
  } finally {
    setBusy(false);
  }
}

// Sign one Injective transaction with MetaMask and wait until it lands in a block.
async function sendTx(msgs, gas) {
  const { checked, eth, inj, rest } = state;
  await ensureEvmChain(checked.evmChainId);
  let account;
  let height;
  let baseFee;
  try {
    [account, height, baseFee] = await Promise.all([fetchAccount(rest, inj), fetchLatestHeight(rest), fetchBaseFee(rest)]);
  } catch {
    throw new WalletError("LCD_ERROR");
  }
  if (!account) throw new WalletError("NO_CHAIN_ACCOUNT");

  const fee = feeFor(gas, baseFee);
  const timeoutHeight = height + TIMEOUT_BLOCKS;
  const typedData = buildTypedData({
    msgs,
    chainId: checked.chainId,
    evmChainId: checked.evmChainId,
    accountNumber: account.accountNumber,
    sequence: account.sequence,
    timeoutHeight,
    fee,
  });
  const signature = await signTypedData(eth, typedData);
  const txRaw = await assembleTx({
    msgs,
    chainId: checked.chainId,
    evmChainId: checked.evmChainId,
    accountNumber: account.accountNumber,
    sequence: account.sequence,
    timeoutHeight,
    fee,
    typedData,
    signature,
    accountPubKeyBase64: account.pubKey && account.pubKey.key ? account.pubKey.key : null,
  });
  return broadcast(rest, txRaw);
}

// Returns null when there is no session to read status with (it ended while the page was open).
async function pollStatus(until) {
  if (!state.session) return null;
  for (let i = 0; i < STATUS_POLLS; i += 1) {
    await refreshStatus();
    if (until()) return true;
    await new Promise((resolve) => setTimeout(resolve, STATUS_POLL_MS));
  }
  return false;
}

function amountFromInput() {
  const raw = $("amount-input").value;
  const base = toBaseUnits(raw, USDC_DECIMALS);
  if (base === null) throw new WalletError("AMOUNT_INVALID");
  if (base === 0n) throw new WalletError("AMOUNT_ZERO");
  const after = Number(fromBaseUnits(base, USDC_DECIMALS)) + state.balance;
  if (after > Number(state.config.max_deposit_usd)) throw new WalletError("AMOUNT_OVER");
  if (after < Number(state.config.min_deposit_usd)) throw new WalletError("AMOUNT_BELOW_MIN");
  return base;
}

async function onDeposit() {
  showError("deposit", "");
  showNote("deposit", "");
  setBusy(true);
  try {
    const amount = amountFromInput();
    const before = state.balance;
    const msg = buildDepositMsg({
      injAddress: state.inj,
      subaccountId: state.sub,
      denom: state.config.market.quote_denom,
      amount: amount.toString(),
    });
    await sendTx(msg, GAS_DEPOSIT);
    $("amount-input").value = "";
    const landed = await pollStatus(() => state.balance > before);
    if (landed === false) showError("deposit", "The deposit was sent but your balance has not updated yet. Wait a moment and reload.");
    if (landed === null) showNote("deposit", "Deposit confirmed on chain. Reload and sign in again to see your new balance.");
    await refreshWalletBalance();
  } catch (error) {
    showError("deposit", txMessage(error));
  } finally {
    setBusy(false);
  }
}

function withdrawAmount() {
  const base = toBaseUnits($("withdraw-amount").value, USDC_DECIMALS);
  if (base === null) throw new WalletError("AMOUNT_INVALID");
  if (base === 0n) throw new WalletError("AMOUNT_ZERO");
  if (state.equityRead && known(state.available) && base > toBaseUnits(sixDecimals(state.available), USDC_DECIMALS)) throw new WalletError("WITHDRAW_OVER");
  return base;
}

function sixDecimals(text) {
  const [whole, fraction = ""] = String(text).split(".");
  return fraction ? `${whole}.${fraction.slice(0, USDC_DECIMALS)}` : whole;
}

function openWithdraw() {
  state.withdrawOpen = true;
  render();
  $("withdraw-amount").focus();
}

function fillWithdrawMax() {
  showError($("funds"), "");
  $("withdraw-amount").value = sixDecimals(state.available);
}

// Signed by the owner's wallet and broadcast straight to the chain. Our server sees none of it.
async function onWithdraw() {
  const note = $("withdraw-note");
  showError($("funds"), "");
  note.hidden = true;
  setBusy(true);
  try {
    const before = Number(state.available);
    // The sender and subaccount come from this wallet, worked out here. No server value reaches them.
    const msg = buildWithdrawMsg({
      injAddress: state.inj,
      subaccountId: state.sub,
      denom: state.config.market.quote_denom,
      amount: withdrawAmount().toString(),
    });
    await sendTx(msg, GAS_WITHDRAW);
    $("withdraw-amount").value = "";
    note.hidden = false;
    note.textContent = "Sent to your wallet.";
    await pollStatus(() => Number(state.available) < before);
    await refreshWalletBalance();
  } catch (error) {
    showError($("funds"), txMessage(error));
  } finally {
    setBusy(false);
    render();
  }
}

function txMessage(error) {
  if (error instanceof WalletError || error instanceof GuardError) return COPY[error.code] || COPY.WALLET_ERROR;
  if (error instanceof ApiError) return COPY[error.code] || COPY.WALLET_ERROR;
  return COPY.TX_FAILED;
}

async function onGrant() {
  showError("grant", "");
  showNote("grant", "");
  setBusy(true);
  try {
    const { checked } = state;
    const msgs = buildGrantMsgs({
      granter: state.inj,
      grantee: checked.grantee,
      subaccountId: state.sub,
      marketId: checked.marketId,
      grants: checked.grants,
      expiresUnix: grantExpiryUnix(Date.now(), checked.ttlSeconds),
    });
    await sendTx(msgs, GAS_GRANT);
    const landed = await pollStatus(() => state.grantOk);
    if (landed === false) showError("grant", "The grant was sent but is not visible on chain yet. Wait a moment and reload.");
    if (landed === null) showNote("grant", "Grant confirmed on chain.");
  } catch (error) {
    showError("grant", txMessage(error));
  } finally {
    setBusy(false);
  }
}

function lockPresets(locked) {
  document.querySelectorAll("input[name='preset']").forEach((input) => {
    input.disabled = locked;
  });
  lockTune(locked);
}

// The preset and its settings are in the signed text, so a signature is good for that choice alone.
// The choice stays open during the wallet prompt, and a change made there asks for a new signature.
async function signResume() {
  for (;;) {
    const preset = chosenPreset();
    const tuning = chosenTuning();
    const chosen = stable(tuning);
    const info = await challengeFor("resume", preset, tuning);
    lockPresets(false);
    const signature = await personalSign(state.eth, info.message);
    lockPresets(true);
    if (chosenPreset() === preset && stable(chosenTuning()) === chosen) return { preset, tuning, signature };
  }
}

async function onStart() {
  if (!chosenPreset() || !state.config.agent_available) return;
  showError("strategy", "");
  setBusy(true);
  try {
    if (!hasStartedBefore()) {
      // Creates the wallet `paused`. Nothing trades until the signature below.
      const created = await api.complete(state.eth, state.session);
      state.userStatus = created.status;
      Object.assign(state, { revoked: false, revokedPosition: false, revokedCloseAsk: false, grantRemoved: false }); // A wallet returning after revoke is back.
      $("revoke-followup").hidden = true;
      render();
    }
    const { preset, tuning, signature } = await signResume();
    const result = await api.resume(state.eth, signature, preset, tuning);
    state.userStatus = result.status;
    state.setupOpen = null;
    await refreshAgent();
    $("agent-status").focus();
  } catch (error) {
    if (isSessionEnd(error)) {
      sessionEnded(); // Step 4 folds away with the session, so the message goes where the action is.
      showError("connect", COPY.SESSION_REQUIRED);
    } else {
      showError("strategy", actionMessage(error));
    }
  } finally {
    setBusy(false);
    schedulePoll();
  }
}

// A wallet with a strategy row is past /onboard/complete. Any other wallet goes through it.
function hasStartedBefore() {
  return Boolean(state.agent && state.agent.preset);
}

// ---------------------------------------------------------------------------------------- chat
// The owner asks their agent about its state and for changes to its settings. The agent proposes;
// a change is applied only by the wallet signing the same message Start signs.

/** The chat is for a wallet whose agent has started, signed in, with the model configured. */
function chatAvailable() {
  if (state.demo) return true;
  const { config } = state;
  return Boolean(
    config && config.agent_available && state.session && walletExists() && hasStarted() && !state.revoked && !state.sessionLost && state.agent !== null,
  );
}

function sendChat(text) {
  if (!state.demo) return api.chat(state.eth, state.session, text);
  const { agent } = state;
  const answer = state.demoChat.demoAnswer(text, { agent, spec: tuningSpec(agent.preset), name: presetName(agent.preset) });
  return new Promise((resolve) => {
    setTimeout(() => resolve({ ...answer, notice: false }), DEMO_REPLY_MS);
  });
}

// The settings are in the text the wallet shows. A message without the proposed line is never signed.
async function applyProposal(proposal) {
  if (state.demo) {
    state.demoChat.demoApply(state.agent, proposal, tuningSpec(proposal.preset));
    render();
    return;
  }
  setBusy(true);
  try {
    const info = await challengeFor("resume", proposal.preset, proposal.tuning);
    if (typeof info.message !== "string" || !proposal.summary || !info.message.includes(proposal.summary)) throw new GuardError("CHALLENGE_REFUSED");
    const signature = await personalSign(state.eth, info.message);
    const result = await api.resume(state.eth, signature, proposal.preset, proposal.tuning);
    state.userStatus = result.status;
    const spec = tuningSpec(proposal.preset);
    if (spec) state.tuning[proposal.preset] = sanitize(spec, proposal.tuning); // the editor shows what now runs
    document.querySelectorAll("input[name='preset']").forEach((input) => {
      if (input.value === proposal.preset) input.checked = true;
    });
    state.setupOpen = null;
    await refreshAgent();
  } finally {
    setBusy(false);
    schedulePoll();
  }
}

function chatError(error, forCard) {
  if (forCard) return actionMessage(error);
  if (error instanceof ApiError && (error.code === "RATE_LIMITED" || error.code === "NETWORK")) return COPY[error.code];
  return COPY.CHAT_FAILED;
}

function chatSessionEnded(error) {
  if (!isSessionEnd(error)) return false;
  sessionEnded();
  showError("connect", COPY.SESSION_REQUIRED);
  return true;
}

function bindChat() {
  chat = createChat({
    available: chatAvailable,
    demo: () => state.demo,
    busy: () => state.busy,
    running: isRunning,
    status: () => statusParts().map((part) => part.text).join(" "),
    spec: tuningSpec,
    presetName,
    current: () => (state.agent ? { preset: state.agent.preset, tuning: state.agent.tuning } : null),
    send: sendChat,
    history: () => api.chatHistory(state.eth, state.session),
    apply: applyProposal,
    explain: chatError,
    ended: chatSessionEnded,
  });
}

function askClose() {
  showError($("agent"), "");
  state.closing = true;
  render();
  $("close-no").focus(); // The safe answer is the one under the keyboard.
}

function keepPosition() {
  state.closing = false;
  render();
  $("close-btn").focus();
}

const CLOSE_RESULT = {
  failed: "The close failed on chain. The position is still open. Try again.",
  unknown: "The close was sent but is not confirmed yet. Check back in a minute.",
};

// A close the server could not answer for may still have gone out, so it is never called a failure.
function closeMessage(error) {
  return message(error, error instanceof ApiError ? CLOSE_RESULT.unknown : COPY.WALLET_ERROR);
}

async function confirmClose() {
  showError($("agent"), "");
  setBusy(true);
  try {
    const result = await api.agentClose(state.eth, state.session);
    state.closing = false;
    if (result.outcome !== "closed") showError($("agent"), CLOSE_RESULT[result.outcome] || CLOSE_RESULT.unknown);
    await refreshAgent();
  } catch (error) {
    if (isSessionEnd(error)) sessionEnded();
    else showError($("agent"), closeMessage(error));
    state.closing = false;
    render();
  } finally {
    setBusy(false);
  }
}

function agentActionFailed(error) {
  if (isSessionEnd(error)) {
    sessionEnded();
    return;
  }
  showError($("agent"), actionMessage(error));
}

function assertTelegramUrl(raw) {
  let url;
  try {
    url = new URL(raw);
  } catch {
    throw new ApiError("BAD_LINK", null, {});
  }
  if (url.protocol !== "https:" || url.hostname !== "t.me") throw new ApiError("BAD_LINK", null, {});
}

async function connectTelegram() {
  showError($("agent"), "");
  setBusy(true);
  try {
    const info = await challengeFor("telegram");
    const signature = await personalSign(state.eth, info.message);
    const link = await api.telegramLink(state.eth, signature);
    assertTelegramUrl(link.url);
    state.telegramLink = { url: link.url, expiresMs: link.expires_ms };
    const anchor = $("telegram-open");
    anchor.href = link.url;
    render();
    // A wallet prompt usually outlasts the click that allows a new tab, and a browser would block
    // the attempt with a warning. Then the visible link is the way on, opened by a real click.
    if (navigator.userActivation && navigator.userActivation.isActive) anchor.click();
  } catch (error) {
    agentActionFailed(error);
    if (error instanceof ApiError && error.code === "TELEGRAM_ALREADY_LINKED") await refreshAgent();
  } finally {
    setBusy(false);
  }
}

async function disconnectTelegram() {
  showError($("agent"), "");
  setBusy(true);
  try {
    await api.telegramUnlink(state.eth, state.session);
    state.telegramLink = null;
    await refreshAgent();
  } catch (error) {
    agentActionFailed(error);
  } finally {
    setBusy(false);
  }
}

function revokeFailed(error) {
  showError($("manage"), actionMessage(error));
  // A server failure must not stand between the user and ending the grant.
  if (error instanceof ApiError || error instanceof GuardError) showChainRevoke(false);
}

// Asked before any signature, because the wallet prompt covers the page.
async function askRevoke() {
  showError($("manage"), "");
  setBusy(true);
  let info;
  try {
    info = await challengeFor("revoke");
  } catch (error) {
    revokeFailed(error);
    setBusy(false);
    return;
  }
  setBusy(false);
  // `position_open` comes from the server's own records. The panel's read of the chain counts too.
  if (info.position_open === true || Boolean(state.agent && state.agent.position)) {
    state.revokeAsk = true;
    render();
    $("revoke-no").focus();
    return;
  }
  await manageAction("revoke");
}

function keepAccess() {
  state.revokeAsk = false;
  render();
  $("revoke-btn").focus();
}

async function manageAction(action) {
  const scope = action === "pause" ? $("agent") : $("manage");
  showError(scope, "");
  setBusy(true);
  try {
    const info = await challengeFor(action);
    const signature = await personalSign(state.eth, info.message);
    const result = await api[action](state.eth, signature);
    state.userStatus = result.status;
    if (action === "revoke") {
      state.revoked = true;
      state.chainRevokeOffer = false;
      // The agent is gone, and anything further needs a fresh ownership proof.
      clearSession();
      clearPoll();
      Object.assign(state, {
        grantOk: false, agent: null, agentReadFailed: false, equityRead: false, available: null, sessionLost: false, closing: false, telegramLink: null, revokeAsk: false, setupOpen: null,
        revokedPosition: result.position_open ?? null, revokedCloseAsk: false, grantRemoved: false,
      });
      showChainRevoke(true);
    }
    render();
    if (action === "pause") {
      await refreshAgent();
      $("agent-status").focus();
    }
  } catch (error) {
    state.revokeAsk = false;
    if (action === "revoke") revokeFailed(error);
    else showError(scope, actionMessage(error));
    render();
  } finally {
    setBusy(false);
  }
}

function askRevokedClose() {
  showError($("manage"), "");
  state.revokedCloseAsk = true;
  render();
  $("revoked-close-no").focus();
}

function keepRevokedPosition() {
  state.revokedCloseAsk = false;
  render();
  $("revoked-close-btn").focus();
}

// Revoke ended the session, so this signs in first. The close itself needs no more than that.
async function confirmRevokedClose() {
  showError($("manage"), "");
  setBusy(true);
  try {
    if (!state.session) {
      await signIn();
      await refreshStatus().catch(() => {}); // Only the steps above use this. It must not hold up a close.
    }
    const result = await api.agentClose(state.eth, state.session);
    if (result.outcome === "closed") state.revokedPosition = "closed";
    else showError($("manage"), CLOSE_RESULT[result.outcome] || CLOSE_RESULT.unknown);
  } catch (error) {
    if (error instanceof ApiError && error.code === "NO_POSITION") {
      state.revokedPosition = "flat";
    } else if (isSessionEnd(error)) {
      clearSession();
      showError($("manage"), COPY.CLOSE_SIGN_IN);
    } else {
      showError($("manage"), closeMessage(error));
    }
  } finally {
    state.revokedCloseAsk = false;
    render();
    setBusy(false);
  }
}

function showChainRevoke(serverDone) {
  $("revoke-followup").hidden = false;
  $("revoke-chain-note").hidden = true;
  if (!serverDone) $("revoke-once").textContent = "Our server did not confirm the revoke, so it may still be able to trade this wallet. Remove the grant on chain: that stops it whatever the server does.";
}

async function onRevokeChain() {
  const note = $("revoke-chain-note");
  showError($("manage"), "");
  setBusy(true);
  try {
    const { checked } = state;
    const msgs = buildRevokeMsgs({ granter: state.inj, grantee: checked.grantee, grants: checked.grants });
    const tx = await sendTx(msgs, GAS_REVOKE);
    note.hidden = false;
    note.textContent = `Grant removed on chain. Transaction ${tx.txHash}.`;
    state.grantRemoved = true;
    render();
  } catch (error) {
    showError($("manage"), txMessage(error));
  } finally {
    setBusy(false);
  }
}

function renderConfig() {
  const { config, checked } = state;
  const isTest = config.network !== "mainnet";
  $("network-note").textContent = isTest ? "Injective testnet" : "Injective mainnet";
  $("network-note").hidden = false;
  $("testnet-note").hidden = !isTest;
  const ticker = config.market.ticker;
  bindText("ticker", ticker);
  bindText("asset", ticker.split("/")[0]);
  bindText("max-deposit", config.max_deposit_usd);
  bindText("min-deposit", config.min_deposit_usd);
  bindText("grantee", checked.grantee);
  bindText("host", config.public_host);
  $("market-id").textContent = checked.marketId;
  const days = Math.round(checked.ttlSeconds / 86400);
  $("grant-expiry").textContent = `${days} days after you sign, about ${formatDate(Date.now() + checked.ttlSeconds * 1000)}. Renew it on this page.`;
  renderPresets();
  const list = $("grant-list");
  list.replaceChildren(
    ...checked.grants.map((grant) => {
      const li = document.createElement("li");
      li.textContent = `${grant.permits}: ${grant.authz_type_url}`;
      return li;
    }),
  );
}

function renderPresets() {
  const options = (state.config.presets || []).map((preset) => {
    const option = document.createElement("label");
    option.className = "preset";
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "preset";
    input.value = preset.id;
    const name = document.createElement("span");
    name.className = "preset-name";
    name.textContent = preset.name;
    const summary = document.createElement("span");
    summary.className = "preset-summary";
    summary.textContent = preset.summary;
    option.append(input, name, summary);
    return option;
  });
  $("presets").append(...options);
}

function bindEvents() {
  bindThemeToggle();
  document.querySelectorAll("[data-toggle]").forEach((toggle) => {
    toggle.dataset.label = toggle.textContent;
    toggle.addEventListener("click", () => {
      const step = toggle.closest(".step");
      step.classList.toggle("open");
      toggle.textContent = step.classList.contains("open") ? "Hide" : toggle.dataset.label;
    });
  });
  $("connect-btn").addEventListener("click", onConnect);
  $("prove-btn").addEventListener("click", onProve);
  $("amount-input").addEventListener("input", () => showError("deposit", ""));
  $("deposit-btn").addEventListener("click", onDeposit);
  $("grant-btn").addEventListener("click", onGrant);
  $("presets").addEventListener("change", syncStart);
  $("tune-fields").addEventListener("change", onTune);
  $("tune-reset").addEventListener("click", resetTune);
  $("start-btn").addEventListener("click", onStart);
  $("stop-btn").addEventListener("click", () => manageAction("pause"));
  $("start-again-btn").addEventListener("click", openStart);
  $("withdraw-open").addEventListener("click", openWithdraw);
  $("withdraw-max").addEventListener("click", fillWithdrawMax);
  $("withdraw-btn").addEventListener("click", onWithdraw);
  $("withdraw-amount").addEventListener("input", () => showError($("funds"), ""));
  $("setup-toggle").addEventListener("click", toggleSetup);
  $("decisions-list").addEventListener("click", onFeedClick);
  document.querySelectorAll(".segmented button").forEach((button) => {
    button.addEventListener("click", () => setRange(Number(button.dataset.range)));
  });
  if (window.ResizeObserver) new ResizeObserver(drawDashboard).observe($("agent"));
  $("decisions-more").addEventListener("click", () => {
    state.decisionRows += DECISION_ROWS;
    render();
  });
  $("close-btn").addEventListener("click", askClose);
  $("close-no").addEventListener("click", keepPosition);
  $("close-yes").addEventListener("click", confirmClose);
  $("telegram-connect").addEventListener("click", connectTelegram);
  $("telegram-disconnect").addEventListener("click", disconnectTelegram);
  $("agent-signin-btn").addEventListener("click", onProve);
  $("revoke-btn").addEventListener("click", askRevoke);
  $("revoke-yes").addEventListener("click", () => manageAction("revoke"));
  $("revoke-no").addEventListener("click", keepAccess);
  $("revoked-close-btn").addEventListener("click", askRevokedClose);
  $("revoked-close-no").addEventListener("click", keepRevokedPosition);
  $("revoked-close-yes").addEventListener("click", confirmRevokedClose);
  $("revoke-chain-btn").addEventListener("click", onRevokeChain);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") pollAgent();
    else clearPoll();
  });
  try {
    const provider = getProvider();
    if (provider.on) {
      provider.on("accountsChanged", (accounts) => {
        if (accounts[0] && accounts[0].toLowerCase() === (state.eth || "").toLowerCase()) return;
        resetWallet();
        if (accounts[0]) adoptAddress(accounts[0]);
      });
    }
  } catch {
    // No wallet at load: the connect button reports it.
  }
}

function resetWallet() {
  clearSession();
  clearPoll();
  if (chat) chat.reset();
  Object.assign(state, {
    eth: null, identity: null, inj: null, sub: null, verified: false, userStatus: null, balance: 0, grantOk: false,
    grantExpiresMs: null, revoked: false, chainRevokeOffer: false, agent: null, agentReadFailed: false, equityRead: false,
    sessionLost: false, closing: false, telegramLink: null, revokeAsk: false, revokedPosition: false, revokedCloseAsk: false,
    grantRemoved: false, setupOpen: null, decisionRows: DECISION_ROWS, available: null, withdrawOpen: false, tuning: {},
  });
  $("revoke-followup").hidden = true;
  render();
}

async function restoreSession() {
  let saved;
  try {
    saved = JSON.parse(sessionStorage.getItem(SESSION_KEY) || "null");
  } catch {
    return;
  }
  if (!saved || !saved.eth || !saved.session || saved.expires <= Date.now()) return;
  try {
    const accounts = await getProvider().request({ method: "eth_accounts" });
    if (!accounts[0] || accounts[0].toLowerCase() !== saved.eth.toLowerCase()) {
      clearSession();
      return;
    }
    adoptAddress(accounts[0]);
    state.session = saved.session;
    state.sessionExpires = saved.expires;
    state.verified = true;
    await refreshStatus();
    await refreshWalletBalance();
    await refreshAgent({ readStatus: false });
    await readRevokedPosition();
    schedulePoll();
  } catch {
    clearSession();
    state.verified = false;
    render();
  }
}

// Nothing here reaches our server or a wallet: the page draws made-up data and its buttons are off.
async function startDemo() {
  const [{ demoData }, demoChat] = await Promise.all([import("./demo.js"), import("./chat-demo.js")]);
  const data = demoData(Date.now());
  Object.assign(state, {
    demoChat,
    demo: true, config: data.config, checked: data.checked, eth: data.eth, verified: true, userStatus: "active",
    balance: data.balance, available: data.available, grantOk: true, equityRead: true, agent: data.agent, agentReadMs: Date.now(), candles: data.candles,
  });
  $("demo-banner").hidden = false;
  renderConfig();
  render();
}

// After every render, so a redraw can never bring back what the example must not offer.
function lockDemo() {
  ["setup", "funds", "manage", "agent-telegram"].forEach((id) => {
    $(id).hidden = true;
  });
  ["stop-btn", "close-btn"].forEach((id) => {
    $(id).disabled = true;
    $(id).title = "Not available in the example";
  });
}

function refusePage(text) {
  document.querySelector(".steps").querySelectorAll("button").forEach((button) => {
    button.disabled = true;
  });
  showError("connect", text);
}

async function init() {
  bindEvents();
  bindChat();
  if (window.location.pathname === DEMO_PATH) {
    await startDemo();
    return;
  }
  try {
    state.config = await getConfig();
  } catch (error) {
    refusePage(message(error, "The server is not available right now. Reload this page in a minute."));
    return;
  }
  try {
    state.checked = verifyServerConfig(state.config);
  } catch (error) {
    refusePage(message(error, COPY.CONFIG_REFUSED));
    return;
  }
  const network = state.config.network === "mainnet" ? Network.Mainnet : Network.Testnet;
  state.rest = getNetworkEndpoints(network).rest;
  renderConfig();
  render();
  await restoreSession();
  if (typeof window.ethereum === "undefined") $("no-wallet").hidden = false;
}

init();
