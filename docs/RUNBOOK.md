# halo-trade runbook

Operating procedure for a testnet or mainnet deployment. `docs/SPEC.md` is the specification.

**Everything real-money is a human action.** No script, agent or automation in this repository
may fund the mainnet grantee, set `HALO_ALLOW_MAINNET`, sign a mainnet grant or deposit, or deploy
to mainnet. Code and agents stop at testnet. Testnet transactions are the human operator's choice
too: the scripts below only run when someone runs them.

All commands use the explicit project and file: `docker compose -p halo-trade -f compose.yaml ...`.
Below, `DC` stands for that prefix. Never use plain `docker compose up`, and never prune.

## 1. Settings that must be set

Set these in `.env`, next to `compose.yaml` (see `.env.example` for every variable and default).

| Variable | Value for a real deployment |
|---|---|
| `HALO_CHAIN_BACKEND` | `injective` (the default `mock` is not a chain) |
| `HALO_NETWORK` | `testnet` or `mainnet` |
| `HALO_PUBLIC_BASE_URL` | `https://your.domain`: scheme, host and optional port only, no path. It goes into the wallet sign-in message |
| `HALO_GRANTEE_KEY_PATH` | Path to the key file inside the container, for example `/data/grantee.key` (section 4) |
| `HALO_TRUSTED_PROXY` | Your reverse proxy's address as the app sees it (section 2) |
| `HALO_ALERT_WEBHOOK` | URL that receives alerts. Empty is allowed, but then only the database and the log have them |
| `HALO_MARKET_TICKER` | Default `BTC/USDC PERP` |
| `HALO_LLM_API_KEY`, `HALO_LLM_MODEL` | The model for every lane. Empty means the agent cannot start. Do not set OpenRouter `order` or `require_parameters` |
| `HALO_DESK_ENABLED` | `1` runs the market desk, `0` turns it off (default on). See section 9 |
| `HALO_TELEGRAM_BOT_TOKEN`, `HALO_TELEGRAM_BOT_USERNAME`, `HALO_TELEGRAM_WEBHOOK_SECRET` | One bot for the whole service. Users connect from the page. Register the webhook with `scripts/telegram_setup.py` |
| `HALO_ALLOW_MAINNET` | Mainnet only, exactly `yes-real-money`. Set by a human |

The lane budgets (`HALO_RESEARCH_*`, `HALO_REFLECT_*`, `HALO_ANALYST_DAILY_CALLS`) have safe defaults. Setting a research or reflection budget to 0 turns that lane off without stopping the agent.

Also check the risk limits in `config.py` / `.env.example` (`HALO_RISK_PER_TRADE`,
`HALO_MIN_DEPOSIT_USD`, `HALO_MAX_DEPOSIT_USD`, `HALO_MAX_USERS`, and the rest). `HALO_DB_PATH` is
fixed to `/data/halo.sqlite` by compose. On testnet the chart API has almost no candles, so set
`HALO_CANDLE_NETWORK=mainnet` there (read-only).

Set `HALO_CANDLE_SOURCE=binance` on any deployment that should keep deciding through quiet hours.
The on-chain BTC market can go more than two hours without a trade, and then the features stop with
`MARKET_UNAVAILABLE`. With this set, analysis candles come from Binance BTCUSDT and the chart API is
the fallback. Orders and risk checks still read the chain.

The server refuses to start (`src/halo_trade/startup.py`) when:

- `HALO_PUBLIC_BASE_URL` is not `scheme://host[:port]`, or not https on a host other than localhost
- `HALO_TRUSTED_PROXY` is invalid, contains `*`, or is a /0
- `FORWARDED_ALLOW_IPS` contains `*`
- `HALO_DESK_ENABLED` is not `0` or `1`, or an LLM lane budget is negative or absurdly large
- on `HALO_NETWORK=mainnet`: the backend is not `injective`, the URL is not https, `HALO_GRANTEE_KEY_PATH` is empty, or `HALO_ALLOW_MAINNET` is not `yes-real-money`

The `injective` backend also refuses an empty `HALO_GRANTEE_KEY_PATH` on any network, and a key file that group or others can read. An empty
`HALO_ALERT_WEBHOOK` is only a warning.

## 2. HTTPS and `HALO_TRUSTED_PROXY`

Compose publishes the service on `127.0.0.1:18080` only. Terminate TLS in a reverse proxy on the
same host and forward `https://your.domain` to that port. The app enforces https through
`HALO_PUBLIC_BASE_URL` and sends HSTS when it is https.

Per-client limits (`/onboard/start`, setup-code failures) use the socket peer unless
`HALO_TRUSTED_PROXY` names the proxy. Set it to the address the app sees as the peer, which behind
the published port is usually the Docker bridge gateway of the `halo-trade_default` network (check
with `docker network inspect halo-trade_default`), and make the proxy append to `X-Forwarded-For`. Without
it every client shares one limit bucket. `X-Forwarded-For` is honoured only from addresses listed
here, and the app runs uvicorn with `--no-proxy-headers` so nothing else can trust it.

## 3. Pin the grantee and market in the web bundle

The page refuses a server response whose grantee or market differs from what was compiled in, and
on mainnet it refuses to run with nothing compiled in. The values are Docker build args, read from
`.env` when compose builds:

```
HALO_EXPECTED_GRANTEE_TESTNET=inj1...        HALO_EXPECTED_MARKET_ID_TESTNET=0x...
HALO_EXPECTED_GRANTEE_MAINNET=inj1...        HALO_EXPECTED_MARKET_ID_MAINNET=0x...
```

Set the grantee address from section 4 and the market id your network's market has (`/onboard/config`
reports it; check it against an independent source such as the Injective explorer before pinning).
A changed pin takes effect only after `DC up -d --build server`.

## 4. The grantee key

Generate it inside the container, so it is never on the host, in a shell history or in an image:

```bash
DC build server
DC run --rm --no-deps -T server python - <<'PY'
import os
from pyinjective.wallet import PrivateKey
key = PrivateKey.generate()[1]
fd = os.open("/data/grantee.key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="ascii") as handle:
    handle.write(key.to_hex())
print(key.to_public_key().to_address().to_acc_bech32())
PY
```

It prints the grantee address. Set `HALO_GRANTEE_KEY_PATH=/data/grantee.key`. The key stays on the
`halo-data` volume, mode 600, owned by the `app` user. Keep one encrypted copy offline, by hand.
Never put it in the repo, an image, a chat or a log.

This key can place and cancel orders in every subaccount that granted it, until those grants
expire (at most 30 days) or are revoked. It cannot withdraw. If it is lost or exposed: pause
(section 7), generate a new key, change the pinned build arg, rebuild, and have every user sign a
new grant. The old key must also be removed from each user's grants by the user, or expire.

**Gas.** The grantee pays for every order and cancel. Send INJ to its address: from the testnet
faucet on testnet, from a wallet a human controls on mainnet. One transaction costs about 0.0001
INJ. New entries stop below `HALO_GRANTEE_MIN_INJ` (default 0.05), and `grantee_low_gas` fires, but
closes keep running as long as the balance can pay. Keep at least 10 times that floor.

Deploy with `DC up -d --build server`, then check `https://your.domain/v1/health` shows `ok` and a
small `heartbeat_age_ms`.

## 5. Testnet scripts

Testnet only. Both scripts refuse anything else, and check the node's chain id as well. They use
their own wallets (`data/testnet_wallets.json`, mode 600, gitignored, never printed, never baked
into an image), not the server's grantee key. Build the test image first:
`DC --profile test build test`.

```bash
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/app" halo-trade-test:latest \
    python scripts/testnet_setup.py [--market-ticker "BTC/USDC PERP"]
```

Creates the wallets on the first run, prints what each still needs (exit 2 until funded from the
testnet faucet: at least 0.5 INJ each, 50 USDC on the trader, 300 on the maker), then issues the
grants and deposits (exit 0 when ready). Re-running is safe.

The trader wallet gets the production grant list plus one named extra,
`TESTNET_ONLY_LIMIT_ORDER_GRANT`, because cases b, c and d rest limit orders through the grant. The
service's own check calls that wallet `unexpected_grant`, so **the trader test wallet cannot be
onboarded through the service**. Use a separate, fresh wallet for the flow in section 6. The
scripts use `grant_problems`, which accepts only that extra and fails on anything else.

```bash
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/app" halo-trade-test:latest \
    python scripts/testnet_cases.py [--cases a,b,c,d,e,v1] [--dry-run]
```

`--dry-run` signs every case with placeholder account numbers and asks the node to simulate it;
nothing is broadcast. Without it, funded wallets send real testnet transactions. Exit 0 all PASS,
1 any case not PASS, 2 stopped before running (no funds or no grant). Case e needs an oracle that
moves and may report BLOCKED, which counts as not PASS: record it. Case v1 is the on-chain proof
that the market grant covers the conditional stop.

## 6. The gate before inviting users

All of these on the exact commit being deployed, recorded by a human:

1. `docker compose -p halo-trade -f compose.yaml --profile test run --rm --build test` passes.
2. Web `npm test` and the Playwright run pass at 1280 and 390 px.
3. A real LLM drives the decision and chat lanes on the mock chain without malformed replies.
4. On funded testnet wallets, `scripts/testnet_cases.py` reports PASS for a, b, c, d and v1 (e PASS or BLOCKED and recorded).
5. A testnet deployment with `HALO_CHAIN_BACKEND=injective`, the pinned grantee and market, and
   HTTPS: one operator with a fresh wallet (not the test trader wallet) walks the whole page, deposits at least
   the minimum, signs the grant, starts the agent, and a cycle opens a position with a live stop visible on
   the explorer. Then Close position closes it. Stop, start, Telegram connect and disconnect, revoke
   and the on-chain "remove the grant" step also work.
6. Force one alert on the testnet deployment (for example set `HALO_GRANTEE_MIN_INJ` above the
   grantee's balance so `grantee_low_gas` fires, then restore it) and confirm it reaches the webhook.
7. For mainnet: a human sets the mainnet pins and `HALO_ALLOW_MAINNET=yes-real-money`, funds the
   grantee, and does the first deposit, grant and trade from their own wallet with the minimum
   deposit. Only then invite anyone.

## 7. Operating: `scripts/ops.py`

The image does not ship scripts, so feed it over stdin (standard library only):

```bash
DC exec -T server python - status < scripts/ops.py            # kill_switch=on|off
DC exec -T server python - pause < scripts/ops.py             # no new entries for anyone
DC exec -T server python - resume < scripts/ops.py
DC exec -T server python - reset-risk USER_ID < scripts/ops.py
```

`pause` blocks new entries for every user and needs no restart. Closes, stops, the watcher and
flattens keep working. `reset-risk` sets a user's day-start and peak equity to their last equity
snapshot, for a user stuck at a daily-loss or drawdown limit after a transfer the watcher could not
classify. A user can pause or revoke themselves from the page.

Alerts: every alert is stored in the `alerts` table, logged as `alert level=... kind=...`
(`DC logs server`) and posted to `HALO_ALERT_WEBHOOK` as JSON. Failed posts are retried.

## 8. What each alert means

| Kind | Meaning | What to do |
|---|---|---|
| `stop_missing` (critical) | A position had no live stop covering its whole size for more than `HALO_STOP_GRACE_BLOCKS`, a position has no intent (`orphan_position`, it gets flattened), or a position is past its stop's trigger (`trigger_breached`, it gets closed), or the stop step threw an error. The watcher is already replacing the stop or closing | Check the user's position on the explorer. If it repeats, `pause`, then check chain access and gas. If the position remains open and unprotected, the user's own wallet can close it |
| `flatten` (critical) | The stop could not be placed, so the position was closed with a reduce-only market order. The submit returned `STOP_FAILED_FLATTENED` | Read `txs.raw_log` for the stop's failure and the `intents` row. Repeats mean something structural: `pause` |
| `tx_unknown` (critical) | A signed tx is unresolved after 2 minutes. No new tx can be signed for any user while it is open | Check the node and chain status. Never re-send by hand: the same bytes are retried and it resolves at `timeout_height`. Fix node access, or wait |
| `watcher_stale` (critical) | The watcher heartbeat is older than 60 s. Nothing is reconciling or protecting positions | `DC logs server`, then `DC restart server`. Then check open intents |
| `grant_lost` (critical) | A user's on-chain grant no longer passes the check: expired, revoked on chain, or an extra grant exists. New entries get `GRANT_MISSING` and the service's retries back off | Ask the user to renew on the page. A stop already on chain stays there. Repeats daily until fixed |
| `grantee_low_gas` (critical) | The grantee's INJ balance is under `HALO_GRANTEE_MIN_INJ`. New entries get `GRANTEE_LOW_GAS` | Fund the grantee (a human on mainnet). Closes are not blocked but need gas to land |
| `close_failed` (critical) | A user's close used its 3 attempts in the minute without landing. The stop stays on chain | Check the book depth and `txs.raw_log`. The next `/v1/close` after the minute retries |
| `take_failed` (warning) | The take-profit order was not placed. The position still has its stop | Nothing urgent. The position runs on its stop and the agent can close it |

## 9. Market desk

The market desk is part of the decision engine. With the engine present, the server reads public
market data and news feeds on one loop, as plain outbound GETs to hosts that are fixed in the
engine's code. Allow them if the host filters outbound traffic; the list is kept with the engine.

A source that fails logs `market feed ... failed` and its value expires to null on its own. The
agent keeps deciding without it. Any of these hosts blocking the server is therefore a quiet
degradation, not an outage: watch the log and the Market panel. To turn the whole thing off, set
`HALO_DESK_ENABLED=0` and restart the server; the panel disappears and every desk value reads as
null. The analyst and reflection calls stop on the kill switch; the feeds themselves keep reading.

## 10. Residual risks

Stated plainly. These are not fixed by anything in this repository.

- **You can lose the whole deposit.** A stop can fill worse than its worst price or fail in a
  fast or thin market. The watcher then closes at a wider price, which can still lose more than the
  previewed worst case. Nothing here promises a profit. An LLM is making the decisions.
- **The server holds a key that can trade every granted subaccount.** A compromised server or
  stolen grantee key can open bad trades until grants expire (30 days at most) or users revoke
  them. It cannot withdraw, but it can lose the deposit through trading.
- **A phished wallet signature** that the user approves despite the wallet's domain warning can
  start the agent or connect a Telegram chat to it.
- **One signing key, one tx at a time.** A stuck tx (`tx_unknown`) delays every user's protective
  transactions. `HALO_MAX_USERS` limits this. They do not remove it.
- **Single process, single SQLite file.** If the process is down, on-chain stops stay in place, but
  nothing replaces a missing stop or flattens. The database lives on one volume. Back it up.
- **Chain halts or a frozen oracle** stop triggers from firing. Entries are refused, positions can
  still move against the user.
- **Alerts go to one webhook, with no paging or delivery guarantee.** Someone has to be watching.
- **Revoking on the page stops the agent but not the on-chain grant.** The user must also do the
  "remove the grant" step, or wait for expiry.
- **A false headline can still produce a wrong market read.** Headlines reach the trading model only
  as closed-list values, so a headline cannot instruct it, but a false story can still push the
  market read the wrong way. Size, stops and limits stay in code either way.
- **Testing limits.** The test suites run on a mock chain. On-chain behaviour is only proven by
  `scripts/testnet_cases.py` and the gate above, on testnet, whose books and oracles differ from
  mainnet.
