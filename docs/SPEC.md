# halo-trade MVP specification

An AI agent trades one Injective perpetual market through this service. The agent holds no chain
key. It reads state, previews and submits intents. The server sizes the position, re-checks limits,
signs through a trade-only AuthZ grant, and places the stop as an on-chain conditional order.

`docs/HOSTED_SPEC.md` describes the hosted agent and overrides this file where they differ. The
`/v1/*` agent API below remains in the code but no token is issued for it.

Shares nothing with HaloArena: separate repo, DB, network. Market: BTC/USDC PERP (config).

Authoritative shared code, do not fork or redefine: `src/halo_trade/contracts.py`,
`src/halo_trade/chain/base.py`, `src/halo_trade/db.py`, `src/halo_trade/config.py`.
If you need a change there, make the smallest additive change and say so in your report.

## Hard rules (these are the product)

1. No chain key in the agent. No endpoint accepts a raw transaction, address or calldata.
2. Size, leverage, stop price and limits are computed and enforced by server code. A number in the skill text is documentation only.
3. Every position has a reduce-only on-chain stop. If the stop cannot be placed, flatten immediately and verify the flatten.
4. Any failure ends in no new risk. Never invent an action. `POST /v1/close` is never blocked by a limit.
5. One entry intent per (user, 15-minute window). A ticket is single-use. Missed windows are never replayed.
6. Sign once. Persist tx bytes, hash, sequence and timeout_height BEFORE broadcast. On a transport error re-broadcast the SAME bytes only. "Unknown" is resolved by `timeout_height`: included => final; latest height > timeout_height and not found => not executed. Only then may a new tx be built. Never use `MsgBatchUpdateOrders` for entry+stop. One in-flight tx per signing key.
7. `cid` is deterministic from (user, window, leg) and is for matching only, never for dedupe.
8. Error responses carry codes and limit values only, never commands or free text from third parties. State contains numbers and enums, no external text.
9. All money math uses `Decimal`. Prices and quantities are rounded to market ticks, in the direction that cannot increase risk.
10. No telemetry, no third-party calls other than Injective endpoints and the optional alert webhook.

## Time

`window_id = floor(now_ms / 900000)`. Inject the clock (`now_ms` argument or a `Clock`), never read wall time inside pure logic.

## Agent API (Bearer token; token stored only as sha256)

| Call | Behaviour |
|---|---|
| `GET /v1/state` | `StateOut`. Issues a fresh `observation_id` for this user and window. Blocked reasons are listed, it never errors for a limit |
| `POST /v1/preview` | `PreviewIn` -> `PreviewOut`: quantity, stop price, take-profit price, worst loss, ticket valid 30 s. Runs all entry checks. No side effects except the ticket and the decision log |
| `POST /v1/submit` | `SubmitIn` -> `SubmitOut`. Executes the ticket. Same ticket again returns the stored result |
| `POST /v1/close` | `CloseIn` -> `SubmitOut`. Reduce-only market close, cancels remaining stop and take-profit. Never blocked by a limit, the kill switch or the rate limiter. A failing close is retried at most 3 times per minute; calling it again within that minute does not buy more attempts |
| `GET /v1/health` | Liveness, no token. Reports the age of the watcher heartbeat |

Error body is always `ErrorOut` with HTTP 4xx/5xx. `outcome`:
- `rejected`: nothing happened, the agent may try a new decision in a later window
- `failed`: attempted and definitively did not take effect
- `unknown`: result not yet known. The agent must NOT retry; next cycle reads `/v1/state`

Rate limit per token: 60 requests/min -> `RATE_LIMITED`.

## Entry checks (order matters, all in `limits.py`, pure functions, each has a test proving it rejects)

Kill switch (`PAUSED`), user status, grant valid (`GRANT_MISSING`), grantee gas balance at or above `HALO_GRANTEE_MIN_INJ` (`GRANTEE_LOW_GAS`, never blocks a close), market data fresh < 30 s (`MARKET_UNAVAILABLE`, also when either side of the book is empty, or at state and preview when there are too few closed hourly candles or the newest is over 2 h old; `as_of_ms` is the older of the block header time and the oracle's own timestamp), observation exists, belongs to user, same window, age < 120 s (`STALE_OBSERVATION`), intent menu valid and stop price above zero (`INVALID_INTENT`), no open position (`POSITION_OPEN`), window unused (`WINDOW_USED`), cooldown (`COOLDOWN`), daily loss, including `worst_loss` having to fit the remaining day budget (`DAILY_LOSS_LIMIT`), drawdown (`DRAWDOWN_LIMIT`), over equity cap (`OVER_CAP`), take-profit distance >= `MIN_TP_COST_MULT` x round-trip cost (`TP_TOO_CLOSE`), size, at the mark and at the stop's worst price (`SIZE_TOO_SMALL`), balance (`BALANCE_TOO_LOW`), depth on both the entry and the exit side (`BOOK_TOO_THIN`).

At submit, repeat the checks with a fresh snapshot, plus ticket valid (`TICKET_EXPIRED`, `TICKET_USED`), drift `abs(mark_now - ticket.mark) / ticket.mark <= DRIFT_MAX` else `STALE_DECISION`, and risk growth: with `worst_entry_now = mark_now * (1 +/- COLLAR)`, `qty * (worst_entry_now - stop)` (sign-adjusted) must not exceed `MAX_RISK_GROWTH` x `qty * abs(worst_entry_at_ticket - stop)`, where `worst_entry_at_ticket` is the ticket's mark with the same collar, else `STALE_DECISION`. A submit refused by any of these burns the ticket and uses up the window. Default `DRIFT_MAX` is 0.0025, half of `MIN_STOP_PCT`; default `MAX_RISK_GROWTH` is 1.5.

## Sizing (`sizing.py`, pure)

```
risk_amount   = min(equity * RISK_PER_TRADE, day_left)
day_left      = max(0, equity - day_start_equity * (1 - DAILY_LOSS))   what is left of today's loss limit
stop_distance = max(stop_atr * atr_1h, mark * MIN_STOP_PCT)
stop_price    = mark - stop_distance (long) | mark + stop_distance (short)
entry_worst   = mark * (1 +/- COLLAR)                  rounded so it never leaves the collar
stop_worst    = stop_price * (1 -/+ STOP_WORST_PCT)    rounded so it never exceeds STOP_WORST_PCT
worst_loss(q) = q * abs(entry_worst - stop_worst) + q * (entry_worst + stop_worst) * taker_fee
qty           = risk_amount / worst_loss(1)
qty           = min(qty, equity * MAX_LEVERAGE * INTERNAL_CAP / mark)
qty rounded DOWN to quantity tick; notional must be >= market min notional
margin_needed = notional / MAX_LEVERAGE  must be <= available
tp_price      = mark +/- take_profit_atr * atr_1h, or none
```
`RISK_PER_TRADE` is a budget for the worst case: entry filled at the collar, stop filled at its worst price, the taker fee on both legs. So `worst_loss(qty) <= risk_amount`; the leverage cap and the rounding can only shrink it.

Defaults: `HALO_RISK_PER_TRADE` 0.03, `HALO_COLLAR` 0.003, `HALO_STOP_WORST_PCT` 0.015, `HALO_CLOSE_WORST_PCT` 0.03, `HALO_DRIFT_MAX` 0.0025, `HALO_DAILY_LOSS` 0.06. The rest are in `config.py` and `.env.example`.

Stop price rounds so that the stop distance does not grow. Worst price for a market entry is `mark * (1 +/- COLLAR)` and must also respect depth. Stop worst price is `trigger * (1 -/+ STOP_WORST_PCT)`. A reduce-only close or flatten takes `mark * (1 -/+ CLOSE_WORST_PCT)`, wider than a stop because getting out matters most.

## Execution flow

1. Insert the intent in one `BEGIN IMMEDIATE` transaction under `UNIQUE(user_id, window_id)`.
2. Build, persist, broadcast the market entry. Resolve the outcome through the tx machine.
3. Confirm the position from chain state. Partial fill: use the actual position quantity.
4. Build, persist, broadcast the reduce-only STOP order sized to the actual quantity, in a separate tx from the entry (a market entry + reduce-only stop in one tx is rejected by the chain with code 81). A TAKE, if requested, goes in its own tx once the stop is live. It is best effort: a failure raises `take_failed` and never flattens.
5. If the stop is not live: retry once with a fresh tx after the previous is final, then flatten with a reduce-only market order and report `STOP_FAILED_FLATTENED`. A stop whose trigger the mark has already passed would be refused, so that case flattens at once. Any failure on the way to a live stop other than a transport outage counts as "stop not placed" and raises an alert. Code 97 on a retry means the earlier stop is still live: re-read, do not flatten. When the last flatten is final and the position is still open, one fresh stop attempt goes out before the next flatten.

Intent stages: `entry_pending -> entry_sent -> entry_filled -> stop_pending -> protected -> closed`; terminal alternatives `rejected`, `failed`, `flattened`. While unresolved the stage stays where it is and the intent's outcome is `unknown`.

## Watcher (`watcher.py`, every `WATCH_INTERVAL_S`, also on startup)

- Resolve txs in `signed`/`sent` state; resume intents from their persisted stage. Restart must never rebroadcast a closed window's entry.
- For each active or paused user, and any user with an open intent: position without a live stop covering its whole size for more than `STOP_GRACE_BLOCKS` blocks: replace it, else flatten. Past grace this always raises `stop_missing`, whatever stage the intent is in. A position with no intent has no known stop price, so past grace it is flattened.
- Position exists and mark is beyond the stop trigger for more than `STOP_GRACE_BLOCKS` blocks (trigger failed on a thin book): reduce-only market close.
- No position but stop/take orders remain: cancel them. Position closed: write `trades`, cooldown after a stop and after any other exit that lost money except a take-profit. A position counts as closed on two flat reads at increasing heights, or one read at or past our own landed close; a read at a height already passed is ignored.
- Equity snapshot every 60 s. A balance move > 1% between snapshots that are both flat or both in a position, balance being equity without unrealized pnl, is a deposit/withdrawal: rebase day-start and peak against the last non-zero balance. Not while realized pnl can be in the difference: a trade closed since the last snapshot, or an open intent whose position is gone or changed size. Withdrawing everything and redepositing clears nothing. `scripts/ops.py reset-risk USER_ID` resets a user's baselines by hand.
- Alerts: stop missing, flatten happened, unknown unresolved > 2 min, watcher heartbeat stale, grant lost, grantee gas low, close attempts exhausted, take-profit failed.
- At most once an hour, delete observations older than 7 days and tickets older than 7 days that are used or expired.
- Each user step and the whole grant check run under a time limit, so a hung call cannot stall the tick. Protective txs are limited to `RETRY_LIMIT` per user per `RETRY_WINDOW_MS`, and fewer while the grant is bad.

## Chain reads

Every chain call has a deadline and raises `ChainUnavailable` past it. Heights on account reads come from each response's `x-cosmos-block-height` header, and an account read is only as new as its oldest response. The position's mark is carried on the account read, so the stop, close and flatten paths depend neither on the book nor on the display-only oracle field. The market's candles come from `HALO_CANDLE_NETWORK` (default `HALO_NETWORK`); on testnet it may be set to mainnet to read the same ticker's candles, read-only, because the testnet chart API has almost none. `HALO_TX_TIMEOUT_BLOCKS` defaults to 20. One grantee key serves all users for the MVP; users are staggered by cron minute.

## Onboarding (web page plus `/onboard/*`)

1. Connect MetaMask.
2. Prove address ownership: the page asks the wallet to `personal_sign` a server-issued EIP-4361 (Sign-In with Ethereum) message, so the wallet can check the domain itself. The statement says plainly that the signature can issue a trading token, for this site only.
3. Deposit USDC into the dedicated subaccount (`MIN_DEPOSIT_USD`, default 50, to `MAX_DEPOSIT_USD`, default 200). At $20 some stops fall under the market's minimum size.
4. Sign one trade-only AuthZ grant: two typed exchange authorizations, market and conditional orders plus cancel, limited to the subaccount and the single market, with no transfer permission (`docs/GRANTS.md`).
5. `POST /onboard/complete` verifies grant and balance on chain and creates the wallet stopped. Starting the agent is a separate, signed step (`docs/HOSTED_SPEC.md`).

Limits and rules:

- `MAX_USERS` slot rules are in `docs/HOSTED_SPEC.md`.
- Challenges are keyed by (address, action) and sessions are stored apart from them: `/onboard/start` from anyone can never disturb another action's pending signature or a verified session. Each is single-use.
- `/onboard/start` is limited per client for addresses not yet in `users`; the cap on pending challenges never applies to addresses already in `users`, so they can always pause or revoke. `/onboard/status` is limited per session and reads the chain at most every 10 s per address.
- A request body above 16 KB gets 413 whether or not it declares a length. Every string field has a maximum length.
- The client address used for limits is the socket peer unless `HALO_TRUSTED_PROXY` (comma-separated IPs/CIDRs, default none, never `*`) names the proxy in front of the service. `FORWARDED_ALLOW_IPS=*` is refused at startup too.
- The server refuses to start with a `HALO_PUBLIC_BASE_URL` that is not `https` (unless localhost) or holds anything but scheme, host and port; on mainnet it also needs the `injective` backend, the grantee key and `HALO_ALLOW_MAINNET=yes-real-money`.
- The page derives the Injective address and subaccount itself and refuses a mismatch from the server. The allowed authorizations, the market id and the expected grantee per network are compiled in at build time (`HALO_EXPECTED_*` build args); a mismatch is refused, as is mainnet with none compiled in. Grant expiry is capped at 30 days on the client.
- Users can pause or revoke. If the server revoke fails, the page still offers the on-chain "remove the grant" step. On chain, the grant can always be removed from any Injective wallet.

Residual risks: a hostile page, which the build-time checks do not help against if the attacker serves it, and a phished wallet signature that the user approves despite the wallet's domain warning.

## Public stats (`GET /stats.json`, `GET /stats`)

Aggregate only: funded wallets, total deposits, cumulative and 24 h volume, trades, positions open, stop coverage percent, last fills with explorer links (tx hash only). No token, no per-user PII. The linked transactions are public and show the wallet that traded.

## Decision log

Every state, preview, submit and close that reaches the service writes a row to `decisions` (payload JSON), except a close that finds no position (`NO_POSITION`), where nothing was attempted. This is the future replay dataset. Never log tokens.

## Validation

Docker only: `docker compose -p halo-trade -f compose.yaml --profile test run --rm --build test`. Do not run plain `docker compose up`, do not touch any container, volume or network that is not in project `halo-trade`, never `docker system/volume prune`.
Every rule above needs a test that fails if the rule is removed. Tests run against `chain/mock.py`, which must enforce Injective's documented behaviour (see `chain/base.py` docstrings).
Mainnet transactions are out of scope for all code tasks here: nothing in tests or scripts may broadcast to mainnet.
