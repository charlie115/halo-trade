// The price chart. Candles stay neutral so the only colour on it is what the agent did.
// Built from DOM calls only: every label is text, and the page's CSP allows no inline style.

const NS = "http://www.w3.org/2000/svg";
const MARGIN = { top: 14, right: 92, bottom: 26, left: 0 };
const RAIL = 14; // the strip under the candles that shows each check
const HALO_RADIUS = 11;
const TAG_GAP = 18;

export function shape(tag, attributes = {}, text) {
  const made = document.createElementNS(NS, tag);
  Object.entries(attributes).forEach(([name, value]) => made.setAttribute(name, value));
  if (text !== undefined) made.textContent = text;
  return made;
}

export function formatPrice(value) {
  return Number(value).toLocaleString("en-US", { maximumFractionDigits: value >= 1000 ? 0 : 2 });
}

function niceStep(span, count) {
  const raw = span / count;
  const power = 10 ** Math.floor(Math.log10(raw));
  const fraction = raw / power;
  const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10;
  return nice * power;
}

function timeTicks(start, end, rangeHours, narrow) {
  const base = rangeHours <= 24 ? 4 : rangeHours <= 72 ? 12 : 24;
  const step = narrow && base < 24 ? base * 2 : base;
  const every = narrow && rangeHours > 72 ? 2 : 1;
  const ticks = [];
  const first = new Date(start);
  first.setMinutes(0, 0, 0);
  let days = 0;
  for (let at = first.getTime(); at <= end; at += 3_600_000) {
    const date = new Date(at);
    if (date.getHours() % step !== 0) continue;
    if (date.getHours() === 0 && days++ % every !== 0) continue;
    const label = date.getHours() === 0
      ? date.toLocaleDateString("en-GB", { day: "numeric", month: "short" })
      : date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
    ticks.push({ at, label });
  }
  return ticks;
}

// Labels on the price axis may not sit on top of each other. Keeps them in order, pushed apart.
function spread(tags, top, bottom) {
  const sorted = [...tags].sort((a, b) => a.y - b.y);
  sorted.forEach((tag, index) => {
    if (index > 0) tag.y = Math.max(tag.y, sorted[index - 1].y + TAG_GAP);
  });
  let limit = bottom;
  for (let index = sorted.length - 1; index >= 0; index -= 1) {
    sorted[index].y = Math.max(top, Math.min(sorted[index].y, limit));
    limit = sorted[index].y - TAG_GAP;
  }
  return sorted;
}

function markerShape(mark) {
  const body = { class: "m-body" };
  if (mark.kind === "long") return shape("path", { ...body, d: "M0 -6.5 6 4.5H-6Z" });
  if (mark.kind === "short") return shape("path", { ...body, d: "M0 6.5 6 -4.5H-6Z" });
  return shape("circle", { ...body, r: mark.kind === "refused" ? 3.5 : 4.5 });
}

/**
 * Draws into `holder` and returns `{ select }` to move the halo without a redraw.
 * model: { candles, periodMs, rangeHours, trades, marks, checks, position, last, selected, onSelect, onHover }
 */
export function drawPriceChart(holder, model) {
  const width = holder.clientWidth;
  const height = holder.clientHeight;
  holder.replaceChildren();
  if (!width || !height) return { select() {} };

  const narrow = width < 520;
  const margin = narrow ? { ...MARGIN, right: 54 } : MARGIN;
  const plotRight = width - margin.right;
  const plotBottom = height - margin.bottom - RAIL;
  const last = model.candles[model.candles.length - 1];
  const end = last[0] + model.periodMs * 2.5; // a little room, so the newest bar is not on the axis
  const start = end - model.rangeHours * 3_600_000;
  const x = (ms) => ((ms - start) / (end - start)) * plotRight;
  const visible = model.candles.filter((candle) => candle[0] + model.periodMs > start);
  const inRange = (mark) => mark.ms >= start && mark.ms <= end;
  const marks = model.marks.filter(inRange);

  const levels = visible.flatMap((candle) => [candle[2], candle[3]]);
  marks.forEach((mark) => levels.push(mark.price));
  const { position } = model;
  if (position) [position.entry, position.stop, position.take].forEach((level) => level !== null && levels.push(level));
  const lowest = Math.min(...levels);
  const highest = Math.max(...levels);
  const pad = (highest - lowest || highest * 0.01) * 0.08;
  const low = lowest - pad;
  const high = highest + pad;
  const y = (price) => margin.top + (1 - (price - low) / (high - low)) * (plotBottom - margin.top);

  const svg = shape("svg", {
    class: "price-svg",
    width,
    height,
    viewBox: `0 0 ${width} ${height}`,
    role: "group",
    "aria-label": `Price over the last ${model.rangeHours} hours, with ${marks.length} agent moves. Select a move to read why.`,
  });

  // The grid and its prices.
  const step = niceStep(high - low, narrow ? 3 : 5);
  const reserved = [];
  if (model.last !== null) reserved.push(y(model.last));
  if (position) [position.entry, position.stop, position.take].forEach((level) => level !== null && reserved.push(y(level)));
  for (let level = Math.ceil(low / step) * step; level <= high; level += step) {
    const at = y(level);
    svg.append(shape("line", { class: "grid", x1: 0, x2: plotRight, y1: at, y2: at }));
    if (!reserved.some((taken) => Math.abs(taken - at) < TAG_GAP)) {
      svg.append(shape("text", { class: "axis", x: plotRight + 10, y: at + 4 }, formatPrice(level)));
    }
  }
  timeTicks(start, end, model.rangeHours, narrow).forEach((tick) => {
    const at = x(tick.at);
    if (at < 16 || at > plotRight - 16) return;
    svg.append(shape("text", { class: "axis axis-time", x: at, y: height - 8, "text-anchor": "middle" }, tick.label));
  });

  // What is at risk on the open position, behind the candles.
  const tags = [];
  if (position) {
    const from = Math.max(0, position.openedMs ? x(position.openedMs) : 0);
    const band = (level, name) => {
      const [top, bottom] = [y(position.entry), y(level)].sort((a, b) => a - b);
      svg.append(shape("rect", { class: `band ${name}`, x: from, width: Math.max(0, plotRight - from), y: top, height: bottom - top }));
    };
    if (position.stop !== null) band(position.stop, "band-stop");
    if (position.take !== null) band(position.take, "band-take");
    const level = (price, name, label) => {
      svg.append(shape("line", { class: `level ${name}`, x1: from, x2: plotRight, y1: y(price), y2: y(price) }));
      tags.push({ y: y(price), text: narrow ? formatPrice(price) : `${label} ${formatPrice(price)}`, name: name.replace("level-", "tag-") });
    };
    level(position.entry, "level-entry", "Entry");
    if (position.stop !== null) level(position.stop, "level-stop", "Stop");
    if (position.take !== null) level(position.take, "level-take", "Target");
  }

  // Candles.
  const bar = Math.max(1, Math.floor(((plotRight / model.rangeHours) * (model.periodMs / 3_600_000)) * 0.62));
  const candles = shape("g", { class: "candles" });
  visible.forEach(([open, o, h, l, c]) => {
    const centre = x(open + model.periodMs / 2);
    if (centre < 0) return;
    const kind = c >= o ? "c-up" : "c-down";
    candles.append(shape("line", { class: `wick ${kind}`, x1: centre, x2: centre, y1: y(h), y2: y(l) }));
    candles.append(shape("rect", { class: kind, x: centre - bar / 2, width: bar, y: y(Math.max(o, c)), height: Math.max(1, Math.abs(y(o) - y(c))) }));
  });
  svg.append(candles);

  // The rail: each check that did not lead to a trade.
  const railY = plotBottom + 7;
  svg.append(shape("line", { class: "rail", x1: 0, x2: plotRight, y1: railY, y2: railY }));
  model.checks.filter((ms) => ms >= start && ms <= end).forEach((ms) => {
    svg.append(shape("rect", { class: "rail-check", x: x(ms) - 0.75, width: 1.5, y: railY - 3, height: 6 }));
  });

  // Each round trip as one line, entry to exit.
  model.trades.forEach((trade) => {
    if (trade.exit.ms < start) return;
    const kind = trade.win === null ? "" : trade.win ? "trip-win" : "trip-loss";
    svg.append(shape("line", { class: `trip ${kind}`, x1: x(trade.entry.ms), x2: x(trade.exit.ms), y1: y(trade.entry.price), y2: y(trade.exit.price) }));
  });

  // Crosshair: under the moves, so they stay clickable.
  const cross = shape("g", { class: "cross", visibility: "hidden" });
  const crossLine = shape("line", { x1: 0, x2: 0, y1: margin.top, y2: plotBottom });
  cross.append(crossLine);
  svg.append(cross);
  const hit = shape("rect", { class: "hit", x: 0, y: 0, width: plotRight, height: plotBottom + RAIL });
  svg.append(hit);
  const leave = () => {
    cross.setAttribute("visibility", "hidden");
    model.onHover(null);
  };
  hit.addEventListener("pointermove", (event) => {
    const box = svg.getBoundingClientRect();
    const at = event.clientX - box.left;
    let nearest = visible[0];
    let distance = Infinity;
    visible.forEach((candle) => {
      const gap = Math.abs(x(candle[0] + model.periodMs / 2) - at);
      if (gap < distance) {
        distance = gap;
        nearest = candle;
      }
    });
    const centre = x(nearest[0] + model.periodMs / 2);
    crossLine.setAttribute("x1", centre);
    crossLine.setAttribute("x2", centre);
    cross.setAttribute("visibility", "visible");
    model.onHover(nearest);
  });
  hit.addEventListener("pointerleave", leave);

  // The halo marks the move being read. There is only ever one.
  const halo = shape("circle", { class: "halo", r: HALO_RADIUS, visibility: "hidden" });
  svg.append(halo);
  const placed = new Map();

  marks.forEach((mark) => {
    const at = [x(mark.ms), y(mark.price)];
    if (!placed.has(mark.key)) placed.set(mark.key, at);
    const group = shape("g", {
      class: `m m-${mark.kind}`,
      transform: `translate(${at[0].toFixed(1)} ${at[1].toFixed(1)})`,
      tabindex: 0,
      role: "button",
      "aria-label": mark.label,
    });
    group.append(shape("title", {}, mark.label), shape("circle", { class: "m-hit", r: 15 }), markerShape(mark));
    const choose = () => model.onSelect(mark.key);
    group.addEventListener("click", choose);
    group.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        choose();
      }
    });
    svg.append(group);
  });

  // Right-hand labels go last, over the grid.
  if (model.last !== null) tags.push({ y: y(model.last), text: formatPrice(model.last), name: "tag-last" });
  spread(tags, margin.top + 6, plotBottom).forEach((tag) => {
    const group = shape("g", { class: `tag ${tag.name}`, transform: `translate(${plotRight + 4} ${tag.y})` });
    group.append(shape("rect", { x: 0, y: -9, width: margin.right - 4, height: 18, rx: 3 }));
    group.append(shape("text", { x: 6, y: 4 }, tag.text));
    svg.append(group);
  });

  holder.append(svg);

  function select(key) {
    const at = key === null ? undefined : placed.get(key);
    halo.setAttribute("visibility", at ? "visible" : "hidden");
    if (at) {
      halo.setAttribute("cx", at[0]);
      halo.setAttribute("cy", at[1]);
    }
  }
  select(model.selected);
  return { select };
}

// Where the price sits between the stop and the target. Left is always the stop, whichever the side.
export function drawLadder(holder, { stop, entry, take, mark, label }) {
  const width = holder.clientWidth;
  holder.replaceChildren();
  if (!width || stop === null) return;
  const height = 38;
  const bar = 28;
  const inset = 8;
  const far = take === null ? entry + (entry - stop) * 2 : take;
  const at = (price) => inset + Math.max(0, Math.min(1, (price - stop) / (far - stop))) * (width - inset * 2);
  const svg = shape("svg", { class: "ladder-svg", width, height, viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });
  svg.append(shape("text", { class: "ladder-end", x: 0, y: 11 }, "Stop"));
  svg.append(shape("text", { class: "ladder-end", x: width, y: 11, "text-anchor": "end" }, take === null ? "No target" : "Target"));
  if (label) svg.append(shape("text", { class: "ladder-now", x: Math.max(52, Math.min(width - 52, at(mark))), y: 11, "text-anchor": "middle" }, label));
  svg.append(shape("line", { class: "ladder-risk", x1: at(stop), x2: at(entry), y1: bar, y2: bar }));
  svg.append(shape("line", { class: take === null ? "ladder-open" : "ladder-reward", x1: at(entry), x2: at(far), y1: bar, y2: bar }));
  svg.append(shape("line", { class: "ladder-entry", x1: at(entry), x2: at(entry), y1: bar - 6, y2: bar + 6 }));
  svg.append(shape("circle", { class: "ladder-halo", cx: at(mark), cy: bar, r: 7.5 }));
  svg.append(shape("circle", { class: "ladder-mark", cx: at(mark), cy: bar, r: 3 }));
  holder.append(svg);
}
