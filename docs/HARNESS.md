# The agent harness: what is open and what is not

The hosted agent has two parts. Everything a user has to trust is in this repository. The decision
engine is not: it is kept in a private tree, and the live site runs with it.

| Part | Where | In this repository |
|---|---|---|
| Strategies: the text the model reads, and the same rules as code | `strategies.py` | yes |
| The ranges a wallet may set, and the line of the signed message that states them | `tuning.py` | yes |
| Entry checks, position size, leverage and risk limits | `limits.py`, `sizing.py`, `config.py` | yes |
| The order path, sign-once transactions and the stop watcher | `service.py`, `txmachine.py`, `watcher.py` | yes |
| The trade-only grant and its check | `chain/grants.py`, `GRANTS.md` | yes |
| The price monitor that brings a decision forward | `monitor.py` | yes |
| Market features from candles | `features.py` | yes |
| The LLM client and the daily call budget of each lane | `llm.py` | yes |
| The page, onboarding and the Telegram commands | `web/`, `onboarding.py`, `telegram.py` | yes |
| The decision loop: what the model is shown and how its answer is read | the engine | no |
| Research tools, per-wallet memory and reflection | the engine | no |
| The shared market desk and the news analyst | the engine | no |
| The chat assistant | the engine | no |

Reviewers can ask for read access to the engine. See the README.

## How the two parts meet

`plugin.engine()` is the one door. The open code calls it when it needs the engine and gets `None`
when there is none. Nothing else imports an engine module.

| Call through the door | Used by |
|---|---|
| `feeds`, `attach`, `loops` | `api.py`, at startup: the engine's data sources and background loops |
| `memory_view`, `market_view` | `onboarding.py`: the memory and market panels of the dashboard |
| `chat`, `chat_history` | `onboarding.py`: the chat panel |
| `telegram_answer` | `telegram.py`: free text in a linked chat |

With no engine the service still starts. The page, onboarding, deposits, the grant, the order path,
the limits and the watcher work as they do on the live site. `/onboard/config` reports
`agent_available: false`, starting an agent is refused with `AGENT_UNAVAILABLE`, the chat endpoints
answer the same, and free text on Telegram gets one fixed line. Nothing decides, so no order is
placed. `tests/test_engine_absent.py` holds this.

## What the engine does

For each active wallet, on the wallet's own interval, or sooner when `monitor.py` sees a sharp move
or a price near the stop:

1. It reads the wallet's state through `Service.state`. On an outage, a block or no change it stops
   here, with no model call.
2. It builds a data pack (four timeframes, the book, the position) and runs the preset's rules,
   `strategies.evaluate`, which give the `setup`.
3. With a position open or a setup that qualifies, a research step may look things up with
   read-only tools. Only tool results are kept. What the model wrote during research is dropped.
4. One model call with no tools returns one strict JSON object: hold, close or enter, a short
   reason, and what would prove the decision wrong. An answer that cannot be read places no order
   and is recorded as `no_decision`, never as a hold.
5. It acts only through `Service`: `preview` then `submit` for an entry, `close` for a close.

Around that cycle the engine keeps a memory for each wallet (its closed trades with the conditions
at entry, one-line lessons, recall by similar conditions; nothing crosses between wallets), a market
desk shared by all wallets (public market data and headlines), a separate analyst call that reads
the headlines and answers only in closed lists, and the chat assistant, which has read-only tools.

## The one rule

Text we did not write never reaches the model that places orders.

News headlines are written by strangers, so a headline is an instruction channel. They go to the
analyst call, which has no tools and can only answer in closed lists. The decision call receives
those list values, never the words.

| Input | Written by | Reaches the decision call as |
|---|---|---|
| Prices, candles, funding, open interest, positioning | exchanges | numbers |
| Headlines, central bank releases | strangers | closed-list values from the analyst, never text |
| Chat with the owner | the owner | nothing |
| Own lessons | a reflection call, from numbers and own reasons only | text, length-capped, links and addresses refused |
| Tool results | our server | JSON |

This rule is kept inside the engine, so this repository states it but cannot show it.

## What the engine cannot do

These hold whatever the engine or the model asks for, and the code that holds them is here.

| It cannot | Because | Code |
|---|---|---|
| Withdraw or transfer funds | The wallet's grant covers only derivative market orders and cancels, for one subaccount and one market. The chain refuses anything else | `chain/grants.py`, `GRANTS.md` |
| Choose a size, leverage or price | The server computes them from the stop distance and the wallet's risk settings | `sizing.py`, `service.py` |
| Pass a risk limit | Worst-case loss per trade, maximum leverage, daily loss, drawdown, one entry per window, the cooldown after a loss and the price collar are checked on every preview and submit | `limits.py`, `service.py` |
| Leave a position without a stop | Every entry places a reduce-only stop on chain. The watcher replaces a missing stop and flattens a position it cannot protect | `service.py`, `watcher.py` |
| Send a transaction twice | A transaction is signed once, stored, and only then broadcast | `txmachine.py` |
| Change a wallet's settings | The settings are part of the message the wallet signs, and `/onboard/resume` checks them against the menus again | `tuning.py`, `onboarding.py` |
| Trade for a paused or revoked wallet, or past the kill switch | `Service` refuses | `service.py` |

One check sits on the engine's side of the door. The rules a preset's entry must meet are open
(`strategies.evaluate`, below). The step that holds the model's `enter` against them, and refuses
it with `SETUP_NOT_MET` before `Service` is called, runs in the engine. So this repository shows
what the rules are and every limit an order must pass. It does not show the line that compares the
model's answer with the rules.

## Rule engine (`strategies.evaluate`)

The preset text is what the model reads. `evaluate` is the same rules in code, so the two cannot
disagree, and a test holds them together. It takes the preset, the state, the desk's values and
the data pack, and returns a frozen `Setup`:

```json
{"side": "long|short|null", "ready": true, "stop_atr": 3, "take_profit_atr": 4,
 "checks": [{"id": "trend", "ok": true, "value": "up"}],
 "exit_signal": null}
```

| | Enter | Stop / TP | exit_signal (position open) |
|---|---|---|---|
| Steady | trend up with ret_1h and ret_4h above 0, short mirrored | 3 / 4 | trend points against the position |
| Trend | side from trend, flat is none | 2 / 3, or 4 when ret_4h points with the trend and funding is not crowded that way | trend points against the position |
| Reversal | abs(ret_24h) >= 6 x atr_1h and volatility_ratio > 1.2: short after a rise, long after a fall | 2 / 2 | ret_1h shows the original move resuming |

Filters, as the text says them. Context can only keep you out:

| check | Steady | Trend | Reversal |
|---|---|---|---|
| `event_risk` high blocks | yes | yes | yes |
| `fomc` under 6 hours blocks | yes | yes | no |
| `funding` crowded the way of the trade (above 5 long, below -5 short) blocks | yes | no: only moves take-profit to 3 | as crowding against the fade (above 5 long, below -5 short) |
| `higher_timeframe`: the 4h trend must not point against the trade | yes | yes | no |

`checks` uses a closed list of ids: `trend`, `momentum_1h`, `momentum_4h`, `higher_timeframe`,
`stretched`, `volatility`, `event_risk`, `fomc`, `funding`, `direction`. `value` is a short number
or a closed-list string. A context value that is null gives `ok: null` and blocks nothing. A core
signal that is missing means no setup: `side` null, `ready` false. `side` stays set when a filter
blocks, so the owner can see what the rules would have done. `ready` is the only thing an entry
needs.

The engine uses the result in two places. With a position open and `exit_signal` true, a `hold`
from the model becomes a close. On `enter`, if `ready` is false or the side does not match, nothing
is placed and the decision is recorded as `rejected` with `SETUP_NOT_MET`. If it passes, the stop
and take-profit distances come from the `Setup`, whatever the model wrote.

## A wallet's own settings (`tuning.py`)

The three presets are starting points. Before it starts, a wallet may change these, each from a short
menu the server sends in `/onboard/config`:

| Setting | Menu | Note |
|---|---|---|
| Direction | long and short, long only, short only | A side that is ruled out adds the check `direction` and blocks |
| Stop distance | 1.5, 2 or 3 x the hourly range | |
| Take-profit | 2, 3 or 4 x the hourly range | Trend may also extend it one step when the 4-hour move agrees |
| Filters | on or off, with one number each | Fed decision within 3, 6, 12 or 24 hours; funding past 2, 5 or 10 bps; the 4-hour trend |
| Reversal's entry | 24-hour move of 4, 6 or 8 x the hourly range; volatility above 1.0, 1.2 or 1.5 | Always on |
| Decision interval | 15 or 30 minutes, 1 or 4 hours | Never shorter than the operator's |
| Risk per trade, daily loss limit, leverage | Steps at or below the operator's limits | A wallet can only tighten them |

There is no free text, so nothing a wallet types reaches the model. `tuning.normalize` rejects any
value off the menus and returns one complete, canonical object, stored in `strategies.tuning`.
`strategies.instructions` and `strategies.evaluate` both read that object, so the text the model
reads and the check that is run still cannot disagree. The engine takes the interval from it, and
`tuning.risk_of` gives the risk checks the tighter of the wallet's and the operator's limits.

The settings are part of what the wallet signs. `/onboard/start` puts them in the message as one
line (`Settings: long only; stop 1.5x and target 4x the hourly range; ...`), and `/onboard/resume`
rebuilds the message from the preset and settings it is sent, so a start with other settings than the
signed ones fails the signature check. The dashboard shows the same line, and names the strategy
"Steady, edited" when it is not the preset as designed.

## Candles

Analysis candles can come from the wider market. The on-chain BTC market can go hours without a
trade, and an hour with no trade has no candle, so its own chart would stop the agent in a quiet
stretch. The on-chain mark follows an oracle, so wider-market candles describe the same price.
Orders, the mark, the spread and every risk check still read the chain. If the wider feed fails,
the on-chain chart is used.

## Budgets

Each lane has its own daily pool of model calls (`llm.py`, `config.py`), so research can never
spend a decision's budget. When a pool runs out the step is skipped and the cycle continues.

| Lane | Per wallet per day | Service per day |
|---|---|---|
| decision | 200 | 3,000 |
| research | 300 | 6,000 |
| reflect | 24 | 480 |
| analyst | not per wallet | 96 |

## What the page shows

Each decision opens to what the agent looked at, the market read at that moment, the trades it
recalled and what would prove it wrong, plus the rules' verdict with every check and the trend on
four timeframes. `POST /onboard/agent` also returns why each cycle ran, the position's age and
result so far in R, the wallet's memory (case count, up to 5 lessons, win rate and average R by
side and trend) and the newest run's statistics (nulls when unknown, never a fake zero). A market
panel shows the current market read and the headlines behind it, with links and source credit, and
`GET /onboard/candles` draws the last 168 hourly candles (public market data, about 30 s cache).
