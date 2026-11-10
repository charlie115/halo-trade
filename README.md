# HaloTrade

**Your agent can trade. It cannot withdraw.**

HaloTrade runs an AI trading agent for you on [Injective](https://injective.com). You connect a wallet, fund one
subaccount and sign one trade-only permission. The agent trades one perpetual market (BTC/USDC by
default) from that subaccount. It can never withdraw or transfer funds, and you can stop it at any time.

Live on testnet: <https://trade.dev.haloarena.ai>

Trades can lose money, up to the whole deposit. No profit is promised. This is not financial advice.

## How AI is used

Four separate jobs run in one service, and only the decision can lead to an order.

| | Decision | Research | Market analyst | Chat |
|---|---|---|---|---|
| Job | Choose hold, close or enter | Look things up before a decision, only with a position open or a setup that qualifies | Read headlines for the whole market | Answer the owner on the dashboard and on Telegram |
| Input | Market, indicators across four timeframes, account, position, market desk values, the preset's rule check, own memory, research results | The same numbers and read-only tools | Public headlines only | The owner's question and read-only tools |
| Output | One JSON object from a fixed menu | Tool results; its own words are dropped | Values from closed lists | Plain text. On the dashboard also a proposed settings change, which only the wallet's signature applies |
| On failure | No order. Recorded as `no_decision`, never as a hold | Decide on the baseline data (multi-timeframe pack and rule check included) | The last brief stands until it expires | The reply fails. Trading is unaffected |

The model chooses only hold, close or enter, and a short reason. **A preset's entry and exit rules are
checked in server code**: an entry the rules do not back is refused and places nothing, an exit the
rules call for is carried out, and the stop and take-profit distances come from the preset and the wallet's own settings, not the model. **Position size, leverage, prices and every
limit are computed by the server.** Chat text never reaches the decision, and the chat lane has no
tool that can open a position, close one, or change a setting. On the dashboard the owner can ask the
agent for a change in plain words ("only go long", "make it safer"). The agent answers with a card that
shows each setting before and after. Nothing is stored until the wallet signs a message that states
the new settings in full, the same message that starts the agent.

Text we did not write never reaches a model that can lead to an order. Headlines go to the analyst,
which has no tools and answers in closed lists, and only those values move on.

The engine that runs these four jobs is not in this repository. What is here, and what holds the
engine whatever it asks for, is under "What is open, and what is not" below.

## How Injective is integrated

- Perpetual orders on the Injective exchange module, submitted with `injective-py` as `MsgExec`.
- A typed AuthZ grant limited to creating derivative market orders and cancelling orders, for one subaccount and one market. See `docs/GRANTS.md`.
- Every position gets a reduce-only conditional stop **on chain**, so it holds even if this server is down.
- The wallet signs deposits and the grant in the browser through MetaMask (EIP-712) and sends them to the chain directly. The server never sees a wallet key.
- Every fill links to its transaction on the Injective explorer from the public stats page.

## How to use it

1. Connect a wallet and sign in.
2. Deposit USDC into the trading subaccount.
3. Sign the trade-only grant.
4. Choose a strategy, adjust it within set ranges, and start the agent. The settings are part of the message your wallet signs.
5. Follow its positions and decisions on the dashboard or in Telegram. Ask the agent how it is going, why it decided what it did, or for a change to its settings, in the chat on the dashboard.
6. Stop the agent, close the position or revoke access whenever you want.

## Safety in code, not in prompts

Worst-case risk per trade, maximum leverage, daily loss and drawdown limits, one entry per window, a
cooldown after a loss, a price collar and per-wallet deposit caps are all enforced by the server. A
transaction is signed once, persisted, and only then broadcast. A background watcher replaces a missing
stop and flattens a position it cannot protect. See `docs/SPEC.md` and `docs/HOSTED_SPEC.md`.

## What is open, and what is not

Everything a user has to trust is in this repository:

- the strategies, with the exact instructions the model reads and the same rules as code (`strategies.py`),
- the ranges a wallet can set, and the line of the signed message that states them (`tuning.py`),
- the entry checks, position sizing and risk limits (`limits.py`, `sizing.py`, `config.py`),
- the order path, sign-once transactions and the stop watcher (`service.py`, `txmachine.py`, `watcher.py`),
- the trade-only grant (`chain/grants.py`, `docs/GRANTS.md`),
- the page, and the tests for all of the above.

The decision engine is private: the loop that runs a decision cycle, its research tools, per-wallet
memory, the shared market desk with its news analyst, and the chat assistant. The live site runs with
it. [docs/HARNESS.md](docs/HARNESS.md) says what the engine does, what it cannot do, and which open
code holds it to that.

This repository builds and runs without the engine. The page, onboarding, the order path, the limits
and the watcher work, `/onboard/config` reports `agent_available: false`, and no agent can be started,
so nothing decides and no order is placed.

Hackathon judges and reviewers can get read access to the engine. Ask by opening an issue here, or
through the contact on our application.

## Status

Early testnet prototype.

- **Verified:** the full test suite on a mock chain, a real LLM driving the decision, research, analyst and chat lanes on that mock chain with live public market data, real read access to Injective testnet, and the live Telegram webhook.
- **See it without a wallet:** <https://trade.dev.haloarena.ai/demo> shows the dashboard with made-up data.
- **Not verified yet:** a transaction on any real chain, a real MetaMask signature, and Telegram commands from a linked account.

## Components

| Path | What it is |
|---|---|
| `src/halo_trade/api.py`, `auth.py` | HTTP API, health, and the background tasks |
| `src/halo_trade/strategies.py` | The three presets: the instructions the model reads, and the same entry and exit rules as code |
| `src/halo_trade/tuning.py` | A wallet's own settings for a preset: menus, checks, and the line of the signed message that states them |
| `src/halo_trade/plugin.py` | The one door to the decision engine. The engine itself is not in this repository |
| `src/halo_trade/llm.py` | OpenAI-compatible client and the daily call budget, for each lane |
| `src/halo_trade/telegram.py`, `notify.py` | Telegram bot (linking, commands, trade notices) and the outbox |
| `src/halo_trade/service.py`, `txmachine.py` | Intent state machine and the sign-once transaction machine |
| `src/halo_trade/limits.py`, `sizing.py`, `features.py` | Pure entry checks, position sizing, market features |
| `src/halo_trade/watcher.py`, `alerts.py` | Background reconciliation and operator alerts, in the API process |
| `src/halo_trade/monitor.py` | Price monitor between decisions: a sharp move or a price near the stop brings a wallet's next decision forward. It places nothing itself |
| `src/halo_trade/onboarding.py`, `account_view.py`, `stats.py` | Wallet onboarding and the dashboard's API (`/onboard/*`), read-only account views, and the public stats page |
| `src/halo_trade/chain/` | `injective.py` (real chain), `mock.py` (test double), `grants.py` (the grant list and its check) |
| `src/halo_trade/db.py`, `config.py` | SQLite schema (`/data/halo.sqlite`) and all settings (`HALO_*`, see `.env.example`) |
| `web/` | Onboarding, the dashboard, the `/demo` example account and the stats page, bundled with esbuild into the server image |
| `scripts/ops.py` | Pause, resume, status, reset-risk (no dependencies, run against the database) |
| `scripts/testnet_setup.py`, `scripts/testnet_cases.py` | Testnet-only wallet setup and on-chain behaviour cases |

## Tests

Everything runs in Docker, under the project name `halo-trade`.

```bash
# Python suite (mock chain)
docker compose -p halo-trade -f compose.yaml --profile test run --rm --build test

# Web unit tests (signing payloads, page guards)
docker build --target web -t halo-trade-web:test .
docker run --rm -v "$PWD/web/test:/web/test:ro" halo-trade-web:test npm test
```

The Python suite here runs against a stand-in for the engine (`tests/standin.py`), which answers the
door's calls with fixed values. `tests/test_engine_absent.py` covers the service with no engine at
all. The engine's own tests, and the browser suite that drives the page against a scripted engine,
are kept with the engine.

Do not run plain `docker compose up`. Always pass `-p halo-trade -f compose.yaml`.

## Run locally on the mock

```bash
docker compose -p halo-trade -f compose.yaml up -d --build server
curl -s http://127.0.0.1:18080/v1/health
# Open http://127.0.0.1:18080/ for the page, /stats for the stats page.
docker compose -p halo-trade -f compose.yaml down
```

The mock is an in-memory chain with no users and no test wallet funding, so the page renders and
the API answers, but there is no funded wallet to onboard with, and without the engine no agent can
be started. The default settings are for this local mock. Do not expose it.

## License

Copyright (c) 2026 HaloArena. All rights reserved. No license is granted to use, copy or modify this code.
