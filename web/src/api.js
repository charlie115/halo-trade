// Calls to our own server. Errors carry a code only.

export class ApiError extends Error {
  constructor(code, limit, extra) {
    super(code);
    this.code = code;
    this.limit = limit;
    this.extra = extra;
  }
}

async function request(path, options) {
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new ApiError("NETWORK", null, {});
  }
  let body = {};
  try {
    body = await response.json();
  } catch {
    // A non-JSON failure is still a failure, reported by status below.
  }
  if (!response.ok || body.ok === false) {
    throw new ApiError(body.code || `HTTP_${response.status}`, body.limit ?? null, body);
  }
  return body;
}

export function getConfig() {
  return request("/onboard/config");
}

export function getCandles() {
  return request("/onboard/candles");
}

function post(path, payload) {
  return request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export const api = {
  // `preset` and `tuning` go with `resume` only: JSON drops them when undefined, and the server refuses them elsewhere.
  start: (eth_address, action, preset, tuning) => post("/onboard/start", { eth_address, action, preset, tuning }),
  verify: (eth_address, signature) => post("/onboard/verify", { eth_address, signature }),
  status: (eth_address, session) => post("/onboard/status", { eth_address, session }),
  complete: (eth_address, session) => post("/onboard/complete", { eth_address, session }),
  pause: (eth_address, signature) => post("/onboard/pause", { eth_address, signature }),
  resume: (eth_address, signature, preset, tuning) => post("/onboard/resume", { eth_address, signature, preset, tuning }),
  revoke: (eth_address, signature) => post("/onboard/revoke", { eth_address, signature }),
  agent: (eth_address, session) => post("/onboard/agent", { eth_address, session }),
  agentClose: (eth_address, session) => post("/onboard/agent/close", { eth_address, session }),
  telegramLink: (eth_address, signature) => post("/onboard/telegram/link", { eth_address, signature }),
  telegramUnlink: (eth_address, session) => post("/onboard/telegram/unlink", { eth_address, session }),
  chat: (eth_address, session, text) => post("/onboard/chat", { eth_address, session, text }),
  chatHistory: (eth_address, session) => post("/onboard/chat/history", { eth_address, session }),
};
