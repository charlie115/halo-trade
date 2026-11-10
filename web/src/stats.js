import { bindThemeToggle } from "./theme-toggle.js";

function usd(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "0";
  return number.toLocaleString("en-US", { maximumFractionDigits: 0 });
}

function time(ms) {
  return new Date(ms).toISOString().slice(0, 16).replace("T", " ");
}

async function load() {
  const asof = document.getElementById("stats-asof");
  const errorEl = document.getElementById("stats-error");
  try {
    const response = await fetch("/stats.json", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(String(response.status));
    const stats = await response.json();
    document.getElementById("s-funded").textContent = String(stats.funded_wallets);
    document.getElementById("s-deposits").textContent = `${usd(stats.total_deposits_usd)} USDC`;
    document.getElementById("s-volume").textContent = `$${usd(stats.volume_usd)}`;
    document.getElementById("s-volume24").textContent = `$${usd(stats.volume_24h_usd)}`;
    document.getElementById("s-trades").textContent = String(stats.trades);
    document.getElementById("s-open").textContent = String(stats.positions_open);
    document.getElementById("s-stop").textContent =
      stats.stop_coverage_pct === null ? "No open positions" : `${Number(stats.stop_coverage_pct).toFixed(0)}%`;
    asof.textContent = `Updated ${time(stats.as_of_ms)} UTC`;

    const list = document.getElementById("fills-list");
    list.replaceChildren(
      ...stats.last_fills.map((fill) => {
        const li = document.createElement("li");
        const a = document.createElement("a");
        a.href = fill.explorer_url;
        a.rel = "noopener noreferrer";
        a.target = "_blank";
        a.textContent = fill.tx_hash;
        li.append(a);
        return li;
      }),
    );
    document.getElementById("fills-empty").hidden = stats.last_fills.length !== 0;
    errorEl.hidden = true;
  } catch {
    asof.textContent = "";
    errorEl.textContent = "Stats are not available right now. Reload this page in a minute.";
    errorEl.hidden = false;
  }
}

bindThemeToggle();
load();
setInterval(load, 30000);
