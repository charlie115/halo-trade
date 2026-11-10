// The chat panel: the owner asks their agent how it is doing and for changes to its settings.
// The agent can explain and propose. A proposal is a card; only the wallet's signature applies it.
// Every string from the server or the model is written as text, never as markup.

import { fieldsFor, stable } from "./tune.js";

const MAX_CHARS = 2000;

const $ = (id) => document.getElementById(id);

function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined && text !== null) el.textContent = text;
  return el;
}

const shown = (field) => {
  const option = field.options.find((item) => item.value === field.value);
  return option ? option.label : String(field.value);
};

/**
 * What a proposal changes, one row per setting: `[{label, from, to}]`. Null when it cannot be laid
 * out (settings this page does not know), so the card falls back to the line the wallet will show.
 * On a change of strategy only the rows both strategies have are compared. No DOM here.
 */
export function proposalRows(proposal, specFor, nameFor) {
  try {
    const before = proposal.from;
    const rows = [];
    if (before.preset !== proposal.preset) rows.push({ label: "Strategy", from: nameFor(before.preset), to: nameFor(proposal.preset) });
    const was = new Map(fieldsFor(specFor(before.preset), before.tuning).map((field) => [field.id, field]));
    fieldsFor(specFor(proposal.preset), proposal.tuning).forEach((field) => {
      const old = was.get(field.id);
      if (!old) return;
      const from = shown(old);
      const to = shown(field);
      if (from !== to) rows.push({ label: field.label, from, to });
    });
    return rows.length ? rows : null;
  } catch {
    return null;
  }
}

/** True while the settings a proposal started from are still the ones in force. */
export function stillCurrent(proposal, current) {
  if (!current || !current.tuning) return true; // nothing to compare against: the wallet message still shows every setting
  return current.preset === proposal.from.preset && stable(current.tuning) === stable(proposal.from.tuning);
}

const CARD_NOTES = {
  stale: "Replaced by a newer proposal.",
  outdated: "Your settings changed after this was proposed. Ask again.",
  dismissed: "Dismissed. Nothing changed.",
};

export function createChat(host) {
  const els = {
    ask: $("ask"),
    askForm: $("ask-form"),
    askInput: $("ask-input"),
    askSend: $("ask-send"),
    launch: $("chat-launch"),
    panel: $("chat"),
    sub: $("chat-sub"),
    close: $("chat-close"),
    log: $("chat-log"),
    chips: $("chat-chips"),
    form: $("chat-form"),
    input: $("chat-input"),
    send: $("chat-send"),
    rule: $("chat-rule"),
    left: $("chat-left"),
  };
  let open = false;
  let sending = false;
  let messages = [];
  let left = null;
  let epoch = 0; // bumped on reset, so an answer for another wallet or session is dropped
  let ready = null; // the history read, once per session
  let past = 0; // how many of the messages came back from the server: those carry no card
  const empty = node("p", "chat-empty", "Ask how it is doing, why it made a decision, or for a change to its settings. It can explain and propose. Only your signature changes anything.");
  const earlier = node("p", "chat-earlier", "Earlier messages. To use a change proposed in them, ask for it again.");
  const typing = node("div", "msg msg-assistant msg-typing");
  typing.append(node("span", "visually-hidden", "Your agent is writing"), node("span", "dots"));
  typing.lastChild.setAttribute("aria-hidden", "true");
  typing.lastChild.append(node("i"), node("i"), node("i"));

  function applyLabel() {
    if (host.demo()) return "Apply in this example";
    return host.running() ? "Sign to apply" : "Sign and start";
  }

  function paintCard(message) {
    const { proposal, state } = message;
    const rows = proposalRows(proposal, host.spec, host.presetName);
    const parts = [node("h3", "proposal-title", "Proposed change")];
    if (rows) {
      const list = node("dl", "proposal-rows");
      rows.forEach((row) => {
        const value = node("dd");
        const arrow = node("span", "proposal-arrow", "→");
        arrow.setAttribute("aria-hidden", "true");
        value.append(node("span", "proposal-was", row.from), node("span", "visually-hidden", " becomes "), arrow, node("span", "proposal-now", row.to));
        list.append(node("dt", null, row.label), value);
      });
      parts.push(list);
    }
    if (proposal.summary) {
      if (rows) {
        const full = node("details", "proposal-full");
        full.append(node("summary", null, host.demo() ? "All settings after the change" : "What your wallet will show"), node("p", null, proposal.summary));
        parts.push(full);
      } else {
        parts.push(node("p", "proposal-line", proposal.summary));
      }
    }
    if (state === "applied") {
      parts.push(node("p", "proposal-done", host.demo() ? "Applied to the example. Nothing was signed." : "Applied. The agent now runs with these settings."));
    } else if (CARD_NOTES[state]) {
      parts.push(node("p", "proposal-note", CARD_NOTES[state]));
    } else {
      const starts = !host.demo() && !host.running();
      const note = host.demo()
        ? "This is the example account, so nothing is signed. On your own agent you sign this change in your wallet."
        : `Nothing changes until you sign. ${starts ? "Signing also starts the agent." : "A new run starts, so the result restarts."}`;
      parts.push(node("p", "proposal-note", note));
      if (message.error) {
        const error = node("p", "error", message.error);
        error.setAttribute("role", "alert");
        parts.push(error);
      }
      const actions = node("div", "proposal-actions");
      const apply = node("button", "primary", state === "signing" ? "Waiting for your wallet" : applyLabel());
      apply.type = "button";
      apply.dataset.act = "apply";
      apply.disabled = state === "signing" || host.busy();
      apply.addEventListener("click", () => applyCard(message));
      const dismiss = node("button", null, "Dismiss");
      dismiss.type = "button";
      dismiss.dataset.act = "dismiss";
      dismiss.disabled = state === "signing" || host.busy();
      dismiss.addEventListener("click", () => setState(message, "dismissed"));
      actions.append(apply, dismiss);
      parts.push(actions);
    }
    message.card.dataset.state = state;
    message.card.replaceChildren(...parts);
  }

  function setState(message, state) {
    message.state = state;
    paintCard(message);
  }

  function build(message) {
    const el = node("div", `msg msg-${message.role}`);
    if (message.role !== "notice") el.append(node("span", "visually-hidden", message.role === "user" ? "You: " : "Your agent: "));
    el.append(node("p", "msg-text", message.text));
    if (message.proposal) {
      message.card = node("section", "proposal");
      message.card.setAttribute("aria-label", "Proposed change to your settings");
      el.append(message.card);
      paintCard(message);
    }
    message.el = el;
    return message;
  }

  function paint() {
    const rows = messages.map((message) => message.el);
    if (past) rows.splice(past, 0, earlier);
    if (!rows.length && !sending) rows.push(empty);
    if (sending) rows.push(typing);
    els.log.replaceChildren(...rows);
    els.log.scrollTop = els.log.scrollHeight;
    els.chips.hidden = messages.length > 0 || sending;
  }

  function add(message) {
    messages.push(build(message));
    paint();
  }

  function loadHistory() {
    if (ready) return ready;
    const mine = epoch;
    ready = (async () => {
      if (host.demo()) return;
      try {
        const body = await host.history();
        if (mine !== epoch) return;
        const kept = (body.turns || []).map((turn) => build({ role: turn.role === "user" ? "user" : "assistant", text: String(turn.text || "") }));
        messages = [...kept, ...messages];
        past = kept.length;
        if (typeof body.left_today === "number") left = body.left_today;
        paint();
        sync();
      } catch (error) {
        if (mine !== epoch) return;
        ready = null; // earlier messages are a convenience: read again the next time the panel opens
        host.ended(error);
      }
    })();
    return ready;
  }

  async function submit(raw) {
    const text = String(raw || "").trim().slice(0, MAX_CHARS);
    if (!text || sending || host.busy() || !host.available()) return;
    const mine = epoch;
    sending = true;
    add({ role: "user", text });
    sync();
    try {
      await loadHistory(); // first, so the order on screen is the order the server keeps
      if (mine !== epoch) return;
      const body = await host.send(text);
      if (mine !== epoch) return;
      if (typeof body.left_today === "number") left = body.left_today;
      sending = false;
      if (body.proposal) {
        messages.forEach((message) => {
          if (message.proposal && message.state === "pending") setState(message, "stale");
        });
      }
      add({ role: body.notice ? "notice" : "assistant", text: String(body.reply || ""), proposal: body.proposal || null, state: "pending" });
    } catch (error) {
      if (mine !== epoch) return;
      sending = false;
      if (host.ended(error)) return;
      add({ role: "notice", text: host.explain(error) });
    } finally {
      if (mine === epoch) {
        sending = false;
        paint();
        sync();
      }
    }
  }

  async function applyCard(message) {
    if (message.state !== "pending" || host.busy()) return;
    if (!stillCurrent(message.proposal, host.current())) {
      setState(message, "outdated");
      return;
    }
    const mine = epoch;
    message.error = null;
    setState(message, "signing");
    try {
      await host.apply(message.proposal);
      if (mine !== epoch) return;
      setState(message, "applied");
    } catch (error) {
      if (mine !== epoch) return;
      if (host.ended(error)) return;
      message.error = host.explain(error, true);
      setState(message, "pending");
    }
  }

  /** Brings every control in line with the page: who may chat, what is busy, what the agent is doing. */
  function sync() {
    const can = host.available();
    if (!can) open = false;
    els.ask.hidden = !can;
    els.launch.hidden = !can || open;
    els.panel.hidden = !can || !open;
    els.launch.setAttribute("aria-expanded", String(open));
    document.body.classList.toggle("has-chat", can);
    if (!can) return;
    const busy = host.busy();
    els.sub.textContent = host.demo() ? "Example account. Answers here are scripted." : host.status();
    els.send.disabled = busy || sending || !els.input.value.trim();
    els.askSend.disabled = busy || sending; // with nothing typed it opens the panel
    document.querySelectorAll("[data-ask]").forEach((chip) => {
      chip.disabled = busy || sending;
    });
    els.left.textContent = host.demo() || left === null ? "" : `${left} left today`;
    messages.forEach((message) => {
      if (!message.proposal || message.state !== "pending") return;
      message.card.querySelectorAll("button").forEach((button) => {
        button.disabled = busy;
      });
      const apply = message.card.querySelector("[data-act='apply']");
      if (apply) apply.textContent = applyLabel();
    });
  }

  function show(text) {
    if (!host.available()) return;
    open = true;
    sync();
    paint();
    loadHistory();
    els.input.focus();
    if (text) submit(text);
  }

  function hide() {
    if (!open) return;
    open = false;
    sync();
    if (!els.launch.hidden) els.launch.focus();
  }

  /** Forgets the conversation: another wallet, or the session ended. */
  function reset() {
    epoch += 1;
    open = false;
    sending = false;
    messages = [];
    past = 0;
    left = null;
    ready = null;
    els.input.value = "";
    els.askInput.value = "";
    paint();
    sync();
  }

  els.launch.addEventListener("click", () => show());
  els.close.addEventListener("click", hide);
  els.panel.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hide();
  });
  els.form.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = els.input.value;
    if (!text.trim() || sending || host.busy()) return;
    els.input.value = "";
    submit(text);
  });
  els.input.addEventListener("input", sync);
  els.input.addEventListener("keydown", (event) => {
    // Enter sends and Shift+Enter breaks the line. An Enter that picks a word in an input method is neither.
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    els.form.requestSubmit();
  });
  els.askForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (sending || host.busy()) return;
    const text = els.askInput.value.trim();
    els.askInput.value = "";
    show(text);
  });
  document.querySelectorAll("[data-ask]").forEach((chip) => {
    chip.addEventListener("click", () => show(chip.dataset.ask));
  });

  paint();
  return { sync, reset, open: show };
}
