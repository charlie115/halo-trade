# Hosted agent specification (2026-11-09)

Binding for the hosted rebuild. Where it conflicts with `docs/SPEC.md`, this file wins. Everything
in `docs/SPEC.md` about sizing, limits, the transaction machine, the watcher and grants still holds.

## What changes

Users no longer connect their own Hermes. The server runs the agent. Two lanes share one process:

| | Decision lane (the engine) | Chat lane (`telegram.py`, the dashboard's chat panel, the engine's assistant) |
|---|---|---|
| Job | Each interval, choose hold, close or enter | Status, explanations, notifications, and on the dashboard a proposed settings change |
| LLM | A read-only research step, then one call with no tools, JSON only, one re-ask | Tool loop, read-only tools, capped steps |
| Memory | Server-written journal of codes and numbers, plus the wallet's own trade cases and lessons | Per-user `chat_memory`, `chat_turns` |
| Failure | No order. Recorded as `no_decision`, never as `hold` | Reply fails. Trading is not affected |

Hard rules for this rebuild:

1. **The decision lane never reads `chat_memory`, `chat_turns`, any text a person typed, or any text a stranger wrote.** Its input is `service.state()`, the market desk's numbers and closed-list values, the journal, the wallet's own recalled trades and lessons, and read-only tool results (`docs/HARNESS.md`). Agent `reason` text is never fed back to the decision call (existing rule 8); the reflection call and the chat lane may read it, so owners can ask why.
2. **A failed or unreadable decision places no order and is recorded as `no_decision` with a code.** It is not `hold`. It is excluded from the journal given to the next decision, except as a count.
3. **Data that could not be read is reported as unavailable**, never as empty or zero.
4. **The chat lane has no tool that can open a position, resume the agent, change a strategy, deposit, or move funds.** `/pause`, `/close` and `/unlink` are the only state changes reachable from Telegram, and each only reduces risk or access. `/close` needs a confirm button. On the dashboard the assistant has one more tool, `propose_settings`, which stores nothing: it hands the page a change, and the change takes effect only when the wallet signs the `resume` message that states the new settings (Web chat, below).
5. **Tools are bound to the linked user on the server.** The LLM never supplies a user id, address or chat id.
6. **Telegram output is plain text** (no `parse_mode`), so nothing a model or user writes can become markup.
7. `llm_api_key`, `telegram_bot_token` and `telegram_webhook_secret` never appear in logs, responses, alerts or the database.
8. All limits stay in code on the server. A preset is the instructions the LLM sees and the entry rules the server checks (`strategies.evaluate`, `docs/HARNESS.md`). A preset cannot change size, leverage or any limit.
9. Both lanes respect the kill switch and `users.status`. Only `status='active'` runs decisions. `POST /v1/close` and the close paths stay unblocked.

## States

`users.status` already means what the agent needs. `active` = agent runs; `paused` = no new decisions, an open position stays with its on-chain stop; `revoked` = gone.

New wallets are created **`paused`** (created off, started in a separate step). Stop does not close a position, and the page says so.

## Removed

`skill/`, `GET /v1/skill`, `POST /v1/setup/exchange`, `setup_codes`, the setup prompt, `scripts/hermes_e2e/`, `SKILL_VERSION`, `tests/test_setup_exchange.py`. The agent API (`/v1/state|preview|submit|close`) stays in the code for a later bring-your-own-agent option, unreachable because no token is issued. `users.token_hash` keeps its pending placeholder.

## Presets (`strategies.py`)

`PRESETS: dict[str, Preset]`, each with `id`, `name`, `summary` (one line for the page), `instructions` (LLM text). No performance claims anywhere.

| id | name | Idea |
|---|---|---|
| `steady` | Steady | Needs trend and momentum aligned, widest stop, about one trade every few days |
| `trend` | Trend | Follows the 1h trend unless the 4h disagrees, 2 ATR stop, 3 to 4 ATR take-profit |
| `reversal` | Reversal | Fades stretched 24h moves when volatility is elevated, nearest take-profit |

## Engine (`plugin.py`)

The decision loop, its research step, per-wallet memory, the market desk, the analyst and the chat assistant are the engine. It is specified and kept outside this repository. `docs/HARNESS.md` says what it does and which open code holds it.

The open code reaches the engine only through `plugin.engine()`, which returns `None` when there is none:

- `api.py` asks it for its data sources (`feeds`), attaches it to the app (`attach`) and starts its loops (`loops`). The price monitor starts only with an engine, because without one there is nothing to wake.
- `onboarding.py` asks it for the dashboard's memory and market panels (`memory_view`, `market_view`) and for the chat (`chat`, `chat_history`).
- `telegram.py` passes it free text (`telegram_answer`).

With no engine, `agent_available` is false, `/onboard/resume` is refused with `AGENT_UNAVAILABLE`, both chat endpoints answer `409 AGENT_UNAVAILABLE`, the memory panel is empty, `market` is null, and free text on Telegram gets one fixed line. Everything else is unchanged.

## Decisions

The decision loop is specified with the engine. The open code relies on this much of it:

- Only wallets with `users.status='active'` are decided for, and none while the kill switch is on. `strategies.next_run_ms` says when a wallet is due, and the monitor may bring it forward.
- One `agent_decisions` row is written per cycle. `outcome` is one of `hold`, `entered`, `closed`, `rejected`, `skipped`, `no_decision` or `unknown`, with a code, the trigger and a snapshot for the owner's page.
- The model's answer is a single JSON object: `hold`, `close`, or `enter` with a side, a stop of 1.5, 2 or 3 ATR and a take-profit of 2, 3 or 4 ATR or none, plus a short reason. An answer that cannot be read is asked for once more, then recorded as `no_decision`. It is never repaired or guessed.
- An `enter` needs the preset's rules to back it (`strategies.evaluate`): `setup.ready` and the same side, else `rejected` with `SETUP_NOT_MET` and nothing is placed. The stop and take-profit distances then come from the rules and the wallet's settings, not from the model. This check runs in the engine.
- An entry goes through `Service.preview` and then `Service.submit`, and a close through `Service.close`, so every limit in `docs/SPEC.md` applies. Limit refusals are recorded as `rejected` with their code. A submit is never retried.
- After `entered`, `closed` or `unknown` the owner is told through `notify.enqueue`. Three `no_decision` in a row raise one notification and one `alerts` row.

## Scale and price events

Real reads take about 300 to 600 ms, so the limit is serial network waits and the public nodes' per-IP limits (20 chain and 50 indexer requests per second, 429 above that), not CPU.

- **Request budget (`chain/limiter.py`, `injective.py`).** A token bucket over every chain request, `HALO_CHAIN_RPS`, and another over chart and indexer calls, `HALO_INDEXER_RPS`. Broadcast, tx lookup, signing and everything on a close or stop path (`urgent_requests()`) take the next token ahead of every waiting read. Waiting counts against the call's deadline.
- **429.** A rate refusal is `ChainThrottled`, a kind of `ChainUnavailable`: the read is unavailable, never empty or zero. The bucket stops for the node's `Retry-After`, else 1 s doubling to 60 s, urgent requests included. It is counted, with one `chain_throttled` alert per hour.
- **Shared reads (`chain/shared.py`).** `snapshot` is single-flight with a 1.5 s TTL. Candles are kept until a new one can exist. The raw market inside `account` is shared the same way, so one account read is three requests. Account reads are never cached in the money path.
- **Watcher.** Up to `HALO_WATCH_CONCURRENCY` wallets at once, each with its per-user lock and time limit; signing still goes through the one signer slot. A wallet with a position or an unfinished order or tx, a close in progress, or a start within the last flat interval is read every `HALO_WATCH_INTERVAL_S`. Every other wallet, paused ones included, is read every `HALO_WATCH_FLAT_INTERVAL_S`, spread across that interval. Equity snapshots and grant checks stay about once a minute and reuse that tick's read. On-chain stops do not depend on this loop.

**Monitor (`monitor.py`).** One loop reads the market every `HALO_MONITOR_INTERVAL_S` behind a small `PriceSource` interface (polling today). One read serves every wallet and it writes nothing unless it wakes someone. For each `active` wallet, using only the database, it sets `next_run_ms = now` and a `wake_reason` when:

- `price_move`: the mark moved at least `HALO_WAKE_MOVE_ATR` times the 1h ATR since the wallet's last decision, or
- `near_stop`: a protected position's mark is within `HALO_WAKE_STOP_ATR` ATR of its stop.

At most one wake per wallet per `HALO_WAKE_COOLDOWN_S`. A wake only schedules a normal cycle: it places, changes and closes nothing, every limit applies, one entry per window included, and its LLM calls come from the decision pool. If the monitor fails, schedules continue and nothing is recorded. It respects the kill switch and `users.status`. A cycle runs at the engine's next tick, so up to 20 s after the wake.

**Skipping the LLM.** On a quiet scheduled cycle with no position the engine may make no model call and record `skipped` with code `NO_CHANGE`. Open positions and woken cycles are always evaluated, except that a wake with no position in a window whose entry is used is `skipped` like any other blocked cycle. `NO_CHANGE` rows are left out of the journal.

`agent_decisions.snapshot` also holds the desk's values at decision time, the tools the decision used, the recalled trades and `counter`, for the owner's page. Prompts take only closed-list conditions from it, through `trade_cases`.

`agent_decisions.trigger` says why a cycle ran: `schedule`, `price_move`, `near_stop` or `first`. `strategies.last_mark` is the mark when the model last answered.

**15-minute candles.** `ret_15m_pct`, `atr_15m_pct` and `trend_15m` come from closed 15-minute candles only. If they cannot be read those fields are null, never zero, and the decision proceeds on hourly data.

## Onboarding API (`onboarding.py`)

- `/onboard/complete` still proves grant and deposit, creates the user with status `paused`, and returns `{ok,status,inj_address,subaccount_id}`. **The session stays valid** (it no longer deletes `onboard_sessions`). No code, no prompt.
- `/onboard/start` accepts action `telegram` for the Telegram link signature. `/onboard/config` adds `presets:[{id,name,summary}]`, `agent_available:bool` (LLM configured and the engine present), `telegram_bot:str|null`.
- `POST /onboard/resume` body gains required `preset`; unknown preset is `422`-style `_Deny(400,"BAD_PRESET")`. Needs `agent_available` (`_Deny(409,"AGENT_UNAVAILABLE")`), a grant, and a balance within limits. In one transaction: upsert `strategies` (`next_run_ms=now`), set status `active`, open a `strategy_runs` row with `start_equity`.
- `POST /onboard/pause`: as today, and closes the active `strategy_runs` row (`stop_reason='user'`, `end_equity` if readable). Same on revoke (`stop_reason='revoked'`), which also releases the Telegram link as described under Telegram.
- EIP-4361 statements: `resume` says it starts the AI agent, which can open and close positions in the trading subaccount and cannot withdraw. `pause` says it stops the agent and leaves any open position with its stop. `onboard` no longer mentions a trading token: it says signing proves the wallet is yours.
- New, session-authenticated (`SessionIn`), rate limited per session:
  - `POST /onboard/agent` returns `{ok,status,halted_by_operator,preset,next_run_ms,run:{preset,started_ms,start_equity}|null,position:{side,quantity,entry_price,unrealized_pnl_usd,stop_price,take_profit_price,opened_ms,unrealized_r}|null,decisions:[{created_ms,outcome,side,stop_atr,take_profit_atr,reason,code,trigger,snapshot}],market:{...}|null,telegram:{available,linked,display},memory:{cases,lessons,playbook},stats:{trades,wins,losses,win_rate,avg_r,best_r,worst_r,avg_hold_ms,max_drawdown_pct}}`. `market` is the engine's page view of the market desk, null when the desk has nothing to show or there is no engine. Last 10 decisions. `position` is null only when the read succeeded and the account is flat; add `position_unavailable:true` when it could not be read.
  - `POST /onboard/agent/close` body `SessionIn`: `service.close`, returns the `SubmitOut` JSON.
  - `GET /onboard/candles` (public, per-client rate limit, about 30 s server-side cache): `{ok,period_ms,candles:[[open_ms,o,h,l,c],...],mark,as_of_ms}`, the last 168 hourly candles including the bar still open, oldest first. When the chain cannot be read: `ok:true` with `candles:[]` and `mark:null`, never a 500. `GET /demo` serves `index.html` with the same headers as `/`.
- Slots: a wallet holds a slot if `status in (active,paused)` and it has a `strategies` row with a minimum-deposit balance, or it finished `/onboard/complete` less than 24 h ago. Never-started wallets release after 24 h.

## Telegram (`telegram.py`)

`TelegramWorker(service)` with `async run(stop)` delivering the outbox and answering queued updates, and a FastAPI `router` in `telegram.py`.

**Webhook.** `POST /telegram/webhook`: constant-time check of `X-Telegram-Bot-Api-Secret-Token`, else 404, after the 256 KB body limit in `api.PATH_BODY_LIMITS`. Startup refuses a webhook secret under 32 characters when a bot token is set. Only private chats. Messages and callbacks older than 5 minutes are ignored; a callback carries no time of its own, so a button is as old as the message it sits on. `scripts/telegram_setup.py` registers with `drop_pending_updates`.

The webhook path does database and memory work only, and answers 200 (503 on a database error, so Telegram redelivers). Linkage is checked before anything is queued:

- An unlinked chat gets at most one fixed reply per 10 minutes, from an in-memory bounded map, with no database write and no queue slot. `/start <code>` from such a chat costs one read; only a valid code goes further. `telegram_seen_updates` is written for linked chats only, with a per-chat limit of 20 messages a minute and one fixed "slow down" reply. `/pause`, `/unlink` and a live `/close` button still work past that limit.
- `/pause` and `/unlink` are applied on the webhook path after the dedupe, in one transaction, never behind a queue. They read no chain.
- Everything else goes on a bounded queue (`UPDATE_QUEUE_MAX`), and an overflow gets a fixed "busy" reply.

**Linking.** `POST /onboard/telegram/link` takes `{eth_address, signature}` after `POST /onboard/start` with action `telegram`, whose statement says it connects a Telegram chat that can see this wallet's agent, stop it and close its position. A session is not enough. A wallet that already has a link is refused with `409 TELEGRAM_ALREADY_LINKED`: it must be disconnected first. It returns `{ok,url:"https://t.me/<bot>?start=<code>",expires_ms}`: a single-use code, stored as sha256 only, 10 minutes. `POST /onboard/telegram/unlink` stays session-authenticated.

`/start <code>` never links. The bot replies "Connect this chat to wallet 0xabcd...1234?" with a Connect button, single use, bound to that chat, and valid no longer than the code. Only the button links. A chat already connected to another wallet is told to send `/unlink` there. `telegram_links.display` holds `@username`, else the first name, plain text, capped at 64 characters, and `POST /onboard/agent` returns it as `telegram:{available,linked,display}`.

On any unlink (`/unlink` in the chat, the site's unlink) or revoke, `onboarding.release_telegram_link` deletes the link, link codes, `chat_memory`, `chat_turns` and unsent outbox rows in the same transaction, and queues one fixed line telling the old chat it was disconnected.

Accepted residual risk: someone the owner forwards a live link to can still press Connect. The masked wallet in the prompt and the display name on the dashboard are the mitigations.

**Commands, no LLM:** `/status`, `/why` (latest decision and its reason), `/pause` (status→paused, closes the run, `stop_reason='telegram'`), `/close`, `/unlink`, `/help`. `/close` sends a confirm button bound to the chat and the open intent id; if the position has changed the button does nothing and says so. Its reply says only what is known: a confirmed close, or the failure plus the stop state from an actual read, or that the account could not be read. `/status` and the assistant read the chain with a 3 second overall timeout and a per-user cache of about 10 seconds, compare the read against the highest height the service has seen without writing it, and report unavailable if older. A stalled chain cannot hold a worker, and a lagging read never says "no position".

**Free text** goes to the engine's assistant (`plugin.engine().telegram_answer`), only for wallets holding a `strategies` row and not revoked: `chat_daily_limit` messages per user per UTC day, and at most `CHAT_QUEUE_PER_USER` waiting per user, answered one at a time. The assistant's tools are read-only, and a failure touches nothing but the reply. With no engine the chat gets one fixed line. The watcher purges `chat_turns`, `telegram_seen_updates` and the outbox older than 7 days.

**Web chat.** `POST /onboard/chat` takes `{eth_address, session, text}` (text 1 to 2000 characters), at most 12 a minute per session, and answers `{ok, reply, notice, proposal, left_today}`. `POST /onboard/chat/history` returns the last 20 turns as words only, so the panel is not empty after a reload; a change proposed earlier is not offered again, and the panel says under those messages to ask for it again. Both need the dashboard session. The answer comes from the engine's assistant (`plugin.engine().chat`), under the same daily message limit and budgets as the Telegram free text. With no engine both endpoints answer `409 AGENT_UNAVAILABLE`.

On the dashboard the assistant can hand the page one more thing, a proposed settings change (`propose_settings`). It is built with `tuning.apply` on top of the current settings (or the new strategy's defaults carried over by `tuning.switched`), so a value that is not on the menu is refused, and a proposal equal to the current settings is refused as `NO_CHANGE`. A valid one is **not stored**. It is returned to the page as `proposal: {preset, name, tuning, summary, from: {preset, name, tuning}}`, where `summary` is the settings line of the signed message. The page shows each changed setting before and after and a "Sign to apply" button. Applying is the existing signed flow: `POST /onboard/start` with action `resume`, the wallet signs a message that states the strategy and every setting, and `POST /onboard/resume` checks them against the menus again (`tuning.normalize`) and stores them. The page refuses to sign a message that does not contain the proposal's `summary`, and marks a card out of date when the settings it started from are no longer in force. A wallet whose agent is already running signs once: the new settings start a new run, and the same settings signed again change nothing. Refusing the signature, dismissing the card or closing the panel leaves everything as it was.

One card is live at a time: each proposal starts from the settings in force, so the page marks an older unsigned card as replaced. A reply may speak of a card only in a turn that made one.

A model that is talked into anything by the owner's text can at most put a card on that owner's own screen with values from the menus. It cannot place, close, start, stop or store. `/demo` has no wallet and no session, so its chat never calls the server: the page answers from a short script and applies a change to the made-up account in memory.

**Budget.** The lanes have separate pools. `llm_daily_calls` and `llm_user_daily_calls` count decision calls only. Chat has `chat_global_daily_calls`, a per-user `chat_daily_limit * chat_max_steps`, and `chat_user_daily_tokens` checked before each step from `llm_usage`. Research, reflection and the analyst each have their own daily pools (`docs/HARNESS.md`, Budgets). When a pool runs out the step is skipped and the cycle continues, so no lane can starve a decision.

**Push, no LLM,** via `notify.enqueue` from `service.py`: position opened with stop, stop or take-profit hit, position closed with P&L, stop could not be placed and the position was flattened. Deduped by intent id.

**Outbox.** Round-robin per chat, trade notices first, at most 25 sends a second in total. A 429 holds that one chat for `retry_after` and is not counted as an attempt. Other failures back off. Trade notices retry for up to 24 hours and raise an `alerts` row if dropped; other kinds drop after 8 attempts. The link is re-read before each send. A message with a button or for a chat that is no longer linked sets `telegram_outbox.chat_id` / `reply_markup`. `httpx` with no redirects. A worker that dies is restarted, and nothing a single message does can end one.

## Foundation already in place

`config.py` settings, `db.py` tables, `context.AppContext.llm`, `llm.py`, `notify.py`. Treat them as fixed. Ask for nothing: if one is wrong, fix it and say so.
