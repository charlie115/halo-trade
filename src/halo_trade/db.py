"""SQLite schema. One process writes (API plus watcher). All timestamps are ms."""
from __future__ import annotations

import sqlite3

# `user_id` is NOT NULL and has no foreign key: a service-level lane books under `llm.SERVICE_USER_ID`.
_LLM_USAGE = """CREATE TABLE IF NOT EXISTS llm_usage (
  day_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  lane TEXT NOT NULL CHECK (lane IN ('decision','chat','research','reflect','analyst')),
  calls INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day_id, user_id, lane)
);"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  inj_address TEXT NOT NULL UNIQUE,
  eth_address TEXT NOT NULL,
  subaccount_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','revoked')),
  token_hash TEXT NOT NULL UNIQUE,
  grant_checked_ms INTEGER,
  created_ms INTEGER NOT NULL
);
-- One live challenge per (address, action). Spent by the matching /onboard call. `nonce` is public.
CREATE TABLE IF NOT EXISTS onboard_challenges (
  inj_address TEXT NOT NULL,
  action TEXT NOT NULL,
  nonce TEXT NOT NULL,
  issued_ms INTEGER NOT NULL,
  expires_ms INTEGER NOT NULL,
  PRIMARY KEY (inj_address, action)
);
-- A verified wallet session, sha256 only. Kept apart from challenges so /onboard/start cannot touch it.
CREATE TABLE IF NOT EXISTS onboard_sessions (
  inj_address TEXT PRIMARY KEY,
  session_hash TEXT NOT NULL,
  expires_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  window_id INTEGER NOT NULL,
  mark TEXT NOT NULL,
  as_of_ms INTEGER NOT NULL,
  created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  window_id INTEGER NOT NULL,
  observation_id TEXT NOT NULL,
  side TEXT NOT NULL,
  quantity TEXT NOT NULL,
  mark TEXT NOT NULL,
  stop_price TEXT NOT NULL,
  take_profit_price TEXT,
  stop_atr TEXT NOT NULL,
  take_profit_atr TEXT,
  reason TEXT NOT NULL DEFAULT '',
  expires_ms INTEGER NOT NULL,
  used_ms INTEGER,
  created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS intents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  window_id INTEGER NOT NULL,
  ticket_id TEXT NOT NULL,
  side TEXT NOT NULL,
  quantity TEXT NOT NULL,
  stop_price TEXT NOT NULL,
  take_profit_price TEXT,
  stage TEXT NOT NULL,
  outcome TEXT,
  error_code TEXT,
  entry_price TEXT,
  result_json TEXT,
  created_ms INTEGER NOT NULL,
  updated_ms INTEGER NOT NULL,
  equity_before TEXT,
  UNIQUE (user_id, window_id)
);
CREATE TABLE IF NOT EXISTS txs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  intent_id INTEGER REFERENCES intents(id),
  purpose TEXT NOT NULL,
  signer TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  timeout_height INTEGER NOT NULL,
  tx_hash TEXT NOT NULL UNIQUE,
  tx_bytes BLOB NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('signed','sent','ok','failed','expired')),
  code INTEGER,
  raw_log TEXT,
  created_ms INTEGER NOT NULL,
  updated_ms INTEGER NOT NULL,
  height INTEGER
);
CREATE INDEX IF NOT EXISTS txs_open ON txs(status) WHERE status IN ('signed','sent');
-- Hard rule 6: a second open tx for one signing key cannot be persisted.
CREATE UNIQUE INDEX IF NOT EXISTS txs_one_open_per_signer ON txs(signer) WHERE status IN ('signed','sent');
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  intent_id INTEGER REFERENCES intents(id),
  side TEXT NOT NULL,
  quantity TEXT NOT NULL,
  entry_price TEXT NOT NULL,
  exit_price TEXT,
  pnl_usd TEXT,
  close_reason TEXT,
  opened_ms INTEGER NOT NULL,
  closed_ms INTEGER
);
CREATE TABLE IF NOT EXISTS equity_snapshots (
  user_id INTEGER NOT NULL REFERENCES users(id),
  ts_ms INTEGER NOT NULL,
  equity TEXT NOT NULL,
  flat INTEGER NOT NULL,
  balance TEXT,  -- equity without unrealized pnl; NULL means same as equity (flat)
  PRIMARY KEY (user_id, ts_ms)
);
CREATE TABLE IF NOT EXISTS user_risk_state (
  user_id INTEGER PRIMARY KEY REFERENCES users(id),
  day_id INTEGER NOT NULL,
  day_start_equity TEXT NOT NULL,
  peak_equity TEXT NOT NULL,
  cooldown_until_window INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  window_id INTEGER,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  level TEXT NOT NULL,
  kind TEXT NOT NULL,
  user_id INTEGER,
  detail TEXT NOT NULL,
  dedupe_key TEXT UNIQUE,
  created_ms INTEGER NOT NULL,
  sent_ms INTEGER
);
CREATE TABLE IF NOT EXISTS system_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- One strategy per wallet. Whether it runs is users.status: 'active' runs, 'paused' does not.
CREATE TABLE IF NOT EXISTS strategies (
  user_id INTEGER PRIMARY KEY REFERENCES users(id),
  preset TEXT NOT NULL,
  next_run_ms INTEGER NOT NULL DEFAULT 0,
  updated_ms INTEGER NOT NULL,
  last_mark TEXT,  -- the mark when the model last decided
  wake_reason TEXT,  -- why the next run was brought forward: first or price_move or near_stop
  last_wake_ms INTEGER,
  tuning TEXT  -- the wallet's own settings for the preset, as signed (tuning.py). NULL is the preset as designed
);
CREATE TABLE IF NOT EXISTS strategy_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  preset TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','stopped')),
  start_equity TEXT,
  end_equity TEXT,
  stop_reason TEXT,
  started_ms INTEGER NOT NULL,
  stopped_ms INTEGER,
  tuning TEXT  -- the settings this run started with
);
CREATE UNIQUE INDEX IF NOT EXISTS strategy_runs_one_active ON strategy_runs(user_id) WHERE status='active';
-- The hosted agent's journal. `reason` is LLM text: the owner and the chat lane see it, the decision lane never does.
CREATE TABLE IF NOT EXISTS agent_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  window_id INTEGER NOT NULL,
  preset TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('hold','entered','closed','rejected','skipped','no_decision','unknown')),
  side TEXT,
  stop_atr TEXT,
  take_profit_atr TEXT,
  reason TEXT NOT NULL DEFAULT '',
  code TEXT,
  model TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  latency_ms INTEGER,
  created_ms INTEGER NOT NULL,
  trigger TEXT,  -- why this cycle ran: schedule or price_move or near_stop or first
  snapshot TEXT  -- JSON for the owner: numbers the model saw, what it chose, what the server did. Prompts get only closed-list conditions from it, through trade_cases.
);
CREATE INDEX IF NOT EXISTS agent_decisions_user ON agent_decisions(user_id, id DESC);
{llm_usage}
CREATE TABLE IF NOT EXISTS telegram_links (
  user_id INTEGER PRIMARY KEY REFERENCES users(id),
  chat_id TEXT NOT NULL UNIQUE,
  linked_ms INTEGER NOT NULL,
  display TEXT  -- @username or first name, plain text, shown on the dashboard
);
CREATE TABLE IF NOT EXISTS telegram_link_codes (
  code_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  expires_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  dedupe_key TEXT UNIQUE,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_try_ms INTEGER NOT NULL DEFAULT 0,
  created_ms INTEGER NOT NULL,
  sent_ms INTEGER,
  chat_id TEXT,  -- set only for a message to a chat that is not (or no longer) this wallet's link
  reply_markup TEXT,  -- JSON inline keyboard
  dropped_ms INTEGER
);
CREATE INDEX IF NOT EXISTS telegram_outbox_unsent ON telegram_outbox(next_try_ms) WHERE sent_ms IS NULL;
CREATE TABLE IF NOT EXISTS telegram_seen_updates (
  update_id INTEGER PRIMARY KEY,
  created_ms INTEGER NOT NULL
);
-- The market desk's latest read of each public source. About the market, shared by every wallet.
CREATE TABLE IF NOT EXISTS market_intel (
  market TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,  -- JSON, written by the market desk. Headlines and the analyst summary reach the page, never a prompt
  as_of_ms INTEGER NOT NULL,
  PRIMARY KEY (market, kind)
);
-- One per closed trade, per wallet: the conditions at entry and the result. Nothing here crosses wallets.
CREATE TABLE IF NOT EXISTS trade_cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  trade_id INTEGER NOT NULL UNIQUE REFERENCES trades(id),
  preset TEXT,
  side TEXT NOT NULL,
  trend TEXT,
  volatility TEXT CHECK (volatility IN ('low','normal','high')),
  funding TEXT CHECK (funding IN ('positive','negative','flat')),
  bias TEXT,
  stop_atr TEXT,
  take_profit_atr TEXT,
  result_r REAL,  -- profit in units of the planned loss; NULL when the result is not known
  close_reason TEXT,
  opened_ms INTEGER NOT NULL,
  closed_ms INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT '',  -- the decision's own words: the reflection call reads it, nothing else does
  lesson TEXT,
  reflect_state TEXT NOT NULL DEFAULT 'waiting' CHECK (reflect_state IN ('waiting','done','gave_up')),
  reflect_attempts INTEGER NOT NULL DEFAULT 0,
  reflect_tried_ms INTEGER
);
CREATE INDEX IF NOT EXISTS trade_cases_user ON trade_cases(user_id, closed_ms DESC);
CREATE INDEX IF NOT EXISTS trade_cases_waiting ON trade_cases(user_id, id) WHERE reflect_state='waiting';
-- Chat lane only. The decision lane must never read these two tables.
CREATE TABLE IF NOT EXISTS chat_memory (
  user_id INTEGER NOT NULL REFERENCES users(id),
  bucket TEXT NOT NULL CHECK (bucket IN ('user','notes')),
  content TEXT NOT NULL,
  updated_ms INTEGER NOT NULL,
  PRIMARY KEY (user_id, bucket)
);
CREATE TABLE IF NOT EXISTS chat_turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  role TEXT NOT NULL CHECK (role IN ('user','assistant')),
  content TEXT NOT NULL,
  created_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_turns_user ON chat_turns(user_id, id DESC);
""".replace("{llm_usage}", _LLM_USAGE)

# Columns added after a table first shipped, for databases created before them.
_ADDED_COLUMNS = (
    ("equity_snapshots", "balance", "TEXT"),
    ("telegram_links", "display", "TEXT"),
    ("telegram_outbox", "chat_id", "TEXT"),
    ("telegram_outbox", "reply_markup", "TEXT"),
    ("telegram_outbox", "dropped_ms", "INTEGER"),
    ("agent_decisions", "snapshot", "TEXT"),
    ("agent_decisions", "trigger", "TEXT"),
    ("strategies", "last_mark", "TEXT"),
    ("strategies", "wake_reason", "TEXT"),
    ("strategies", "last_wake_ms", "INTEGER"),
    ("strategies", "tuning", "TEXT"),
    ("strategy_runs", "tuning", "TEXT"),
)


def _widen_llm_lanes(conn: sqlite3.Connection) -> None:
    """SQLite cannot change a CHECK, so a table from before these lanes is rebuilt with its rows."""
    table = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='llm_usage'").fetchone()
    if "'analyst'" in table["sql"]:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE llm_usage RENAME TO llm_usage_before_lanes")
        conn.execute(_LLM_USAGE)
        conn.execute("INSERT INTO llm_usage SELECT day_id, user_id, lane, calls, input_tokens, output_tokens FROM llm_usage_before_lanes")
        conn.execute("DROP TABLE llm_usage_before_lanes")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _widen_llm_lanes(conn)
    for table, column, kind in _ADDED_COLUMNS:
        if column not in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    return conn
