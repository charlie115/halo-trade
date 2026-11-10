"""Runtime configuration. Risk numbers live here and nowhere else."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal


def _dec(name: str, default: str) -> Decimal:
    return Decimal(os.environ.get(name, default))


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


WINDOW_MS = 900_000


@dataclass(frozen=True)
class Risk:
    risk_per_trade: Decimal = field(default_factory=lambda: _dec("HALO_RISK_PER_TRADE", "0.03"))
    max_leverage: Decimal = field(default_factory=lambda: _dec("HALO_MAX_LEVERAGE", "2"))
    internal_cap: Decimal = field(default_factory=lambda: _dec("HALO_INTERNAL_CAP", "0.98"))
    min_stop_pct: Decimal = field(default_factory=lambda: _dec("HALO_MIN_STOP_PCT", "0.005"))
    collar: Decimal = field(default_factory=lambda: _dec("HALO_COLLAR", "0.003"))
    stop_worst_pct: Decimal = field(default_factory=lambda: _dec("HALO_STOP_WORST_PCT", "0.015"))
    close_worst_pct: Decimal = field(default_factory=lambda: _dec("HALO_CLOSE_WORST_PCT", "0.03"))
    drift_max: Decimal = field(default_factory=lambda: _dec("HALO_DRIFT_MAX", "0.0025"))
    max_risk_growth: Decimal = field(default_factory=lambda: _dec("HALO_MAX_RISK_GROWTH", "1.5"))
    daily_loss: Decimal = field(default_factory=lambda: _dec("HALO_DAILY_LOSS", "0.06"))
    max_drawdown: Decimal = field(default_factory=lambda: _dec("HALO_MAX_DRAWDOWN", "0.30"))
    min_tp_cost_mult: Decimal = field(default_factory=lambda: _dec("HALO_MIN_TP_COST_MULT", "3"))
    max_equity_usd: Decimal = field(default_factory=lambda: _dec("HALO_MAX_EQUITY_USD", "400"))
    max_deposit_usd: Decimal = field(default_factory=lambda: _dec("HALO_MAX_DEPOSIT_USD", "200"))
    min_deposit_usd: Decimal = field(default_factory=lambda: _dec("HALO_MIN_DEPOSIT_USD", "50"))
    cooldown_windows: int = field(default_factory=lambda: _int("HALO_COOLDOWN_WINDOWS", 2))


@dataclass(frozen=True)
class Settings:
    db_path: str = field(default_factory=lambda: os.environ.get("HALO_DB_PATH", "halo.sqlite"))
    chain_backend: str = field(default_factory=lambda: os.environ.get("HALO_CHAIN_BACKEND", "mock"))
    network: str = field(default_factory=lambda: os.environ.get("HALO_NETWORK", "testnet"))
    candle_network: str = field(default_factory=lambda: os.environ.get("HALO_CANDLE_NETWORK", ""))  # empty means `network`
    candle_source: str = field(default_factory=lambda: os.environ.get("HALO_CANDLE_SOURCE", ""))  # "binance", or empty for the chain's own chart
    market_ticker: str = field(default_factory=lambda: os.environ.get("HALO_MARKET_TICKER", "BTC/USDC PERP"))
    grantee_key_path: str = field(default_factory=lambda: os.environ.get("HALO_GRANTEE_KEY_PATH", ""))
    alert_webhook: str = field(default_factory=lambda: os.environ.get("HALO_ALERT_WEBHOOK", ""))
    public_base_url: str = field(default_factory=lambda: os.environ.get("HALO_PUBLIC_BASE_URL", "http://127.0.0.1:18080"))
    trusted_proxy: str = field(default_factory=lambda: os.environ.get("HALO_TRUSTED_PROXY", ""))  # comma-separated IPs/CIDRs; empty = none
    max_users: int = field(default_factory=lambda: _int("HALO_MAX_USERS", 20))
    tx_timeout_blocks: int = field(default_factory=lambda: _int("HALO_TX_TIMEOUT_BLOCKS", 20))
    watch_interval_s: int = field(default_factory=lambda: _int("HALO_WATCH_INTERVAL_S", 5))  # wallets with a position or an unfinished order
    watch_flat_interval_s: int = field(default_factory=lambda: _int("HALO_WATCH_FLAT_INTERVAL_S", 60))  # everyone else; 0 means every tick
    watch_concurrency: int = field(default_factory=lambda: _int("HALO_WATCH_CONCURRENCY", 20))
    chain_rps: int = field(default_factory=lambda: _int("HALO_CHAIN_RPS", 15))  # all chain requests, one IP
    indexer_rps: int = field(default_factory=lambda: _int("HALO_INDEXER_RPS", 30))
    stop_grace_blocks: int = field(default_factory=lambda: _int("HALO_STOP_GRACE_BLOCKS", 5))
    grantee_min_inj: Decimal = field(default_factory=lambda: _dec("HALO_GRANTEE_MIN_INJ", "0.05"))
    allow_mainnet: bool = field(default_factory=lambda: os.environ.get("HALO_ALLOW_MAINNET", "") == "yes-real-money")
    # Hosted agent. Empty key or model means the agent cannot start, and nothing else changes.
    llm_base_url: str = field(default_factory=lambda: os.environ.get("HALO_LLM_BASE_URL", "https://openrouter.ai/api/v1"))
    llm_api_key: str = field(default_factory=lambda: os.environ.get("HALO_LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.environ.get("HALO_LLM_MODEL", ""))
    llm_timeout_s: int = field(default_factory=lambda: _int("HALO_LLM_TIMEOUT_S", 45))  # two tries must fit OBSERVATION_MAX_AGE_MS
    llm_daily_calls: int = field(default_factory=lambda: _int("HALO_LLM_DAILY_CALLS", 3000))  # decision lane only, all users, UTC day
    llm_user_daily_calls: int = field(default_factory=lambda: _int("HALO_LLM_USER_DAILY_CALLS", 200))  # decision lane only
    decision_interval_s: int = field(default_factory=lambda: _int("HALO_DECISION_INTERVAL_S", 900))
    decision_concurrency: int = field(default_factory=lambda: _int("HALO_DECISION_CONCURRENCY", 10))
    decision_max_per_tick: int = field(default_factory=lambda: _int("HALO_DECISION_MAX_PER_TICK", 30))
    # Price-event wakeups: one shared loop that can only bring a wallet's next cycle forward.
    monitor_interval_s: int = field(default_factory=lambda: _int("HALO_MONITOR_INTERVAL_S", 3))
    wake_move_atr: Decimal = field(default_factory=lambda: _dec("HALO_WAKE_MOVE_ATR", "1.0"))
    wake_stop_atr: Decimal = field(default_factory=lambda: _dec("HALO_WAKE_STOP_ATR", "0.5"))
    wake_cooldown_s: int = field(default_factory=lambda: _int("HALO_WAKE_COOLDOWN_S", 300))
    # A scheduled cycle with nothing new makes no LLM call.
    skip_move_atr: Decimal = field(default_factory=lambda: _dec("HALO_SKIP_MOVE_ATR", "0.25"))
    max_skip_s: int = field(default_factory=lambda: _int("HALO_MAX_SKIP_S", 3600))
    # The market desk: one shared loop over public market data and headlines. HALO_DESK_ENABLED=0 turns it off.
    desk_enabled: bool = field(default_factory=lambda: os.environ.get("HALO_DESK_ENABLED", "1") != "0")
    # Each of these lanes has its own pools, so none of them can spend a decision's budget.
    research_daily_calls: int = field(default_factory=lambda: _int("HALO_RESEARCH_DAILY_CALLS", 6000))
    research_user_daily_calls: int = field(default_factory=lambda: _int("HALO_RESEARCH_USER_DAILY_CALLS", 300))
    reflect_daily_calls: int = field(default_factory=lambda: _int("HALO_REFLECT_DAILY_CALLS", 480))
    reflect_user_daily_calls: int = field(default_factory=lambda: _int("HALO_REFLECT_USER_DAILY_CALLS", 24))
    analyst_daily_calls: int = field(default_factory=lambda: _int("HALO_ANALYST_DAILY_CALLS", 96))  # service-level, not per wallet
    # Telegram. Empty token means no bot: no link button, no webhook, no notifications.
    telegram_bot_token: str = field(default_factory=lambda: os.environ.get("HALO_TELEGRAM_BOT_TOKEN", ""))
    telegram_bot_username: str = field(default_factory=lambda: os.environ.get("HALO_TELEGRAM_BOT_USERNAME", ""))
    telegram_webhook_secret: str = field(default_factory=lambda: os.environ.get("HALO_TELEGRAM_WEBHOOK_SECRET", ""))
    chat_daily_limit: int = field(default_factory=lambda: _int("HALO_CHAT_DAILY_LIMIT", 30))
    chat_max_steps: int = field(default_factory=lambda: _int("HALO_CHAT_MAX_STEPS", 4))
    # The chat lane's own pools: chat can never spend a decision's budget.
    chat_global_daily_calls: int = field(default_factory=lambda: _int("HALO_CHAT_GLOBAL_DAILY_CALLS", 600))
    chat_user_daily_tokens: int = field(default_factory=lambda: _int("HALO_CHAT_USER_DAILY_TOKENS", 300000))
    risk: Risk = field(default_factory=Risk)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_bot_username and self.telegram_webhook_secret)
