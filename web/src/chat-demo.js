// Scripted answers for the example account at /demo. No model and no server call: the page answers
// from the made-up data it already shows, in English or Korean by what the visitor types.
// A change is proposed the same way the real agent proposes one, and applies to the example only.

import { applyChoice, edited, fieldsFor, stable } from "./tune.js";

const HANGUL = /[ㄱ-힝]/;
const SIDES = { long: "롱", short: "숏" };

const usd = (value) => Math.abs(Number(value)).toFixed(2);
const signed = (value) => `${Number(value) < 0 ? "−" : "+"}${usd(value)}`;
const price = (value) => Number(value).toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const percent = (fraction) => `${Number((Number(fraction) * 100).toFixed(2))}%`;
const clock = (ms) => new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
const day = (ms, korean) => new Date(ms).toLocaleDateString(korean ? "ko-KR" : "en-GB", korean ? { month: "long", day: "numeric" } : { day: "numeric", month: "short" });

const label = (field) => (field.options.find((option) => option.value === field.value) || { label: String(field.value) }).label;

/** Every setting in one line, in the shape the dashboard's Settings list reads. */
export function settingsLine(spec, tuning) {
  return fieldsFor(spec, tuning).map((field) => `${field.label}: ${label(field)}`).join("; ");
}

/** The menu entry one step below (-1) or above (+1) the current one, or null at the end of the menu. */
function step(menu, current, direction) {
  const sorted = [...(menu || [])].sort((a, b) => Number(a) - Number(b));
  const index = sorted.findIndex((item) => String(item) === String(current));
  const next = index === -1 ? undefined : sorted[index + direction];
  return next === undefined ? null : String(next);
}

// A request for a change, the rows it sets, and what the agent says about it.
const CHANGES = [
  {
    match: /\bsafer?\b|less risk|(?:lower|reduce|cut)(?: the| my)? risk|conservative|cautious|안전|보수적|(?:위험|리스크).*(?:낮|줄)/i,
    rows: (spec, tuning) => [
      ["risk.per_trade", step(spec.options.risk.per_trade, tuning.risk.per_trade, -1)],
      ["risk.leverage", "1"],
      ["checks.funding", "5"],
    ],
    en: "Here is a safer version: less risk on each trade, no leverage, and no entries while funding is crowded.",
    ko: "더 안전한 설정입니다. 거래당 위험을 낮추고, 레버리지를 쓰지 않고, 펀딩이 한쪽으로 쏠렸을 때는 진입하지 않습니다.",
  },
  {
    match: /aggressive|more active|more trades|trade more|bolder|more risk|공격적|적극적|더 자주 거래|(?:위험|리스크).*(?:높|올)/i,
    rows: (spec, tuning) => [
      ["risk.per_trade", step(spec.options.risk.per_trade, tuning.risk.per_trade, 1)],
      ["checks.higher_timeframe", "off"],
    ],
    en: "Here is a more active version: more risk on each trade, and entries are allowed against the 4h trend.",
    ko: "더 적극적인 설정입니다. 거래당 위험을 올리고, 4시간 추세와 반대 방향 진입도 허용합니다.",
  },
  {
    match: /both (?:ways|directions|sides)|long and short|양방향/i,
    rows: () => [["direction", "both"]],
    en: "This lets it trade both ways again.",
    ko: "롱과 숏을 모두 거래하도록 되돌립니다.",
  },
  {
    match: /long[- ]only|only (?:go )?long|롱만|롱 ?온리|매수만/i,
    rows: () => [["direction", "long"]],
    en: "This keeps it to longs only. It will sit out when the trend points down.",
    ko: "롱만 거래하도록 바꿉니다. 추세가 아래를 가리키면 쉬어 갑니다.",
  },
  {
    match: /short[- ]only|only (?:go )?short|숏만|숏 ?온리|매도만/i,
    rows: () => [["direction", "short"]],
    en: "This keeps it to shorts only. It will sit out when the trend points up.",
    ko: "숏만 거래하도록 바꿉니다. 추세가 위를 가리키면 쉬어 갑니다.",
  },
  {
    match: /tight(?:er|en)?.*stop|(?:closer|smaller) stop|손절.*(?:좁|짧|타이트|가깝)/i,
    rows: (spec, tuning) => [["stop_atr", step(spec.options.stop_atr, tuning.stop_atr, -1)]],
    en: "This moves the stop closer to the entry. A trade is cut sooner, and is stopped out more often.",
    ko: "손절을 진입가에 더 가깝게 둡니다. 손실은 빨리 끊기지만 손절에 더 자주 걸립니다.",
  },
  {
    match: /wid(?:er|en).*stop|(?:looser|bigger) stop|손절.*(?:넓|길|멀)/i,
    rows: (spec, tuning) => [["stop_atr", step(spec.options.stop_atr, tuning.stop_atr, 1)]],
    en: "This gives each trade more room before the stop. The position is sized smaller, so the risk stays the same.",
    ko: "손절까지 여유를 더 둡니다. 그만큼 수량을 줄이므로 거래당 위험은 같습니다.",
  },
  {
    match: /every hour|hourly|once an hour|1 ?시간마다|한 ?시간마다|매시간/i,
    rows: () => [["interval_s", "3600"]],
    en: "This makes it check the market once an hour.",
    ko: "한 시간마다 시장을 확인하도록 바꿉니다.",
  },
  {
    match: /every 15|15 ?min|15 ?분/i,
    rows: () => [["interval_s", "900"]],
    en: "This makes it check the market every 15 minutes.",
    ko: "15분마다 시장을 확인하도록 바꿉니다.",
  },
  {
    match: /every 30|30 ?min|30 ?분/i,
    rows: () => [["interval_s", "1800"]],
    en: "This makes it check the market every 30 minutes.",
    ko: "30분마다 시장을 확인하도록 바꿉니다.",
  },
  {
    match: /(?:4|four) hours|4 ?시간/i,
    rows: () => [["interval_s", "14400"]],
    en: "This makes it check the market every 4 hours.",
    ko: "4시간마다 시장을 확인하도록 바꿉니다.",
  },
  {
    match: /less often|check less|덜 자주|가끔/i,
    rows: (spec, tuning) => [["interval_s", step(spec.options.interval_s, tuning.interval_s, 1)]],
    en: "This makes it check the market less often.",
    ko: "시장을 덜 자주 확인하도록 바꿉니다.",
  },
  {
    match: /more often|check more|더 자주/i,
    rows: (spec, tuning) => [["interval_s", step(spec.options.interval_s, tuning.interval_s, -1)]],
    en: "This makes it check the market more often.",
    ko: "시장을 더 자주 확인하도록 바꿉니다.",
  },
  {
    match: /no leverage|without leverage|\b1x\b|leverage (?:to |of )?1\b|레버리지.*(?:없|1배|낮)/i,
    rows: () => [["risk.leverage", "1"]],
    en: "This turns leverage off.",
    ko: "레버리지를 쓰지 않도록 바꿉니다.",
  },
  {
    match: /\b2x\b|leverage (?:to |of )?2\b|레버리지.*2배/i,
    rows: () => [["risk.leverage", "2"]],
    en: "This allows leverage up to 2×.",
    ko: "레버리지를 최대 2배까지 허용합니다.",
  },
];

const ORDERS = /\b(?:buy|sell)\b|close (?:it|the|my)\b|open a (?:long|short|position)|go (?:long|short) now|stop (?:the agent|trading|it)\b|\bpause\b|withdraw|매수해|매도해|청산|닫아|사 ?줘|팔아|진입해|출금|(?:에이전트|거래).*(?:중지|멈|정지)/i;

// "Why did you close the last trade?" names an order and is still a question.
const ASKS_WHY = /\bwhy\b|왜|이유/i;

const DID = {
  en: { hold: "it held", entered: "it opened a position", closed: "it closed the position", rejected: "it wanted a trade and the rules refused it" },
  ko: { hold: "유지하기로 했습니다", entered: "포지션을 열었습니다", closed: "포지션을 닫았습니다", rejected: "거래를 원했지만 규칙이 막았습니다" },
};
const ENDED = {
  en: { stop: "hit its stop", take_profit: "reached its target", close: "was closed early by the agent" },
  ko: { stop: "손절가에 닿아", take_profit: "목표가에 닿아", close: "에이전트가 먼저 닫아" },
};

function holding(agent, korean) {
  const { position } = agent;
  if (!position) return korean ? "지금 열린 포지션은 없습니다." : "It has no position open.";
  if (korean) {
    return `지금은 ${price(position.entry_price)}에 연 ${SIDES[position.side]}을 들고 있고 손절가는 ${price(position.stop_price)}입니다. 이 거래에서 잃을 수 있는 최대는 ${usd(position.loss_if_stopped_usd)} USDC입니다.`;
  }
  return `It is holding a ${position.side} from ${price(position.entry_price)} with the stop at ${price(position.stop_price)}, so this trade can lose at most ${usd(position.loss_if_stopped_usd)} USDC.`;
}

const ANSWERS = [
  {
    match: /what can you|\bhelp\b|who are you|뭘 할 수|무엇을 할 수|도움|할 수 있/i,
    say: (_, korean) => (korean
      ? "에이전트가 어떻게 하고 있는지, 왜 그렇게 판단했는지, 설정이 무엇인지 알려 드리고 설정 변경을 제안할 수 있습니다. 직접 거래하거나 자금을 옮기거나 설정을 바꿀 수는 없습니다. 변경은 지갑으로 서명해야만 적용됩니다."
      : "I can tell you how the agent is doing, why it made a decision and what its settings are, and I can propose a change to those settings. I cannot trade, move funds or change anything myself. A change applies only when you sign it."),
  },
  {
    match: /\bwhy\b|reason|decision|decide|왜|이유|판단|결정/i,
    say: ({ agent }, korean) => {
      const last = agent.decisions[0];
      const entry = agent.decisions.find((row) => row.outcome === "entered");
      const did = DID[korean ? "ko" : "en"][last.outcome] || (korean ? "기록을 남겼습니다" : "it logged a check");
      const first = korean ? `${clock(last.created_ms)}에 ${did}. 이유: “${last.reason}”` : `At ${clock(last.created_ms)} ${did}. Its reason: “${last.reason}”`;
      if (!entry || entry === last) return first;
      const wrong = entry.snapshot && entry.snapshot.counter;
      if (korean) return `${first}\n\n지금 ${SIDES[entry.side]}은 이런 이유로 열었습니다: “${entry.reason}”${wrong ? ` 이렇게 되면 틀린 판단으로 봅니다: “${wrong}”` : ""}`;
      return `${first}\n\nIt opened the ${entry.side} because: “${entry.reason}”${wrong ? ` It would call that wrong on: “${wrong}”` : ""}`;
    },
  },
  {
    match: /risk|limit|drawdown|how much can|위험|한도|리스크/i,
    say: ({ agent }, korean) => {
      const { risk } = agent.tuning;
      const { daily_loss: today, position } = agent;
      if (korean) {
        return `거래 한 번에 계좌의 ${percent(risk.per_trade)}까지 위험을 지고, 레버리지는 최대 ${risk.leverage}배입니다. 하루 손실이 ${percent(risk.daily_loss)}에 닿으면 그날 거래를 멈춥니다. 오늘은 ${usd(today.limit_usd)} USDC 중 ${usd(today.used_usd)} USDC를 썼습니다.${position ? ` 열린 ${SIDES[position.side]}은 손절가에서 ${usd(position.loss_if_stopped_usd)} USDC를 잃습니다.` : ""}`;
      }
      return `Each trade risks ${percent(risk.per_trade)} of the account with leverage up to ${risk.leverage}×, and trading stops for the day after a ${percent(risk.daily_loss)} loss. Today ${usd(today.used_usd)} of ${usd(today.limit_usd)} USDC is used.${position ? ` The open ${position.side} loses ${usd(position.loss_if_stopped_usd)} USDC at its stop.` : ""}`;
    },
  },
  {
    match: /settings?|strategy|config|rules|설정|전략|규칙/i,
    say: ({ agent, spec, name }, korean) => {
      const rows = fieldsFor(spec, agent.tuning).map((field) => `${field.label}: ${label(field)}`).join("\n");
      return korean ? `${name} 전략으로 돌고 있습니다. 지금 설정입니다.\n${rows}` : `It runs the ${name} strategy with these settings.\n${rows}`;
    },
  },
  {
    match: /position|holding|open (?:trade|long|short)|포지션|보유|들고/i,
    say: ({ agent }, korean) => {
      const { position } = agent;
      if (!position) return holding(agent, korean);
      const now = Number(position.unrealized_pnl_usd);
      if (korean) return `${holding(agent, true)} 지금 평가손익은 ${signed(now)} USDC이고 목표가는 ${price(position.take_profit_price)}입니다.`;
      return `${holding(agent, false)} Right now it is ${now < 0 ? "down" : "up"} ${usd(now)} USDC on it, and the target is ${price(position.take_profit_price)}.`;
    },
  },
  {
    match: /trades?\b|win ?rate|history|거래|승률|매매|내역/i,
    say: ({ agent }, korean) => {
      const { stats } = agent;
      const latest = agent.trades[0];
      const rate = Math.round(stats.win_rate * 100);
      const how = ENDED[korean ? "ko" : "en"][latest.close_reason];
      if (korean) return `끝난 거래 ${stats.trades}건 중 이익 ${stats.wins}건, 손실 ${stats.losses}건으로 승률은 ${rate}%입니다. 가장 최근 거래는 ${SIDES[latest.side]}이었고 ${how} ${signed(latest.pnl_usd)} USDC로 끝났습니다.`;
      return `${stats.trades} closed trades: ${stats.wins} wins and ${stats.losses} losses, a ${rate}% win rate. The latest was a ${latest.side} that ${how} for ${signed(latest.pnl_usd)} USDC.`;
    },
  },
  {
    match: /how.*(?:going|doing)|status|\bstate\b|performance|result|pnl|p&l|profit|return|어때|어떻|상태|현황|수익|성과|결과/i,
    say: ({ agent }, korean) => {
      const { performance, stats } = agent;
      const net = Number(performance.net_usd);
      if (korean) {
        return `${day(performance.since_ms, true)} 이후 ${signed(net)} USDC입니다. 끝난 거래 ${stats.trades}건 중 ${stats.wins}건이 이익이었습니다. ${holding(agent, true)} 다음 확인은 ${clock(agent.next_run_ms)}입니다.`;
      }
      return `${net < 0 ? "Down" : "Up"} ${usd(net)} USDC since ${day(performance.since_ms, false)} across ${stats.trades} closed trades, ${stats.wins} of them wins. ${holding(agent, false)} The next check is at ${clock(agent.next_run_ms)}.`;
    },
  },
];

function proposalFor(change, { agent, spec, name }) {
  let tuning = agent.tuning;
  change.rows(spec, tuning).forEach(([id, value]) => {
    if (value !== null) tuning = applyChoice(spec, tuning, id, value);
  });
  if (stable(tuning) === stable(agent.tuning)) return null;
  return {
    preset: agent.preset,
    name,
    tuning,
    summary: settingsLine(spec, tuning),
    from: { preset: agent.preset, name, tuning: agent.tuning },
  };
}

/** What the example agent says to `text`: `{reply, proposal}`. `context` is `{agent, spec, name}`. */
export function demoAnswer(text, context) {
  const korean = HANGUL.test(text);
  if (ORDERS.test(text) && !ASKS_WHY.test(text)) {
    return {
      reply: korean
        ? "대화로는 거래를 열거나 닫을 수 없고, 에이전트를 멈추거나 자금을 옮길 수도 없습니다. 에이전트는 규칙대로만 거래합니다. 포지션 닫기와 에이전트 중지는 이 화면 위쪽의 버튼으로 합니다."
        : "I cannot open or close a trade from chat, and I cannot stop the agent or move funds. The agent trades by its rules. Closing a position and stopping the agent are the buttons at the top of this page.",
      proposal: null,
    };
  }
  const change = CHANGES.find((row) => row.match.test(text));
  if (change) {
    const proposal = proposalFor(change, context);
    if (!proposal) return { reply: korean ? "이미 그렇게 설정되어 있습니다." : "Those are already the agent's settings.", proposal: null };
    const ready = korean ? "아래 카드에서 확인하고 적용할 수 있습니다." : "It is ready to apply in the card below.";
    return { reply: `${korean ? change.ko : change.en} ${ready}`, proposal };
  }
  const answer = ANSWERS.find((row) => row.match.test(text));
  if (answer) return { reply: answer.say(context, korean), proposal: null };
  return {
    reply: korean
      ? "이 예시는 몇 가지만 답합니다. 지금 상태, 마지막 판단의 이유, 거래 내역, 위험 한도, 설정을 물어볼 수 있고, “더 안전하게”, “롱만”, “1시간마다 확인”처럼 설정 변경도 요청할 수 있습니다. 내 에이전트에서는 무엇이든 물어볼 수 있습니다."
      : "This example answers a few things: how it is going, why it made its last decision, its trades, its risk and its settings. You can also ask for a change, such as “make it safer”, “long only” or “check every hour”. Your own agent answers anything you ask.",
    proposal: null,
  };
}

/** Puts a proposal's settings into the example account. Nothing leaves the page. */
export function demoApply(agent, proposal, spec) {
  agent.tuning = proposal.tuning;
  agent.custom = edited(spec, proposal.tuning);
  agent.settings = proposal.summary;
  agent.daily_loss = {
    ...agent.daily_loss,
    limit_usd: (Number(agent.performance.start_equity) * Number(proposal.tuning.risk.daily_loss)).toFixed(2),
  };
}
